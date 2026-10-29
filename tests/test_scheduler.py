# SPDX-License-Identifier: Apache-2.0
"""The built-in cron (#39, schemas decision 49): cron in horae.yaml pipelines, time zones and
daylight saving, `HORAE_SCHEDULER`, missed runs, overlap, shutdown and the state for
`/healthz`. Driven by a fake clock: nothing here sleeps."""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.config import InstanceError, load_instance
from horae.config.cron import cron_problem, next_fire
from horae.config.loader import check_instance
from horae.scheduler import (
    LOCKED,
    MAX_SLEEP_S,
    ModeError,
    RunOutcome,
    Schedule,
    Scheduler,
    SystemClock,
    scheduler_mode,
    schedules,
)
from runner_fixtures import write_instance


class FakeClock:
    """A wall clock that moves only when the test says so."""

    def __init__(self, start: datetime) -> None:
        self.time = start
        self._moved = asyncio.Event()
        self.waits = 0

    def now(self) -> datetime:
        return self.time

    async def sleep_until(self, when: datetime, stop: asyncio.Event) -> bool:
        while self.time < when:
            if stop.is_set():
                return False
            self.waits += 1
            moved = asyncio.ensure_future(self._moved.wait())
            stopped = asyncio.ensure_future(stop.wait())
            await asyncio.wait({moved, stopped}, return_when=asyncio.FIRST_COMPLETED)
            moved.cancel()
            stopped.cancel()
        return not stop.is_set()

    async def set(self, when: datetime) -> None:
        self.time = when
        self._moved.set()
        self._moved = asyncio.Event()
        await settle()


async def settle() -> None:
    """Let every ready task run until the loop is idle (the fakes never block)."""
    for _ in range(50):
        await asyncio.sleep(0)


class Runs:
    """A fake run function: records every call; `hold` keeps runs running until released."""

    def __init__(self, status: str = "ok") -> None:
        self.calls: list[str] = []
        self.status = status
        self.hold: asyncio.Event | None = None
        self.cancelled: list[str] = []

    async def __call__(self, name: str) -> RunOutcome:
        self.calls.append(name)
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise
        return RunOutcome(self.status, f"run-{len(self.calls)}")


class Pings:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, bool]] = []

    async def __call__(self, name: str, outcome: RunOutcome, duration_s: float) -> None:
        self.sent.append((name, outcome.status, outcome.up))


T0 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def _scheduler(
    clock: FakeClock,
    runs: Runs,
    pings: Pings,
    *entries: Schedule,
    mode: str = "builtin",
) -> Scheduler:
    return Scheduler(entries, mode=mode, run=runs, ping=pings, clock=clock)


def _drive(body: Coroutine[Any, Any, None]) -> None:
    asyncio.run(body)


# --- cron expressions per pipeline in horae.yaml / pipelines ----------------------------------


def _with_trigger(root: Path, name: str, trigger: dict[str, Any] | None) -> None:
    path = root / "pipelines" / f"{name}.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    if trigger is None:
        data.pop("trigger", None)
    else:
        data["trigger"] = trigger
    path.write_text(yaml.safe_dump(data), "utf-8")


def test_triggers_are_read_per_pipeline_with_their_time_zone(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "i")
    _with_trigger(root, "collect", {"cron": "30 5 * * *"})
    _with_trigger(root, "slow", {"cron": "*/5 * * * *", "timezone": "America/New_York"})
    found = schedules(load_instance(root))
    assert found == [
        Schedule("collect", "30 5 * * *", "Europe/Paris"),  # the instance's time zone
        Schedule("slow", "*/5 * * * *", "America/New_York"),
    ]


def test_an_invalid_cron_expression_is_a_config_problem(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "i")
    _with_trigger(root, "collect", {"cron": "0 0 30 2 *"})
    with pytest.raises(InstanceError) as raised:
        load_instance(root)
    [problem] = raised.value.problems
    assert "invalid cron expression '0 0 30 2 *'" in problem.message
    assert problem.file.endswith("collect.yaml")


def test_cron_problem_and_names() -> None:
    assert cron_problem("30 5 * * MON-FRI") is None
    assert cron_problem("0 0 L * *") is None
    assert cron_problem("61 * * * *") == "invalid cron expression '61 * * * *' (bad minute)"
    with pytest.raises(ValueError, match="aware"):
        next_fire("* * * * *", "UTC", datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="minute"):
        next_fire("61 * * * *", "UTC", T0)


def test_next_fire_is_strictly_after_and_in_the_trigger_time_zone() -> None:
    at = datetime(2026, 1, 5, 4, 30, tzinfo=UTC)  # 05:30 in Paris (CET, +01:00)
    assert next_fire("30 5 * * *", "Europe/Paris", at) == datetime(2026, 1, 6, 4, 30, tzinfo=UTC)
    before = at - timedelta(seconds=1)
    assert next_fire("30 5 * * *", "Europe/Paris", before) == at
    assert next_fire("30 5 * * *", "UTC", at) == datetime(2026, 1, 5, 5, 30, tzinfo=UTC)


def _fires(expression: str, zone: str, start: datetime, count: int) -> list[str]:
    out, at = [], start
    for _ in range(count):
        at = next_fire(expression, zone, at)
        out.append(at.isoformat())
    return out


def test_daylight_saving_spring_forward() -> None:
    # Europe/Paris, 29 March 2026: 02:00 CET becomes 03:00 CEST.
    start = datetime(2026, 3, 28, 12, tzinfo=UTC)
    # A time inside the gap fires once, at the first instant after it (03:00 CEST).
    assert _fires("30 2 * * *", "Europe/Paris", start, 2) == [
        "2026-03-29T01:00:00+00:00",
        "2026-03-30T00:30:00+00:00",
    ]
    # A daily time outside the gap keeps its local time (05:30 CET, then 05:30 CEST).
    assert _fires("30 5 * * *", "Europe/Paris", start, 1) == ["2026-03-29T03:30:00+00:00"]
    # An hourly job loses the skipped hour.
    hourly = datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    assert _fires("0 * * * *", "Europe/Paris", hourly, 3) == [
        "2026-03-29T00:00:00+00:00",  # 01:00 CET
        "2026-03-29T01:00:00+00:00",  # 03:00 CEST
        "2026-03-29T02:00:00+00:00",  # 04:00 CEST
    ]


def test_daylight_saving_fall_back() -> None:
    # Europe/Paris, 25 October 2026: 03:00 CEST becomes 02:00 CET, 02:xx happens twice.
    start = datetime(2026, 10, 24, 12, tzinfo=UTC)
    # A fixed time in the repeated hour fires once, at its first occurrence (CEST).
    assert _fires("30 2 * * *", "Europe/Paris", start, 2) == [
        "2026-10-25T00:30:00+00:00",
        "2026-10-26T01:30:00+00:00",
    ]
    # A job whose hour is `*` fires at every real occurrence: the repeated hour twice.
    early = datetime(2026, 10, 24, 23, 50, tzinfo=UTC)  # 01:50 CEST
    assert _fires("*/30 * * * *", "Europe/Paris", early, 4) == [
        "2026-10-25T00:00:00+00:00",  # 02:00 CEST
        "2026-10-25T00:30:00+00:00",  # 02:30 CEST
        "2026-10-25T01:00:00+00:00",  # 02:00 CET
        "2026-10-25T01:30:00+00:00",  # 02:30 CET
    ]


def test_a_scheduled_pipeline_fires_across_a_dst_change() -> None:
    async def body() -> None:
        clock = FakeClock(datetime(2026, 3, 28, 12, tzinfo=UTC))
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "30 2 * * *", "Europe/Paris"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        [entry] = scheduler.state().pipelines
        assert entry.next_fire == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
        await clock.set(datetime(2026, 3, 29, 1, 0, tzinfo=UTC))
        assert runs.calls == ["brief"]
        scheduler.stop()
        await task

    _drive(body())


# --- the scheduler: firing, missed runs, overlap, shutdown, state -----------------------------


def test_triggers_fire_at_their_times_and_ping_each_run() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(
            clock,
            runs,
            pings,
            Schedule("brief", "30 5 * * *", "UTC"),
            Schedule("remind", "*/5 * * * *", "UTC"),
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        assert runs.calls == []
        await clock.set(T0 + timedelta(minutes=5))
        assert runs.calls == ["remind"]
        await clock.set(T0 + timedelta(minutes=30))
        assert sorted(runs.calls) == ["brief", "remind", "remind"]
        assert ("brief", "ok", True) in pings.sent
        assert len(pings.sent) == 3
        state = scheduler.state()
        assert state.active
        brief = state.pipelines[0]
        assert brief.pipeline == "brief"
        assert brief.last_fire == T0 + timedelta(minutes=30)
        assert brief.last_outcome == "ok"
        assert brief.last_run_id is not None
        assert brief.next_fire == T0 + timedelta(days=1, minutes=30)
        scheduler.stop()
        await task
        assert not scheduler.state().active
        assert scheduler.state().pipelines[0].next_fire is None

    _drive(body())


def test_a_failed_run_pings_down() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs("failed"), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "1 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        assert pings.sent == [("brief", "failed", False)]
        scheduler.stop()
        await task

    _drive(body())


def test_missed_fire_times_are_not_caught_up_on_start() -> None:
    async def body() -> None:
        # Horae was down at 05:30 and starts at 06:00: the 05:30 run is skipped.
        clock = FakeClock(T0 + timedelta(hours=1))
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "30 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        assert runs.calls == []
        assert scheduler.state().pipelines[0].next_fire == T0 + timedelta(days=1, minutes=30)
        scheduler.stop()
        await task

    _drive(body())


def test_a_late_wake_runs_once_not_once_per_missed_time() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        # The host was suspended for an hour: twelve fire times passed.
        await clock.set(T0 + timedelta(hours=1, seconds=10))
        assert runs.calls == ["remind"]
        assert scheduler.state().pipelines[0].last_fire == T0 + timedelta(minutes=5)
        assert scheduler.state().pipelines[0].next_fire == T0 + timedelta(hours=1, minutes=5)
        scheduler.stop()
        await task

    _drive(body())


def test_a_fire_time_while_the_previous_run_runs_is_skipped_and_recorded() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=5))
        assert scheduler.state().pipelines[0].running
        assert scheduler.state().pipelines[0].last_outcome is None
        await clock.set(T0 + timedelta(minutes=10))
        entry = scheduler.state().pipelines[0]
        assert runs.calls == ["remind"]
        assert entry.skipped == 1
        assert entry.last_skip == T0 + timedelta(minutes=10)
        runs.hold.set()
        await settle()
        assert scheduler.state().pipelines[0].running is False
        assert pings.sent == [("remind", "ok", True)]
        await clock.set(T0 + timedelta(minutes=15))
        assert runs.calls == ["remind", "remind"]
        scheduler.stop()
        await task

    _drive(body())


def test_a_run_locked_by_another_process_is_skipped_without_a_ping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(LOCKED), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "1 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        entry = scheduler.state().pipelines[0]
        assert entry.skipped == 1
        assert entry.last_fire is None
        assert entry.last_outcome is None
        assert not entry.running
        assert pings.sent == []
        scheduler.stop()
        await task

    with caplog.at_level("WARNING", logger="horae.scheduler"):
        _drive(body())
    assert "skipped brief at 2026-01-05T05:01:00Z: the pipeline is running in another process" in (
        caplog.text
    )


def test_stop_cancels_runs_in_progress_and_pings_them_down() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "1 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        scheduler.stop()
        scheduler.stop()  # idempotent
        await task
        assert runs.cancelled == ["brief"]
        assert pings.sent == [("brief", "cancelled", False)]
        assert scheduler.state().pipelines[0].last_outcome == "cancelled"

    _drive(body())


def test_a_crashing_run_or_ping_never_stops_the_scheduler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def crash(name: str) -> RunOutcome:
        raise RuntimeError("synthetic")

    async def bad_ping(name: str, outcome: RunOutcome, duration_s: float) -> None:
        raise RuntimeError("synthetic")

    async def body() -> None:
        clock = FakeClock(T0)
        scheduler = Scheduler(
            [Schedule("brief", "* * * * *", "UTC")],
            mode="builtin",
            run=crash,
            ping=bad_ping,
            clock=clock,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        assert scheduler.state().pipelines[0].last_outcome == "internal"
        await clock.set(T0 + timedelta(minutes=2))
        assert scheduler.state().pipelines[0].last_fire == T0 + timedelta(minutes=2)
        scheduler.stop()
        await task

    with caplog.at_level("WARNING", logger="horae.scheduler"):
        _drive(body())
    assert "run of brief crashed (RuntimeError)" in caplog.text
    assert "ping of brief failed (RuntimeError)" in caplog.text
    assert "synthetic" not in caplog.text


def test_state_to_dict_is_json_ready() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        scheduler = _scheduler(clock, Runs(), Pings(), Schedule("brief", "30 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        assert scheduler.state().to_dict() == {
            "mode": "builtin",
            "active": True,
            "pipelines": [
                {
                    "pipeline": "brief",
                    "cron": "30 5 * * *",
                    "timezone": "UTC",
                    "next_fire": "2026-01-05T05:30:00Z",
                    "last_fire": None,
                    "last_outcome": None,
                    "last_run_id": None,
                    "running": False,
                    "skipped": 0,
                    "last_skip": None,
                }
            ],
        }
        scheduler.stop()
        await task

    _drive(body())


# --- HORAE_SCHEDULER=external disables the internal cron --------------------------------------


def test_horae_scheduler_overrides_the_configured_mode() -> None:
    assert scheduler_mode("builtin", {}) == "builtin"
    assert scheduler_mode("external", {}) == "external"
    assert scheduler_mode("builtin", {"HORAE_SCHEDULER": "external"}) == "external"
    assert scheduler_mode("external", {"HORAE_SCHEDULER": "builtin"}) == "builtin"
    assert scheduler_mode("external", {"HORAE_SCHEDULER": " "}) == "external"
    with pytest.raises(ModeError, match="HORAE_SCHEDULER='cron': expected one of builtin"):
        scheduler_mode("builtin", {"HORAE_SCHEDULER": "cron"})


def test_external_mode_fires_nothing() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(
            clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"), mode="external"
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(hours=2))
        assert runs.calls == []
        assert pings.sent == []
        state = scheduler.state()
        assert state.mode == "external"
        assert state.active
        assert state.pipelines[0].next_fire is None
        assert clock.waits == 0
        scheduler.stop()
        await task

    _drive(body())


# --- the system clock -------------------------------------------------------------------------


def test_the_system_clock_returns_when_due_or_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_wait_for(awaitable: Any, timeout: float) -> None:
        waits.append(timeout)
        awaitable.close()
        clock_now[0] += timedelta(seconds=timeout)
        raise TimeoutError

    clock_now = [T0]
    clock = SystemClock()
    monkeypatch.setattr(clock, "now", lambda: clock_now[0])
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    async def body() -> None:
        stop = asyncio.Event()
        assert await clock.sleep_until(T0 + timedelta(seconds=70), stop)
        stop.set()
        assert not await clock.sleep_until(T0 + timedelta(days=1), stop)

    asyncio.run(body())
    # The wall clock is re-read at least every MAX_SLEEP_S.
    assert waits == [MAX_SLEEP_S, MAX_SLEEP_S, 10.0]
    assert isinstance(SystemClock().now(), datetime)
    assert SystemClock().now().tzinfo is UTC


def test_doctor_checks_a_monitor_secret_and_unknown_pipelines(tmp_path: Path) -> None:
    root = write_instance(
        tmp_path / "i",
        scheduler={
            "monitors": {"collect": {"secret": "PUSH_URL"}, "nope": {"secret": "OTHER_URL"}}
        },
    )
    problems = [str(p) for p in check_instance(root, environ={}).problems]
    assert any("push monitor for unknown pipeline 'nope'" in p for p in problems)
    assert any("secret PUSH_URL" in p for p in problems)
