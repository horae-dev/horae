# SPDX-License-Identifier: Apache-2.0
"""The run lifecycle (#19): lock, heartbeat, statuses, timeouts, cancellation, crashes and
stale-run recovery."""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from horae.config import STORE_FILE, Instance
from horae.contracts import RunId, RunRecord
from horae.engine import RunCancelled
from horae.runner import (
    DEFAULT_STALE_AFTER_S,
    PipelineLocked,
    Ports,
    RunLost,
    Runner,
    UnknownPipeline,
    new_run_id,
    pid_alive,
)
from horae.store import PipelineBusy, Store, StoreError
from runner_fixtures import Plugins, brief_llm, instance, write_instance

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


# --- statuses ------------------------------------------------------------------------------


def test_ok_run_is_saved_and_releases_the_lock(inst: Instance, store: Store) -> None:
    record = asyncio.run(_runner(inst, store).run("collect"))
    assert record.status == "ok"
    assert store.get_run(record.id) == record
    summary = store.run_summary(record.id)
    assert summary is not None
    assert summary.status == "ok"
    assert summary.pid == os.getpid()
    assert store.list_runs(status="running") == []
    assert record.context.timezone == "UTC"  # the instance default
    # The lock is free again: a second run starts.
    assert asyncio.run(_runner(inst, store).run("collect")).status == "ok"


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
            assert isinstance(info.value, PipelineBusy)
            # Another pipeline is not locked.
            assert (await _runner(inst, other).run("collect")).status == "ok"
        plugins.release.set()
        assert (await first).status == "ok"

    asyncio.run(scenario())
    assert len(store.list_runs(pipeline="slow")) == 1  # the refused run wrote nothing


def test_lock_message_without_owner_details() -> None:
    locked = PipelineLocked("p", "run-1")
    assert str(locked) == (
        "pipeline 'p' is already running as run 'run-1'; try again when it has finished"
    )


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


def test_a_killed_process_is_recovered_by_the_next_run(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    ready = tmp_path / "ready"
    first = _child(root, "slow", ready, tmp_path / "never")
    try:
        _wait_for(ready, first)
    finally:
        first.kill()
        first.communicate()  # reaped: its pid no longer exists
    with Store.open(root / "state" / STORE_FILE) as store:
        (dead,) = store.list_runs(status="running")
    second = _child(root, "collect")
    _out, err = second.communicate(timeout=60)
    assert second.returncode == 0, err
    with Store.open(root / "state" / STORE_FILE) as store:
        summary = store.run_summary(dead.id)
        assert summary is not None
        assert summary.status == "failed"
        assert summary.abandoned_at is not None
        assert summary.error == f"abandoned: its process (pid {dead.pid}) is gone"
        # The lock of `slow` is free again.
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


def _begin(store: Store, run_id: str, pipeline: str, at: datetime, pid: int, host: str) -> None:
    store.begin_run(run_id, pipeline, at, pid=pid, host=host)


def test_recover_stale(inst: Instance, store: Store) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(seconds=DEFAULT_STALE_AFTER_S + 5)
    runner = _runner(inst, store, host="here", is_alive=lambda pid: pid != 4242)
    _begin(store, "old-elsewhere", "collect", old, 1, "elsewhere")
    _begin(store, "fresh-elsewhere", "partial", now, 4242, "elsewhere")
    _begin(store, "dead-here", "broken", now, 4242, "here")
    _begin(store, "alive-here", "brief", now, 7, "here")
    recovered = runner.recover_stale()
    assert [run.id for run in recovered] == ["old-elsewhere", "dead-here"]
    errors = {run.id: run.error for run in store.list_runs()}
    assert errors["old-elsewhere"] is not None
    assert errors["old-elsewhere"].startswith("abandoned: no heartbeat since ")
    assert errors["dead-here"] == "abandoned: its process (pid 4242) is gone"
    assert {run.id for run in store.list_runs(status="running")} == {
        "fresh-elsewhere",
        "alive-here",
    }
    # Recovery runs before each run: the stale holder of `collect` no longer blocks it.
    assert asyncio.run(runner.run("collect")).status == "ok"


def test_recover_stale_tolerates_a_run_that_finished_meanwhile(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    _begin(store, "gone", "collect", now - timedelta(hours=1), 1, "elsewhere")
    real = store.abandon_run

    def race(run_id: str, *, at: datetime, error: str) -> None:
        real(run_id, at=at, error="someone else")
        real(run_id, at=at, error=error)  # raises RunNotRunning

    monkeypatch.setattr(store, "abandon_run", race)
    assert _runner(inst, store).recover_stale() == []


def test_pid_alive() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is True  # cannot tell: never a process group
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert pid_alive(proc.pid) is False
    if os.getuid() != 0:
        assert pid_alive(1) is True  # exists, owned by root (EPERM)


def test_a_run_abandoned_while_running_is_stopped(inst: Instance, store: Store) -> None:
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


def test_heartbeat_survives_a_busy_store(
    inst: Instance, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugins = Plugins()
    beats: list[int] = []

    def busy(run_id: str, at: datetime) -> None:
        beats.append(1)
        raise StoreError("busy")

    monkeypatch.setattr(store, "heartbeat", busy)

    async def scenario() -> RunRecord:
        task = asyncio.create_task(_runner(inst, store, plugins, heartbeat_s=0.01).run("slow"))
        await plugins.started.wait()
        await asyncio.sleep(0.1)
        plugins.release.set()
        return await task

    assert asyncio.run(scenario()).status == "ok"
    assert len(beats) >= 2


# --- timeouts, cancellation, crashes -------------------------------------------------------


def test_run_timeout_stops_the_step_and_marks_the_run(inst: Instance, store: Store) -> None:
    record = asyncio.run(_runner(inst, store, timeout_s=0.2).run("slow"))
    assert record.status == "failed"
    assert record.error == "step 0 (collect): the run timed out after 0.2 s"
    collect, notify = record.steps
    assert collect.status == "failed"
    assert collect.error is not None
    assert collect.error.kind == "timeout"
    assert notify.status == "skipped"
    assert store.get_run(record.id) == record
    assert store.list_runs(status="running") == []


def test_run_timeout_defaults_to_horae_yaml(tmp_path: Path) -> None:
    inst = instance(tmp_path, runs={"timeout_s": 1})
    with Store.open(inst.store_path) as store:
        runner = _runner(inst, store)
        assert runner.run_timeout_s == 1
        record = asyncio.run(runner.run("slow"))
    assert record.error == "step 0 (collect): the run timed out after 1 s"
    assert instance(tmp_path).config.runs.timeout_s == 900


def test_cancelling_the_run_saves_the_partial_record(inst: Instance, store: Store) -> None:
    plugins = Plugins()

    async def scenario() -> None:
        task = asyncio.create_task(_runner(inst, store, plugins).run("slow"))
        await plugins.started.wait()
        task.cancel()
        with pytest.raises(RunCancelled) as info:
            await task
        record = info.value.record
        assert record.status == "failed"
        assert record.steps[0].error is not None
        assert record.steps[0].error.kind == "cancelled"
        assert store.get_run(record.id) == record

    asyncio.run(scenario())
    assert store.list_runs(status="running") == []


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


def test_runner_rejects_bad_settings(inst: Instance, store: Store) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        _runner(inst, store, timeout_s=0)
    with pytest.raises(ValueError, match="heartbeat_s"):
        _runner(inst, store, heartbeat_s=0)
    with pytest.raises(ValueError, match="stale_after_s"):
        _runner(inst, store, heartbeat_s=10, stale_after_s=10)
