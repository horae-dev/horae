# SPDX-License-Identifier: Apache-2.0
"""Journal and undo (#22): journal first, apply, mark; undo restores every note byte for byte.

The apply phase and undo run against the in-memory `FixtureSink`; the runner wiring with the
synthetic pipeline of `apply_fixtures`."""

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apply_fixtures import DAILY, HOOK, INBOX, INTENTS, PIPELINE, START, plugins, run_pipeline
from horae.apply import (
    Applied,
    ApplyRefused,
    SinkCall,
    SinkError,
    StaticSinks,
    apply_changes,
    plan_changes,
    undo_run,
)
from horae.apply.testing import FixtureSink
from horae.apply.undo import render_undo_text, undo_to_dict
from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, RunRecord
from horae.runner import Ports, Runner
from horae.store import Store, UnknownRun

INBOX_BYTES = b"# Inbox\r\n\r\nkeep \xc3\xa9 \x00\x1b[31m bytes\r\n"
"""CRLF line endings, non-ASCII, a NUL and an escape: restored byte for byte."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clock() -> Any:
    return START


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store.open(tmp_path / "state" / "horae.db") as opened:
        yield opened


def _record(intents: list[dict[str, Any]] | None = None, **update: Any) -> RunRecord:
    run = asyncio.run(run_pipeline(dry_run=False, fixture=plugins(intents)))
    return run.model_copy(update=update) if update else run


def _sink() -> FixtureSink:
    return FixtureSink(files={INBOX: INBOX_BYTES})


def _apply(store: Store, run: RunRecord, sink: FixtureSink, *, save: bool = True) -> Any:
    store.begin_run(run.id, run.pipeline, run.started_at, dry_run=run.dry_run)
    sinks = StaticSinks(default=sink)

    def notified(key: str) -> bool:
        return store.get_notification(key) is not None

    async def go() -> Any:
        planned = await plan_changes(run, sinks, notified=notified)
        return await apply_changes(run, planned, sinks, store, clock=_clock)

    result = asyncio.run(go())
    if save:
        store.save_run(result.record)
    return result


def _undo(store: Store, run_id: str, sink: FixtureSink, **kwargs: Any) -> Any:
    return asyncio.run(undo_run(store, run_id, StaticSinks(default=sink), clock=_clock, **kwargs))


def _failure(result: Any) -> str:
    assert result.failure is not None
    return str(result.failure.kind)


# --- apply ---------------------------------------------------------------------------------


def test_apply_journals_every_change_then_applies_it(store: Store) -> None:
    run, sink = _record(), _sink()
    result = _apply(store, run, sink)
    assert result.failure is None
    assert [c.intent for c in result.record.changes] == [0, 1, 2, 3, 4, 5]
    assert [c.target for c in result.record.changes] == [
        DAILY,
        INBOX,
        "fp-new",
        "fp-old",
        HOOK,
        "ntfy.push",
    ]
    journal = store.journal(run.id)
    assert [e.intent_type for e in journal] == [i["type"] for i in INTENTS]
    assert [e.undoable for e in journal] == [True, True, False, False, False, False]
    assert all(e.applied for e in journal)
    assert journal[0].before_sha256 is None
    assert store.blob(journal[1].before_sha256 or "") == INBOX_BYTES
    assert store.blob(journal[1].after_sha256 or "") == sink.files[INBOX]
    assert {e.applied_by for e in journal} == {"vault.write_note", "vault.tasks", "hooks.post"} | {
        "ntfy.push"
    }
    assert sink.files[INBOX].startswith(INBOX_BYTES)
    assert store.get_run(run.id) == result.record
    assert len(sink.sent) == 2


def test_dry_run_never_applies(store: Store) -> None:
    run, sink = _record(dry_run=True), _sink()
    with pytest.raises(ApplyRefused):
        _apply(store, run, sink, save=False)
    assert sink.applied == []
    assert sink.sent == []
    assert store.journal(run.id) == []


@pytest.mark.parametrize("status", ["failed", "undone"])
def test_failed_or_undone_runs_are_never_applied(store: Store, status: str) -> None:
    run = _record(status=status, error="synthetic" if status == "failed" else None)
    with pytest.raises(ApplyRefused):
        _apply(store, run, _sink(), save=False)


def test_apply_checks_its_arguments(store: Store) -> None:
    run = _record()
    sinks = StaticSinks(default=_sink())
    planned = asyncio.run(plan_changes(run, sinks))
    with pytest.raises(ApplyRefused, match="another run"):
        asyncio.run(
            apply_changes(
                run.model_copy(update={"id": "other"}), planned, sinks, store, clock=_clock
            )
        )
    data = run.model_dump(mode="json")
    data["changes"] = [{"intent": 0, "target": DAILY}]
    applied = RunRecord.model_validate(data)
    with pytest.raises(ApplyRefused, match="already applied"):
        asyncio.run(apply_changes(applied, planned, sinks, store, clock=_clock))


def test_a_plan_with_problems_applies_nothing(store: Store) -> None:
    run, sink = _record(), FixtureSink()  # no Inbox: `note_missing`
    result = _apply(store, run, sink)
    assert _failure(result) == "plan_invalid"
    assert result.record.status == "failed"
    assert result.record.changes == []
    assert "plan_invalid" in (result.record.error or "")
    assert sink.applied == []
    assert store.journal(run.id) == []


def test_first_failure_stops_the_apply_and_the_run_fails(store: Store) -> None:
    run, sink = _record(), _sink()
    sink.fail[2] = SinkError("tasks_unavailable", "synthetic")
    result = _apply(store, run, sink)
    assert _failure(result) == "tasks_unavailable"
    assert result.record.status == "failed"
    assert [c.intent for c in result.record.changes] == [0, 1]
    error = result.record.error or ""
    assert error.startswith("tasks_unavailable: intent 2 (vault.tasks)")
    assert "2 change(s) applied before it, the rest not applied" in error
    journal = store.journal(run.id)
    assert [(e.intent, e.applied) for e in journal] == [(0, True), (1, True), (2, False)]
    assert sink.sent == []


def test_a_target_changed_after_planning_stops_the_apply(store: Store) -> None:
    run, sink = _record(), _sink()
    sinks = StaticSinks(default=sink)
    store.begin_run(run.id, run.pipeline, run.started_at)
    planned = asyncio.run(plan_changes(run, sinks))
    sink.files[INBOX] = b"# Inbox\nedited meanwhile\n"
    result = asyncio.run(apply_changes(run, planned, sinks, store, clock=_clock))
    assert _failure(result) == "target_changed"
    assert [c.intent for c in result.record.changes] == [0]


def test_a_sink_crash_message_is_never_kept(store: Store) -> None:
    class Crashing(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            if call.index == 4:
                raise RuntimeError("secret-looking detail")
            return await super().apply(call, expected_before_sha256=expected_before_sha256)

    run = _record()
    result = _apply(store, run, Crashing(files={INBOX: INBOX_BYTES}))
    assert _failure(result) == "host_error"
    assert "secret-looking" not in (result.record.error or "")
    assert store.journal(run.id)[-1].applied is False  # maybe applied


def test_a_sink_writing_other_bytes_fails_the_run(store: Store) -> None:
    class Liar(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            return Applied(applied.target, applied.before, b"something else")

    result = _apply(store, _record(), Liar(files={INBOX: INBOX_BYTES}))
    assert _failure(result) == "apply_mismatch"
    assert [c.intent for c in result.record.changes] == [0]


def test_a_sink_returning_garbage_from_read_stops_the_apply(store: Store) -> None:
    run = _record([INTENTS[1]])
    sink = _sink()
    sinks = StaticSinks(default=sink)
    store.begin_run(run.id, run.pipeline, run.started_at)
    planned = asyncio.run(plan_changes(run, sinks))

    class Garbage(FixtureSink):
        async def read(self, call: SinkCall) -> bytes | None:
            return "text"  # type: ignore[return-value]

    bad = StaticSinks(default=Garbage())
    result = asyncio.run(apply_changes(run, planned, bad, store, clock=_clock))
    assert _failure(result) == "preview_invalid"


def test_missing_sink_and_unchanged_notes(store: Store) -> None:
    run = _record([INTENTS[1]])
    planned = asyncio.run(plan_changes(run, StaticSinks(default=_sink())))
    store.begin_run(run.id, run.pipeline, run.started_at)
    result = asyncio.run(apply_changes(run, planned, StaticSinks(), store, clock=_clock))
    assert _failure(result) == "no_sink"


def test_a_note_left_unchanged_is_not_journalled(store: Store) -> None:
    block = (
        b"<!-- horae:begin horae-brief -->\n- [ ] Review PR 42\n<!-- horae:end horae-brief -->\n"
    )
    run, sink = _record([INTENTS[0]]), FixtureSink(files={DAILY: block})
    result = _apply(store, run, sink)
    assert result.unchanged == (0,)
    assert result.record.changes == []
    assert store.journal(run.id) == []


def test_notifications_are_deduplicated_across_reruns(store: Store) -> None:
    notify = [INTENTS[5]]
    first = _record(notify, input_hash="a" * 64)
    second = _record(notify, input_hash="a" * 64, id="20261027T053000Z-dry-brief-02")
    sink = _sink()
    assert _apply(store, first, sink).duplicates == ()
    result = _apply(store, second, sink)
    assert result.duplicates == (0,)
    assert result.record.changes == []
    assert len(sink.sent) == 1
    assert len(sink.dedupe_keys) == 1
    assert sink.dedupe_keys[0] is not None
    state = store.get_notification(sink.dedupe_keys[0])
    assert state is not None
    assert state.count == 1
    assert state.run_id == first.id
    assert store.journal(second.id) == []


def test_two_identical_notifications_of_one_run_are_both_sent(store: Store) -> None:
    sink = _sink()
    _apply(store, _record([INTENTS[5], INTENTS[5]]), sink)
    assert len(sink.sent) == 2


# --- undo ----------------------------------------------------------------------------------


def test_undo_restores_every_note_byte_for_byte_and_marks_the_run_undone(store: Store) -> None:
    run, sink = _record(), _sink()
    _apply(store, run, sink)
    result = _undo(store, run.id, sink)
    assert result.complete
    assert result.marked_undone
    assert sink.files == {INBOX: INBOX_BYTES}  # the created daily note is removed
    assert [(n.target, n.action, n.status) for n in result.notes] == [
        (INBOX, "restore", "done"),
        (DAILY, "remove", "done"),
    ]
    assert store.get_run(run.id).status == "undone"  # type: ignore[union-attr]
    assert store.run_summary(run.id).status == "undone"  # type: ignore[union-attr]
    assert all(e.undone_at is not None for e in store.journal(run.id) if e.undoable)
    types = [item.intent_type for item in result.not_undoable]
    assert types == ["task.create", "task.update_status", "http.post", "notify.push"]
    text = render_undo_text(result)
    assert "Not undoable" in text
    assert "notify.push ntfy.push via ntfy.push" in text
    assert "The run is now marked undone." in text
    data = undo_to_dict(result)
    assert data["complete"] is True
    assert [item["intent_type"] for item in data["not_undoable"]] == types


def test_undo_of_chained_edits_to_one_note(store: Store) -> None:
    edits = [
        {**INTENTS[1], "heading": "One", "content": "first\n"},
        {**INTENTS[1], "heading": "Two", "content": "second\r\nline\n"},
        {**INTENTS[1], "heading": "Three", "content": "third\n"},
    ]
    run, sink = _record(edits), _sink()
    _apply(store, run, sink)
    assert sink.files[INBOX].count(b"## ") == 3
    result = _undo(store, run.id, sink)
    assert result.complete
    assert sink.files[INBOX] == INBOX_BYTES
    assert sink.restored == [(INBOX, INBOX_BYTES)]  # one write for the three entries
    (note,) = result.notes
    assert note.intents == (0, 1, 2)


def test_undo_is_idempotent(store: Store) -> None:
    run, sink = _record(), _sink()
    _apply(store, run, sink)
    _undo(store, run.id, sink)
    again = _undo(store, run.id, sink)
    assert again.complete
    assert not again.marked_undone
    assert {n.action for n in again.notes} == {"already_undone"}
    assert len(sink.restored) == 2


def test_undo_refuses_when_a_note_changed_since_unless_forced(store: Store) -> None:
    run, sink = _record(), _sink()
    _apply(store, run, sink)
    edited = sink.files[INBOX] + b"user edit\n"
    sink.files[INBOX] = edited
    refused = _undo(store, run.id, sink)
    assert refused.refused is not None
    assert refused.refused.kind == "changed"
    assert not refused.complete
    assert sink.restored == []
    assert sink.files[INBOX] == edited
    assert DAILY in sink.files
    changed = [n for n in refused.notes if n.status == "changed"]
    assert [n.target for n in changed] == [INBOX]
    assert changed[0].found_sha256 == _sha(edited)
    assert "changed since the run" in render_undo_text(refused)
    assert store.run_summary(run.id).status == "ok"  # type: ignore[union-attr]
    forced = _undo(store, run.id, sink, force=True)
    assert forced.complete
    assert forced.marked_undone
    assert sink.files == {INBOX: INBOX_BYTES}
    assert "forced" in render_undo_text(forced)


def test_undo_refuses_a_created_note_that_was_edited(store: Store) -> None:
    run, sink = _record([INTENTS[0]]), _sink()
    _apply(store, run, sink)
    sink.files[DAILY] += b"mine\n"
    assert _undo(store, run.id, sink).refused is not None
    assert DAILY in sink.files


def test_undo_resumes_after_a_crash(store: Store) -> None:
    class Crash(BaseException):
        pass

    class CrashOnce(FixtureSink):
        crashed = False

        async def restore(
            self, target: str, content: bytes | None, *, expected_sha256: str | None
        ) -> None:
            await super().restore(target, content, expected_sha256=expected_sha256)
            if not self.crashed:
                self.crashed = True
                raise Crash  # the process dies after writing, before marking

    run = _record()
    sink = CrashOnce(files={INBOX: INBOX_BYTES})
    _apply(store, run, sink)
    with pytest.raises(Crash):
        _undo(store, run.id, sink)
    assert not any(e.undone_at for e in store.journal(run.id))
    result = _undo(store, run.id, sink)
    assert result.complete
    assert result.marked_undone
    assert [n.action for n in result.notes] == ["already_restored", "remove"]
    assert sink.files == {INBOX: INBOX_BYTES}


def test_undo_after_a_crash_mid_apply(store: Store) -> None:
    class Crash(BaseException):
        pass

    class CrashAfterWrite(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            if call.index == 1:
                raise Crash  # written, not marked
            return applied

    run = _record()
    sink = CrashAfterWrite(files={INBOX: INBOX_BYTES})
    with pytest.raises(Crash):
        _apply(store, run, sink, save=False)
    store.abandon_run(run.id, at=START, error="run crashed (Crash)")
    journal = store.journal(run.id)
    assert [(e.intent, e.applied) for e in journal] == [(0, True), (1, False)]
    result = _undo(store, run.id, sink)
    assert result.complete
    assert result.abandoned
    assert not result.marked_undone
    assert sink.files == {INBOX: INBOX_BYTES}
    assert store.run_summary(run.id).status == "failed"  # type: ignore[union-attr]
    assert "abandoned" in render_undo_text(result)


def test_undo_of_a_change_journalled_but_never_applied(store: Store) -> None:
    class Refusing(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            raise SinkError("unwritable", "synthetic")

    run, sink = _record([INTENTS[1]]), Refusing(files={INBOX: INBOX_BYTES})
    result = _apply(store, run, sink)
    assert store.journal(run.id)[0].applied is False
    assert result.record.status == "failed"
    undone = _undo(store, run.id, sink)
    assert undone.complete
    assert [n.action for n in undone.notes] == ["already_restored"]
    assert sink.restored == []
    assert store.run_summary(run.id).status == "undone"  # type: ignore[union-attr]


def test_undo_problems_and_refusals(store: Store) -> None:
    with pytest.raises(UnknownRun):
        _undo(store, "nope", _sink())
    store.begin_run("running-1", "dry-brief", START)
    running = _undo(store, "running-1", _sink())
    assert running.refused is not None
    assert running.refused.kind == "run_running"
    store.abandon_run("running-1", at=START, error="synthetic")
    dry = _record(dry_run=True, id="dry-1")
    store.save_run(dry)
    assert _undo(store, "dry-1", _sink()).refused.kind == "dry_run"

    run, sink = _record(), _sink()
    _apply(store, run, sink)

    class NotRestorable:
        async def read(self, call: SinkCall) -> bytes | None:
            return None

        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            raise AssertionError

    result = asyncio.run(
        undo_run(store, run.id, StaticSinks(default=NotRestorable()), clock=_clock)
    )
    assert {p.kind for p in result.problems} == {"no_sink"}
    assert not result.complete

    class Unreadable(FixtureSink):
        async def read_target(self, target: str) -> bytes | None:
            raise SinkError("unreadable", "synthetic") if target == INBOX else RuntimeError("x")

    result = _undo(store, run.id, Unreadable())
    assert {p.kind for p in result.problems} == {"unreadable", "host_error"}
    assert "Problem:" in render_undo_text(result)

    class FailingRestore(FixtureSink):
        async def restore(
            self, target: str, content: bytes | None, *, expected_sha256: str | None
        ) -> None:
            raise RuntimeError("detail")

    failing = FailingRestore(files=dict(sink.files))
    result = _undo(store, run.id, failing)
    assert [p.kind for p in result.problems] == ["host_error"]
    assert [n.status for n in result.notes] == ["failed", "pending"]
    assert store.run_summary(run.id).status == "ok"  # type: ignore[union-attr]

    class Racing(FixtureSink):
        async def restore(
            self, target: str, content: bytes | None, *, expected_sha256: str | None
        ) -> None:
            raise SinkError("target_changed", "edited meanwhile")

    result = _undo(store, run.id, Racing(files=dict(sink.files)))
    assert [p.kind for p in result.problems] == ["target_changed"]


# --- the runner ----------------------------------------------------------------------------


def _instance(root: Path) -> Instance:
    config = {"models": {"default": {"backend": "claude-cli", "model": "sonnet"}}}
    return Instance(
        root=root,
        config=InstanceConfig.model_validate(config),
        pipelines={PIPELINE.name: PIPELINE},
        lock=Lockfile(schema_version=1),
    )


def _runner(root: Path, store: Store, sink: FixtureSink | None) -> Runner:
    fixture = plugins()
    sinks = None if sink is None else StaticSinks(default=sink)
    return Runner(_instance(root), store, Ports(fixture, fixture, None, sinks))


def test_the_runner_applies_then_undo_restores(tmp_path: Path, store: Store) -> None:
    sink = _sink()
    record = asyncio.run(_runner(tmp_path, store, sink).run(PIPELINE.name))
    assert record.status == "ok"
    assert [c.intent for c in record.changes] == [0, 1, 2, 3, 4, 5]
    assert store.get_run(record.id) == record
    assert DAILY in sink.files
    result = _undo(store, record.id, sink)
    assert result.complete
    assert sink.files == {INBOX: INBOX_BYTES}
    assert store.get_run(record.id).status == "undone"  # type: ignore[union-attr]


def test_the_runner_never_applies_a_dry_run(tmp_path: Path, store: Store) -> None:
    sink = _sink()
    record = asyncio.run(_runner(tmp_path, store, sink).run(PIPELINE.name, dry_run=True))
    assert record.changes == []
    assert sink.applied == []
    assert sink.sent == []
    assert sink.files == {INBOX: INBOX_BYTES}
    assert store.journal(record.id) == []


def test_the_runner_without_sinks_applies_nothing(tmp_path: Path, store: Store) -> None:
    record = asyncio.run(_runner(tmp_path, store, None).run(PIPELINE.name))
    assert record.status == "ok"
    assert record.changes == []
    assert store.journal(record.id) == []


def test_the_runner_records_a_failed_apply(tmp_path: Path, store: Store) -> None:
    sink = _sink()
    sink.fail[4] = SinkError("http_error", "synthetic")
    record = asyncio.run(_runner(tmp_path, store, sink).run(PIPELINE.name))
    assert record.status == "failed"
    assert [c.intent for c in record.changes] == [0, 1, 2, 3]
    assert store.get_run(record.id) == record
    assert sink.sent == []


def test_a_crash_mid_apply_abandons_the_run_and_undo_still_works(
    tmp_path: Path, store: Store
) -> None:
    class Crash(BaseException):
        pass

    class CrashAfterWrite(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            if call.index == 1:
                raise Crash
            return applied

    sink = CrashAfterWrite(files={INBOX: INBOX_BYTES})
    with pytest.raises(Crash):
        asyncio.run(_runner(tmp_path, store, sink).run(PIPELINE.name))
    (summary,) = store.list_runs()
    assert summary.status == "failed"
    assert summary.abandoned_at is not None
    result = _undo(store, summary.id, sink)
    assert result.complete
    assert sink.files == {INBOX: INBOX_BYTES}
