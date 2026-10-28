# SPDX-License-Identifier: Apache-2.0
"""The run lifecycle (#19): lock, heartbeat, statuses, timeouts, cancellation, crashes and
stale-run recovery."""

import asyncio
import json
import logging
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from horae.config import STORE_FILE, Instance
from horae.contracts import RunId, RunRecord
from horae.engine import ToolResult, canonical_pipeline
from horae.runner import (
    LOCKS_DIR,
    PipelineLock,
    PipelineLocked,
    Ports,
    RunLost,
    Runner,
    RunTimedOut,
    UnknownPipeline,
    lock_is_free,
    lock_path,
    new_run_id,
)
from horae.runner import _runner as runner_module
from horae.store import RunExists, RunNotStale, Store, StoreError
from runner_fixtures import SKILL, Plugins, brief_llm, instance, write_instance

FIXTURES = Path(__file__).resolve().parent / "runner_fixtures.py"


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    return instance(tmp_path)


@pytest.fixture
def store(inst: Instance) -> Iterator[Store]:
    with Store.open(inst.store_path) as opened:
        yield opened


def _runner(inst: Instance, store: Store, plugins: Plugins | None = None, **kw: object) -> Runner:
    plugins = plugins or Plugins()
    llm = kw.pop("llm", None)
    return Runner(inst, store, Ports(plugins, plugins, llm), **kw)  # type: ignore[arg-type]


class Stubborn(Plugins):
    """`fixture.slow` ignores cancellation: it keeps waiting for `release` (`late=False`), or
    returns at once as if it had finished (`late=True`)."""

    def __init__(self, *, late: bool = False) -> None:
        super().__init__()
        self.late = late
        self.ignored = asyncio.Event()

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        if ref != "fixture.slow":
            return await super().call(ref, params, input)
        self.started.set()
        while not self.release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.ignored.set()
                if self.late:
                    break
        return ToolResult(data=[])


# --- run ids -----------------------------------------------------------------------------


def test_run_ids_sort_by_start_and_match_the_contract() -> None:
    from pydantic import TypeAdapter

    t0 = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)
    ids = [new_run_id("daily-brief", t0 + timedelta(microseconds=n)) for n in (0, 1, 10, 10**6)]
    assert ids == sorted(ids)
    assert ids[0].startswith("20261027T053000000000Z-daily-brief-")
    longest = new_run_id("a" * 64, t0)
    for run_id in [*ids, longest]:
        assert TypeAdapter(RunId).validate_python(run_id) == run_id
    with pytest.raises(ValueError, match="aware"):
        new_run_id("x", datetime(2026, 1, 1))


def test_a_run_id_collision_is_retried_once(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = "20200101T000000000000Z-collect-aaaaaa"
    store.begin_run(taken, "other", datetime(2020, 1, 1, tzinfo=UTC))
    store.abandon_run(taken, at=datetime(2020, 1, 1, tzinfo=UTC), error="old")
    ids = iter([taken, "20200101T000000000000Z-collect-bbbbbb"])
    monkeypatch.setattr(runner_module, "new_run_id", lambda name, at: next(ids))
    record = asyncio.run(_runner(inst, store).run("collect"))
    assert record.id == "20200101T000000000000Z-collect-bbbbbb"
    # Twice in a row: the error propagates, nothing is left running.
    monkeypatch.setattr(runner_module, "new_run_id", lambda name, at: taken)
    with pytest.raises(RunExists):
        asyncio.run(_runner(inst, store).run("collect"))
    assert store.list_runs(status="running") == []
    assert lock_is_free(inst.state_dir, "collect") is True


# --- statuses ------------------------------------------------------------------------------


def test_ok_run_is_saved_and_releases_the_lock(inst: Instance, store: Store) -> None:
    seen: list[RunRecord] = []
    record = asyncio.run(_runner(inst, store).run("collect", on_record=seen.append))
    assert record.status == "ok"
    assert seen == [record]
    assert store.get_run(record.id) == record
    summary = store.run_summary(record.id)
    assert summary is not None
    assert summary.status == "ok"
    assert summary.pid == os.getpid()
    assert store.list_runs(status="running") == []
    assert record.context.timezone == "UTC"  # the instance default
    assert lock_is_free(inst.state_dir, "collect") is True
    # The lock is free again: a second run starts.
    assert asyncio.run(_runner(inst, store).run("collect")).status == "ok"


def test_the_record_keeps_lock_config_digests_models_and_definition(
    inst: Instance, store: Store
) -> None:
    """What `horae replay` compares and replays with (#23, D1 and D3)."""
    record = asyncio.run(_runner(inst, store, llm=brief_llm()).run("brief"))
    assert record.lock_digest == inst.lock_digest()
    assert record.config_digest == inst.config_digest()
    assert record.models == {"default": "sonnet"}
    assert record.pipeline_definition == canonical_pipeline(inst.pipelines["brief"])
    assert record.plugins["fixture"].provides == ["fixture.items"]
    assert record.plugins["pack"].provides == [SKILL]
    assert len({inst.lock_digest(), inst.config_digest()}) == 2
    # The config digest covers `models` and `plugins` only.
    other = Instance(
        root=inst.root,
        config=inst.config.model_copy(update={"timezone": "Europe/Paris"}),
        pipelines=inst.pipelines,
        lock=inst.lock,
    )
    assert other.config_digest() == inst.config_digest()
    renamed = inst.config.model_validate(
        {"models": {"default": {"backend": "claude-cli", "model": "opus"}}}
    )
    moved = Instance(root=inst.root, config=renamed, pipelines=inst.pipelines, lock=inst.lock)
    assert moved.config_digest() != inst.config_digest()


def test_statuses_degraded_failed_and_noop(inst: Instance, store: Store) -> None:
    assert asyncio.run(_runner(inst, store).run("partial")).status == "degraded"
    failed = asyncio.run(_runner(inst, store).run("broken"))
    assert failed.status == "failed"
    assert failed.steps[1].error is not None
    assert failed.steps[1].error.kind == "tool_error"
    degraded = asyncio.run(_runner(inst, store, llm=brief_llm(fail=True)).run("brief"))
    assert degraded.status == "degraded"
    first = asyncio.run(_runner(inst, store, llm=brief_llm()).run("brief"))
    assert first.status == "ok"
    # Unchanged input: the previous accepted output is reused, with no LLM call.
    llm = brief_llm()
    second = asyncio.run(_runner(inst, store, llm=llm).run("brief"))
    assert second.status == "noop"
    assert llm.requests == []
    assert second.llm[0].cached is True
    statuses = {run.id: run.status for run in store.list_runs()}
    assert statuses[second.id] == "noop"
    assert statuses[failed.id] == "failed"


def test_running_is_visible_while_the_run_runs(inst: Instance, store: Store) -> None:
    plugins = Plugins()

    async def scenario() -> RunRecord:
        task = asyncio.create_task(_runner(inst, store, plugins).run("slow"))
        await plugins.started.wait()
        (running,) = store.list_runs(status="running")
        assert running.pipeline == "slow"
        assert running.finished_at is None
        assert running.duration is None
        assert lock_is_free(inst.state_dir, "slow") is False
        plugins.release.set()
        return await task

    record = asyncio.run(scenario())
    assert record.status == "ok"
    assert store.list_runs(status="running") == []


def test_steps_are_recorded_as_they_finish(inst: Instance, store: Store) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins).run("slow"))
        await plugins.started.wait()
        (running,) = store.list_runs(status="running")
        assert store.steps(running.id) == []
        plugins.release.set()
        record = await task
        assert [s.status for s in store.steps(record.id)] == ["ok", "ok"]

    asyncio.run(scenario())


def test_dry_run_and_vars_are_passed_through(inst: Instance, store: Store) -> None:
    record = asyncio.run(_runner(inst, store).run("collect", dry_run=True, vars={"who": "me"}))
    assert record.dry_run is True
    assert record.context.vars["who"] == "me"
    summary = store.run_summary(record.id)
    assert summary is not None
    assert summary.dry_run is True


def test_trigger_timezone_wins_over_the_instance_default(tmp_path: Path) -> None:
    inst = instance(tmp_path, timezone="Europe/Paris")
    pipelines = dict(inst.pipelines)
    data = pipelines["collect"].model_dump(mode="json", exclude_none=True)
    data["trigger"] = {"cron": "0 6 * * *", "timezone": "America/New_York"}
    pipelines["collect"] = type(pipelines["collect"]).model_validate(data)
    inst = Instance(inst.root, inst.config, pipelines, inst.lock)
    with Store.open(inst.store_path) as store:
        assert asyncio.run(_runner(inst, store).run("collect")).context.timezone == (
            "America/New_York"
        )
        assert asyncio.run(_runner(inst, store).run("partial")).context.timezone == ("Europe/Paris")


def test_unknown_pipeline(inst: Instance, store: Store) -> None:
    with pytest.raises(UnknownPipeline, match="'nope'") as info:
        asyncio.run(_runner(inst, store).run("nope"))
    assert isinstance(info.value, LookupError)
    assert store.list_runs() == []
    assert not (inst.state_dir / LOCKS_DIR / "nope.lock").exists()


# --- locking -------------------------------------------------------------------------------


def test_second_run_of_a_running_pipeline_is_refused(inst: Instance, store: Store) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        first = asyncio.create_task(_runner(inst, store, plugins).run("slow"))
        await plugins.started.wait()
        (holder,) = store.list_runs(status="running")
        with Store.open(inst.store_path) as other:
            with pytest.raises(PipelineLocked) as info:
                await _runner(inst, other).run("slow")
            message = str(info.value)
            assert holder.id in message
            assert "'slow' is already running" in message
            assert f"pid {os.getpid()}" in message
            assert info.value.run_id == holder.id
            assert info.value.holder is not None
            # Dry runs take the lock too.
            with pytest.raises(PipelineLocked):
                await _runner(inst, other).run("slow", dry_run=True)
            # Another pipeline is not locked.
            assert (await _runner(inst, other).run("collect")).status == "ok"
        plugins.release.set()
        assert (await first).status == "ok"

    asyncio.run(scenario())
    assert len(store.list_runs(pipeline="slow")) == 1  # the refused runs wrote nothing


def test_a_held_lock_without_a_running_row(inst: Instance, store: Store) -> None:
    held = PipelineLock.try_acquire(inst.state_dir, "collect")
    assert held is not None
    with held:
        assert held.held
        with pytest.raises(PipelineLocked) as info:
            asyncio.run(_runner(inst, store).run("collect"))
    assert info.value.run_id is None
    assert str(info.value) == (
        "pipeline 'collect' is already running (its lock is held); try again when it has finished"
    )
    assert store.list_runs() == []
    assert not held.held


def test_a_briefly_held_lock_is_retried(inst: Instance, store: Store) -> None:
    held = PipelineLock.try_acquire(inst.state_dir, "collect")
    assert held is not None

    async def scenario() -> RunRecord:
        asyncio.get_running_loop().call_later(0.05, held.release)
        return await _runner(inst, store).run("collect")

    assert asyncio.run(scenario()).status == "ok"


def test_lock_message_with_and_without_owner_details() -> None:
    locked = PipelineLocked("p", "run-1")
    assert str(locked) == (
        "pipeline 'p' is already running as run 'run-1'; try again when it has finished"
    )
    assert locked.pipeline == "p"


def test_lock_files_are_private_and_probing_creates_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert lock_is_free(state, "collect") is True
    assert not state.exists()
    lock = PipelineLock.try_acquire(state, "collect")
    assert lock is not None
    try:
        assert lock.path == lock_path(state, "collect") == state / "locks" / "collect.lock"
        assert stat.S_IMODE(lock.path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600
        assert lock_is_free(state, "collect") is False
        assert PipelineLock.try_acquire(state, "collect") is None
        assert lock_is_free(state, "other") is True
        assert not lock_path(state, "other").exists()
    finally:
        lock.release()
        lock.release()  # idempotent
    assert lock_is_free(state, "collect") is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_lock_probe_that_cannot_open_the_file_cannot_tell(tmp_path: Path) -> None:
    state = tmp_path / "state"
    lock = PipelineLock.try_acquire(state, "collect")
    assert lock is not None
    lock.release()
    lock_path(state, "collect").chmod(0)
    assert lock_is_free(state, "collect") is None


def _child(*args: object) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 (our own interpreter and test script)
        [sys.executable, str(FIXTURES), *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, proc: subprocess.Popen[str], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"child exited early ({proc.returncode}): {out} {err}")
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            pytest.fail("child did not start in time")
        time.sleep(0.02)


def test_two_processes_one_pipeline_the_second_exits_with_a_lock_message(
    tmp_path: Path,
) -> None:
    root = write_instance(tmp_path / "instance")
    ready, release = tmp_path / "ready", tmp_path / "release"
    first = _child(root, "slow", ready, release)
    try:
        _wait_for(ready, first)
        with Store.open(root / "state" / STORE_FILE) as store:
            (holder,) = store.list_runs(status="running")
        second = _child(root, "slow")
        out, err = second.communicate(timeout=60)
        assert second.returncode == 3, (out, err)
        assert holder.id in err
        assert "pipeline 'slow' is already running as run" in err
        release.write_text("go", encoding="utf-8")
        out, err = first.communicate(timeout=60)
        assert first.returncode == 0, err
        assert json.loads(out) == {"id": holder.id, "status": "ok"}
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()
    # Released: a new run starts.
    third = _child(root, "slow", tmp_path / "ready2", release)
    out, err = third.communicate(timeout=60)
    assert third.returncode == 0, err


def test_a_killed_process_is_recovered_by_the_next_run_of_its_pipeline(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    ready, release = tmp_path / "ready", tmp_path / "release"
    first = _child(root, "slow", ready, release)
    try:
        _wait_for(ready, first)
    finally:
        first.kill()  # SIGKILL: the kernel releases its lock
        first.communicate()
    with Store.open(root / "state" / STORE_FILE) as store:
        (dead,) = store.list_runs(status="running")
    assert lock_is_free(root / "state", "slow") is True
    # Another pipeline does not recover it.
    other = _child(root, "collect")
    _out, err = other.communicate(timeout=60)
    assert other.returncode == 0, err
    with Store.open(root / "state" / STORE_FILE) as store:
        assert [run.id for run in store.list_runs(status="running")] == [dead.id]
    # The next run of `slow` does, at once (no stale delay), and runs.
    release.write_text("go", encoding="utf-8")
    second = _child(root, "slow", tmp_path / "ready2", release)
    out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    assert json.loads(out)["status"] == "ok"
    with Store.open(root / "state" / STORE_FILE) as store:
        summary = store.run_summary(dead.id)
        assert summary is not None
        assert summary.status == "failed"
        assert summary.abandoned_at is not None
        assert summary.error is not None
        assert summary.error.startswith("abandoned: its process died (last heartbeat ")
        assert store.list_runs(status="running") == []


# --- heartbeat and recovery ----------------------------------------------------------------


def test_heartbeat_while_running(inst: Instance, store: Store) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins, heartbeat_s=0.02).run("slow"))
        await plugins.started.wait()
        (running,) = store.list_runs(status="running")
        await asyncio.sleep(0.15)
        (later,) = store.list_runs(status="running")
        assert later.heartbeat_at is not None
        assert later.heartbeat_at > running.started_at
        plugins.release.set()
        await task

    asyncio.run(scenario())


def _begin(store: Store, run_id: str, pipeline: str, at: datetime) -> None:
    store.begin_run(run_id, pipeline, at, pid=4242, host="elsewhere")


def test_recovery_abandons_only_the_rows_of_the_pipeline_being_started(
    inst: Instance, store: Store
) -> None:
    now = datetime.now(UTC)
    # A fresh heartbeat does not matter: with the lock held, the row is dead.
    _begin(store, "dead-collect", "collect", now)
    _begin(store, "dead-partial", "partial", now - timedelta(days=1))
    record = asyncio.run(_runner(inst, store).run("collect"))
    assert record.status == "ok"
    dead = store.run_summary("dead-collect")
    assert dead is not None
    assert dead.status == "failed"
    assert dead.abandoned_at is not None
    assert dead.error is not None
    assert dead.error.startswith("abandoned: its process died (last heartbeat ")
    assert [run.id for run in store.list_runs(status="running")] == ["dead-partial"]


def test_recovery_leaves_a_row_that_changed_and_the_store_guard_refuses(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _begin(store, "beating", "collect", datetime.now(UTC))

    def changed(run_id: str, **kw: object) -> None:
        raise RunNotStale(run_id)

    monkeypatch.setattr(store, "abandon_run", changed)
    with (
        caplog.at_level(logging.WARNING, "horae.runner"),
        pytest.raises(PipelineLocked, match="'beating'"),
    ):
        asyncio.run(_runner(inst, store).run("collect"))
    assert "running without the pipeline lock" in caplog.text
    assert lock_is_free(inst.state_dir, "collect") is True


def test_recovery_tolerates_a_run_that_finished_meanwhile(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _begin(store, "gone", "collect", datetime.now(UTC) - timedelta(hours=1))
    real = store.abandon_run

    def race(run_id: str, *, at: datetime, error: str, **kw: Any) -> None:
        real(run_id, at=at, error="someone else")
        real(run_id, at=at, error=error, **kw)  # raises RunNotRunning

    monkeypatch.setattr(store, "abandon_run", race)
    assert asyncio.run(_runner(inst, store).run("collect")).status == "ok"
    gone = store.run_summary("gone")
    assert gone is not None
    assert gone.error == "someone else"


def test_a_run_abandoned_while_running_is_stopped_by_the_heartbeat(
    inst: Instance, store: Store
) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins, heartbeat_s=0.02).run("slow"))
        await plugins.started.wait()
        (running,) = store.list_runs(status="running")
        store.abandon_run(running.id, at=datetime.now(UTC), error="judged stale")
        with pytest.raises(RunLost, match=re.escape(running.id)):
            await task

    asyncio.run(scenario())
    (run,) = store.list_runs()
    assert run.status == "failed"
    assert run.error == "judged stale"
    assert lock_is_free(inst.state_dir, "slow") is True


def test_a_run_abandoned_while_running_is_lost_at_its_next_step(
    inst: Instance, store: Store
) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        # No heartbeat before the end: the step write notices.
        task = asyncio.create_task(_runner(inst, store, plugins, heartbeat_s=50).run("slow"))
        await plugins.started.wait()
        (running,) = store.list_runs(status="running")
        store.abandon_run(running.id, at=datetime.now(UTC), error="judged stale")
        plugins.release.set()
        with pytest.raises(RunLost, match=re.escape(running.id)):
            await task

    asyncio.run(scenario())
    (run,) = store.list_runs()
    assert (run.status, run.error) == ("failed", "judged stale")
    assert store.get_run(run.id) is None


def test_a_run_abandoned_before_its_save_is_lost(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store.save_run

    def abandoned_first(record: RunRecord) -> None:
        store.abandon_run(record.id, at=datetime.now(UTC), error="judged stale")
        real(record)  # InvalidRow: the run was abandoned

    monkeypatch.setattr(store, "save_run", abandoned_first)
    seen: list[RunRecord] = []
    with pytest.raises(RunLost):
        asyncio.run(_runner(inst, store, heartbeat_s=50).run("collect", on_record=seen.append))
    assert seen == []
    (run,) = store.list_runs()
    assert (run.status, run.error) == ("failed", "judged stale")


def test_a_save_that_fails_otherwise_is_a_crash(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.store import InvalidRow

    def refuse(record: RunRecord) -> None:
        raise InvalidRow("synthetic")

    monkeypatch.setattr(store, "save_run", refuse)
    with pytest.raises(InvalidRow):
        asyncio.run(_runner(inst, store).run("collect"))
    (run,) = store.list_runs()
    assert run.error == "run crashed (InvalidRow)"


@pytest.mark.parametrize("error", [StoreError("busy"), RuntimeError("a bug in the store")])
def test_heartbeat_survives_failures_and_logs_once(
    inst: Instance,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    plugins = Plugins()
    beats: list[int] = []

    def failing(run_id: str, at: datetime) -> None:
        beats.append(1)
        raise error

    monkeypatch.setattr(store, "heartbeat", failing)

    async def scenario() -> RunRecord:
        task = asyncio.create_task(_runner(inst, store, plugins, heartbeat_s=0.01).run("slow"))
        await plugins.started.wait()
        await asyncio.sleep(0.1)
        plugins.release.set()
        return await task

    with caplog.at_level(logging.WARNING, "horae.runner"):
        assert asyncio.run(scenario()).status == "ok"
    assert len(beats) >= 2
    warnings = [r for r in caplog.records if "heartbeat failed" in r.getMessage()]
    assert len(warnings) == 1
    assert type(error).__name__ in warnings[0].getMessage()
    assert str(error) not in warnings[0].getMessage()


# --- timeouts ------------------------------------------------------------------------------


def test_run_timeout_stops_the_step_and_marks_the_run(inst: Instance, store: Store) -> None:
    seen: list[RunRecord] = []
    with pytest.raises(RunTimedOut) as info:
        asyncio.run(_runner(inst, store, timeout_s=0.2).run("slow", on_record=seen.append))
    assert not isinstance(info.value, asyncio.CancelledError)
    record = info.value.record
    assert record is not None
    assert seen == [record]
    assert info.value.timeout_s == 0.2
    assert str(info.value) == f"run {record.id!r} timed out after 0.2 s"
    assert record.status == "failed"
    assert record.error == "step 0 (collect): the run timed out after 0.2 s"
    collect, notify = record.steps
    assert collect.status == "failed"
    assert collect.error is not None
    assert collect.error.kind == "timeout"
    assert notify.status == "skipped"
    assert store.get_run(record.id) == record
    assert store.list_runs(status="running") == []
    assert lock_is_free(inst.state_dir, "slow") is True


def test_run_timeout_defaults_to_horae_yaml(tmp_path: Path) -> None:
    inst = instance(tmp_path, runs={"timeout_s": 1})
    with Store.open(inst.store_path) as store:
        runner = _runner(inst, store)
        assert runner.run_timeout_s == 1
        with pytest.raises(RunTimedOut) as info:
            asyncio.run(runner.run("slow"))
    assert info.value.record is not None
    assert info.value.record.error == "step 0 (collect): the run timed out after 1 s"
    assert instance(tmp_path).config.runs.timeout_s == 900


def test_a_step_that_ignores_the_timeout_is_abandoned_after_the_grace_period(
    inst: Instance, store: Store
) -> None:
    plugins = Stubborn()

    async def scenario() -> RunTimedOut:
        runner = _runner(inst, store, plugins, timeout_s=0.1, stop_grace_s=0.1)
        begin = time.monotonic()
        with pytest.raises(RunTimedOut) as info:
            await runner.run("slow")
        assert time.monotonic() - begin < 5  # bounded: timeout + grace
        assert plugins.ignored.is_set()
        assert lock_is_free(inst.state_dir, "slow") is True
        # The stray step finishes later: its writes are refused, nothing changes.
        plugins.release.set()
        await asyncio.sleep(0.1)
        return info.value

    timed_out = asyncio.run(scenario())
    assert timed_out.record is None
    assert "did not stop" in str(timed_out)
    (run,) = store.list_runs()
    assert run.status == "failed"
    assert run.abandoned_at is not None
    assert run.error == (
        "timeout: the run timed out after 0.1 s and its step did not stop within 0.1 s"
    )
    assert store.get_run(run.id) is None


def test_a_run_that_finishes_after_its_timeout_is_failed_not_ok(
    inst: Instance, store: Store
) -> None:
    # A transform ignoring the cancellation: the step (and so the run) finishes late.
    plugins = Stubborn(late=True)
    with pytest.raises(RunTimedOut) as info:
        asyncio.run(_runner(inst, store, plugins, timeout_s=0.1).run("slow-transform"))
    record = info.value.record
    assert record is not None
    assert record.status == "failed"
    assert record.error == "the run timed out after 0.1 s (it finished after the deadline)"
    assert [step.status for step in record.steps] == ["ok", "ok"]
    assert store.get_run(record.id) == record


def test_a_collect_call_ignoring_the_cancellation_still_stops_its_step(
    inst: Instance, store: Store
) -> None:
    # The collect step's task group re-raises the cancellation even when a call ignores it.
    plugins = Stubborn(late=True)
    with pytest.raises(RunTimedOut) as info:
        asyncio.run(_runner(inst, store, plugins, timeout_s=0.1).run("slow"))
    record = info.value.record
    assert record is not None
    assert record.error == "step 0 (collect): the run timed out after 0.1 s"
    assert [step.status for step in record.steps] == ["failed", "skipped"]


# --- cancellation and crashes --------------------------------------------------------------


def test_cancelling_the_run_saves_the_partial_record(inst: Instance, store: Store) -> None:
    plugins = Plugins()
    seen: list[RunRecord] = []

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins).run("slow", on_record=seen.append))
        await plugins.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as info:
            await task
        assert type(info.value) is asyncio.CancelledError  # plain, not RunCancelled

    asyncio.run(scenario())
    (record,) = seen
    assert record.status == "failed"
    assert record.steps[0].error is not None
    assert record.steps[0].error.kind == "cancelled"
    assert store.get_run(record.id) == record
    assert store.list_runs(status="running") == []
    assert lock_is_free(inst.state_dir, "slow") is True


def test_asyncio_timeout_around_a_run_raises_timeout_error(inst: Instance, store: Store) -> None:
    seen: list[RunRecord] = []

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await _runner(inst, store).run("slow", on_record=seen.append)

    asyncio.run(scenario())
    (record,) = seen
    assert record.status == "failed"
    assert record.steps[0].error is not None
    assert record.steps[0].error.kind == "cancelled"
    assert store.get_run(record.id) == record


def test_a_cancelled_run_that_ignores_it_is_abandoned(inst: Instance, store: Store) -> None:
    plugins = Stubborn()

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins, stop_grace_s=0.1).run("slow"))
        await plugins.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        plugins.release.set()
        await asyncio.sleep(0.1)

    asyncio.run(scenario())
    (run,) = store.list_runs()
    assert run.abandoned_at is not None
    assert run.error == "cancelled: the run was cancelled and its step did not stop within 0.1 s"


def test_cancelled_before_the_engine_started_gets_a_minimal_record(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def never(*args: object, **kwargs: object) -> RunRecord:
        # Like a task cancelled before its first step: a plain CancelledError, no record.
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(runner_module, "execute", never)
    seen: list[RunRecord] = []

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store).run("slow", on_record=seen.append))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    (record,) = seen
    assert record.status == "failed"
    assert record.error == "the run was cancelled before it started"
    assert [step.status for step in record.steps] == ["skipped", "skipped"]
    assert [step.kind for step in record.steps] == ["collect", "notify"]
    assert store.get_run(record.id) == record


def test_a_cancellation_while_the_heartbeat_stops_is_not_swallowed(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopping = asyncio.Event()

    async def slow_to_stop(self: Runner, state: object, task: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await asyncio.sleep(0.2)  # a heartbeat that takes a while to stop

    monkeypatch.setattr(Runner, "_heartbeat", slow_to_stop)
    seen: list[RunRecord] = []

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store).run("collect", on_record=seen.append))
        await stopping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    (record,) = seen  # the run had finished: its record is saved as is
    assert record.status == "ok"
    assert store.get_run(record.id) == record


def test_a_crash_abandons_the_run(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(run_id: str, index: int, step: object) -> None:
        raise StoreError("disk full: secret-looking detail")

    monkeypatch.setattr(store, "record_step", broken)
    with pytest.raises(StoreError, match="disk full"):
        asyncio.run(_runner(inst, store).run("collect"))
    (run,) = store.list_runs()
    assert run.status == "failed"
    assert run.abandoned_at is not None
    assert run.error == "run crashed (StoreError)"
    assert lock_is_free(inst.state_dir, "collect") is True


def test_a_crash_that_cannot_be_abandoned_is_logged(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise StoreError("disk full: secret-looking detail")

    monkeypatch.setattr(store, "record_step", broken)
    monkeypatch.setattr(store, "abandon_run", broken)
    with (
        caplog.at_level(logging.ERROR, "horae.runner"),
        pytest.raises(StoreError, match="disk full"),
    ):
        asyncio.run(_runner(inst, store).run("collect"))
    assert "could not be abandoned (StoreError)" in caplog.text
    assert "secret-looking" not in caplog.text
    assert len(store.list_runs(status="running")) == 1  # the next run of `collect` recovers it
    assert lock_is_free(inst.state_dir, "collect") is True


def test_a_store_failure_in_the_run_is_a_crash(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise StoreError("synthetic")

    # (A failing LLM cache is only a miss: tests/test_llm_cache.py.)
    monkeypatch.setattr(store, "record_step", broken)
    with pytest.raises(StoreError):
        asyncio.run(_runner(inst, store).run("collect"))
    (run,) = store.list_runs()
    assert run.error == "run crashed (StoreError)"


def test_runner_rejects_bad_settings(inst: Instance, store: Store) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        _runner(inst, store, timeout_s=0)
    with pytest.raises(ValueError, match="heartbeat_s"):
        _runner(inst, store, heartbeat_s=0)
    with pytest.raises(ValueError, match="stop_grace_s"):
        _runner(inst, store, stop_grace_s=0)
