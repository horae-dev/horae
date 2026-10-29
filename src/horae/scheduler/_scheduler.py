# SPDX-License-Identifier: Apache-2.0
"""The built-in cron (#39, schemas decision 49): one loop per pipeline with a `trigger`.

- **Mode.** `scheduler.mode` in `horae.yaml` (`builtin` by default), overridden by the
  `HORAE_SCHEDULER` environment variable (`builtin` or `external`, any case; any other value
  is a configuration error: `horae.config.schedule`). In `external` mode no trigger fires: an
  external scheduler (Coolify Scheduled Tasks, cron, a systemd timer) calls `horae run`.
- **Time.** A trigger is evaluated in its `timezone`, else the instance's (`horae.config.cron`
  for the daylight-saving rules); times are kept and reported in UTC.
- **Missed runs are skipped.** On start, each pipeline waits for its next fire time after
  *now*: a fire time that passed while Horae was stopped is not caught up (its push monitor
  reports the missed run). A loop that wakes late (a suspended host, a busy event loop) runs
  the fire time it waited for once, then waits for the first fire time after *now*: several
  missed fire times never become several runs. A fire time that would start more than
  `misfire_grace_s` late (after a suspend, or after waiting for a slot) is skipped and
  recorded (`misfire`) instead.
- **Concurrency.** At most `max_concurrent` scheduled runs are in progress at once (default
  1: scheduled runs are serialised). A fire time that finds no free slot waits, first in
  first out, and starts when a run ends (unless it is then past its misfire grace). Whatever
  `max_concurrent` is, two applies never overlap: every run takes the instance's apply lock
  for its apply phase and waits for it at most `APPLY_LOCK_WAIT_S` (30 s) before it fails
  `apply_locked`; serialising scheduled runs keeps them from failing on each other's applies.
- **No overlap.** A fire time while the pipeline's previous scheduled run is still running
  or waiting for a slot is skipped and recorded (`running`); so is one whose run finds the
  pipeline lock held by another process (`locked`: a manual `horae run`, the runner's
  `PipelineLocked`), since the run takes the same per-pipeline `flock` as any run. A skipped
  fire time pings no monitor (the run in progress pings its own).
- **Isolation.** A pipeline whose next fire time cannot be computed (an unknown time zone, a
  date out of range) logs an error, shows no `next_fire` and retries after `RETRY_MIN_S`,
  doubling up to `RETRY_MAX_S`: the other pipelines, and the runs in progress, carry on.
- **Shutdown.** `stop()` (the first SIGTERM or SIGINT in `horae serve`) stops every loop,
  drops the fire times waiting for a slot and cancels the runs in progress; `run_forever`
  returns once those runs have stopped and been reported. How a cancelled run is reported is
  the run function's answer: its saved status (an apply in progress finishes first and its
  record is saved with its real status) pinged accordingly, or `cancelled` when nothing was
  saved, which pings nothing (the monitor's own heartbeat window reports it).
  `force_stop()` (a second signal) also stops waiting for the reports and pings.
- **State.** `state()` is a snapshot (`SchedulerState`) for `/healthz` (#37): the mode, when
  the scheduler started and, per pipeline, the next fire time, the last fire time, its
  outcome and ping result, whether a run is in progress or waiting, and the skipped fire
  times. It is held in memory only: a restart starts from scratch (a follow-up).

The clock (`Clock`) and the run and ping functions are injected, so tests drive the scheduler
with a fake clock and never sleep.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from horae.config import Instance
from horae.config.cron import next_fire
from horae.config.schedule import MODES, SCHEDULER_ENV, ModeError, scheduler_mode
from horae.contracts.instance import DEFAULT_MISFIRE_GRACE_S, SchedulerMode
from horae.scheduler._monitor import PingResult, RunOutcome

logger = logging.getLogger("horae.scheduler")

MAX_SLEEP_S = 30.0
"""The system clock re-reads the wall clock at least this often while it waits, so that a
clock step or a suspended host delays a fire time by at most this much."""
RETRY_MIN_S = 60.0
"""Seconds before a pipeline whose next fire time could not be computed tries again."""
RETRY_MAX_S = 3600.0
"""The longest wait between two such tries (the wait doubles from `RETRY_MIN_S`)."""
LOCKED = "locked"
"""The `RunOutcome.status` of a run that found its pipeline lock held (skipped)."""
CANCELLED = "cancelled"
"""The `RunOutcome.status` of a run stopped before its record was saved (no ping)."""

LastPing = Literal["ok", "failed", "skipped", "none"]
"""How a run's push-monitor ping went: `ok` (the monitor answered 2xx), `failed` (see the
warning logged), `skipped` (not sent: the run was stopped before its record was saved, or a
forced stop), `none` (the pipeline has no monitor)."""
SkipReason = Literal["running", "locked", "misfire"]
"""Why a fire time was skipped: the pipeline's scheduled run was still running or waiting
(`running`), another process held its lock (`locked`), or it would have started more than
`misfire_grace_s` late (`misfire`)."""
_PING_WORDS: dict[str, LastPing] = {"sent": "ok", "failed": "failed", "none": "none"}


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
    """The real wall clock (`datetime.now(UTC)`), re-read at least every `max_sleep_s`."""

    def __init__(self, max_sleep_s: float = MAX_SLEEP_S) -> None:
        if not max_sleep_s > 0:
            raise ValueError("max_sleep_s must be > 0")
        self.max_sleep_s = max_sleep_s

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep_until(self, when: datetime, stop: asyncio.Event) -> bool:
        while not stop.is_set():
            remaining = (when - self.now()).total_seconds()
            if remaining <= 0:
                return True
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(remaining, self.max_sleep_s))
            except TimeoutError:
                continue
        return False


RunFunction = Callable[[str], Coroutine[Any, Any, RunOutcome]]
"""Runs one pipeline (by name) and says how it ended: `LOCKED` when its lock was held, and,
when cancelled, the status of the record it saved (else `CANCELLED`)."""
PingFunction = Callable[[str, RunOutcome, float], Awaitable[PingResult]]
"""Pings a pipeline's push monitor with an outcome and the run's duration in seconds."""


@dataclass(frozen=True)
class PipelineState:
    """What the scheduler knows about one pipeline (times in UTC)."""

    pipeline: str
    cron: str
    timezone: str
    next_fire: datetime | None = None
    """The fire time it waits for (`None` when stopped, in `external` mode, or while its next
    fire time cannot be computed)."""
    last_fire: datetime | None = None
    """The fire time of the last run started (a fire time skipped starts none)."""
    last_outcome: str | None = None
    """How that run ended (`RunOutcome.status`); `None` while it runs."""
    last_run_id: str | None = None
    last_ping: LastPing | None = None
    """How that run's ping went; `None` until it is sent."""
    running: bool = False
    waiting: bool = False
    """Whether a fire time waits for a free slot (`max_concurrent`)."""
    skipped: int = 0
    """Fire times skipped and recorded (see `SkipReason`)."""
    last_skip: datetime | None = None
    last_skip_reason: SkipReason | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "cron": self.cron,
            "timezone": self.timezone,
            "next_fire": _iso(self.next_fire),
            "last_fire": _iso(self.last_fire),
            "last_outcome": self.last_outcome,
            "last_run_id": self.last_run_id,
            "last_ping": self.last_ping,
            "running": self.running,
            "waiting": self.waiting,
            "skipped": self.skipped,
            "last_skip": _iso(self.last_skip),
            "last_skip_reason": self.last_skip_reason,
        }


@dataclass(frozen=True)
class SchedulerState:
    """A snapshot of the scheduler, for `/healthz` (#37) and `horae serve`."""

    mode: SchedulerMode
    """`builtin` (triggers fire) or `external` (they do not)."""
    active: bool
    """Whether the scheduler is running (started and not stopped)."""
    started_at: datetime | None = None
    """When `run_forever` started (`None` before)."""
    max_concurrent: int = 1
    pipelines: tuple[PipelineState, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active": self.active,
            "started_at": _iso(self.started_at),
            "max_concurrent": self.max_concurrent,
            "pipelines": [p.to_dict() for p in self.pipelines],
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class _Slot:
    """One pipeline: its trigger, its state, and its scheduled run."""

    schedule: Schedule
    state: PipelineState
    run: "asyncio.Task[RunOutcome] | None" = None
    """The pipeline's last scheduled run (done once it has ended)."""
    reports: "set[asyncio.Task[None]]" = field(default_factory=set)
    waiting: bool = False

    @property
    def busy(self) -> bool:
        return self.waiting or (self.run is not None and not self.run.done())


class Scheduler:
    """Fires `schedules` with `run` and pings with `ping` (see the module docstring)."""

    def __init__(
        self,
        schedules: Sequence[Schedule],
        *,
        mode: SchedulerMode,
        run: RunFunction,
        ping: PingFunction,
        clock: Clock | None = None,
        max_concurrent: int = 1,
        misfire_grace_s: float = DEFAULT_MISFIRE_GRACE_S,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if not misfire_grace_s > 0:
            raise ValueError("misfire_grace_s must be > 0")
        self.mode: SchedulerMode = mode
        self.max_concurrent = max_concurrent
        self.misfire_grace = timedelta(seconds=misfire_grace_s)
        self._run = run
        self._ping = ping
        self._clock: Clock = clock or SystemClock()
        self._slots = {
            s.pipeline: _Slot(s, PipelineState(s.pipeline, s.cron, s.timezone)) for s in schedules
        }
        self._queue: deque[tuple[_Slot, datetime]] = deque()
        self._in_progress = 0
        self._stop = asyncio.Event()
        self._active = False
        self._started_at: datetime | None = None

    def state(self) -> SchedulerState:
        """A snapshot of the scheduler's state."""
        return SchedulerState(
            self.mode,
            self._active,
            self._started_at,
            self.max_concurrent,
            tuple(slot.state for _, slot in sorted(self._slots.items())),
        )

    def stop(self) -> None:
        """Stop firing, drop the fire times waiting for a slot and cancel the runs in progress
        (idempotent; `run_forever` returns once they have stopped and been reported)."""
        self._stop.set()
        while self._queue:
            slot, fire = self._queue.popleft()
            slot.waiting = False
            slot.state = replace(slot.state, waiting=False)
            logger.info("scheduler: %s at %s dropped: stopping", slot.schedule.pipeline, _iso(fire))
        for slot in self._slots.values():
            if slot.run is not None and not slot.run.done():
                slot.run.cancel()

    def force_stop(self) -> None:
        """`stop()`, and stop waiting for the reports (their pings are not sent). The runs
        themselves are still awaited by `run_forever`, so that their records are saved."""
        self.stop()
        for slot in self._slots.values():
            for report in list(slot.reports):
                report.cancel()

    async def run_forever(self) -> None:
        """Run until `stop()`. In `external` mode nothing fires: it only waits."""
        self._active = True
        self._started_at = self._clock.now()
        loops: list[asyncio.Task[None]] = []
        try:
            if self.mode == "builtin":
                loops = [asyncio.create_task(self._loop(slot)) for slot in self._slots.values()]
            await self._stop.wait()
        finally:
            self.stop()
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            runs = [s.run for s in self._slots.values() if s.run is not None and not s.run.done()]
            if runs:
                await asyncio.wait(runs)
            reports = [task for slot in self._slots.values() for task in slot.reports]
            if reports:
                await asyncio.wait(reports)
            self._active = False
            for slot in self._slots.values():
                slot.state = replace(slot.state, next_fire=None)

    async def _loop(self, slot: _Slot) -> None:
        """Wait for each fire time of `slot` and fire it, until `stop()`. Never raises: a
        failure is logged and retried with a growing delay (`RETRY_MIN_S`, `RETRY_MAX_S`)."""
        cursor = self._clock.now()
        retry_s = RETRY_MIN_S
        while not self._stop.is_set():
            try:
                fired = await self._next(slot, cursor)
            except Exception as exc:
                slot.state = replace(slot.state, next_fire=None)
                logger.error(
                    "scheduler: cannot schedule %s (%s); retrying in %g s",
                    slot.schedule.pipeline,
                    type(exc).__name__,
                    retry_s,
                )
                del exc
                wake = self._clock.now() + timedelta(seconds=retry_s)
                retry_s = min(retry_s * 2, RETRY_MAX_S)
                if not await self._clock.sleep_until(wake, self._stop):
                    return
                # The fire times that passed meanwhile are missed, not caught up.
                cursor = max(cursor, self._clock.now())
                continue
            if fired is None:
                return
            retry_s = RETRY_MIN_S
            # Late or not, the next fire time is after both the one just fired and now (a
            # clock stepped back never fires the same time twice).
            cursor = max(fired, self._clock.now())

    async def _next(self, slot: _Slot, cursor: datetime) -> datetime | None:
        """Wait for the first fire time after `cursor` and fire it (`None`: stopped)."""
        schedule = slot.schedule
        fire = next_fire(schedule.cron, schedule.timezone, cursor)
        slot.state = replace(slot.state, next_fire=fire)
        if not await self._clock.sleep_until(fire, self._stop):
            return None
        self._fire(slot, fire)
        return fire

    def _late(self, fire: datetime) -> bool:
        return self._clock.now() - fire > self.misfire_grace

    def _fire(self, slot: _Slot, fire: datetime) -> None:
        if slot.busy:
            self._skipped(slot, fire, "running", "its previous scheduled run is still running")
        elif self._late(fire):
            self._skipped(slot, fire, "misfire", "it would start past its misfire grace")
        elif self._in_progress < self.max_concurrent:
            self._start(slot, fire)
        else:
            slot.waiting = True
            slot.state = replace(slot.state, waiting=True)
            self._queue.append((slot, fire))
            logger.info(
                "scheduler: %s at %s waits for a free slot (max_concurrent %d)",
                slot.schedule.pipeline,
                _iso(fire),
                self.max_concurrent,
            )

    def _start(self, slot: _Slot, fire: datetime) -> None:
        work = self._run(slot.schedule.pipeline)
        self._in_progress += 1
        slot.run = asyncio.create_task(work)
        slot.run.add_done_callback(self._released)
        report = asyncio.create_task(self._report(slot, fire, slot.run))
        slot.reports.add(report)
        report.add_done_callback(slot.reports.discard)

    def _released(self, _: "asyncio.Task[RunOutcome]") -> None:
        """A run ended: start the fire times waiting for its slot, oldest first."""
        self._in_progress -= 1
        while self._queue and self._in_progress < self.max_concurrent:
            slot, fire = self._queue.popleft()
            slot.waiting = False
            slot.state = replace(slot.state, waiting=False)
            if self._late(fire):
                self._skipped(slot, fire, "misfire", "it waited for a slot past its misfire grace")
            else:
                self._start(slot, fire)

    def _skipped(self, slot: _Slot, fire: datetime, reason: SkipReason, why: str) -> None:
        slot.state = replace(
            slot.state, skipped=slot.state.skipped + 1, last_skip=fire, last_skip_reason=reason
        )
        logger.warning("scheduler: skipped %s at %s: %s", slot.schedule.pipeline, _iso(fire), why)

    async def _report(self, slot: _Slot, fire: datetime, run: "asyncio.Task[RunOutcome]") -> None:
        """Wait for `run` (never cancelled itself: `stop()` cancels `run`, `force_stop()`
        this), record its outcome and ping its monitor."""
        name = slot.schedule.pipeline
        previous = slot.state
        slot.state = replace(
            slot.state,
            running=True,
            last_fire=fire,
            last_outcome=None,
            last_run_id=None,
            last_ping=None,
        )
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait([run])
        except asyncio.CancelledError:
            slot.state = replace(slot.state, running=False, last_ping="skipped")
            raise
        duration = asyncio.get_running_loop().time() - started
        if run.cancelled():
            outcome = RunOutcome(CANCELLED)
        elif (error := run.exception()) is not None:
            outcome = RunOutcome("internal")
            logger.error("scheduler: run of %s crashed (%s)", name, type(error).__name__)
            del error
        else:
            outcome = run.result()
        if outcome.status == LOCKED:
            # Nothing ran: only the fields this report changed go back.
            slot.state = replace(
                slot.state,
                running=False,
                last_fire=previous.last_fire,
                last_outcome=previous.last_outcome,
                last_run_id=previous.last_run_id,
                last_ping=previous.last_ping,
            )
            self._skipped(slot, fire, "locked", "the pipeline is running in another process")
            return
        slot.state = replace(
            slot.state, running=False, last_outcome=outcome.status, last_run_id=outcome.run_id
        )
        logger.info(
            "scheduler: run %s of %s (fired at %s): %s",
            outcome.run_id or "-",
            name,
            _iso(fire),
            outcome.status,
        )
        if outcome.status == CANCELLED:
            slot.state = replace(slot.state, last_ping="skipped")
            return
        try:
            result: str = await self._ping(name, outcome, duration)
        except asyncio.CancelledError:
            slot.state = replace(slot.state, last_ping="skipped")
            raise
        except Exception as exc:  # the ping function should not raise; never stop for it
            logger.warning("scheduler: ping of %s failed (%s)", name, type(exc).__name__)
            del exc
            result = "failed"
        slot.state = replace(slot.state, last_ping=_PING_WORDS.get(result, "failed"))


__all__ = [
    "CANCELLED",
    "LOCKED",
    "MAX_SLEEP_S",
    "MODES",
    "RETRY_MAX_S",
    "RETRY_MIN_S",
    "SCHEDULER_ENV",
    "Clock",
    "LastPing",
    "ModeError",
    "PingFunction",
    "PipelineState",
    "RunFunction",
    "Schedule",
    "Scheduler",
    "SchedulerState",
    "SkipReason",
    "SystemClock",
    "scheduler_mode",
    "schedules",
]
