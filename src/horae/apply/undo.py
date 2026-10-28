# SPDX-License-Identifier: Apache-2.0
"""`horae undo <run-id>`: restore every note a run changed, byte for byte (#22, decision 34).

`undo_run(store, run_id, sinks)` reads the run's journal and puts each note it changed back
to the bytes it had before the run, through the sink that changed it
(`JournalEntry.applied_by`; `RestorableSink.read_target` / `restore`). A note the run
created is removed. Tasks, notifications and HTTP calls cannot be undone: they are listed in
`UndoResult.not_undoable`.

**Per note, not per entry.** A run may change one note several times (chained changes): the
entries of one (sink, note) are undone together, by one write of the bytes the note had
before the first of them, in reverse order of the notes' last change.

**Changed since?** Before writing anything, every note is read. A note is

- *already restored* when it holds its original bytes (or, for a created note, does not
  exist): nothing to write; this is what makes undo resumable after a crash, and also covers
  a change journalled but never applied;
- *as the run left it* when its SHA-256 is the `after` of the last applied entry, or of a
  later entry still "maybe applied" (`applied = 0`: the process died between apply and
  mark): it is restored;
- otherwise *changed since*: the whole undo is refused (`changed`, nothing is written)
  unless `force`, which restores it anyway.

Each write is a compare-and-swap against the digest just read (`target_changed` if the note
changes in between: the undo stops there). Once a note is restored its entries are marked
undone (`Store.journal_mark_undone`), so re-running undo after a crash continues where it
stopped. When every undoable entry is undone, a run with a record is saved `undone`; a run
abandoned by a crash has no record and stays `failed` (the store keeps abandoned runs failed),
with its journal entries undone.
"""

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Literal

from horae.apply.render import safe
from horae.apply.sink import RestorableSink, SinkError, SinkRegistry
from horae.store import JournalEntry, Store, UnknownRun

UndoRefusalKind = Literal["run_running", "dry_run", "changed"]


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
    status: Literal["done", "changed", "pending", "failed"]
    """`done`; `changed` since the run (refused without `force`); `pending` (not reached:
    the undo stopped or was refused); `failed` (see `UndoResult.problems`)."""
    forced: bool = False
    expected_sha256: tuple[str | None, ...] = ()
    """The digests the note could have as the run left it."""
    found_sha256: str | None = None


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


@dataclass
class _Group:
    applied_by: str | None
    target: str
    entries: list[JournalEntry]

    @property
    def original(self) -> str | None:
        return self.entries[0].before_sha256

    def expected(self) -> tuple[str | None, ...]:
        """The digests the note may have as the run left it (see the module docstring)."""
        applied = [i for i, entry in enumerate(self.entries) if entry.applied]
        start = applied[-1] if applied else 0
        return tuple(dict.fromkeys(entry.after_sha256 for entry in self.entries[start:]))


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _groups(entries: list[JournalEntry]) -> list[_Group]:
    """Undoable entries per (sink, note), ordered by their last entry, latest first."""
    groups: dict[tuple[str | None, str], _Group] = {}
    for entry in entries:
        if entry.undoable:
            key = (entry.applied_by, entry.target)
            groups.setdefault(key, _Group(entry.applied_by, entry.target, [])).entries.append(entry)
    return sorted(groups.values(), key=lambda g: g.entries[-1].seq, reverse=True)


def _not_undoable(entries: list[JournalEntry]) -> tuple[NotUndoable, ...]:
    return tuple(
        NotUndoable(e.intent, e.intent_type, e.target, e.applied_by, e.applied)
        for e in entries
        if not e.undoable
    )


async def undo_run(
    store: Store,
    run_id: str,
    sinks: SinkRegistry,
    *,
    force: bool = False,
    clock: Callable[[], datetime],
) -> UndoResult:
    """Undo run `run_id` (see the module docstring). Raises `UnknownRun`, or `StoreError`."""
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

    # Phase 1: read every note still to undo, and classify it. Nothing is written.
    notes: list[NoteUndo] = []
    todo: list[tuple[int, _Group, RestorableSink, str | None]] = []
    problems: list[UndoProblem] = []
    for group in _groups(entries):
        intents = tuple(dict.fromkeys(entry.intent for entry in group.entries))
        note = NoteUndo(group.target, group.applied_by, intents, "already_undone", "done")
        if all(entry.undone_at is not None for entry in group.entries):
            notes.append(note)
            continue
        sink = sinks.sink_for(group.applied_by or "")
        if not isinstance(sink, RestorableSink):
            message = f"no sink can restore notes for {group.applied_by or 'this run'}"
            problems.append(UndoProblem(group.target, group.applied_by, "no_sink", message))
            notes.append(replace(note, action="restore", status="failed"))
            continue
        try:
            current = await sink.read_target(group.target)
        except SinkError as error:
            problems.append(UndoProblem(group.target, group.applied_by, error.kind, error.message))
            notes.append(replace(note, action="restore", status="failed"))
            continue
        except Exception:
            message = "the sink read failed"
            problems.append(UndoProblem(group.target, group.applied_by, "host_error", message))
            notes.append(replace(note, action="restore", status="failed"))
            continue
        found = _sha(current)
        action: Literal["restore", "remove", "already_restored"]
        action = "remove" if group.original is None else "restore"
        expected = group.expected()
        note = replace(note, action=action, expected_sha256=expected, found_sha256=found)
        if found == group.original:
            note = replace(note, action="already_restored", status="pending")
        elif found not in expected:
            note = replace(note, status="changed" if not force else "pending", forced=force)
        else:
            note = replace(note, status="pending")
        todo.append((len(notes), group, sink, found))
        notes.append(note)

    if problems:
        return replace(base, notes=tuple(notes), problems=tuple(problems))
    changed = [note for note in notes if note.status == "changed"]
    if changed:
        names = ", ".join(note.target for note in changed[:5])
        message = f"{len(changed)} note(s) changed since the run ({names}); use --force"
        return replace(base, notes=tuple(notes), refused=UndoRefusal("changed", message))

    # Phase 2: restore, latest change first; mark each note's entries undone once written.
    for position, group, sink, found in todo:
        note = notes[position]
        try:
            if note.action != "already_restored":
                content = None
                if group.original is not None:
                    content = store.blob(group.original)
                    if content is None:
                        message = "the journal lost the note's original bytes"
                        raise SinkError("blob_missing", message)
                await sink.restore(group.target, content, expected_sha256=found)
        except SinkError as error:
            problems.append(UndoProblem(group.target, group.applied_by, error.kind, error.message))
            notes[position] = replace(note, status="failed")
            break
        except Exception:
            message = "the sink restore failed"
            problems.append(UndoProblem(group.target, group.applied_by, "host_error", message))
            notes[position] = replace(note, status="failed")
            break
        with store.transaction():
            for entry in group.entries:
                if entry.undone_at is None:
                    store.journal_mark_undone(entry.id, at=clock())
        notes[position] = replace(note, status="done")

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
                "intents": list(note.intents),
                "action": note.action,
                "status": note.status,
                "forced": note.forced,
                "expected_sha256": list(note.expected_sha256),
                "found_sha256": note.found_sha256,
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
                what += " (forced: it had changed since the run)"
        else:
            what = _NOT_DONE.get(note.status, "not restored")
        lines.append(f"  {safe(note.target)}: {what}")
    if result.not_undoable:
        lines.append("Not undoable (left as they are):")
        for item in result.not_undoable:
            maybe = "" if item.applied else " (maybe applied)"
            via = f" via {safe(item.applied_by)}" if item.applied_by else ""
            lines.append(f"  {item.intent_type} {safe(item.target)}{via}{maybe}")
    for problem in result.problems:
        lines.append(f"Problem: {safe(problem.target)}: {problem.kind}: {safe(problem.message)}")
    if result.marked_undone:
        lines.append("The run is now marked undone.")
    elif result.complete and result.abandoned:
        lines.append("Every file is restored; the run was abandoned, so it stays failed.")
    return "\n".join(lines) + "\n"
