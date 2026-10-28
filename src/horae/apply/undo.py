# SPDX-License-Identifier: Apache-2.0
"""`horae undo <run-id>`: restore every note a run changed, byte for byte (#22, decision 34).

`undo_run(store, run_id, sinks)` reads the run's journal and puts each note it changed back
to the bytes it had before the run, through the sink that changed it
(`JournalEntry.applied_by`; `RestorableSink.read_target` / `restore`). A note the run
created is removed. Tasks, notifications and HTTP calls cannot be undone: they are listed in
`UndoResult.not_undoable` (task undo is left to the tasks story; notifications stay sent, and
their dedupe keys are kept, so an undo never causes a notification to be sent again).

**Sinks first.** The registry's optional `prepare` is awaited with the tools of the journal's
undoable entries (`horae.apply.sink.prepare_sinks`) before any identity is read: a plugin
sink fetches its identity over MCP. A violation a plugin sink meets here (e.g.
`secret_in_output`) is only logged: there is no run record to add it to.

**Refused before anything is read** when the run is running (`run_running`), a dry run
(`dry_run`), has no undoable change at all (`nothing_to_undo`; its status is left as it is),
or when a sink now resolves to another place than the one that applied a change
(`sink_mismatch`, also under `force`: e.g. `--vault` names another vault than the run's).

**Per note, not per entry.** A run may change one note several times (chained changes, maybe
through several tools of one sink): the entries of one (sink identity, note) are undone
together, by one write of the bytes the note had before the first of them, in reverse order of
the notes' last change.

**Changed since?** Before writing anything, every note is read. A note is

- *already restored* when it holds its original bytes (or, for a created note, does not
  exist): nothing to write; this is what makes undo resumable after a crash, and also covers
  a change journalled but never applied. A missing note whose directory is missing too is
  never taken for "already removed" when a change of it was applied (`parent_missing`: most
  likely the wrong vault);
- *as the run left it* when its SHA-256 is the `after` (or the bytes the sink really wrote,
  `actual_after`) of the last applied entry, or of a later entry still "maybe applied"
  (`applied = 0`: the process died between apply and mark): it is restored;
- otherwise *changed since*: the whole undo is refused (`changed`, nothing is written)
  unless `force`, which restores it anyway and keeps the overwritten bytes in the journal
  (`undo_overwrote_sha256`, reported).

**Last in, first out.** If a later run (any pipeline) applied a change to the same note
through the same place and that change is not undone, the undo is refused (`newer_run`, naming
the runs: undo them first) unless `force`, which reports them.

Each sink call is bounded by `call_timeout_s` (`timeout`). Each write is a compare-and-swap
against the digest just read (`target_changed` if the note changes in between: the undo stops
there). Once a note is restored its entries are marked undone (`Store.journal_mark_undone`), so
re-running undo after a crash continues where it stopped. When every undoable entry is undone,
a run with a record is saved `undone`; a run abandoned by a crash has no record and stays
`failed` (the store keeps abandoned runs failed), with its journal entries undone.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from horae.apply.render import safe
from horae.apply.sink import (
    RestorableSink,
    Sink,
    SinkError,
    SinkRegistry,
    error_kind,
    prepare_sinks,
    sink_identity,
)
from horae.store import JournalEntry, Store, UnknownRun

UndoRefusalKind = Literal[
    "run_running", "dry_run", "nothing_to_undo", "sink_mismatch", "newer_run", "changed"
]


@dataclass(frozen=True)
class UndoRefusal:
    kind: UndoRefusalKind
    message: str


@dataclass(frozen=True)
class NoteUndo:
    """One note of the run and what undo did (or would do) with it."""

    target: str
    applied_by: str | None
    intents: tuple[int, ...]
    action: Literal["restore", "remove", "already_restored", "already_undone"]
    """`restore` the original bytes, `remove` a note the run created, nothing to write
    (`already_restored`), or its entries were undone before (`already_undone`)."""
    status: Literal["done", "changed", "newer_run", "sink_mismatch", "pending", "failed"]
    """`done`; `changed` since the run, or changed by a `newer_run` (refused without
    `force`); `sink_mismatch` (refused); `pending` (not reached: the undo stopped or was
    refused); `failed` (see `UndoResult.problems`)."""
    forced: bool = False
    expected_sha256: tuple[str | None, ...] = ()
    """The digests the note could have as the run left it."""
    found_sha256: str | None = None
    later_runs: tuple[str, ...] = ()
    """Later runs whose applied, not undone changes touch the note (undo them first)."""
    overwrote_sha256: str | None = None
    """Forced: the digest of the bytes the undo overwrote, kept in the journal's blobs."""
    sink_identity: str | None = None
    """Where the sink that undoes it writes (`horae.apply.sink_identity`)."""


@dataclass(frozen=True)
class NotUndoable:
    """A change undo cannot revert (a task, a notification, an HTTP call)."""

    intent: int
    intent_type: str
    target: str
    applied_by: str | None
    applied: bool
    """`False`: maybe applied (the process may have died before marking it)."""


@dataclass(frozen=True)
class UndoProblem:
    target: str
    applied_by: str | None
    kind: str
    message: str


@dataclass(frozen=True)
class UndoResult:
    run_id: str
    pipeline: str
    run_status: str
    """The run's status after the undo."""
    abandoned: bool
    force: bool
    notes: tuple[NoteUndo, ...] = ()
    not_undoable: tuple[NotUndoable, ...] = ()
    problems: tuple[UndoProblem, ...] = ()
    refused: UndoRefusal | None = None
    marked_undone: bool = False
    """The run was saved `undone` by this call."""

    @property
    def complete(self) -> bool:
        """Every undoable change of the run is undone."""
        return (
            self.refused is None
            and not self.problems
            and all(note.status == "done" for note in self.notes)
        )


@dataclass(frozen=True)
class _Place:
    """The sink an entry's tool resolves to now, and where it writes."""

    sink: Sink | None
    identity: str | None = None
    problem: tuple[str, str] | None = None
    """(kind, message) when the identity could not be read."""


@dataclass
class _Group:
    place: _Place
    target: str
    entries: list[JournalEntry] = field(default_factory=list)

    @property
    def applied_by(self) -> str | None:
        return self.entries[0].applied_by

    @property
    def original(self) -> str | None:
        return self.entries[0].before_sha256

    def expected(self) -> tuple[str | None, ...]:
        """The digests the note may have as the run left it (see the module docstring)."""
        applied = [i for i, entry in enumerate(self.entries) if entry.applied]
        start = applied[-1] if applied else 0
        digests: list[str | None] = []
        for entry in self.entries[start:]:
            if entry.actual_after_sha256 is not None:
                digests.append(entry.actual_after_sha256)
            digests.append(entry.after_sha256)
        return tuple(dict.fromkeys(digests))

    def mismatched(self) -> bool:
        """Some entry was applied through another place than the sink resolves to now."""
        recorded = {e.sink_identity for e in self.entries if e.sink_identity is not None}
        return bool(recorded) and recorded != {self.place.identity}


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _place(sinks: SinkRegistry, tool: str) -> _Place:
    sink = sinks.sink_for(tool)
    if sink is None:
        return _Place(None)
    try:
        return _Place(sink, sink_identity(sink))
    except SinkError as error:
        return _Place(sink, problem=(error_kind(error.kind), str(error.message)))
    except Exception:
        return _Place(sink, problem=("host_error", "the sink's identity could not be read"))


def _groups(entries: list[JournalEntry], sinks: SinkRegistry) -> list[_Group]:
    """Undoable entries per (resolved sink identity, note), ordered by their last entry,
    latest first."""
    places: dict[str, _Place] = {}
    groups: dict[tuple[str, str], _Group] = {}
    for entry in entries:
        if not entry.undoable:
            continue
        tool = entry.applied_by or ""
        if tool not in places:
            places[tool] = _place(sinks, tool)
        place = places[tool]
        if place.identity is not None:
            where = f"identity:{place.identity}"
        elif place.sink is not None:
            where = f"sink:{id(place.sink)}"
        else:
            where = f"tool:{tool}"
        group = groups.setdefault((where, entry.target), _Group(place, entry.target))
        group.entries.append(entry)
    return sorted(groups.values(), key=lambda g: g.entries[-1].seq, reverse=True)


def _not_undoable(entries: list[JournalEntry]) -> tuple[NotUndoable, ...]:
    return tuple(
        NotUndoable(e.intent, e.intent_type, e.target, e.applied_by, e.applied)
        for e in entries
        if not e.undoable
    )


def _later_runs(store: Store, run_id: str, group: _Group) -> tuple[str, ...]:
    """Later runs with an applied, not undone change of the group's note in the same place."""
    last = max(entry.id for entry in group.entries)
    identity = group.place.identity
    runs = [
        entry.run_id
        for entry in store.journal_later(run_id, group.target, last)
        if entry.applied
        and (entry.sink_identity is None or identity is None or entry.sink_identity == identity)
    ]
    return tuple(dict.fromkeys(runs))


class _Failed(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(kind)
        self.kind, self.message = kind, message


async def _call[T](coro: Awaitable[T], what: str, timeout_s: float | None) -> T:
    """Await a sink call, at most `timeout_s`; any failure is a `_Failed`."""
    try:
        async with asyncio.timeout(timeout_s):
            return await coro
    except SinkError as error:
        kind = error_kind(error.kind)
        message = str(error.message) if kind != "host_error" else f"the sink {what} failed"
        raise _Failed(kind, message) from None
    except TimeoutError:
        raise _Failed("timeout", f"the sink {what} took longer than {timeout_s:g} s") from None
    except Exception:
        # Only the kind: an exception's message may hold data from a plugin.
        raise _Failed("host_error", f"the sink {what} failed") from None


def _refusal(kind: UndoRefusalKind, notes: list[NoteUndo], status: str, what: str) -> UndoRefusal:
    hit = [note for note in notes if note.status == status]
    names = ", ".join(note.target for note in hit[:5])
    return UndoRefusal(kind, f"{len(hit)} note(s) ({names}) {what}")


async def undo_run(
    store: Store,
    run_id: str,
    sinks: SinkRegistry,
    *,
    force: bool = False,
    clock: Callable[[], datetime],
    call_timeout_s: float | None = None,
) -> UndoResult:
    """Undo run `run_id` (see the module docstring); each sink call is bounded by
    `call_timeout_s` (`None`: unbounded). Raises `UnknownRun`, or `StoreError`."""
    summary = store.run_summary(run_id)
    if summary is None:
        raise UnknownRun(run_id)
    base = UndoResult(
        run_id,
        summary.pipeline,
        summary.status,
        summary.abandoned_at is not None,
        force,
    )
    if summary.status == "running":
        return replace(base, refused=UndoRefusal("run_running", "the run is still running"))
    if summary.dry_run:
        return replace(base, refused=UndoRefusal("dry_run", "a dry run applied nothing"))
    entries = store.journal(run_id)
    base = replace(base, not_undoable=_not_undoable(entries))
    if not any(entry.undoable for entry in entries):
        message = "the run changed no note: nothing to undo (its status is left as it is)"
        return replace(base, refused=UndoRefusal("nothing_to_undo", message))

    # Phase 0: every note still to undo must resolve to the place that changed it. A plugin
    # sink fetches its identity first (`sink_identity` is synchronous).
    tools = (entry.applied_by for entry in entries if entry.undoable and entry.applied_by)
    await prepare_sinks(sinks, tools)
    groups = _groups(entries, sinks)
    notes: list[NoteUndo] = []
    for group in groups:
        intents = tuple(dict.fromkeys(entry.intent for entry in group.entries))
        note = NoteUndo(group.target, group.applied_by, intents, "already_undone", "done")
        note = replace(note, sink_identity=group.place.identity)
        if any(entry.undone_at is None for entry in group.entries):
            note = replace(
                note, action="remove" if group.original is None else "restore", status="pending"
            )
            restorable = isinstance(group.place.sink, RestorableSink)
            if restorable and group.place.problem is None and group.mismatched():
                note = replace(note, status="sink_mismatch")
        notes.append(note)
    if any(note.status == "sink_mismatch" for note in notes):
        what = "were changed through another sink or vault than the one given now"
        refusal = _refusal("sink_mismatch", notes, "sink_mismatch", what)
        return replace(base, notes=tuple(notes), refused=refusal)

    # Phase 1: read every note still to undo, and classify it. Nothing is written.
    todo: list[tuple[int, _Group, RestorableSink, bytes | None]] = []
    problems: list[UndoProblem] = []
    for position, group in enumerate(groups):
        note = notes[position]
        if note.action == "already_undone":
            continue

        def failed(
            kind: str,
            message: str,
            at: int = position,
            group: _Group = group,
            note: NoteUndo = note,
        ) -> None:
            problems.append(UndoProblem(group.target, group.applied_by, kind, message))
            notes[at] = replace(note, status="failed")

        sink = group.place.sink
        if not isinstance(sink, RestorableSink):
            failed("no_sink", f"no sink can restore notes for {group.applied_by or 'this run'}")
            continue
        if group.place.problem is not None:
            failed(*group.place.problem)
            continue
        try:
            current = await _call(sink.read_target(group.target), "read", call_timeout_s)
            if current is not None and type(current) is not bytes:
                raise _Failed("preview_invalid", "the sink returned something other than bytes")
            found = _sha(current)
            missing_dir = False
            if found is None and found == group.original and any(e.applied for e in group.entries):
                exists = await _call(sink.parent_exists(group.target), "read", call_timeout_s)
                missing_dir = exists is not True
        except _Failed as error:
            failed(error.kind, error.message)
            continue
        if missing_dir:
            message = (
                f"{group.target} and its directory are missing although the run created it: "
                "is it the right vault? (recreate the directory to undo it)"
            )
            failed("parent_missing", message)
            continue
        expected = group.expected()
        note = replace(note, expected_sha256=expected, found_sha256=found)
        if found == group.original:
            note = replace(note, action="already_restored")
        elif found not in expected:
            note = replace(note, status="pending" if force else "changed", forced=force)
        later = _later_runs(store, run_id, group)
        if later:
            note = replace(note, later_runs=later)
            if not force:
                note = replace(note, status="newer_run")
        todo.append((position, group, sink, current))
        notes[position] = note

    if problems:
        return replace(base, notes=tuple(notes), problems=tuple(problems))
    if any(note.status == "newer_run" for note in notes):
        newer = list(dict.fromkeys(run for note in notes for run in note.later_runs))
        what = f"were changed by a later run too: undo {', '.join(newer[:5])} first, or --force"
        refusal = _refusal("newer_run", notes, "newer_run", what)
        return replace(base, notes=tuple(notes), refused=refusal)
    if any(note.status == "changed" for note in notes):
        refusal = _refusal("changed", notes, "changed", "changed since the run; use --force")
        return replace(base, notes=tuple(notes), refused=refusal)

    # Phase 2: restore, latest change first; mark each note's entries undone once written.
    for position, group, sink, current in todo:
        note = notes[position]
        overwrote = current if note.forced and note.action != "already_restored" else None
        try:
            if note.action != "already_restored":
                content = None
                if group.original is not None:
                    content = store.blob(group.original)
                    if content is None:
                        message = "the journal lost the note's original bytes"
                        raise _Failed("blob_missing", message)
                write = sink.restore(group.target, content, expected_sha256=note.found_sha256)
                await _call(write, "restore", call_timeout_s)
        except _Failed as error:
            problems.append(UndoProblem(group.target, group.applied_by, error.kind, error.message))
            notes[position] = replace(note, status="failed")
            break
        with store.transaction():
            for entry in group.entries:
                if entry.undone_at is None:
                    store.journal_mark_undone(entry.id, at=clock(), overwrote=overwrote)
        notes[position] = replace(note, status="done", overwrote_sha256=_sha(overwrote))

    result = replace(base, notes=tuple(notes), problems=tuple(problems))
    if result.complete and summary.status != "undone" and summary.abandoned_at is None:
        record = store.get_run(run_id)
        if record is not None:
            store.save_run(record.model_copy(update={"status": "undone"}))
            result = replace(result, run_status="undone", marked_undone=True)
    return result


UNDO_SCHEMA_VERSION = 1
"""Version of the `horae undo --json` report."""


def undo_to_dict(result: UndoResult) -> dict[str, Any]:
    """The `horae undo --json` report (fixed key order)."""
    return {
        "schema_version": UNDO_SCHEMA_VERSION,
        "run": {
            "id": result.run_id,
            "pipeline": result.pipeline,
            "status": result.run_status,
            "abandoned": result.abandoned,
        },
        "force": result.force,
        "complete": result.complete,
        "marked_undone": result.marked_undone,
        "refused": None if result.refused is None else asdict(result.refused),
        "notes": [
            {
                "target": note.target,
                "applied_by": note.applied_by,
                "sink_identity": note.sink_identity,
                "intents": list(note.intents),
                "action": note.action,
                "status": note.status,
                "forced": note.forced,
                "expected_sha256": list(note.expected_sha256),
                "found_sha256": note.found_sha256,
                "later_runs": list(note.later_runs),
                "overwrote_sha256": note.overwrote_sha256,
            }
            for note in result.notes
        ],
        "not_undoable": [asdict(item) for item in result.not_undoable],
        "problems": [asdict(problem) for problem in result.problems],
    }


_DONE = {
    "restore": "restored",
    "remove": "removed (the run created it)",
    "already_restored": "already restored",
    "already_undone": "already undone",
}
_NOT_DONE = {
    "changed": "changed since the run (not restored; --force restores it)",
    "newer_run": "changed by a later run too (not restored; undo it first, or --force)",
    "sink_mismatch": "changed through another sink or vault (not restored)",
    "failed": "failed",
}


def render_undo_text(result: UndoResult) -> str:
    """`result` for a terminal; untrusted text escaped (`horae.apply.render.safe`)."""
    lines = [f"Run {safe(result.run_id)} ({safe(result.pipeline)}): {result.run_status}"]
    if result.refused is not None:
        lines.append(f"Refused ({result.refused.kind}): {safe(result.refused.message)}")
    for note in result.notes:
        if note.status == "done":
            what = _DONE[note.action]
            if note.forced:
                what += " (forced: it had changed since the run"
                if note.overwrote_sha256 is not None:
                    what += f"; the overwritten bytes are kept as {note.overwrote_sha256}"
                what += ")"
        else:
            what = _NOT_DONE.get(note.status, "not restored")
        if note.later_runs:
            what += f"; later runs: {', '.join(safe(run) for run in note.later_runs)}"
        lines.append(f"  {safe(note.target)}: {what}")
    if result.not_undoable:
        lines.append("Not undoable (left as they are):")
        for item in result.not_undoable:
            maybe = "" if item.applied else " (maybe applied)"
            via = f" via {safe(item.applied_by)}" if item.applied_by else ""
            lines.append(f"  {item.intent_type} {safe(item.target)}{via}{maybe}")
    for problem in result.problems:
        kind, target, message = safe(problem.kind), safe(problem.target), safe(problem.message)
        lines.append(f"Problem: {target}: {kind}: {message}")
    if result.marked_undone:
        lines.append("The run is now marked undone.")
    elif result.complete and result.abandoned:
        lines.append("Every file is restored; the run was abandoned, so it stays failed.")
    return "\n".join(lines) + "\n"
