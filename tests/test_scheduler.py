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
from horae.contracts.instance import SchedulerMode
from horae.scheduler import (
    CANCELLED,
    LOCKED,
    MAX_SLEEP_S,
    RETRY_MAX_S,
    RETRY_MIN_S,
    ModeError,
    PingResult,
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
    """A fake run function: records every call; `hold` keeps runs running until released;
    `saved_on_cancel` makes a cancelled run report that status (a record saved) instead of
    re-raising the cancellation."""

    def __init__(self, status: str = "ok") -> None:
        self.calls: list[str] = []
        self.status = status
        self.hold: asyncio.Event | None = None
        self.cancelled: list[str] = []
        self.saved_on_cancel: str | None = None
        self.ended: list[str] = []

    async def __call__(self, name: str) -> RunOutcome:
        self.calls.append(name)
        run_id = f"run-{len(self.calls)}"
        if self.hold is not None:
            try:
                await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                if self.saved_on_cancel is None:
                    raise
                return RunOutcome(self.saved_on_cancel, run_id)
        self.ended.append(name)
        return RunOutcome(self.status, run_id)


class Pings:
    def __init__(self, result: PingResult = "sent") -> None:
        self.sent: list[tuple[str, str, bool]] = []
        self.result: PingResult = result

    async def __call__(self, name: str, outcome: RunOutcome, duration_s: float) -> PingResult:
        self.sent.append((name, outcome.status, outcome.up))
        return self.result


T0 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def _scheduler(
    clock: FakeClock,
    runs: Runs,
    pings: Pings,
    *entries: Schedule,
    mode: SchedulerMode = "builtin",
    max_concurrent: int = 1,
    misfire_grace_s: float = 3600,
) -> Scheduler:
    return Scheduler(
        entries,
        mode=mode,
        run=runs,
        ping=pings,
        clock=clock,
        max_concurrent=max_concurrent,
        misfire_grace_s=misfire_grace_s,
    )


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
        assert scheduler.state().started_at == T0
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
        assert brief.last_ping == "ok"
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
        assert scheduler.state().pipelines[0].last_ping == "ok"  # the ping itself went fine
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
        assert entry.last_skip_reason == "running"
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
        assert entry.last_skip_reason == "locked"
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


def test_stop_cancels_a_run_that_saved_nothing_and_pings_nothing() -> None:
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
        # Nothing was saved: no ping (the monitor's heartbeat window reports it).
        assert pings.sent == []
        entry = scheduler.state().pipelines[0]
        assert (entry.last_outcome, entry.last_ping, entry.running) == (CANCELLED, "skipped", False)

    _drive(body())


@pytest.mark.parametrize(("saved", "up"), [("ok", True), ("failed", False)])
def test_stop_reports_the_status_a_cancelled_run_saved(saved: str, up: bool) -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold, runs.saved_on_cancel = asyncio.Event(), saved
        scheduler = _scheduler(clock, runs, pings, Schedule("brief", "1 5 * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        scheduler.stop()
        await task
        assert pings.sent == [("brief", saved, up)]
        entry = scheduler.state().pipelines[0]
        assert (entry.last_outcome, entry.last_run_id, entry.last_ping) == (saved, "run-1", "ok")

    _drive(body())


def test_force_stop_does_not_wait_for_the_pings() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs = Runs()
        runs.hold, runs.saved_on_cancel = asyncio.Event(), "ok"
        pinging, never = asyncio.Event(), asyncio.Event()

        async def stuck_ping(name: str, outcome: RunOutcome, duration_s: float) -> PingResult:
            pinging.set()
            await never.wait()
            return "sent"

        scheduler = Scheduler(
            [Schedule("brief", "1 5 * * *", "UTC")],
            mode="builtin",
            run=runs,
            ping=stuck_ping,
            clock=clock,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        scheduler.stop()
        await pinging.wait()
        assert not task.done()
        scheduler.force_stop()
        await task
        entry = scheduler.state().pipelines[0]
        assert (entry.last_outcome, entry.last_ping) == ("ok", "skipped")

    _drive(body())


def test_a_crashing_run_or_ping_never_stops_the_scheduler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def crash(name: str) -> RunOutcome:
        raise RuntimeError("synthetic")

    async def bad_ping(name: str, outcome: RunOutcome, duration_s: float) -> PingResult:
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
        assert scheduler.state().pipelines[0].last_ping == "failed"
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
            "started_at": "2026-01-05T05:00:00Z",
            "max_concurrent": 1,
            "pipelines": [
                {
                    "pipeline": "brief",
                    "cron": "30 5 * * *",
                    "timezone": "UTC",
                    "next_fire": "2026-01-05T05:30:00Z",
                    "last_fire": None,
                    "last_outcome": None,
                    "last_run_id": None,
                    "last_ping": None,
                    "running": False,
                    "waiting": False,
                    "skipped": 0,
                    "last_skip": None,
                    "last_skip_reason": None,
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
    assert scheduler_mode("builtin", {"HORAE_SCHEDULER": " External "}) == "external"
    assert scheduler_mode("external", {"HORAE_SCHEDULER": "BUILTIN"}) == "builtin"
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


class _CountingClock(SystemClock):
    """The system clock, counting its reads of the wall clock."""

    def __init__(self, max_sleep_s: float) -> None:
        super().__init__(max_sleep_s)
        self.reads = 0

    def now(self) -> datetime:
        self.reads += 1
        return super().now()


def test_the_system_clock_wakes_when_due_and_re_reads_the_wall_clock() -> None:
    clock = _CountingClock(max_sleep_s=0.01)

    async def body() -> None:
        stop = asyncio.Event()
        due = clock.now() + timedelta(seconds=0.05)
        assert await clock.sleep_until(due, stop)
        assert clock.now() >= due
        # Woken at least every max_sleep_s to re-read the clock: several reads, not one.
        assert clock.reads >= 4
        assert await clock.sleep_until(due, stop)  # already due: at once

    asyncio.run(body())
    assert SystemClock().now().tzinfo is UTC
    assert SystemClock().max_sleep_s == MAX_SLEEP_S
    with pytest.raises(ValueError, match="max_sleep_s"):
        SystemClock(0)


def test_the_system_clock_returns_as_soon_as_stopped() -> None:
    clock = SystemClock()  # MAX_SLEEP_S between reads: only the stop event can wake it

    async def body() -> None:
        stop = asyncio.Event()
        waiting = asyncio.create_task(clock.sleep_until(clock.now() + timedelta(days=1), stop))
        await settle()
        assert not waiting.done()
        stop.set()
        assert await waiting is False
        assert await clock.sleep_until(clock.now() + timedelta(days=1), stop) is False

    asyncio.run(body())


# --- concurrency, misfire grace, clock jumps, failing triggers --------------------------------


def test_scheduled_runs_are_serialised_first_in_first_out() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(
            clock,
            runs,
            pings,
            Schedule("a", "1 5 * * *", "UTC"),
            Schedule("b", "2 5 * * *", "UTC"),
            Schedule("c", "3 5 * * *", "UTC"),
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=3))  # a fires at :01, b at :02, c at :03
        assert runs.calls == ["a"]
        a, b, c = scheduler.state().pipelines
        states = [(p.running, p.waiting) for p in (a, b, c)]
        assert states == [(True, False), (False, True), (False, True)]
        assert b.skipped == 0
        runs.hold.set()
        await settle()
        assert runs.calls == ["a", "b", "c"]  # in fire order, one at a time
        assert len(pings.sent) == 3
        assert not any(p.waiting or p.running for p in scheduler.state().pipelines)
        scheduler.stop()
        await task

    _drive(body())


def test_a_fire_time_waiting_for_a_slot_past_its_grace_is_skipped() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(
            clock,
            runs,
            pings,
            Schedule("a", "1 5 * * *", "UTC"),
            Schedule("b", "2 5 * * *", "UTC"),
            misfire_grace_s=600,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=2))
        assert scheduler.state().pipelines[1].waiting
        await clock.set(T0 + timedelta(minutes=20))  # b has waited 18 min > 10 min
        runs.hold.set()
        await settle()
        assert runs.calls == ["a"]
        b = scheduler.state().pipelines[1]
        assert (b.skipped, b.last_skip_reason, b.waiting) == (1, "misfire", False)
        assert b.last_skip == T0 + timedelta(minutes=2)
        assert b.last_fire is None
        scheduler.stop()
        await task

    _drive(body())


def test_stop_drops_the_fire_times_waiting_for_a_slot() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(
            clock, runs, pings, Schedule("a", "1 5 * * *", "UTC"), Schedule("b", "1 5 * * *", "UTC")
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        assert scheduler.state().pipelines[1].waiting
        scheduler.stop()
        await task
        assert runs.calls == ["a"]
        assert not scheduler.state().pipelines[1].waiting

    _drive(body())


def test_max_concurrent_runs_pipelines_side_by_side() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(
            clock,
            runs,
            pings,
            Schedule("a", "1 5 * * *", "UTC"),
            Schedule("b", "1 5 * * *", "UTC"),
            Schedule("c", "1 5 * * *", "UTC"),
            max_concurrent=2,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        assert sorted(runs.calls) == ["a", "b"]
        assert [p.waiting for p in scheduler.state().pipelines] == [False, False, True]
        runs.hold.set()
        await settle()
        assert sorted(runs.calls) == ["a", "b", "c"]
        scheduler.stop()
        await task

    _drive(body())
    with pytest.raises(ValueError, match="max_concurrent"):
        Scheduler([], mode="builtin", run=Runs(), ping=Pings(), max_concurrent=0)
    with pytest.raises(ValueError, match="misfire_grace_s"):
        Scheduler([], mode="builtin", run=Runs(), ping=Pings(), misfire_grace_s=0)


def test_a_late_wake_past_the_misfire_grace_is_skipped() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        # Suspended for two hours: the 05:05 fire time would start 1 h 55 min late.
        await clock.set(T0 + timedelta(hours=2))
        assert runs.calls == []
        assert pings.sent == []
        entry = scheduler.state().pipelines[0]
        assert (entry.skipped, entry.last_skip_reason) == (1, "misfire")
        assert entry.last_skip == T0 + timedelta(minutes=5)
        assert entry.next_fire == T0 + timedelta(hours=2, minutes=5)
        scheduler.stop()
        await task

    _drive(body())


def test_a_clock_stepped_back_never_fires_a_time_twice() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=5))
        assert runs.calls == ["remind"]
        # The clock steps back 7 minutes (an NTP correction): 05:00 and 05:05 come again.
        await clock.set(T0 - timedelta(minutes=2))
        assert scheduler.state().pipelines[0].next_fire == T0 + timedelta(minutes=10)
        await clock.set(T0 + timedelta(minutes=5))
        assert runs.calls == ["remind"]
        await clock.set(T0 + timedelta(minutes=10))
        assert runs.calls == ["remind", "remind"]
        scheduler.stop()
        await task

    _drive(body())


def test_a_trigger_that_cannot_be_scheduled_never_stops_the_others(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = next_fire
    failures = {"bad": 3}

    def flaky(expression: str, timezone: str, after: datetime) -> datetime:
        if timezone == "Nowhere/Bad" and failures["bad"] > 0:
            failures["bad"] -= 1
            raise OverflowError("synthetic-detail")
        return real(expression, "UTC" if timezone == "Nowhere/Bad" else timezone, after)

    monkeypatch.setattr("horae.scheduler._scheduler.next_fire", flaky)

    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        runs.hold = asyncio.Event()
        scheduler = _scheduler(
            clock,
            runs,
            pings,
            Schedule("bad", "* * * * *", "Nowhere/Bad"),
            Schedule("good", "1 5 * * *", "UTC"),
            max_concurrent=2,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        bad = scheduler.state().pipelines[0]
        assert bad.next_fire is None
        await clock.set(T0 + timedelta(minutes=1))
        assert runs.calls == ["good"]  # the good pipeline fires; its run is in progress
        # The second try (at 05:01) failed too: the next in 2 min.
        assert scheduler.state().pipelines[0].next_fire is None
        await clock.set(T0 + timedelta(seconds=3 * RETRY_MIN_S))  # third fails: wait 4 min
        await clock.set(T0 + timedelta(seconds=7 * RETRY_MIN_S))  # fourth works
        assert scheduler.state().pipelines[0].next_fire == T0 + timedelta(minutes=8)
        assert scheduler.state().pipelines[1].running  # never orphaned
        runs.hold.set()
        await settle()
        assert scheduler.state().pipelines[1].last_outcome == "ok"
        scheduler.stop()
        await task

    with caplog.at_level("ERROR", logger="horae.scheduler"):
        _drive(body())
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [
        "scheduler: cannot schedule bad (OverflowError); retrying in 60 s",
        "scheduler: cannot schedule bad (OverflowError); retrying in 120 s",
        "scheduler: cannot schedule bad (OverflowError); retrying in 240 s",
    ]
    assert "synthetic-detail" not in caplog.text
    assert RETRY_MAX_S == 3600


def test_a_locked_run_keeps_what_happened_meanwhile() -> None:
    async def body() -> None:
        clock = FakeClock(T0)
        runs, pings = Runs(), Pings()
        scheduler = _scheduler(clock, runs, pings, Schedule("remind", "*/5 * * * *", "UTC"))
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=5))  # a first run, ok
        runs.status, runs.hold = LOCKED, asyncio.Event()
        await clock.set(T0 + timedelta(minutes=10))  # starts, then finds the lock held
        await clock.set(T0 + timedelta(minutes=15))  # skipped: the run is in progress
        runs.hold.set()
        await settle()
        entry = scheduler.state().pipelines[0]
        assert entry.skipped == 2  # the skip recorded meanwhile is kept
        assert (entry.last_skip, entry.last_skip_reason) == (T0 + timedelta(minutes=10), "locked")
        assert (entry.last_fire, entry.last_outcome, entry.last_run_id, entry.last_ping) == (
            T0 + timedelta(minutes=5),
            "ok",
            "run-1",
            "ok",
        )
        assert entry.next_fire == T0 + timedelta(minutes=20)
        scheduler.stop()
        await task

    _drive(body())


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


def test_doctor_checks_the_push_url_and_horae_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "http://kuma.example.org/api/push/doctortoken0123"  # noqa: S105 - synthetic
    root = write_instance(
        tmp_path / "i",
        secrets={"refs": {"PUSH_URL": {"env": "HORAE_TEST_DOCTOR_URL"}}},
        scheduler={"monitors": {"collect": {"secret": "PUSH_URL"}}},
    )
    env = {"HORAE_TEST_DOCTOR_URL": secret, "HORAE_SCHEDULER": "cron"}
    problems = [str(p) for p in check_instance(root, environ=env).problems]
    assert any(
        "secret PUSH_URL is not an https:// URL or an http:// URL on the LAN" in p for p in problems
    )
    assert any("HORAE_SCHEDULER='cron': expected one of builtin, external" in p for p in problems)
    assert not any("doctortoken" in p for p in problems)
    good = {
        "HORAE_TEST_DOCTOR_URL": "https://kuma.example.org/api/push/x",
        "HORAE_SCHEDULER": "External",
    }
    assert check_instance(root, environ=good).problems == []
