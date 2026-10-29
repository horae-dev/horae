# SPDX-License-Identifier: Apache-2.0
"""The built-in cron (#39, schemas decision 49): one loop per pipeline with a `trigger`.

- **Mode.** `scheduler.mode` in `horae.yaml` (`builtin` by default), overridden by the
  `HORAE_SCHEDULER` environment variable (`builtin` or `external`; any other value is a
  configuration error). In `external` mode no trigger fires: an external scheduler (Coolify
  Scheduled Tasks, cron, a systemd timer) calls `horae run <pipeline>`.
- **Time.** A trigger is evaluated in its `timezone`, else the instance's (`horae.config.cron`
  for the daylight-saving rules); times are kept and reported in UTC.
- **Missed runs are skipped.** On start, each pipeline waits for its next fire time after
  *now*: a fire time that passed while Horae was stopped is not caught up (its push monitor
  reports the missed run). A loop that wakes late (a suspended host, a busy event loop) runs
  the fire time it waited for once, then waits for the first fire time after *now*: several
  missed fire times never become several runs.
- **No overlap.** A fire time while the pipeline's previous scheduled run is still running is
  skipped and recorded (`skipped`, `last_skip`); so is one whose run finds the pipeline lock
  held by another process (a manual `horae run`: the runner's `PipelineLocked`), since the run
  takes the same per-pipeline `flock` as any run. A skipped fire time pings no monitor (the
  run in progress pings its own).
- **Shutdown.** `stop()` (SIGTERM or SIGINT in `horae serve`) stops every loop at once and
  cancels the runs in progress; a cancelled run stops like `horae run` on SIGTERM (its partial
  record saved `failed`, an apply in progress finished first, bounded by the runner's grace
  period) and its monitor is pinged `down` (`cancelled`). `run_forever` returns when every run
  has stopped.
- **State.** `state()` is a snapshot (`SchedulerState`) for `/healthz` (#37): the mode and,
  per pipeline, the next fire time, the last fire time and its outcome, whether a run is in
  progress and the skipped fire times.

The clock (`Clock`) and the run and ping functions are injected, so tests drive the scheduler
with a fake clock and never sleep.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from horae.config import Instance
from horae.config.cron import next_fire
from horae.contracts.instance import SchedulerMode
from horae.scheduler._monitor import RunOutcome

logger = logging.getLogger("horae.scheduler")

SCHEDULER_ENV = "HORAE_SCHEDULER"
"""Overrides `scheduler.mode` when set (`builtin` or `external`)."""
MODES: tuple[SchedulerMode, ...] = ("builtin", "external")
MAX_SLEEP_S = 30.0
"""The system clock re-reads the wall clock at least this often while it waits, so that a
clock step or a suspended host delays a fire time by at most this much."""
LOCKED = "locked"
"""The `RunOutcome.status` of a run that found its pipeline lock held (skipped)."""


class ModeError(ValueError):
    """`HORAE_SCHEDULER` holds something other than `builtin` or `external`."""


def scheduler_mode(configured: SchedulerMode, environ: Mapping[str, str] | None = None) -> str:
    """The effective mode: `HORAE_SCHEDULER` when set and not empty, else `configured`."""
    value = (os.environ if environ is None else environ).get(SCHEDULER_ENV, "").strip()
    if not value:
        return configured
    if value not in MODES:
        shown = value[:16].encode("ascii", "replace").decode("ascii")
        raise ModeError(f"{SCHEDULER_ENV}={shown!r}: expected one of {', '.join(MODES)}")
    return value


@dataclass(frozen=True)
class Schedule:
    """One pipeline's trigger."""

    pipeline: str
    cron: str
    timezone: str


def schedules(instance: Instance) -> list[Schedule]:
    """The triggers of `instance`'s pipelines, by pipeline name (the time zone filled in)."""
    return [
        Schedule(name, p.trigger.cron, p.trigger.timezone or instance.config.timezone)
        for name, p in sorted(instance.pipelines.items())
        if p.trigger is not None
    ]


class Clock(Protocol):
    """The wall clock the scheduler reads and waits on."""

    def now(self) -> datetime:
        """The current time, an aware UTC datetime."""
        ...

    async def sleep_until(self, when: datetime, stop: asyncio.Event) -> bool:
        """Wait until `when`; `False` as soon as `stop` is set (then `when` may not be due)."""
        ...


class SystemClock:
    """The real wall clock (`datetime.now(UTC)`), re-read at least every `MAX_SLEEP_S`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep_until(self, when: datetime, stop: asyncio.Event) -> bool:
        while not stop.is_set():
            remaining = (when - self.now()).total_seconds()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(remaining, MAX_SLEEP_S))
            except TimeoutError:
                continue
        return False


RunFunction = Callable[[str], Coroutine[Any, Any, RunOutcome]]
"""Runs one pipeline (by name) and says how it ended; `LOCKED` when its lock was held."""
PingFunction = Callable[[str, RunOutcome, float], Awaitable[Any]]
"""Pings a pipeline's push monitor with an outcome and the run's duration in seconds."""


@dataclass(frozen=True)
class PipelineState:
    """What the scheduler knows about one pipeline (times in UTC)."""

    pipeline: str
    cron: str
    timezone: str
    next_fire: datetime | None = None
    """The fire time it waits for (`None` when stopped or in `external` mode)."""
    last_fire: datetime | None = None
    """The last fire time that started a run (a fire time skipped is not one)."""
    last_outcome: str | None = None
    """How that run ended (`RunOutcome.status`); `None` while it runs."""
    last_run_id: str | None = None
    running: bool = False
    skipped: int = 0
    """Fire times skipped because the pipeline was running (here or in another process)."""
    last_skip: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "cron": self.cron,
            "timezone": self.timezone,
            "next_fire": _iso(self.next_fire),
            "last_fire": _iso(self.last_fire),
            "last_outcome": self.last_outcome,
            "last_run_id": self.last_run_id,
            "running": self.running,
            "skipped": self.skipped,
            "last_skip": _iso(self.last_skip),
        }


@dataclass(frozen=True)
class SchedulerState:
    """A snapshot of the scheduler, for `/healthz` (#37) and `horae serve`."""

    mode: str
    """`builtin` (triggers fire) or `external` (they do not)."""
    active: bool
    """Whether the scheduler is running (started and not stopped)."""
    pipelines: tuple[PipelineState, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active": self.active,
            "pipelines": [p.to_dict() for p in self.pipelines],
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _Slot:
    schedule: Schedule
    state: PipelineState
    run: "asyncio.Task[RunOutcome] | None" = None
    """The scheduled run in progress, if any."""
    reports: "set[asyncio.Task[None]]" = field(default_factory=set)


class Scheduler:
    """Fires `schedules` with `run` and pings with `ping` (see the module docstring)."""

    def __init__(
        self,
        schedules: Sequence[Schedule],
        *,
        mode: str,
        run: RunFunction,
        ping: PingFunction,
        clock: Clock | None = None,
    ) -> None:
        self.mode = mode
        self._run = run
        self._ping = ping
        self._clock: Clock = clock or SystemClock()
        self._slots = {
            s.pipeline: _Slot(s, PipelineState(s.pipeline, s.cron, s.timezone)) for s in schedules
        }
        self._stop = asyncio.Event()
        self._active = False

    def state(self) -> SchedulerState:
        """A snapshot of the scheduler's state."""
        return SchedulerState(
            self.mode, self._active, tuple(slot.state for _, slot in sorted(self._slots.items()))
        )

    def stop(self) -> None:
        """Stop firing and cancel the runs in progress (idempotent; `run_forever` returns
        once they have stopped)."""
        self._stop.set()
        for slot in self._slots.values():
            if slot.run is not None and not slot.run.done():
                slot.run.cancel()

    async def run_forever(self) -> None:
        """Run until `stop()`. In `external` mode nothing fires: it only waits."""
        self._active = True
        try:
            if self.mode != "builtin" or not self._slots:
                await self._stop.wait()
                return
            loops = [asyncio.create_task(self._loop(slot)) for slot in self._slots.values()]
            try:
                await asyncio.gather(*loops)
            finally:
                for loop in loops:
                    loop.cancel()
                reports = [task for slot in self._slots.values() for task in slot.reports]
                if reports:
                    await asyncio.gather(*reports, return_exceptions=True)
        finally:
            self._active = False
            for slot in self._slots.values():
                slot.state = replace(slot.state, next_fire=None)

    async def _loop(self, slot: _Slot) -> None:
        schedule = slot.schedule
        cursor = self._clock.now()
        while not self._stop.is_set():
            fire = next_fire(schedule.cron, schedule.timezone, cursor)
            slot.state = replace(slot.state, next_fire=fire)
            if not await self._clock.sleep_until(fire, self._stop):
                return
            self._fire(slot, fire)
            # Late or not, the next fire time is after both the one just fired and now.
            cursor = max(fire, self._clock.now())

    def _fire(self, slot: _Slot, fire: datetime) -> None:
        name = slot.schedule.pipeline
        if slot.run is not None and not slot.run.done():
            self._skipped(slot, fire, "its previous scheduled run is still running")
            return
        slot.run = asyncio.create_task(self._run(name))
        report = asyncio.create_task(self._report(slot, fire, slot.run))
        slot.reports.add(report)
        report.add_done_callback(slot.reports.discard)

    def _skipped(self, slot: _Slot, fire: datetime, why: str) -> None:
        slot.state = replace(slot.state, skipped=slot.state.skipped + 1, last_skip=fire)
        logger.warning("scheduler: skipped %s at %s: %s", slot.schedule.pipeline, _iso(fire), why)

    async def _report(self, slot: _Slot, fire: datetime, run: "asyncio.Task[RunOutcome]") -> None:
        """Wait for `run` (never cancelled itself: `stop()` cancels `run`), record its outcome
        and ping its monitor."""
        name = slot.schedule.pipeline
        previous = slot.state
        slot.state = replace(
            slot.state, running=True, last_fire=fire, last_outcome=None, last_run_id=None
        )
        started = asyncio.get_running_loop().time()
        await asyncio.wait([run])
        duration = asyncio.get_running_loop().time() - started
        if run.cancelled():
            outcome = RunOutcome("cancelled")
        elif (error := run.exception()) is not None:
            outcome = RunOutcome("internal")
            logger.error("scheduler: run of %s crashed (%s)", name, type(error).__name__)
            del error
        else:
            outcome = run.result()
        if outcome.status == LOCKED:
            slot.state = replace(previous, running=False)
            self._skipped(slot, fire, "the pipeline is running in another process")
            return
        slot.state = replace(
            slot.state,
            running=False,
            last_outcome=outcome.status,
            last_run_id=outcome.run_id,
        )
        logger.info(
            "scheduler: run %s of %s (fired at %s): %s",
            outcome.run_id or "-",
            name,
            _iso(fire),
            outcome.status,
        )
        try:
            await self._ping(name, outcome, duration)
        except Exception as exc:  # the ping function should not raise; never stop for it
            logger.warning("scheduler: ping of %s failed (%s)", name, type(exc).__name__)
            del exc


__all__ = [
    "LOCKED",
    "MAX_SLEEP_S",
    "MODES",
    "SCHEDULER_ENV",
    "Clock",
    "ModeError",
    "PingFunction",
    "PipelineState",
    "RunFunction",
    "Schedule",
    "Scheduler",
    "SchedulerState",
    "SystemClock",
    "scheduler_mode",
    "schedules",
]
