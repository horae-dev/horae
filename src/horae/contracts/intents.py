# SPDX-License-Identifier: Apache-2.0
"""Intents: the closed set of writes a plugin may propose (PLAN.md §2.1.3, §3.3).

A plugin never writes directly. It returns intents; the core validates them against the
plugin's declared `permissions.intents`, shows a dry-run diff, journals them, and a sink
applies them.

Text fields are data, never markup the sink may interpret: a sink must escape what its
target format treats specially, e.g. the Obsidian Tasks emoji signifiers (`📅`, `⏰`, `✅`…)
in a task title.
"""

from typing import Annotated, Any, Literal, Self, get_args

from pydantic import ConfigDict, Field, Strict, StringConstraints, TypeAdapter, model_validator

from horae.contracts.types import (
    BlockId,
    Content,
    Contract,
    Fingerprint,
    Heading,
    HttpsUrl,
    IsoDate,
    Message,
    NotePath,
    Timestamp,
    Title,
)

IntentType = Literal[
    "note.create",
    "note.append_section",
    "note.replace_owned_block",
    "task.create",
    "task.update_status",
    "notify.push",
    "http.post",
]
"""The name of an intent in the closed set."""

INTENT_TYPES: tuple[IntentType, ...] = get_args(IntentType)

ClockTime = Annotated[str, Strict(), StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
"""A time of day, `HH:MM` (24 h)."""

NotifyLink = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^(https|obsidian)://[^\x00-\x20\x7f]+$", max_length=2048),
]
"""An `https://` or `obsidian://` link, with no whitespace or control characters."""


class NoteCreate(Contract):
    """Create a note with the given content."""

    type: Literal["note.create"]
    path: NotePath
    """Note path, relative to the sink's root."""
    content: Content
    """Full note content (Markdown), at most 1 MiB."""


class NoteAppendSection(Contract):
    """Append a section, under a heading, to an existing note."""

    type: Literal["note.append_section"]
    path: NotePath
    """Note path, relative to the sink's root."""
    heading: Heading
    """Heading of the appended section, without the leading `#` characters."""
    content: Content
    """Section body (Markdown), at most 1 MiB."""


class NoteReplaceOwnedBlock(Contract):
    """Replace the content of a block that Horae owns inside a note."""

    type: Literal["note.replace_owned_block"]
    path: NotePath
    """Note path, relative to the sink's root."""
    block: BlockId
    """Identifier of the owned block to replace."""
    content: Content
    """New block content (Markdown), at most 1 MiB."""


class TaskCreate(Contract):
    """Create a task."""

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"required": ["time"], "properties": {"time": {"type": "string"}}},
            "then": {"required": ["due"], "properties": {"due": {"type": "string"}}},
        }
    )

    type: Literal["task.create"]
    title: Title
    """Task text: one line, at most 500 characters."""
    fingerprint: Fingerprint
    """Stable identity used to deduplicate tasks across runs and sources."""
    due: IsoDate | None = None
    """Due date (ISO 8601 `YYYY-MM-DD`)."""
    time: ClockTime | None = None
    """Reminder time on the due date, `HH:MM` (24 h). Requires `due`."""

    @model_validator(mode="after")
    def _time_needs_due(self) -> Self:
        if self.time is not None and self.due is None:
            raise ValueError("`time` requires `due`")
        return self


class TaskUpdateStatus(Contract):
    """Change the status of an existing task."""

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"required": ["status"], "properties": {"status": {"const": "snoozed"}}},
            "then": {"required": ["until"], "properties": {"until": {"not": {"type": "null"}}}},
            "else": {"properties": {"until": {"type": "null"}}},
        }
    )

    type: Literal["task.update_status"]
    task: Fingerprint
    """The task's fingerprint."""
    status: Literal["todo", "done", "cancelled", "snoozed"]
    """New status."""
    until: IsoDate | Timestamp | None = None
    """For `snoozed` only (and then required): when the task comes back."""

    @model_validator(mode="after")
    def _until_iff_snoozed(self) -> Self:
        if (self.status == "snoozed") != (self.until is not None):
            raise ValueError("`until` is required for `snoozed` and only allowed there")
        return self


class NotifyPush(Contract):
    """Send a push notification."""

    type: Literal["notify.push"]
    title: Title | None = None
    """Optional notification title: one line, at most 500 characters."""
    body: Message
    """Notification text, at most 4096 characters."""
    link: NotifyLink | None = None
    """Optional `https://` or `obsidian://` link opened when the notification is tapped."""


class HttpPost(Contract):
    """POST a JSON body to an https URL.

    The core checks `url_host(url)` against the proposer's `permissions.network`, using
    exactly that parsed host (any port is allowed on a declared host), and caps the compact
    JSON body at 1 MiB (`horae.intents`). The sink must never follow redirects, or must
    re-check the host of every hop with `url_host` against the same list."""

    type: Literal["http.post"]
    url: HttpsUrl
    """Target URL: https only, a host name (no IP literal), optional port."""
    body: Any
    """JSON request body."""


Intent = Annotated[
    NoteCreate
    | NoteAppendSection
    | NoteReplaceOwnedBlock
    | TaskCreate
    | TaskUpdateStatus
    | NotifyPush
    | HttpPost,
    Field(discriminator="type"),
]
"""Any intent, discriminated by its `type`."""

INTENT_MODELS: dict[IntentType, type[Contract]] = {
    "note.create": NoteCreate,
    "note.append_section": NoteAppendSection,
    "note.replace_owned_block": NoteReplaceOwnedBlock,
    "task.create": TaskCreate,
    "task.update_status": TaskUpdateStatus,
    "notify.push": NotifyPush,
    "http.post": HttpPost,
}

intent_adapter: TypeAdapter[Intent] = TypeAdapter(Intent)
"""Validates any intent."""
