# SPDX-License-Identifier: Apache-2.0
"""The apply-layer port: what the core needs from a sink or notifier (ADR 0002, #21/#22).

The plugin host (#24) implements `Sink` over MCP for each `act` sink and `notify` notifier;
`horae.apply.local` has a built-in, read-only preview for notes in a local vault and
`horae.apply.testing` an in-memory reference implementation for tests.

**Two operations.**

- `preview(call)` says what applying the intent **would** change: the current bytes of the
  target note and the proposed bytes (`FileChange`, for `note.*`), the task line or state
  change (`TaskChange`, for `task.*`), or a description of what would be sent
  (`Notification` for `notify.push`, `HttpCall` for `http.post`). It is used by the dry-run
  diff (`horae.apply.plan_changes`).
- `apply(call)` performs the change and returns what the journal needs (`Applied`). It is used
  by the journal and undo (#22), **never** by the dry-run diff.

**`preview` must be side-effect free.** It may read its target, nothing else: no file
created, written, renamed, removed or locked, no directory created, no network request, no
notification, no process started, no cache or state updated. A dry run is only trustworthy if
every preview is (PLAN.md §2.2 "`--dry-run` writes nothing").

**Untrusted text.** Titles, bodies and note content in an intent are data from the proposer.
A sink must treat them as data, never as markup to interpret, commands or further targets.
The target itself (note `path`, `http.post` `url`, notification `link`) was bound by the
engine to a trusted pipeline parameter (schemas decision 30); a preview must describe that
target and no other (`plan_changes` checks it).

A preview that cannot be made (the note is missing, an owned block is not found…) raises
`PreviewError`, with a machine-readable `kind`.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from horae.contracts.intents import Intent


class PreviewError(Exception):
    """The sink cannot preview (or apply) this intent; `kind` is machine-readable."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class SinkCall:
    """One routed intent, as handed to its sink."""

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
    """The run's `input_hash`, for notification deduplication."""


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
    (`before` is `None` for a new task, or when the sink does not know the current state)."""

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
"""What `Sink.preview` returns."""


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

    async def preview(self, call: SinkCall) -> Preview:
        """What applying `call` would change. Side-effect free; raises `PreviewError`."""
        ...

    async def apply(self, call: SinkCall) -> Applied:
        """Apply `call` (#22 only, never during a dry run); raises `PreviewError`."""
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
