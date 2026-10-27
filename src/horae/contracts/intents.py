# SPDX-License-Identifier: Apache-2.0
"""Intents: the closed set of writes a plugin may propose (PLAN.md §2.1.3, §3.3).

A plugin never writes directly. It returns intents; the core validates them against the
plugin's declared `permissions.intents`, shows a dry-run diff, journals and applies them
through a sink.
"""

import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, StringConstraints, TypeAdapter

from horae.contracts._base import Contract, HttpUrl, NonEmptyStr

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

NotePath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
"""A note path relative to the sink's root (for example the vault), e.g. `Daily/x.md`."""


class NoteCreate(Contract):
    """Create a note with the given content."""

    type: Literal["note.create"]
    path: NotePath
    """Note path, relative to the sink's root."""
    content: str
    """Full note content (Markdown)."""


class NoteAppendSection(Contract):
    """Append a section, under a heading, to an existing note."""

    type: Literal["note.append_section"]
    path: NotePath
    """Note path, relative to the sink's root."""
    heading: NonEmptyStr
    """Heading of the appended section, without the leading `#` characters."""
    content: str
    """Section body (Markdown)."""


class NoteReplaceOwnedBlock(Contract):
    """Replace the content of a block that Horae owns inside a note."""

    type: Literal["note.replace_owned_block"]
    path: NotePath
    """Note path, relative to the sink's root."""
    block: NonEmptyStr
    """Identifier of the owned block to replace."""
    content: str
    """New block content (Markdown)."""


class TaskCreate(Contract):
    """Create a task."""

    type: Literal["task.create"]
    title: NonEmptyStr
    """Task text."""
    due: datetime.date | None = None
    """Due date (ISO 8601 `YYYY-MM-DD`)."""
    time: Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] | None = (
        None
    )
    """Reminder time on the due date, `HH:MM` (24 h)."""
    fingerprint: NonEmptyStr | None = None
    """Stable identity used to deduplicate tasks across runs and sources."""


class TaskUpdateStatus(Contract):
    """Change the status of an existing task."""

    type: Literal["task.update_status"]
    task: NonEmptyStr
    """The task's fingerprint."""
    status: Literal["todo", "done", "cancelled"]
    """New status."""


class NotifyPush(Contract):
    """Send a push notification."""

    type: Literal["notify.push"]
    title: NonEmptyStr | None = None
    """Optional notification title."""
    body: NonEmptyStr
    """Notification text."""
    link: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    """Optional link opened when the notification is tapped (e.g. an `obsidian://` link)."""


class HttpPost(Contract):
    """POST a JSON body to a URL. The host must be declared in `permissions.network`."""

    type: Literal["http.post"]
    url: HttpUrl
    """Target URL."""
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
