# SPDX-License-Identifier: Apache-2.0
"""Journal and undo (#22) under adverse conditions: another vault, later runs, timeouts,
mismatching sinks, forced undo, cancellation, locks, and the v2 to v3 migration."""

import asyncio
import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import horae.runner._runner as runner_module
from apply_fixtures import DAILY, INBOX, INTENTS, PIPELINE, START, plugins, run_pipeline
from horae.apply import (
    Applied,
    SinkCall,
    SinkError,
    StaticSinks,
    apply_changes,
    error_kind,
    plan_changes,
    sink_identity,
    undo_run,
)
from horae.apply.testing import FixtureSink
from horae.apply.undo import UndoProblem, UndoResult, render_undo_text, undo_to_dict
from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, RunRecord
from horae.runner import Ports, Runner, recover_pipeline, try_acquire_apply_lock
from horae.store import Store, migrate

INBOX_BYTES = b"# Inbox\n\nkeep\n"


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


def _apply(
    store: Store, run: RunRecord, sink: Any, *, save: bool = True, timeout: float | None = None
) -> Any:
    store.begin_run(run.id, run.pipeline, run.started_at, dry_run=run.dry_run)
    sinks = StaticSinks(default=sink)

    async def go() -> Any:
        planned = await plan_changes(run, sinks)
        return await apply_changes(run, planned, sinks, store, clock=_clock, call_timeout_s=timeout)

    result = asyncio.run(go())
    if save:
        store.save_run(result.record)
    return result


def _undo(store: Store, run_id: str, sink: Any, **kwargs: Any) -> UndoResult:
    return asyncio.run(undo_run(store, run_id, StaticSinks(default=sink), clock=_clock, **kwargs))


# --- the sink's identity (finding 2) -------------------------------------------------------


def test_the_journal_records_where_each_change_was_written(store: Store) -> None:
    run, sink = _record(), FixtureSink(files={INBOX: INBOX_BYTES}, identity="vault-a")
    _apply(store, run, sink)
    assert {e.sink_identity for e in store.journal(run.id)} == {"vault-a"}


def test_undo_through_another_place_is_refused_even_when_forced(store: Store) -> None:
    run, sink = _record(), FixtureSink(files={INBOX: INBOX_BYTES}, identity="vault-a")
    _apply(store, run, sink)
    for force in (False, True):
        other = FixtureSink(files=dict(sink.files), identity="vault-b")
        result = _undo(store, run.id, other, force=force)
        assert result.refused is not None
        assert result.refused.kind == "sink_mismatch"
        assert {n.status for n in result.notes} == {"sink_mismatch"}
        assert other.restored == []
        assert "another sink or vault" in render_undo_text(result)

    unidentified = _undo(store, run.id, FixtureSink(identity=None))
    assert unidentified.refused is not None
    assert unidentified.refused.kind == "sink_mismatch"
    assert store.run_summary(run.id).status == "ok"  # type: ignore[union-attr]
    assert _undo(store, run.id, sink).complete


def test_a_sink_without_identity_still_undoes(store: Store) -> None:
    run, sink = _record(), FixtureSink(files={INBOX: INBOX_BYTES}, identity=None)
    _apply(store, run, sink)
    assert {e.sink_identity for e in store.journal(run.id)} == {None}
    assert _undo(store, run.id, sink).complete


def test_a_sink_identity_that_cannot_be_read(store: Store) -> None:
    class Broken(FixtureSink):
        @property
        def identity(self) -> str:
            raise SinkError("vault_unset", "no vault")

        @identity.setter
        def identity(self, value: object) -> None:
            pass  # the dataclass sets its default

    class Crashing(Broken):
        @property
        def identity(self) -> str:
            raise RuntimeError("s3cr3t")

        @identity.setter
        def identity(self, value: object) -> None:
            pass

    run = _record()
    for sink, kind in [(Broken(), "vault_unset"), (Crashing(), "host_error")]:
        sink.files[INBOX] = INBOX_BYTES
        failed = _apply(store, run.model_copy(update={"id": f"r-{kind}"}), sink)
        assert failed.failure.kind == kind
        assert "s3cr3t" not in failed.failure.message
    good = FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, run, good)
    for sink, kind in [(Broken(), "vault_unset"), (Crashing(), "host_error")]:
        result = _undo(store, run.id, sink)
        assert {p.kind for p in result.problems} == {kind}
    with pytest.raises(SinkError) as caught:
        sink_identity(FixtureSink(identity="x" * 600))
    assert caught.value.kind == "identity_invalid"


def test_a_missing_note_in_a_missing_directory_is_never_already_restored(store: Store) -> None:
    run, sink = _record(), FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, run, sink)
    del sink.files[DAILY]
    sink.missing_dirs.add("Daily")
    result = _undo(store, run.id, sink)
    assert [p.kind for p in result.problems] == ["parent_missing"]
    assert sink.restored == []
    sink.missing_dirs.clear()  # the directory is back (but the note is gone): already removed
    done = _undo(store, run.id, sink)
    assert done.complete
    assert [n.action for n in done.notes] == ["restore", "already_restored"]


# --- grouping (finding 3) ------------------------------------------------------------------


def _journal_two_tools(store: Store, identity: str | None) -> str:
    run_id = "two-tools"
    store.begin_run(run_id, "dry-brief", START)
    middle, last = INBOX_BYTES + b"one\n", INBOX_BYTES + b"one\ntwo\n"
    for tool, before, after in [("a.write", INBOX_BYTES, middle), ("b.write", middle, last)]:
        entry = store.journal_append(
            run_id,
            intent=0 if tool == "a.write" else 1,
            intent_type="note.append_section",
            target=INBOX,
            before=before,
            after=after,
            applied_by=tool,
            sink_identity=identity,
            at=START,
        )
        store.journal_mark_applied(entry.id)
    store.abandon_run(run_id, at=START, error="synthetic")
    return run_id


@pytest.mark.parametrize("identity", ["fixture", None])
def test_two_tools_of_one_sink_editing_one_note_are_undone_together(
    store: Store, identity: str | None
) -> None:
    run_id = _journal_two_tools(store, identity)
    sink = FixtureSink(files={INBOX: INBOX_BYTES + b"one\ntwo\n"}, identity=identity)
    sinks = StaticSinks(sinks={"a.write": sink, "b.write": sink})
    result = asyncio.run(undo_run(store, run_id, sinks, clock=_clock))
    assert result.complete
    (note,) = result.notes
    assert note.intents == (0, 1)
    assert sink.restored == [(INBOX, INBOX_BYTES)]


# --- the bytes a sink really wrote (finding 4) ---------------------------------------------


def test_the_bytes_a_mismatching_sink_wrote_are_journalled_and_undone(store: Store) -> None:
    class Mismatching(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            written = (applied.after or b"") + b"extra\n"
            self.files[applied.target] = written
            return Applied(applied.target, applied.before, written)

    run = _record([INTENTS[1]])
    sink = Mismatching(files={INBOX: INBOX_BYTES})
    result = _apply(store, run, sink)
    assert result.failure.kind == "apply_mismatch"
    (entry,) = store.journal(run.id)
    assert entry.applied
    assert entry.actual_after_sha256 == _sha(sink.files[INBOX])
    assert store.blob(entry.actual_after_sha256) == sink.files[INBOX]
    undone = _undo(store, run.id, sink)
    assert undone.complete
    assert sink.files[INBOX] == INBOX_BYTES


def test_a_sink_returning_garbage_after_bytes_fails_the_apply(store: Store) -> None:
    class Garbage(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            applied = await super().apply(call, expected_before_sha256=expected_before_sha256)
            return Applied(applied.target, applied.before, "text")  # type: ignore[arg-type]

    result = _apply(store, _record([INTENTS[1]]), Garbage(files={INBOX: INBOX_BYTES}))
    assert result.failure.kind == "apply_mismatch"


# --- bounded sink calls (finding 5) --------------------------------------------------------


class Slow(FixtureSink):
    slow: set[str] = set()  # noqa: RUF012 - set per instance below
    fast_reads = 0
    """Reads answered at once before `read` is slow (the plan's)."""

    async def _wait(self, what: str) -> None:
        if what in self.slow:
            await asyncio.sleep(5)

    async def read(self, call: SinkCall) -> bytes | None:
        if len(self.reads) >= self.fast_reads:
            await self._wait("read")
        return await super().read(call)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        await self._wait("apply")
        return await super().apply(call, expected_before_sha256=expected_before_sha256)

    async def read_target(self, target: str) -> bytes | None:
        await self._wait("read_target")
        return await super().read_target(target)

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        await self._wait("restore")
        await super().restore(target, content, expected_sha256=expected_sha256)


@pytest.mark.parametrize("what", ["read", "apply"])
def test_a_sink_call_past_its_timeout_stops_the_apply(store: Store, what: str) -> None:
    sink = Slow(files={INBOX: INBOX_BYTES})
    sink.slow, sink.fast_reads = {what}, 1
    result = _apply(store, _record([INTENTS[1]]), sink, timeout=0.01)
    assert result.failure.kind == "timeout"
    assert "longer than 0.01 s" in result.failure.message
    assert result.record.status == "failed"
    journal = store.journal(result.record.id)
    assert [e.applied for e in journal] == ([False] if what == "apply" else [])


def test_a_slow_read_while_planning_is_a_timeout_problem(store: Store) -> None:
    sink = Slow(files={INBOX: INBOX_BYTES})
    sink.slow = {"read"}
    run = _record([INTENTS[1]])
    planned = asyncio.run(plan_changes(run, StaticSinks(default=sink), call_timeout_s=0.01))
    assert [p.kind for p in planned.problems] == ["timeout"]


@pytest.mark.parametrize("what", ["read_target", "restore"])
def test_an_undo_sink_call_past_its_timeout(store: Store, what: str) -> None:
    sink = Slow(files={INBOX: INBOX_BYTES})
    run = _record([INTENTS[1]])
    _apply(store, run, sink)
    sink.slow = {what}
    result = _undo(store, run.id, sink, call_timeout_s=0.01)
    assert [p.kind for p in result.problems] == ["timeout"]
    assert not result.complete


def test_the_heartbeat_keeps_beating_through_the_apply(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Sleepy(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            await asyncio.sleep(0.05)
            return await super().apply(call, expected_before_sha256=expected_before_sha256)

    beats: list[str] = []
    original = Store.heartbeat

    def counting(self: Store, run_id: str, at: Any) -> None:
        beats.append(run_id)
        original(self, run_id, at)

    monkeypatch.setattr(Store, "heartbeat", counting)
    seen: list[Any] = []
    real = apply_changes

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["call_timeout_s"])
        before = len(beats)
        result = await real(*args, **kwargs)
        seen.append(len(beats) - before)
        return result

    monkeypatch.setattr(runner_module, "apply_changes", spy)
    sink = Sleepy(files={INBOX: INBOX_BYTES})
    record = asyncio.run(_runner(tmp_path, store, sink, heartbeat_s=0.005).run(PIPELINE.name))
    assert record.status == "ok"
    timeout, beaten = seen
    assert timeout == _instance(tmp_path).config.plugin_defaults.call_timeout_s
    assert beaten > 0


# --- forced undo and later runs (findings 6 and D8) ----------------------------------------


def test_a_forced_undo_keeps_what_it_overwrote(store: Store) -> None:
    run, sink = _record(), FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, run, sink)
    edited = sink.files[INBOX] + b"user edit\n"
    sink.files[INBOX] = edited
    result = _undo(store, run.id, sink, force=True)
    assert result.complete
    inbox = next(n for n in result.notes if n.target == INBOX)
    assert inbox.overwrote_sha256 == _sha(edited)
    assert store.blob(_sha(edited)) == edited
    entries = [e for e in store.journal(run.id) if e.target == INBOX]
    assert {e.undo_overwrote_sha256 for e in entries} == {_sha(edited)}
    daily = next(n for n in result.notes if n.target == DAILY)
    assert daily.overwrote_sha256 is None
    assert _sha(edited) in render_undo_text(result)
    assert undo_to_dict(result)["notes"][0]["overwrote_sha256"] == _sha(edited)


def test_undo_is_last_in_first_out_per_note(store: Store) -> None:
    sink = FixtureSink(files={INBOX: INBOX_BYTES})
    first = _record([INTENTS[1]], id="run-1")
    _apply(store, first, sink)
    after_first = sink.files[INBOX]
    second = _record([{**INTENTS[1], "heading": "Again"}], id="run-2")
    _apply(store, second, sink)
    refused = _undo(store, "run-1", sink)
    assert refused.refused is not None
    assert refused.refused.kind == "newer_run"
    assert "run-2" in refused.refused.message
    assert refused.notes[0].later_runs == ("run-2",)
    assert "later runs: run-2" in render_undo_text(refused)
    assert sink.restored == []
    assert _undo(store, "run-2", sink).complete
    assert sink.files[INBOX] == after_first
    assert _undo(store, "run-1", sink).complete  # run-2 is undone: nothing later
    assert sink.files[INBOX] == INBOX_BYTES


def test_a_forced_undo_over_a_later_run_reports_it(store: Store) -> None:
    sink = FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, _record([INTENTS[1]], id="run-1"), sink)
    _apply(store, _record([{**INTENTS[1], "heading": "Again"}], id="run-2"), sink)
    later_bytes = sink.files[INBOX]
    forced = _undo(store, "run-1", sink, force=True)
    assert forced.complete
    (note,) = forced.notes
    assert note.later_runs == ("run-2",)
    assert note.forced
    assert note.overwrote_sha256 == _sha(later_bytes)
    assert sink.files[INBOX] == INBOX_BYTES


def test_a_later_run_in_another_place_does_not_block(store: Store) -> None:
    sink = FixtureSink(files={INBOX: INBOX_BYTES}, identity="vault-a")
    elsewhere = FixtureSink(files={INBOX: INBOX_BYTES}, identity="vault-b")
    _apply(store, _record([INTENTS[1]], id="run-1"), sink)
    _apply(store, _record([INTENTS[1]], id="run-2"), elsewhere)
    assert _undo(store, "run-1", sink).complete


# --- nothing to undo (finding 7, D3) -------------------------------------------------------


def test_a_run_that_changed_no_note_has_nothing_to_undo(store: Store) -> None:
    run = _record(INTENTS[2:])
    _apply(store, run, FixtureSink())
    result = _undo(store, run.id, FixtureSink())
    assert result.refused is not None
    assert result.refused.kind == "nothing_to_undo"
    assert [item.intent_type for item in result.not_undoable] == [
        "task.create",
        "task.update_status",
        "http.post",
        "notify.push",
    ]
    assert store.run_summary(run.id).status == "ok"  # type: ignore[union-attr]


# --- error kinds and rendering (finding 11) ------------------------------------------------


def test_sink_error_kinds_are_validated(store: Store) -> None:
    assert error_kind("target_changed") == "target_changed"
    for bad in ["Bad-Kind", "", "x" * 33, "\x1b[31m", 3]:
        assert error_kind(bad) == "host_error"

    class Odd(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            raise SinkError("\x1b]8;;evil\x07", "\x1b[31m s3cr3t")

    result = _apply(store, _record([INTENTS[1]]), Odd(files={INBOX: INBOX_BYTES}))
    assert result.failure.kind == "host_error"
    assert "s3cr3t" not in result.failure.message
    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE, never written literally
    problem = UndoProblem(f"{rlo}note", None, "\x1b[2J", "msg")
    text = render_undo_text(UndoResult("r", "p", "ok", False, False, problems=(problem,)))
    assert "\x1b" not in text
    assert rlo not in text


# --- the runner: cancellation, the apply lock, recovery (findings 1, 12, D6) -----------------


def _instance(root: Path) -> Instance:
    config = {"models": {"default": {"backend": "claude-cli", "model": "sonnet"}}}
    return Instance(
        root=root,
        config=InstanceConfig.model_validate(config),
        pipelines={PIPELINE.name: PIPELINE},
        lock=Lockfile(schema_version=1),
    )


def _runner(root: Path, store: Store, sink: FixtureSink | None, **kwargs: Any) -> Runner:
    fixture = plugins()
    sinks = None if sink is None else StaticSinks(default=sink)
    return Runner(_instance(root), store, Ports(fixture, fixture, None, sinks), **kwargs)


def test_a_run_cancelled_after_its_steps_is_failed_and_applies_nothing(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopping = asyncio.Event()

    async def slow_to_stop(self: Runner, state: object, task: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await asyncio.sleep(0.2)

    monkeypatch.setattr(Runner, "_heartbeat", slow_to_stop)
    sink = FixtureSink(files={INBOX: INBOX_BYTES})
    seen: list[RunRecord] = []

    async def scenario() -> None:
        task = asyncio.create_task(
            _runner(tmp_path, store, sink).run(PIPELINE.name, on_record=seen.append)
        )
        await stopping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    (record,) = seen
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.startswith("cancelled before apply")
    assert record.changes == []
    assert sink.applied == []
    assert store.journal(record.id) == []


def test_a_run_waits_for_the_apply_lock_then_fails(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "APPLY_LOCK_WAIT_S", 0.05)
    runner = _runner(tmp_path, store, FixtureSink(files={INBOX: INBOX_BYTES}))
    held = try_acquire_apply_lock(runner.instance.state_dir)
    assert held is not None
    with held:
        record = asyncio.run(runner.run(PIPELINE.name))
    assert record.status == "failed"
    assert (record.error or "").startswith("apply_locked:")
    assert store.journal(record.id) == []
    again = asyncio.run(runner.run(PIPELINE.name))  # the lock is free again
    assert again.status == "ok"


def test_a_run_abandoned_while_applying_is_lost(tmp_path: Path, store: Store) -> None:
    class Abandoning(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            store.abandon_run(call.run_id, at=START, error="by hand")
            return await super().apply(call, expected_before_sha256=expected_before_sha256)

    sink = Abandoning(files={INBOX: INBOX_BYTES})
    with pytest.raises(runner_module.RunLost):
        asyncio.run(_runner(tmp_path, store, sink).run(PIPELINE.name))

    class AbandonedLast(FixtureSink):
        async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
            if call.index == len(INTENTS) - 1:  # the last change: no journal write follows
                store.abandon_run(call.run_id, at=START, error="by hand")
                await asyncio.sleep(0.05)  # the heartbeat notices
            return await super().apply(call, expected_before_sha256=expected_before_sha256)

    last = AbandonedLast(files={INBOX: INBOX_BYTES})
    runner = _runner(tmp_path, store, last, heartbeat_s=0.005)
    with pytest.raises(runner_module.RunLost):
        asyncio.run(runner.run(PIPELINE.name))


def test_recover_pipeline_abandons_dead_rows_only(store: Store) -> None:
    store.begin_run("dead", "dry-brief", START)
    store.begin_run("other", "other-pipeline", START)
    (recovered,) = recover_pipeline(store, "dry-brief", clock=lambda: START + timedelta(1))
    assert recovered.id == "dead"
    assert store.run_summary("dead").abandoned_at is not None  # type: ignore[union-attr]
    assert store.run_summary("other").status == "running"  # type: ignore[union-attr]
    assert recover_pipeline(store, "dry-brief", clock=_clock) == []


# --- the v2 to v3 migration (finding 13) ---------------------------------------------------


def test_a_v2_database_with_a_journal_migrates_to_v3(tmp_path: Path) -> None:
    db = tmp_path / "state" / "horae.db"
    db.parent.mkdir()
    blob = b"before"
    digest = _sha(blob)
    conn = sqlite3.connect(db, autocommit=True)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        assert migrate(conn, target=2) == 2
        conn.execute(
            "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, "
            "abandoned_at, error) VALUES ('old', 'dry-brief', 'failed', 0, "
            "'2026-10-27T05:30:00.000000Z', '2026-10-27T05:31:00.000000Z', "
            "'2026-10-27T05:31:00.000000Z', 'abandoned')"
        )
        conn.execute("INSERT INTO blobs (sha256, data, size) VALUES (?, ?, ?)", (digest, blob, 6))
        conn.execute(
            "INSERT INTO journal (run_id, seq, intent, intent_type, target, before_sha256, "
            "undoable, applied, created_at) VALUES ('old', 0, 0, 'note.append_section', "
            "'Inbox.assistant.md', ?, 1, 1, '2026-10-27T05:30:30.000000Z')",
            (digest,),
        )
        assert migrate(conn, target=3) == 3
        row = conn.execute(
            "SELECT applied_by, sink_identity, actual_after_sha256, undo_overwrote_sha256 "
            "FROM journal"
        ).fetchone()
        assert row == (None, None, None, None)
        bad = [
            "UPDATE journal SET applied_by = ''",
            "UPDATE journal SET sink_identity = ''",
            "UPDATE journal SET sink_identity = printf('%.600c', 'x')",
            "UPDATE journal SET actual_after_sha256 = printf('%.64c', 'a')",
            "UPDATE journal SET undo_overwrote_sha256 = printf('%.64c', 'b')",
        ]
        for statement in bad:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
    finally:
        conn.close()
    with Store.open(db) as opened:
        (entry,) = opened.journal("old")
        assert entry.applied_by is None
        assert entry.sink_identity is None
        sink = FixtureSink(files={INBOX: b"before\nlater\n"})
        assert entry.before_sha256 == digest
        result = asyncio.run(undo_run(opened, "old", StaticSinks(default=sink), clock=_clock))
        assert result.refused is not None  # no `after`: whatever is there changed since
        assert result.refused.kind == "changed"


def test_undo_without_the_original_bytes_stops(tmp_path: Path, store: Store) -> None:
    run, sink = _record([INTENTS[1]]), FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, run, sink)
    conn = sqlite3.connect(tmp_path / "state" / "horae.db", autocommit=True)
    try:
        conn.execute("DELETE FROM blobs WHERE sha256 = ?", (_sha(INBOX_BYTES),))
    finally:
        conn.close()
    result = _undo(store, run.id, sink)
    assert [p.kind for p in result.problems] == ["blob_missing"]
    assert sink.restored == []
    assert store.run_summary(run.id).status == "ok"  # type: ignore[union-attr]


def test_prune_keeps_and_drops_the_new_blobs(store: Store) -> None:
    run = _record([INTENTS[1]])
    sink = FixtureSink(files={INBOX: INBOX_BYTES})
    _apply(store, run, sink)
    sink.files[INBOX] += b"edit\n"
    edited = sink.files[INBOX]
    _undo(store, run.id, sink, force=True)
    assert store.blob(_sha(edited)) == edited
    assert store.prune_runs(START - timedelta(days=1)) == 0
    assert store.blob(_sha(edited)) == edited
    assert store.prune_runs(START + timedelta(days=365)) == 1
    assert store.blob(_sha(edited)) is None


def test_journal_entries_reject_oversized_extra_contents(store: Store) -> None:
    from horae.store import MAX_JOURNAL_BYTES, JournalTooLarge

    store.begin_run("big", "dry-brief", START)
    entry = store.journal_append(
        "big", intent=0, intent_type="note.create", target=INBOX, after=b"x", at=START
    )
    with pytest.raises(JournalTooLarge):
        store.journal_mark_applied(entry.id, actual_after=b"x" * (MAX_JOURNAL_BYTES + 1))
    with pytest.raises(JournalTooLarge):
        store.journal_mark_undone(entry.id, at=START, overwrote=b"x" * (MAX_JOURNAL_BYTES + 1))
