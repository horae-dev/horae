# SPDX-License-Identifier: Apache-2.0
"""The apply-layer port: what the core needs from a sink or notifier (ADR 0002, #21/#22).

The plugin host (#24) implements `Sink` over MCP for each `act` sink and `notify` notifier;
`horae.apply.local` has a built-in, read-only sink for notes in a local vault and
`horae.apply.testing` an in-memory reference implementation for tests.

**The core owns what an intent means** (schemas decision 32, D1). A sink never describes a
change itself: the dry-run diff (`horae.apply.plan_changes`) builds every preview from the
intent and, for `note.*`, from the note's current bytes, with the conventions of
`horae.apply.notes`. So a plugin cannot show one change and make another.

**Two operations.**

- `read(call)` returns the current bytes of the target of a `note.*` intent (`None`: the note
  does not exist). It is the only call the dry-run diff makes, and only for notes. It must be
  side-effect free: it may read its target, nothing else (no file created, written, renamed,
  removed or locked, no directory created, no network request, no notification, no process
  started, no cache or state updated). A dry run is only trustworthy if every read is
  (PLAN.md §2.2 "`--dry-run` writes nothing"). A sink that serves no notes may raise
  `SinkError("not_supported")`.
- `apply(call, *, expected_before_sha256=...)` performs the change and returns what the
  journal needs (`Applied`). It is used by the journal and undo (#22), **never** by the
  dry-run diff. For a `note.*` intent it is a compare-and-swap: `expected_before_sha256` is
  the SHA-256 (hex) of the bytes the change was planned against, `None` meaning "the note
  must not exist"; if the target is not exactly that, the sink changes nothing and raises
  `SinkError("target_changed")`. The sink applies the intent with the conventions of
  `horae.apply.notes`, and the journal (#22) checks `Applied.after` against the bytes the core
  planned. For other intents `expected_before_sha256` is `None` and ignored.

**Targets** are resolved by the sink without following symlinks on any component, a target
with more than one hard link is refused, and ownership (`*.assistant.md`) is decided on that
path (so a link cannot turn a user note into an owned one); `apply` (#22) must do the same.

**Over MCP (#24)** note bytes travel base64-encoded and are capped at `MAX_NOTE_BYTES`
(16 MiB) each way; the core checks the type and size of whatever comes back.

**Untrusted text.** Titles, bodies and note content in an intent are data from the proposer.
A sink must treat them as data, never as markup to interpret, commands or further targets.
The target itself (note `path`, `http.post` `url`, notification `link`) was bound by the
engine to a trusted pipeline parameter (schemas decision 30).

A read or apply that cannot be done raises `SinkError`, with a machine-readable `kind`
(`^[a-z_]{1,32}$`; the core reports any other kind as `host_error`).

**Identity** (optional, #22). A sink may have an `identity` attribute: a short, stable string
naming *where* it writes (e.g. a local vault's real path, device and inode), at most
`MAX_IDENTITY` characters. The journal records it with each change, and `horae undo` refuses
to restore through a sink whose identity differs (`sink_mismatch`): a note path alone does not
tell one vault from another. Reading it may raise `SinkError` (e.g. `vault_unset`).
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from horae.contracts.intents import Intent

MAX_NOTE_BYTES = 16 * 1024 * 1024
"""The largest note read or written through a sink (16 MiB)."""
MAX_IDENTITY = 512
"""The longest sink identity (`sink_identity`)."""
_KIND = re.compile(r"^[a-z_]{1,32}$")


class SinkError(Exception):
    """The sink cannot read (or apply) this intent's target; `kind` is machine-readable."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message


def error_kind(kind: object) -> str:
    """A sink's error kind if it is well-formed (`^[a-z_]{1,32}$`), else `host_error`."""
    return kind if isinstance(kind, str) and _KIND.fullmatch(kind) else "host_error"


def sink_identity(sink: object) -> str | None:
    """The sink's `identity` (see the module docstring), `None` when it has none.

    Raises `SinkError` when the sink raises it, or when the identity is not a string of 1 to
    `MAX_IDENTITY` characters (`identity_invalid`)."""
    identity = getattr(sink, "identity", None)
    if identity is None:
        return None
    if not isinstance(identity, str) or not 1 <= len(identity) <= MAX_IDENTITY:
        raise SinkError("identity_invalid", "the sink's identity is not a short string")
    return identity


@dataclass(frozen=True)
class SinkCall:
    """One routed intent, as handed to its sink (a private deep copy: a sink cannot change the
    run record or the plan through it)."""

    run_id: str
    index: int
    """Index of the intent in `RunRecord.intents`."""
    tool: str
    """The entry's tool (`ProposedIntent.applied_by`), e.g. `obsidian.write_note`."""
    params: dict[str, Any]
    """The entry's trusted parameters, as recorded in the act / notify step's output
    (e.g. `{"path": ..., "mode": "replace-owned"}`); empty when the step did not record them."""
    intent: Intent
    input_hash: str | None = None
    """The run's `input_hash`."""
    dedupe_key: str | None = None
    """For `notify.push`: the notification's deduplication key (`horae.apply.dedupe_key`)."""


@dataclass(frozen=True)
class FileChange:
    """A note write: the target's current bytes (`None`: it does not exist) and the proposed
    bytes."""

    path: str
    before: bytes | None
    after: bytes


@dataclass(frozen=True)
class TaskChange:
    """A task created or changed: human-readable states, e.g. the task line before and after
    (`before` is `None` for a new task, or when the current state is not known)."""

    fingerprint: str
    action: Literal["create", "update_status"]
    before: str | None
    after: str


@dataclass(frozen=True)
class Notification:
    """A notification that would be sent."""

    title: str | None
    body: str
    link: str | None = None


@dataclass(frozen=True)
class HttpCall:
    """An HTTP request that would be made."""

    url: str
    body: Any
    method: Literal["POST"] = "POST"


Preview = FileChange | TaskChange | Notification | HttpCall
"""What the core plans for one intent."""


@dataclass(frozen=True)
class Applied:
    """What `Sink.apply` did, for the journal (#22, which may extend this type)."""

    target: str
    """What was changed (`AppliedChange.target`): a note path, a task fingerprint, a URL…"""
    before: bytes | None = None
    """For a note: its bytes before the change (`None`: it did not exist), for undo."""
    after: bytes | None = None
    """For a note: its bytes after the change, to detect later edits before an undo."""
    details: dict[str, Any] = field(default_factory=dict)


class Sink(Protocol):
    """A sink or notifier that applies routed intents."""

    async def read(self, call: SinkCall) -> bytes | None:
        """The current bytes of a `note.*` intent's target (`None`: no such note).

        Side-effect free; raises `SinkError`."""
        ...

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        """Apply `call` (#22 only, never during a dry run) if the target is still as planned
        (`target_changed` otherwise); raises `SinkError`."""
        ...


@runtime_checkable
class RestorableSink(Sink, Protocol):
    """A sink whose note changes `horae undo` can revert (#22, schemas decision 34).

    Undo restores **bytes**, not intents: the closed intent set (ADR 0002) has no "write
    exactly these bytes" or "delete this note" intent, and a run abandoned mid-apply has no
    record to rebuild a `SinkCall` from. So undo goes through these two target-keyed methods
    instead of a synthetic `apply`. Both resolve `target` (a note path) exactly as `read` and
    `apply` do (no symlink on any component, a single hard link, a regular file)."""

    async def read_target(self, target: str) -> bytes | None:
        """The current bytes of note `target` (`None`: it does not exist). Side-effect free;
        raises `SinkError`."""
        ...

    async def parent_exists(self, target: str) -> bool:
        """Whether the directory note `target` would be in exists (side-effect free): undo
        never takes a missing note for "already removed" when its directory is missing too
        (a wrong vault, most likely). Raises `SinkError`."""
        ...

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        """Make note `target` hold exactly `content` (`None`: remove it), a compare-and-swap:
        if its current bytes do not have SHA-256 `expected_sha256` (`None`: it must not
        exist), nothing changes and `SinkError("target_changed")` is raised."""
        ...


class SinkRegistry(Protocol):
    """Finds the sink for an entry's tool (`ProposedIntent.applied_by`)."""

    def sink_for(self, tool: str) -> Sink | None:
        """The sink serving `tool`, or `None` if there is none."""
        ...


@dataclass(frozen=True)
class StaticSinks:
    """A `SinkRegistry` from a mapping of tools to sinks, with an optional fallback."""

    sinks: dict[str, Sink] = field(default_factory=dict)
    default: Sink | None = None

    def sink_for(self, tool: str) -> Sink | None:
        return self.sinks.get(tool, self.default)
