# SPDX-License-Identifier: Apache-2.0
"""Intents: the closed set of writes a plugin may propose (PLAN.md §2.1.3, §3.3).

A plugin never writes directly. It returns intents; the core validates them against the
plugin's declared `permissions.intents`, shows a dry-run diff, journals them, and a sink
applies them.

Text fields are data, never markup the sink may interpret: a sink must escape what its
target format treats specially, e.g. the Obsidian Tasks emoji signifiers (`📅`, `⏰`, `✅`…)
in a task title.
"""

import re
import unicodedata
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

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

TASK_LINK_FORBIDDEN = (
    r"\x00-\x20\x7f-\xa0\xad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u1680"
    r"\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u2066-\u206f\u3000\ufeff\ufff9-\ufffb"
)
"""A regex class body: the Basic Multilingual Plane characters of the Unicode categories `Cc`,
`Cf`, `Zs`, `Zl` and `Zp` (Unicode 15), as escapes. The supplementary-plane format
characters (tags `U+E0001`, `U+E0020`-`U+E007F`...) and lone surrogates cannot be written the
same way in an ECMA-262, Rust and Python pattern: the model rejects them (`_check_task_link`)."""

TASK_LINK_PATTERN = (
    rf"^https://[^{TASK_LINK_FORBIDDEN}/?#@\\%\[\]]+([/?#][^{TASK_LINK_FORBIDDEN}\\]*)?$"
)
"""The JSON Schema `pattern` of a `TaskLink` (the SDK's `horae_sdk.tasks` holds a copy)."""

_TASK_LINK_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Cs"})
_IP_HEX = re.compile(r"0x[0-9a-f]*")


def task_link_host_is_ip(host: str) -> bool:
    """`host` (a link authority without its port) would be read by a browser as an IP
    address: it ends in a number, as the WHATWG URL standard says (its last label, without
    one trailing dot, after NFKC, is decimal digits or `0x` hex), so `127.0.0.1`, `127.1`,
    `0x7f.1`, `2130706433` and their full-width spellings. Bracketed IPv6 never reaches here
    (`[` is refused by the pattern)."""
    name = unicodedata.normalize("NFKC", host).lower().replace("\u3002", ".")
    labels = name.split(".")
    if len(labels) > 1 and labels[-1] == "":
        labels.pop()
    last = labels[-1]
    return (last.isascii() and last.isdigit()) or _IP_HEX.fullmatch(last) is not None


def _check_task_link(link: str) -> str:
    if any(unicodedata.category(char) in _TASK_LINK_CATEGORIES for char in link):
        raise ValueError("a link may hold no control, format, separator or surrogate character")
    authority = re.split(r"[/?#]", link[len("https://") :], maxsplit=1)[0]
    host = authority.split(":", 1)[0]
    if not host:
        raise ValueError("a link needs a host")
    if task_link_host_is_ip(host):
        raise ValueError("a link host may not be an IP address")
    return link


TaskLink = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=TASK_LINK_PATTERN, max_length=2048),
    AfterValidator(_check_task_link),
]
"""An absolute `https://` link (the `NotifyLink` shape, `https` only), with a non-empty
authority holding no user info (`@`), `%` or brackets, no backslash anywhere, and no character
of the Unicode categories `Cc`, `Cf`, `Zs`, `Zl`, `Zp` (controls, format characters such as
bidi and zero-width ones, spaces and separators) or lone surrogate anywhere. Its host is a
name, never an IP literal (`task_link_host_is_ip`). The pattern checks all but the
supplementary-plane format characters, lone surrogates and the IP rule, which only the model
checks (schemas decision 52)."""


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
    link: TaskLink | None = None
    """Optional `https://` link to the item the task comes from (no user info), routed only
    under the `tasks` entry's trusted `link_prefix`."""

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


ActKind = Literal["task.done", "task.snooze"]
"""What a notification button does to its task, through `/act` (#38, schemas decision 54):
`task.done` applies `task.update_status {status: done}`, `task.snooze` applies `{status:
snoozed, until}` with `until` computed by the core when the button is pressed."""

SnoozeDuration = Literal["1h", "3h", "tomorrow"]
"""How long a `task.snooze` button snoozes: one hour, three hours, or until 09:00 tomorrow
in the instance time zone, from the moment it is pressed."""

ActionLabel = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[^\x00-\x1f\x7f]+$", max_length=64)
]
"""A button label: one line, at most 64 characters, no control character."""

MAX_NOTIFY_ACTIONS = 3
"""The most buttons on one notification (ntfy's limit)."""


class NotifyAction(Contract):
    """A notification button that changes one task once, through a signed `/act` link the
    core mints when it sends the notification (#38). The proposer names the task and the
    kind; it never sees or writes a URL."""

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"kind": {"const": "task.snooze"}}},
            "then": {"required": ["duration"], "properties": {"duration": {"type": "string"}}},
            "else": {"properties": {"duration": {"type": "null"}}},
        }
    )

    label: ActionLabel
    """The button text (untrusted data, shown as is)."""
    kind: ActKind
    """What the button does."""
    task: Fingerprint
    """The task's fingerprint: like a `task.update_status` target, it must appear in the data
    the run collected (rule 4 of `horae.engine.executor`)."""
    duration: SnoozeDuration | None = None
    """For `task.snooze` only (and then required)."""

    @model_validator(mode="after")
    def _duration_iff_snooze(self) -> Self:
        if (self.kind == "task.snooze") != (self.duration is not None):
            raise ValueError("`duration` is required for `task.snooze` and only allowed there")
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
    actions: (
        Annotated[list[NotifyAction], Field(min_length=1, max_length=MAX_NOTIFY_ACTIONS)] | None
    ) = None
    """Optional buttons (at most 3), each changing one task once through `/act` (#38). The
    core mints their links when it sends the notification; without the `/act` key or
    `http.public_url` the notification is sent without them."""


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
