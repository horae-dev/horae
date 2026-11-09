# SPDX-License-Identifier: Apache-2.0
"""`horae undo` of task status changes (#139, schemas decision 59).

A `task.update_status` whose sink said `changed: true` is journalled with the sink's details and
the task index entry it replaced; undo hands the details back to the sink (`restore_status`)
and puts the index entry back. Without `--force` a task edited since is refused; `force` is
the user's alone and reaches the sink only then."""

import asyncio
import hashlib
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from apply_fixtures import INBOX, START, plugins, run_pipeline
from horae.apply import SinkError, StaticSinks, apply_changes, plan_changes, undo_run
from horae.apply.testing import FixtureSink
from horae.apply.undo import render_undo_text, undo_to_dict
from horae.contracts import RunRecord
from horae.store import Store, TaskEntry

DONE: dict[str, Any] = {"type": "task.update_status", "task": "fp-old", "status": "done"}
SNOOZE: dict[str, Any] = {
    "type": "task.update_status",
    "task": "fp-old",
    "status": "snoozed",
    "until": "2026-11-20",
}
CALLS = {"clock": lambda: START}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state" / "horae.db") as opened:
        store = opened
        store.upsert_task(
            TaskEntry(fingerprint="fp-old", title="Old task", status="todo"), at=START
        )
        yield store


def _sink(**kwargs: Any) -> FixtureSink:
    return FixtureSink(status_details=True, **kwargs)


def _run(intents: list[dict[str, Any]], run_id: str) -> RunRecord:
    run = asyncio.run(run_pipeline(dry_run=False, fixture=plugins(intents)))
    return run.model_copy(update={"id": run_id})


def _apply(store: Store, run: RunRecord, sink: FixtureSink) -> None:
    store.begin_run(run.id, run.pipeline, run.started_at, dry_run=False)
    sinks = StaticSinks(default=sink)

    async def go() -> Any:
        planned = await plan_changes(run, sinks)
        return await apply_changes(run, planned, sinks, store, clock=CALLS["clock"])

    result = asyncio.run(go())
    store.save_run(result.record)


def _undo(store: Store, run_id: str, sink: FixtureSink, **kwargs: Any) -> Any:
    sinks = StaticSinks(default=sink)
    return asyncio.run(undo_run(store, run_id, sinks, clock=CALLS["clock"], **kwargs))


def _status(store: Store) -> str:
    entry = store.get_task("fp-old")
    assert entry is not None
    return entry.status


def test_a_tick_is_undone_with_the_exact_previous_index_entry(store: Store) -> None:
    store.set_task_status("fp-old", "snoozed", until=date(2026, 11, 3), tz="Europe/Paris", at=START)
    before = store.get_task("fp-old")
    run, sink = _run([DONE], "run-tick"), _sink()
    _apply(store, run, sink)
    assert _status(store) == "done"
    (entry,) = [e for e in store.journal(run.id) if e.intent_type == "task.update_status"]
    assert entry.undoable
    assert entry.applied
    assert entry.before_sha256 is not None  # the index entry it replaced
    result = _undo(store, run.id, sink)
    assert result.complete
    assert result.marked_undone
    assert [(t.task, t.action, t.status, t.restored) for t in result.tasks] == [
        ("fp-old", "restore", "done", True)
    ]
    assert result.tasks[0].index_restored
    assert sink.task_status["fp-old"] == "todo"  # the sink put the task back
    after = store.get_task("fp-old")
    assert after == before  # status, snooze date and zone: exactly as before
    assert store.due_snoozed(START.replace(year=2027))[0].fingerprint == "fp-old"  # wake_at too
    assert all(e.undone_at is not None for e in store.journal(run.id) if e.undoable)
    assert store.get_run(run.id).status == "undone"  # type: ignore[union-attr]
    assert [item.intent_type for item in result.not_undoable if item.target == "fp-old"] == []
    text = render_undo_text(result)
    assert "task fp-old: status restored" in text
    assert undo_to_dict(result)["tasks"][0]["index_restored"] is True


def test_a_snooze_is_undone(store: Store) -> None:
    run, sink = _run([SNOOZE], "run-snooze"), _sink()
    _apply(store, run, sink)
    assert _status(store) == "snoozed"
    result = _undo(store, run.id, sink)
    assert result.complete
    assert _status(store) == "todo"
    entry = store.get_task("fp-old")
    assert entry is not None
    assert entry.snoozed_until is None
    assert store.due_snoozed(START.replace(year=2030)) == []


def test_undo_twice_does_nothing_more(store: Store) -> None:
    run, sink = _run([DONE], "run-twice"), _sink()
    _apply(store, run, sink)
    assert _undo(store, run.id, sink).complete
    again = _undo(store, run.id, sink)
    assert again.complete
    assert [t.action for t in again.tasks] == ["already_undone"]
    assert len(sink.status_restored) == 1


def test_a_task_edited_since_is_refused_with_a_hint_unless_forced(store: Store) -> None:
    run, sink = _run([DONE], "run-edit"), _sink()
    _apply(store, run, sink)
    sink.status_changed["fp-old"] = True  # the user edited the task line meanwhile
    refused = _undo(store, run.id, sink)
    assert refused.refused is not None
    assert refused.refused.kind == "changed"
    assert "--force" in refused.refused.message
    assert "--force" in render_undo_text(refused)
    assert not refused.complete
    assert [t.status for t in refused.tasks] == ["changed"]
    assert _status(store) == "done"  # nothing written, the index neither
    assert sink.task_status["fp-old"] == "done"
    assert not any(e.undone_at for e in store.journal(run.id))
    assert store.get_run(run.id).status != "undone"  # type: ignore[union-attr]
    assert sink.status_restored == [(sink.status_restored[0][0], False)]  # asked without force
    forced = _undo(store, run.id, sink, force=True)
    assert forced.complete
    assert sink.status_restored[-1][1] is True
    assert _status(store) == "todo"
    assert "(forced)" in render_undo_text(forced)


def test_force_reaches_a_sink_only_when_the_user_asked(store: Store) -> None:
    first, second = _run([DONE, INTENT_NOTE], "run-force"), _sink(files={INBOX: b"x\n"})
    _apply(store, first, second)
    _undo(store, first.id, second)
    assert [force for _, force in second.status_restored] == [False]
    assert second.restore_forced == [False]
    again = _run([DONE, INTENT_NOTE], "run-force-2")
    sink = _sink(files={INBOX: b"x\n"})
    _apply(store, again, sink)
    _undo(store, again.id, sink, force=True)
    assert [force for _, force in sink.status_restored] == [True]
    assert sink.restore_forced
    assert all(sink.restore_forced)


INTENT_NOTE: dict[str, Any] = {
    "type": "note.append_section",
    "path": INBOX,
    "heading": "Brief",
    "content": "2 items",
}


def test_details_can_never_set_force(store: Store) -> None:
    class Forcing(FixtureSink):
        async def apply(self, call: Any, *, expected_before_sha256: str | None) -> Any:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            if applied.details:
                applied.details["force"] = True  # a plugin trying to ask for it
            return applied

    run, sink = _run([DONE], "run-forging"), Forcing(status_details=True)
    _apply(store, run, sink)
    sink.status_changed["fp-old"] = True
    refused = _undo(store, run.id, sink)
    assert refused.refused is not None
    assert sink.status_restored[0][1] is False  # `force` in the details is just data


def test_a_sink_that_cannot_undo_a_status_leaves_it_listed(store: Store) -> None:
    class Old(FixtureSink):
        async def restore_status(self, details: dict[str, Any], *, force: bool = False) -> bool:
            raise SinkError("not_supported", "no")

    run, sink = (
        _run([DONE, INTENT_NOTE], "run-old"),
        Old(status_details=True, files={INBOX: b"a\n"}),
    )
    _apply(store, run, sink)
    result = _undo(store, run.id, sink)
    assert result.complete  # the note is restored; the status change is listed as before
    assert sink.files[INBOX] == b"a\n"
    assert [t.status for t in result.tasks] == ["unsupported"]
    assert ("task.update_status", "fp-old") in {
        (item.intent_type, item.target) for item in result.not_undoable
    }
    assert _status(store) == "done"  # nothing claimed
    assert "cannot undo a status change" in render_undo_text(result)


def test_a_run_with_only_unsupported_status_changes_has_nothing_to_undo(store: Store) -> None:
    class Missing(FixtureSink):
        async def restore_status(self, details: dict[str, Any], *, force: bool = False) -> bool:
            raise SinkError("tool_missing", "the plugin lists no restore_status")

    run, sink = _run([DONE], "run-missing"), Missing(status_details=True)
    _apply(store, run, sink)
    result = _undo(store, run.id, sink)
    assert result.refused is not None
    assert result.refused.kind == "nothing_to_undo"
    assert store.get_run(run.id).status == "ok"  # type: ignore[union-attr]


def test_a_sink_that_reports_no_details_is_not_undoable(store: Store) -> None:
    run, sink = _run([DONE], "run-quiet"), FixtureSink()  # answers no details: as before
    _apply(store, run, sink)
    (entry,) = [e for e in store.journal(run.id) if e.intent_type == "task.update_status"]
    assert not entry.undoable
    result = _undo(store, run.id, sink)
    assert result.refused is not None
    assert result.refused.kind == "nothing_to_undo"


def test_a_later_run_that_changed_the_task_too_is_undone_first(store: Store) -> None:
    sink = _sink()
    first, second = _run([DONE], "run-1"), _run([SNOOZE], "run-2")
    _apply(store, first, sink)
    _apply(store, second, sink)
    refused = _undo(store, first.id, sink)
    assert refused.refused is not None
    assert refused.refused.kind == "newer_run"
    assert "run-2" in refused.refused.message
    assert sink.status_restored == []
    assert _undo(store, second.id, sink).complete
    assert _status(store) == "done"  # back to what the first run left
    assert _undo(store, first.id, sink).complete
    assert _status(store) == "todo"


def test_two_changes_of_one_task_in_a_run_are_undone_latest_first(store: Store) -> None:
    run, sink = _run([DONE, SNOOZE], "run-chain"), _sink()
    _apply(store, run, sink)
    assert _status(store) == "snoozed"
    result = _undo(store, run.id, sink)
    assert result.complete
    assert [(t.intent, t.status) for t in result.tasks] == [(1, "done"), (0, "done")]
    assert _status(store) == "todo"
    assert sink.task_status["fp-old"] == "todo"


def test_a_task_unknown_to_the_index_is_still_undone_in_the_sink(tmp_path: Path) -> None:
    with Store.open(tmp_path / "other" / "horae.db") as empty:  # an index that lost the task
        run, sink = _run([DONE], "run-unknown"), _sink()
        _apply(empty, run, sink)
        assert empty.get_task("fp-old") is None
        result = _undo(empty, run.id, sink)
        assert result.complete
        assert result.tasks[0].index_restored is False
        assert sink.task_status["fp-old"] == "todo"
        assert empty.get_task("fp-old") is None


def test_a_status_change_in_another_vault_is_not_restored(store: Store) -> None:
    run, sink = _run([DONE], "run-vault"), _sink(identity="vault-a")
    _apply(store, run, sink)
    other = _sink(identity="vault-b")
    result = _undo(store, run.id, other)
    assert result.refused is not None
    assert result.refused.kind == "sink_mismatch"
    assert other.status_restored == []
    forced = _undo(store, run.id, other, force=True)
    assert forced.refused is not None  # not even --force crosses vaults


def test_a_sink_failure_is_a_problem_and_stops_before_any_note(store: Store) -> None:
    class Failing(FixtureSink):
        async def restore_status(self, details: dict[str, Any], *, force: bool = False) -> bool:
            raise SinkError("sink_failed", "boom")

    run = _run([DONE, INTENT_NOTE], "run-fail")
    sink = Failing(status_details=True, files={INBOX: b"a\n"})
    _apply(store, run, sink)
    written = dict(sink.files)
    result = _undo(store, run.id, sink)
    assert [p.kind for p in result.problems] == ["sink_failed"]
    assert not result.complete
    assert sink.files == written  # no note was touched
    assert _status(store) == "done"


def test_journal_details_only_for_a_status_change(store: Store) -> None:
    store.begin_run("run-x", "p", START, dry_run=False)
    entry = store.journal_append(
        "run-x", intent=0, intent_type="http.post", target="https://x.example", at=START
    )
    from horae.store import InvalidRow, UnknownJournalEntry

    with pytest.raises(InvalidRow):
        store.journal_mark_applied(entry.id, status_details=b"{}")
    with pytest.raises(UnknownJournalEntry):
        store.journal_mark_applied(9999, status_details=b"{}")


def test_the_index_snapshot_of_another_task_is_never_written(store: Store) -> None:
    store.upsert_task(TaskEntry(fingerprint="fp-other", title="Other", status="todo"), at=START)
    run, sink = _run([DONE], "run-foreign"), _sink()
    _apply(store, run, sink)
    entry = next(e for e in store.journal(run.id) if e.intent_type == "task.update_status")
    assert entry.before_sha256 is not None
    snapshot = store.blob(entry.before_sha256)
    assert snapshot is not None
    forged = json.dumps(json.loads(snapshot) | {"fingerprint": "fp-other", "status": "done"})
    forged_sha = hashlib.sha256(forged.encode()).hexdigest()
    with store.transaction():
        store._put_blob(forged.encode())
        store._conn.execute(
            "UPDATE journal SET before_sha256 = ? WHERE id = ?", (forged_sha, entry.id)
        )
    result = _undo(store, run.id, sink)
    assert result.tasks[0].index_restored is False
    other = store.get_task("fp-other")
    assert other is not None
    assert other.status == "todo"
    assert _status(store) == "done"


# --- follow-ups of #149 -----------------------------------------------------------------------


def test_refusals_count_notes_and_tasks_accurately(store: Store) -> None:
    sink = _sink(files={INBOX: b"x\n"})
    first, second = _run([DONE, INTENT_NOTE], "run-1"), _run([SNOOZE, INTENT_NOTE], "run-2")
    _apply(store, first, sink)
    _apply(store, second, sink)
    newer = _undo(store, first.id, sink).refused
    assert newer is not None
    assert newer.kind == "newer_run"
    assert newer.message.startswith("1 note and 1 task (")
    assert "note(s)" not in newer.message
    other = _sink(identity="elsewhere", files=dict(sink.files))
    mismatch = _undo(store, second.id, other).refused
    assert mismatch is not None
    assert mismatch.kind == "sink_mismatch"
    assert mismatch.message.startswith("1 note and 1 task (")


def test_a_task_only_refusal_does_not_say_notes(store: Store) -> None:
    first, second = _run([DONE], "run-1"), _run([SNOOZE], "run-2")
    sink = _sink()
    _apply(store, first, sink)
    _apply(store, second, sink)
    newer = _undo(store, first.id, sink).refused
    assert newer is not None
    assert newer.message.startswith("1 task (fp-old)")
    assert "note" not in newer.message.split("were changed")[0]


def _two_tasks(store: Store) -> tuple[RunRecord, FixtureSink]:
    store.upsert_task(TaskEntry(fingerprint="fp-b", title="B", status="todo"), at=START)
    second: dict[str, Any] = {"type": "task.update_status", "task": "fp-b", "status": "done"}
    fixture = plugins([DONE, second])
    fixture.handlers["items.today"] = lambda p, i: [{"task": "fp-old"}, {"task": "fp-b"}]
    run = asyncio.run(run_pipeline(dry_run=False, fixture=fixture)).model_copy(
        update={"id": "run-many"}
    )
    sink = _sink()
    _apply(store, run, sink)
    return run, sink


def test_a_refused_task_reports_what_stays_restored_and_a_rerun_resumes(store: Store) -> None:
    run, sink = _two_tasks(store)
    sink.status_changed["fp-old"] = True  # restored second (latest first): refused
    partial = _undo(store, run.id, sink)
    assert partial.refused is not None
    assert partial.refused.kind == "changed"
    assert "1 task restored before it stay restored" in partial.refused.message
    assert partial.restored == ("task fp-b",)
    assert partial.not_restored == ("task fp-old",)
    assert partial.partial
    text = render_undo_text(partial)
    assert "Partly undone (undo is not a transaction, nothing is rolled back)" in text
    assert "Restored: task fp-b. Not restored: task fp-old." in text
    assert "horae undo run-many` again" in text
    assert undo_to_dict(partial)["partial"] is True
    assert sink.task_status["fp-b"] == "todo"  # stays restored
    assert sink.task_status["fp-old"] == "done"
    del sink.status_changed["fp-old"]  # the rerun resumes: fp-b is not asked again
    rerun = _undo(store, run.id, sink)
    assert rerun.complete
    assert not rerun.partial
    assert [(t.task, t.action) for t in rerun.tasks] == [
        ("fp-b", "already_undone"),
        ("fp-old", "restore"),
    ]
    assert [d["task"] for d, _ in sink.status_restored].count("fp-b") == 1


def test_a_complete_or_untouched_undo_is_not_partial(store: Store) -> None:
    run, sink = _run([DONE], "run-whole"), _sink()
    _apply(store, run, sink)
    sink.status_changed["fp-old"] = True
    refused = _undo(store, run.id, sink)
    assert not refused.partial
    assert "Partly undone" not in render_undo_text(refused)
    assert "restored before it" not in (refused.refused.message if refused.refused else "")
    done = _undo(store, run.id, sink, force=True)
    assert done.complete
    assert not done.partial
    assert undo_to_dict(done)["partial"] is False


class _SameFileSink(FixtureSink):
    """A sink whose task line lives in the note the run also changed: restoring the task
    writes the note, so the note's compare-and-swap, read before, no longer holds."""

    async def restore_status(self, details: dict[str, Any], *, force: bool = False) -> bool:
        wrote = await super().restore_status(details, force=force)
        self.files[INBOX] = self.files[INBOX] + b"- [ ] task line restored\n"
        return wrote


def test_a_task_and_a_note_in_one_file_fail_the_note_clearly_and_force_finishes(
    store: Store,
) -> None:
    # Task restores run before note restores (decision 59 C). The core cannot tell which file
    # a task line is in (the sink alone knows), so it cannot order the two by file: the note
    # restore fails its compare-and-swap, says why, and `--force` finishes the undo.
    run = _run([DONE, INTENT_NOTE], "run-same-file")
    sink = _SameFileSink(status_details=True, files={INBOX: b"x\n"})
    _apply(store, run, sink)
    first = _undo(store, run.id, sink)
    assert not first.complete
    assert [p.kind for p in first.problems] == ["target_changed"]
    assert "a task restored in this undo may have edited this note" in first.problems[0].message
    assert "--force" in first.problems[0].message
    assert first.partial  # the task stays restored
    assert [t.status for t in first.tasks] == ["done"]
    assert "Partly undone" in render_undo_text(first)
    again = _undo(store, run.id, sink)  # task already undone; the note is changed since the run
    assert again.refused is not None
    assert again.refused.kind == "changed"
    forced = _undo(store, run.id, sink, force=True)
    assert forced.complete
    assert sink.files[INBOX] == b"x\n"
    assert forced.notes[0].overwrote_sha256 is not None  # the overwritten bytes are kept
