# SPDX-License-Identifier: Apache-2.0
"""The run lifecycle: one pipeline run wired to the instance, the store and the engine (#19).

`Runner.run(name)` does, in order:

1. **Lock.** It takes the pipeline's OS advisory lock (`horae.runner._lock`: `flock` on
   `<state_dir>/locks/<pipeline>.lock`) without waiting, and holds it for the whole run. If
   another process (or task) holds it, `PipelineLocked` names the run that holds it (read from
   the store) and nothing is written. Dry runs take the lock too.
2. **Recovery** (`recover_pipeline`, also used by `horae undo`). With the lock held, no other
   process can be running this pipeline, so any `running` row of *this* pipeline was left by
   a process that died (crash, `kill -9`, OOM, power loss): it is abandoned
   (`Store.abandon_run`: `failed`, no record, journal kept for undo), as a compare-and-set on
   its id and last heartbeat. Other pipelines are recovered when they next start.
3. **Begin.** A sortable run id (`new_run_id`) and `Store.begin_run` (retried once with a fresh
   id on `RunExists`). The store's unique index on running runs stays as a second guard:
   `PipelineBusy` there is reported as `PipelineLocked` too.
4. **Execute** (`horae.engine.execute`) with the LLM cache (`StoreLlmCache` over the store's
   `llm_cache` table, `horae.runner._cache`, #32) for the unchanged-input path (no lookup under
   `cache=False`, `horae run --no-cache`), the model alias targets of `horae.yaml`, and the digests
   of `horae.lock` and of `horae.yaml`'s `models` and `plugins` sections (`Instance.lock_digest`,
   `Instance.config_digest`), which the record keeps so that `horae replay` can tell what changed
   since the run. Each finished step is persisted (`on_step` → `Store.record_step`), and a heartbeat
   (`Store.heartbeat`) is written every `heartbeat_s` seconds. Heartbeats are for
   observability (`horae runs`); the lock, not the heartbeat, decides whether a run is alive.
   The LLM backend is wrapped in `LedgerLlm` (`horae.runner._budget`, #33): the daily
   budgets of `horae.yaml` and the usage ledger, one reserved then settled row per call;
   a dry run is charged too (it calls the model; decision 41 E).
5. **Apply** (#22, provisional): when the run is not a dry run, finished `ok`, `noop` or
   `degraded` within its timeout, and `Ports.sinks` is set, its routed intents are planned
   (`horae.apply.plan_changes`, with the notifications already sent) and applied
   (`horae.apply.apply_changes`: journal first, then the sink, then mark applied) while the
   run's row is still `running`, holding the instance's apply lock
   (`<state_dir>/locks/_apply.lock`, also held by `horae undo`; waited for at most
   `APPLY_LOCK_WAIT_S`, else the run fails `apply_locked` with nothing applied) and with the
   heartbeat still running. The record gets its `changes`; if a change cannot be applied the
   run becomes `failed` (the changes before it stay applied and journalled, the rest are not
   applied, and nothing is rolled back: `horae undo` is the remedy). Without `Ports.sinks`
   nothing is applied (the intents are only recorded, as before #22). The apply is not under
   the run timeout; each sink call is bounded by `plugin_defaults.call_timeout_s` (kind
   `timeout`). **The apply is shielded from cancellation** (schemas decision 37): once it
   has started planning, a cancellation (a signal under `horae run`) is remembered, the
   apply finishes, the record is saved (with its real status: the changes were applied) and
   only then is `asyncio.CancelledError` raised. A cancellation while it waits for the apply
   lock stops the wait: the run is saved `failed` ("cancelled before apply"), nothing
   applied; so is a run whose caller cancels it after its steps finished but before the
   apply. A crash during the apply abandons the run (error `run crashed during apply
   (<type>)`), and its journal still lets `horae undo` revert what was written.
6. **Persist** the record (`Store.save_run`), call `on_record(record)` if given, cache the
   run's accepted LLM output (not for a dry run; also under `cache=False`, which only skips
   the lookup), and release the lock. Before step 3, a non-dry run purges expired cache
   entries and the oldest beyond `cache.max_entries` (best effort). The status is the
   engine's (`ok`, `noop`, `degraded` or `failed`), or `failed` when the apply failed.

**Timeouts.** The run timeout (`horae.yaml` `runs.timeout_s`, default 900 s) bounds the whole
run: past it the engine's task is cancelled and given `stop_grace_s` (5 s) to stop. If it stops,
the partial record (`RunCancelled.record`) is saved `failed`, with the interrupted step `failed`
with error kind `timeout` and the later steps `skipped`, and `RunTimedOut(record)` is raised.
If the run finished anyway after the deadline (a step ignored the cancellation), its record is
saved `failed` with a timeout error, never `ok`. If it still has not stopped after the grace
period, the run is abandoned (error `timeout: ... step did not stop ...`) and `RunTimedOut` is
raised with `record=None`; the stray task is left to finish on its own, and its writes are
refused because the run is no longer `running`. Per-call limits are not the runner's: the
plugin host (#24) applies `plugin_defaults.call_timeout_s` / `plugins.<name>.call_timeout_s`
to each tool call (`ToolCallError` kind `timeout`), and the engine applies the LLM step's
`timeout_s` to its attempts. Cancellation is cooperative: a port that blocks the event loop
(synchronous I/O) cannot be interrupted, so the host and the LLM backends must be async all the
way down.

**Cancellation.** If the caller cancels `run` (Ctrl-C under `asyncio.run`, or
`async with asyncio.timeout(...)`), the partial record is saved the same way with kind
`cancelled` (a run cancelled before its first step gets a minimal record: every step
`skipped`), `on_record` receives it, and a plain `asyncio.CancelledError` is re-raised, so
`asyncio.timeout` turns it into `TimeoutError` as usual. The same grace period applies.

**Crashes and lost runs.** Any other exception (a store failure in `on_step`, a bug) abandons
the run (`failed`, no record; the steps recorded so far stay readable with `Store.steps`) and
propagates; if even the abandon fails, it is logged and the next run of the pipeline recovers
the row. A process that is killed leaves its run `running` until then. If the run's row is
abandoned while it runs (by hand, or by a process that does not use the lock), the next
heartbeat, step write or the final save notices: the run is stopped and `RunLost` is raised.

**Clocks.** Timestamps and run ids use `clock` (the wall clock); the run timeout uses the event
loop's monotonic clock. Since liveness is the lock's, a wall-clock step only shifts recorded
times (and may reorder run ids started across the step); it cannot make a live run look dead.

**Redaction** (#53). `run` installs the log record scrubber (`horae.secrets.logs`) and
resolves every secret the instance names (`Instance.prime_redaction`) before it takes the
lock, so the store's secret gate, the engine's plan-time refusal of a secret in a `--var` or
a parameter, the LLM layer and the logs know them all from the start.

**The CLI.** `horae run` (`horae.cli_run`, #26) cancels the run on SIGINT and SIGTERM, as
above, and exits 130 / 143; `on_begin(run_id)` tells it the run's id as soon as the row
exists, so it can name the run whatever happens next.
The ports are injected (`Ports`), so tests use `horae.engine.testing`, and the plugin host
(#24) plugs in unchanged.
"""

import asyncio
import logging
import secrets
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from horae.apply import SinkRegistry, apply_changes, plan_changes
from horae.config import Instance
from horae.contracts import (
    ActStep,
    CollectStep,
    LlmStep,
    NotifyStep,
    Pipeline,
    RunContext,
    RunRecord,
    StepRecord,
    Usage,
)
from horae.contracts.run import StepKind
from horae.engine import (
    LlmBackend,
    PluginRegistry,
    RunCancelled,
    SkillSource,
    ToolCaller,
    execute,
    make_context,
    pipeline_digest,
)
from horae.runner._budget import LedgerLlm
from horae.runner._cache import StoreLlmCache
from horae.runner._lock import PipelineLock, try_acquire_apply_lock
from horae.secrets import logs
from horae.store import (
    MAX_LIST_RUNS,
    InvalidRow,
    PipelineBusy,
    RunExists,
    RunNotRunning,
    RunNotStale,
    RunSummary,
    Store,
    UnknownRun,
)

_log = logging.getLogger("horae.runner")

DEFAULT_HEARTBEAT_S = 10.0
"""Seconds between two heartbeats of a running run (not configurable in `horae.yaml`)."""
STOP_GRACE_S = 5.0
"""Seconds a cancelled run (timeout or caller) gets to stop before it is abandoned."""
LOCK_RETRY_S = 0.2
"""How long a busy lock with no running row is retried (a run starting or finishing, or a
`horae runs` probing the lock) before `PipelineLocked`."""
APPLY_LOCK_WAIT_S = 30.0
"""How long a run waits for the instance's apply lock (held by `horae undo` or another run's
apply) before it fails `apply_locked`, nothing applied."""
_APPLY_LOCK_POLL_S = 0.05


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RunnerError(Exception):
    """The runner cannot start or finish a run."""


class UnknownPipeline(RunnerError, LookupError):
    """The instance has no pipeline of this name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown pipeline {name!r}")
        self.name = name


class PipelineLocked(RunnerError):
    """Another run of the pipeline is running: this one did not start (nothing was written).

    `run_id` is the run holding the lock and `holder` its summary, when they could be read
    (`None` when the lock is held but its run has no row yet, e.g. it is just starting)."""

    def __init__(
        self, pipeline: str, run_id: str | None = None, holder: RunSummary | None = None
    ) -> None:
        self.pipeline = pipeline
        self.run_id = run_id
        self.holder = holder
        detail = ""
        if holder is not None:
            owner = f", pid {holder.pid}" if holder.pid is not None else ""
            owner += f" on {holder.host}" if holder.host is not None else ""
            detail = f" (started {_stamp(holder.started_at)}{owner})"
        running = f"as run {run_id!r}{detail}" if run_id is not None else "(its lock is held)"
        super().__init__(
            f"pipeline {pipeline!r} is already running {running}; try again when it has finished"
        )


class RunLost(RunnerError):
    """The run's row was abandoned by someone else while it was running; it was stopped."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} was abandoned by another process and stopped")
        self.run_id = run_id


class RunTimedOut(RunnerError):
    """The run timeout fired. `record` is the saved `failed` record, or `None` when the run
    did not stop within the grace period and was abandoned instead."""

    def __init__(self, run_id: str, timeout_s: float, record: RunRecord | None) -> None:
        how = "" if record is not None else "; its step did not stop, so the run was abandoned"
        super().__init__(f"run {run_id!r} timed out after {timeout_s:g} s{how}")
        self.run_id = run_id
        self.timeout_s = timeout_s
        self.record = record


def new_run_id(pipeline: str, started_at: datetime) -> str:
    """A run id that sorts by start time: `20261027T053000123456Z-daily-brief-1a2b3c`.

    It matches the contract's `RunId` (`^[A-Za-z0-9._-]{1,128}$`): a pipeline name is at most
    64 characters of `a-z 0-9 -`."""
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("`started_at` must be timezone-aware")
    stamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{pipeline}-{secrets.token_hex(3)}"


@dataclass(frozen=True)
class Ports:
    """What the engine calls: the plugin host (#24) and the LLM backend (M2), or fixtures."""

    plugins: PluginRegistry
    tools: ToolCaller
    llm: LlmBackend | None = None
    sinks: SinkRegistry | None = None
    """The sinks and notifiers that apply a run's intents (#22); `None` applies nothing."""
    skills: SkillSource | None = None
    """Loads the `llm` step's skill (#27): `horae.skills.DirectorySkills` over the installed
    plugins; `None` sends the whole input and no prompt (fakes only)."""


@dataclass
class _State:
    run_id: str
    lost: bool = False
    """The heartbeat found the row no longer running."""
    settled: bool = False
    """The row is final (saved or abandoned): a crash must not abandon it."""
    applying: bool = False
    """The apply phase has started (a crash then abandons the run "during apply")."""
    stop_requested: bool = False
    """The caller cancelled the run during the (shielded) apply phase."""


def _step_kind(step: object) -> StepKind:
    if isinstance(step, CollectStep):
        return "collect"
    if isinstance(step, LlmStep):
        return "llm"
    if isinstance(step, ActStep):
        return "act"
    if isinstance(step, NotifyStep):
        return "notify"
    return "transform"


def _timed_out(record: RunRecord, timeout_s: float) -> RunRecord:
    """The record of a run stopped (or finished late) past the run timeout: `failed`."""
    message = f"the run timed out after {timeout_s:g} s"
    data = record.model_dump(mode="json")
    error = f"{message} (it finished after the deadline)"
    for index, step in enumerate(data["steps"]):
        why = step.get("error")
        if step["status"] == "failed" and why is not None and why["kind"] == "cancelled":
            why.update(kind="timeout", message=message)
            error = f"step {index} ({step['kind']}): {message}"
    data["status"] = "failed"
    data["error"] = error
    return RunRecord.model_validate(data)


def _failed(record: RunRecord, error: str) -> RunRecord:
    """`record`, `failed` with `error`, nothing applied."""
    return record.model_copy(update={"status": "failed", "error": error})


def recover_pipeline(
    store: Store, pipeline: str, *, clock: Callable[[], datetime]
) -> list[RunSummary]:
    """Abandon the `running` rows of `pipeline`, whose lock the caller holds: they are dead.

    Each is a compare-and-set on its last heartbeat (`Store.abandon_run(heartbeat_before=)`);
    a row that finished meanwhile is skipped, one that beats meanwhile (a process that does not
    take the lock) is logged and left alone. Returns the rows abandoned."""
    recovered: list[RunSummary] = []
    for run in store.list_runs(pipeline=pipeline, status="running", limit=MAX_LIST_RUNS):
        seen = run.heartbeat_at or run.started_at
        why = f"abandoned: its process died (last heartbeat {_stamp(seen)})"
        try:
            store.abandon_run(run.id, at=clock(), error=why, heartbeat_before=seen)
        except (RunNotRunning, UnknownRun):
            continue  # it finished meanwhile
        except RunNotStale:
            # A heartbeat while we hold the lock: a process that does not take it.
            _log.warning("run %s is running without the pipeline lock; left alone", run.id)
            continue
        recovered.append(run)
    return recovered


def _retrieve(task: asyncio.Task[RunRecord]) -> None:
    """Mark a finished task's exception as retrieved (a stray task is never awaited)."""
    if not task.cancelled():
        task.exception()


@dataclass
class Runner:
    """Runs the pipelines of one instance against one store (one thread, one event loop)."""

    instance: Instance
    store: Store
    ports: Ports
    timeout_s: float | None = None
    """The run timeout; `None` uses `horae.yaml` `runs.timeout_s`."""
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    stop_grace_s: float = STOP_GRACE_S
    """Seconds a cancelled run gets to stop (the default is meant to stay; tests shorten it)."""
    clock: Callable[[], datetime] = _utc_now
    host: str | None = field(default_factory=lambda: socket.gethostname() or None)
    """This host's name, recorded with each run (`None`: the store's default)."""

    def __post_init__(self) -> None:
        if self.timeout_s is not None and not self.timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        if not self.heartbeat_s > 0:
            raise ValueError("heartbeat_s must be > 0")
        if not self.stop_grace_s > 0:
            raise ValueError("stop_grace_s must be > 0")

    @property
    def run_timeout_s(self) -> float:
        return self.instance.config.runs.timeout_s if self.timeout_s is None else self.timeout_s

    # --- lock and recovery -----------------------------------------------------------------

    def _holder(self, name: str) -> RunSummary | None:
        running = self.store.list_runs(pipeline=name, status="running", limit=1)
        return running[0] if running else None

    async def _lock(self, name: str) -> PipelineLock:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCK_RETRY_S
        while True:
            lock = PipelineLock.try_acquire(self.instance.state_dir, name)
            if lock is not None:
                return lock
            if loop.time() >= deadline:
                holder = self._holder(name)
                raise PipelineLocked(name, None if holder is None else holder.id, holder)
            await asyncio.sleep(0.02)

    def _recover(self, name: str) -> list[RunSummary]:
        """Abandon the `running` rows of `name` (the caller holds its lock: they are dead)."""
        return recover_pipeline(self.store, name, clock=self.clock)

    def _begin(self, name: str, started_at: datetime, dry_run: bool) -> str:
        for attempt in range(2):
            run_id = new_run_id(name, started_at)
            try:
                self.store.begin_run(run_id, name, started_at, dry_run=dry_run, host=self.host)
            except RunExists:
                if attempt:
                    raise
                continue  # a run id collision (same microsecond, same 24 random bits)
            except PipelineBusy as busy:
                holder = self.store.run_summary(busy.run_id)
                raise PipelineLocked(busy.pipeline, busy.run_id, holder) from None
            return run_id
        raise AssertionError("unreachable")  # pragma: no cover

    # --- one run ---------------------------------------------------------------------------

    def _pipeline(self, name: str) -> Pipeline:
        try:
            return self.instance.pipelines[name]
        except KeyError:
            raise UnknownPipeline(name) from None

    async def run(
        self,
        name: str,
        *,
        dry_run: bool = False,
        vars: Mapping[str, str] | None = None,
        on_record: Callable[[RunRecord], object] | None = None,
        on_begin: Callable[[str], object] | None = None,
        cache: bool = True,
    ) -> RunRecord:
        """Run pipeline `name` once and return its saved record (see the module docstring).

        `vars` are extra template variables. `cache=False` (`horae run --no-cache`) skips the
        LLM cache lookup: the model is called even on unchanged input, and the accepted
        output still replaces the cache entry. `on_begin(run_id)` is called once the run's row
        exists (before its first step). `on_record(record)` is called with every record
        that is saved, also when `run` then raises (cancellation, timeout); neither should
        raise. Raises `UnknownPipeline`, `PipelineLocked` (nothing written), `RunTimedOut`
        (with the saved record, or `None` if the run was abandoned), `asyncio.CancelledError`
        (the caller cancelled; the partial record is saved and passed to `on_record`),
        `RunLost`, `OSError` (the lock file cannot be created), or what crashed the run (it is
        then abandoned)."""
        pipeline = self._pipeline(name)
        # Redaction first (#53): the log scrubber, and every secret the instance names
        # known to the process registry before anything is run, stored or logged.
        logs.install()
        self.instance.prime_redaction()
        lock = await self._lock(name)
        try:
            self._recover(name)
            llm_cache = StoreLlmCache(
                self.store,
                self.instance.config.cache,
                self.clock,
                read=cache,
                models=self.instance.config.models,
            )
            if not dry_run:
                llm_cache.purge()
            started_at = self.clock()
            trigger = pipeline.trigger
            zone = (
                trigger.timezone
                if trigger is not None and trigger.timezone is not None
                else self.instance.config.timezone
            )
            context = make_context(started_at, zone, vars)
            state = _State(self._begin(name, started_at, dry_run))
            try:
                if on_begin is not None:
                    on_begin(state.run_id)
                return await self._drive(
                    pipeline, state, started_at, context, dry_run, llm_cache, on_record
                )
            except BaseException as exc:
                if not state.settled:
                    self._abandon_crashed(state, exc)
                raise
        finally:
            lock.release()

    def _abandon_crashed(self, state: _State, exc: BaseException) -> None:
        # Only the exception's type: its message may hold data from a plugin or the store.
        run_id = state.run_id
        during = " during apply" if state.applying else ""
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
            error = f"cancelled: the run was interrupted{during} before its record was saved"
        else:
            error = f"run crashed{during} ({type(exc).__name__})"
        if state.applying:
            error += f"; run `horae undo {run_id}` to revert what was written"
        try:
            self.store.abandon_run(run_id, at=self.clock(), error=error)
        except RunNotRunning:
            pass  # already final
        except Exception as failure:
            _log.error(
                "run %s crashed (%s) and could not be abandoned (%s); the next run of the "
                "pipeline will recover it",
                run_id,
                type(exc).__name__,
                type(failure).__name__,
            )

    async def _heartbeat(self, state: _State, task: asyncio.Task[RunRecord] | None) -> None:
        """Beat every `heartbeat_s`; once the row is no longer running, mark the run lost and
        cancel `task` (if any)."""
        warned = False
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                self.store.heartbeat(state.run_id, self.clock())
            except RunNotRunning:
                state.lost = True
                if task is not None:
                    task.cancel()
                return
            except Exception as exc:
                # E.g. busy: skip this beat. Logged once per run (type only, no message).
                if not warned:
                    warned = True
                    _log.warning(
                        "run %s: heartbeat failed (%s); retrying at each beat",
                        state.run_id,
                        type(exc).__name__,
                    )

    def _stopped_record(
        self,
        pipeline: Pipeline,
        run_id: str,
        started_at: datetime,
        context: RunContext,
        dry_run: bool,
        message: str,
    ) -> RunRecord:
        """A minimal `failed` record for a run stopped before its first step: all `skipped`."""
        steps = [StepRecord(kind=_step_kind(step), status="skipped") for step in pipeline.steps]
        return RunRecord(
            schema_version=1,
            id=run_id,
            pipeline=pipeline.name,
            pipeline_digest=pipeline_digest(pipeline),
            context=context,
            dry_run=dry_run,
            started_at=started_at,
            finished_at=max(self.clock(), started_at),
            status="failed",
            plugins={},
            inputs=[],
            steps=steps,
            llm=[],
            intents=[],
            changes=[],
            cost=Usage(tokens_in=0, tokens_out=0, usd=0.0),
            error=message,
        )

    def _save(
        self,
        state: _State,
        record: RunRecord,
        on_record: Callable[[RunRecord], object] | None,
    ) -> None:
        try:
            self.store.save_run(record)
        except (InvalidRow, RunNotRunning):
            summary = self.store.run_summary(state.run_id)
            if summary is not None and summary.abandoned_at is not None:
                state.settled = True
                raise RunLost(state.run_id) from None
            raise
        state.settled = True
        if on_record is not None:
            on_record(record)

    def _applies(self, record: RunRecord) -> bool:
        """Whether the apply phase has something to do for `record`."""
        return (
            self.ports.sinks is not None
            and not record.dry_run
            and record.status in {"ok", "noop", "degraded"}
            and bool(record.intents)
        )

    async def _apply_lock(self, state: _State) -> PipelineLock | None:
        """The apply lock, or `None` after `APPLY_LOCK_WAIT_S` or once a stop is requested."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + APPLY_LOCK_WAIT_S
        while True:
            if state.stop_requested:
                return None
            lock = try_acquire_apply_lock(self.instance.state_dir)
            if lock is not None or loop.time() >= deadline:
                return lock
            await asyncio.sleep(_APPLY_LOCK_POLL_S)

    async def _shielded_apply(self, state: _State, record: RunRecord) -> tuple[RunRecord, bool]:
        """`_apply` in its own task, shielded from the caller's cancellation: a cancellation
        is remembered (`state.stop_requested`) and the apply finishes. Returns the record
        and whether the caller cancelled meanwhile."""
        applying = asyncio.create_task(self._apply(state, record))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(applying), cancelled
            except asyncio.CancelledError:
                if applying.done():
                    # The apply finished (or was itself cancelled: `result` re-raises).
                    return applying.result(), True
                cancelled = True
                state.stop_requested = True

    async def _apply(self, state: _State, record: RunRecord) -> RunRecord:
        """Plan and apply the record's intents (see the module docstring, step 5)."""
        sinks = self.ports.sinks
        if sinks is None or not self._applies(record):
            return record

        def notified(key: str) -> bool:
            return self.store.get_notification(key) is not None

        beating = asyncio.create_task(self._heartbeat(state, None))
        try:
            lock = await self._apply_lock(state)
            if lock is None and state.stop_requested:
                message = "cancelled before apply: the run was cancelled after its steps finished"
                return _failed(record, f"{message}; nothing was applied")
            if lock is None:
                return _failed(
                    record,
                    "apply_locked: another process (horae undo, or the apply of another run) "
                    f"held the instance's apply lock for {APPLY_LOCK_WAIT_S:g} s; nothing was "
                    "applied",
                )
            with lock:
                state.applying = True
                timeout_s = self.instance.config.plugin_defaults.call_timeout_s
                planned = await plan_changes(
                    record, sinks, notified=notified, call_timeout_s=timeout_s
                )
                result = await apply_changes(
                    record, planned, sinks, self.store, clock=self.clock, call_timeout_s=timeout_s
                )
        except RunNotRunning:
            state.settled = True
            raise RunLost(state.run_id) from None
        finally:
            beating.cancel()
            await asyncio.wait({beating})
            if not beating.cancelled():
                beating.exception()
        if state.lost:
            state.settled = True
            raise RunLost(state.run_id)
        return result.record

    def _ledger_llm(self, pipeline: Pipeline, run_id: str) -> LlmBackend | None:
        """The run's LLM backend behind the daily budgets and the usage ledger (#33)."""
        step = next((i for i, s in enumerate(pipeline.steps) if isinstance(s, LlmStep)), None)
        if self.ports.llm is None or step is None:
            return self.ports.llm
        config = self.instance.config
        return LedgerLlm(
            self.ports.llm,
            self.store,
            run_id,
            pipeline.name,
            step,
            config.budgets,
            config.timezone,
            self.clock,
        )

    async def _drive(
        self,
        pipeline: Pipeline,
        state: _State,
        started_at: datetime,
        context: RunContext,
        dry_run: bool,
        llm_cache: StoreLlmCache,
        on_record: Callable[[RunRecord], object] | None,
    ) -> RunRecord:
        run_id = state.run_id

        async def on_step(index: int, step: StepRecord) -> None:
            try:
                self.store.record_step(run_id, index, step)
            except RunNotRunning:
                state.lost = True
                raise RunLost(run_id) from None

        models = {alias: spec.model for alias, spec in self.instance.config.models.items()}
        llm = self._ledger_llm(pipeline, run_id)
        task = asyncio.create_task(
            execute(
                pipeline,
                run_id=run_id,
                context=context,
                started_at=started_at,
                plugins=self.ports.plugins,
                tools=self.ports.tools,
                llm=llm,
                skills=self.ports.skills,
                dry_run=dry_run,
                cache=llm_cache,
                models=models,
                lock_digest=self.instance.lock_digest(),
                config_digest=self.instance.config_digest(),
                on_step=on_step,
                clock=self.clock,
            )
        )
        task.add_done_callback(_retrieve)
        beating = asyncio.create_task(self._heartbeat(state, task))
        timeout = self.run_timeout_s
        cancelled = False
        try:
            try:
                done, _ = await asyncio.wait({task}, timeout=timeout)
            except asyncio.CancelledError:
                cancelled, done = True, set()
            timed_out = not done and not cancelled
            if not done:
                task.cancel()
                try:
                    await asyncio.wait({task}, timeout=self.stop_grace_s)
                except asyncio.CancelledError:
                    cancelled = True  # cancelled again while stopping: stop waiting
        finally:
            beating.cancel()
            # Not `with suppress(CancelledError): await beating`, which would also swallow a
            # cancellation of this task: a cancellation here is remembered and re-raised.
            try:
                await asyncio.wait({beating})
            except asyncio.CancelledError:
                cancelled = True
            else:
                if not beating.cancelled():
                    beating.exception()

        if not task.done():
            what = f"timed out after {timeout:g} s" if timed_out else "was cancelled"
            kind = "timeout" if timed_out else "cancelled"
            why = f"{kind}: the run {what} and its step did not stop within {self.stop_grace_s:g} s"
            try:
                self.store.abandon_run(run_id, at=self.clock(), error=why)
            except RunNotRunning:
                state.settled = True
                raise RunLost(run_id) from None
            state.settled = True
            if timed_out:
                raise RunTimedOut(run_id, timeout, None)
            raise asyncio.CancelledError
        if state.lost:
            state.settled = True
            raise RunLost(run_id)
        try:
            record = task.result()
        except RunCancelled as stopped:
            record = stopped.record
        except asyncio.CancelledError:
            # Cancelled before its first step ran: no partial record from the engine.
            what = f"timed out after {timeout:g} s" if timed_out else "was cancelled"
            message = f"the run {what} before it started"
            record = self._stopped_record(pipeline, run_id, started_at, context, dry_run, message)
        if timed_out:
            record = _timed_out(record, timeout)
        elif not cancelled:
            record, cancelled = await self._shielded_apply(state, record)
        elif self._applies(record):
            message = "cancelled before apply: the run was cancelled after its steps finished"
            record = _failed(record, f"{message}; nothing was applied")
        self._save(state, record, on_record)
        if not dry_run:
            llm_cache.remember(record)
        if timed_out:
            raise RunTimedOut(run_id, timeout, record)
        if cancelled:
            # The caller cancelled; if the run finished first, its record is saved as is.
            raise asyncio.CancelledError
        return record


__all__ = [
    "APPLY_LOCK_WAIT_S",
    "DEFAULT_HEARTBEAT_S",
    "LOCK_RETRY_S",
    "STOP_GRACE_S",
    "PipelineLocked",
    "Ports",
    "RunLost",
    "RunTimedOut",
    "Runner",
    "RunnerError",
    "UnknownPipeline",
    "new_run_id",
    "recover_pipeline",
]
