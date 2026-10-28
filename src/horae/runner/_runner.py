# SPDX-License-Identifier: Apache-2.0
"""The run lifecycle: one pipeline run wired to the instance, the store and the engine (#19).

`Runner.run(name)` does, in order:

1. **Stale-run recovery** (`Runner.recover_stale`). A `running` run is abandoned
   (`Store.abandon_run`: `failed`, no record, its journal kept for undo) when its heartbeat is
   older than `stale_after_s`, or when it was started on this host by a process that no longer
   exists (`pid_alive`). A run on another host is only judged by its heartbeat.
2. **Lock.** A sortable run id (`new_run_id`) and `Store.begin_run`, which takes the
   pipeline's lock (the store's unique index on running runs). If another run of the pipeline
   is running, `PipelineLocked` names it; nothing else is written.
3. **Execute** (`horae.engine.execute`) with the last accepted LLM output
   (`Store.last_accepted_llm`) for the unchanged-input path. Each finished step is persisted
   (`on_step` → `Store.record_step`), and a heartbeat (`Store.heartbeat`) is written every
   `heartbeat_s` seconds while the run is running.
4. **Persist** the record (`Store.save_run`), which releases the lock. The status is the
   engine's: `ok`, `noop`, `degraded` or `failed` (`running` exists only in the store).

**Timeouts.** The run timeout (`horae.yaml` `runs.timeout_s`, default 900 s) bounds the whole
run: past it the engine's task is cancelled, and the partial record (`RunCancelled.record`) is
saved `failed`, with the interrupted step `failed` with error kind `timeout` and the later steps
`skipped`. Per-call limits are not the runner's: the plugin host (#24) applies
`plugin_defaults.call_timeout_s` / `plugins.<name>.call_timeout_s` to each tool call
(`ToolCallError` kind `timeout`), and the engine applies the LLM step's `timeout_s` to its
attempts. Cancellation is cooperative: a port that blocks the event loop (synchronous I/O)
cannot be interrupted, so the host and the LLM backends must be async all the way down.

**Cancellation and crashes.** If the caller cancels `run` (e.g. Ctrl-C under `asyncio.run`), the
partial record is saved the same way, with kind `cancelled`, and `RunCancelled` is raised. Any
other exception (a store failure in `on_step`, a bug) abandons the run (`failed`, no record;
the steps recorded so far stay readable with `Store.steps`) and propagates. A process that is
killed leaves its run `running`; the next run's recovery abandons it. If the run is abandoned
while it is still running (another process judged it stale), the heartbeat notices, the run
is stopped and `RunLost` is raised: its record can no longer be saved.

**Not here.** Applying intents (#21/#22) and the CLI `horae run` (#26). The ports are injected
(`Ports`), so tests use `horae.engine.testing`, and the plugin host (#24) plugs in unchanged.
"""

import asyncio
import os
import secrets
import socket
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from horae.config import Instance
from horae.contracts import Pipeline, RunContext, RunRecord, StepRecord
from horae.engine import (
    LlmBackend,
    PluginRegistry,
    PreviousLlm,
    RunCancelled,
    ToolCaller,
    execute,
    make_context,
)
from horae.store import MAX_LIST_RUNS, PipelineBusy, RunNotRunning, RunSummary, Store, StoreError

DEFAULT_HEARTBEAT_S = 10.0
"""Seconds between two heartbeats of a running run."""
DEFAULT_STALE_AFTER_S = 120.0
"""A running run with no heartbeat for this long is abandoned by the next run's recovery."""


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


class PipelineLocked(PipelineBusy, RunnerError):
    """Another run of the pipeline is running: this one did not start.

    `run_id` is the run holding the lock; `holder` its summary, when it could be read."""

    def __init__(self, pipeline: str, run_id: str, holder: RunSummary | None = None) -> None:
        super().__init__(pipeline, run_id)
        self.holder = holder
        detail = ""
        if holder is not None:
            owner = f", pid {holder.pid}" if holder.pid is not None else ""
            owner += f" on {holder.host}" if holder.host is not None else ""
            detail = f" (started {_stamp(holder.started_at)}{owner})"
        self.args = (
            f"pipeline {pipeline!r} is already running as run {run_id!r}{detail}; "
            "try again when it has finished",
        )


class RunLost(RunnerError):
    """The run was abandoned by another process while it was running, and was stopped."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id!r} was abandoned by another process (stale-run recovery) and stopped"
        )
        self.run_id = run_id


def new_run_id(pipeline: str, started_at: datetime) -> str:
    """A run id that sorts by start time: `20261027T053000123456Z-daily-brief-1a2b3c`.

    It matches the contract's `RunId` (`^[A-Za-z0-9._-]{1,128}$`): a pipeline name is at most
    64 characters of `a-z 0-9 -`."""
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("`started_at` must be timezone-aware")
    stamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{pipeline}-{secrets.token_hex(3)}"


def pid_alive(pid: int) -> bool:
    """Whether a process `pid` exists on this host (`True` when it cannot be told)."""
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: it exists but belongs to someone else; anything else: do not guess.
        return True
    return True


@dataclass(frozen=True)
class Ports:
    """What the engine calls: the plugin host (#24) and the LLM backend (M2), or fixtures."""

    plugins: PluginRegistry
    tools: ToolCaller
    llm: LlmBackend | None = None


@dataclass
class _Beat:
    lost: bool = False


def _timed_out(record: RunRecord, timeout_s: float) -> RunRecord:
    """The cancelled record, marked as stopped by the run timeout."""
    message = f"the run timed out after {timeout_s:g} s"
    data = record.model_dump(mode="json")
    error = message
    for index, step in enumerate(data["steps"]):
        why = step.get("error")
        if step["status"] == "failed" and why is not None and why["kind"] == "cancelled":
            why.update(kind="timeout", message=message)
            error = f"step {index} ({step['kind']}): {message}"
    data["error"] = error
    return RunRecord.model_validate(data)


@dataclass
class Runner:
    """Runs the pipelines of one instance against one store (one thread, one event loop)."""

    instance: Instance
    store: Store
    ports: Ports
    timeout_s: float | None = None
    """The run timeout; `None` uses `horae.yaml` `runs.timeout_s`."""
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    stale_after_s: float = DEFAULT_STALE_AFTER_S
    clock: Callable[[], datetime] = _utc_now
    is_alive: Callable[[int], bool] = pid_alive
    """Whether a pid of this host is alive (for stale-run recovery)."""
    host: str | None = field(default_factory=lambda: socket.gethostname() or None)
    """This host's name, recorded with each run (`None`: the store's default)."""

    def __post_init__(self) -> None:
        if self.timeout_s is not None and not self.timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        if not self.heartbeat_s > 0:
            raise ValueError("heartbeat_s must be > 0")
        if not self.stale_after_s > self.heartbeat_s:
            raise ValueError("stale_after_s must be longer than heartbeat_s")

    @property
    def run_timeout_s(self) -> float:
        return self.instance.config.runs.timeout_s if self.timeout_s is None else self.timeout_s

    # --- recovery --------------------------------------------------------------------------

    def _abandon(self, run_id: str, at: datetime, error: str) -> bool:
        try:
            self.store.abandon_run(run_id, at=at, error=error)
        except RunNotRunning:
            return False  # it finished, or another process recovered it first
        return True

    def recover_stale(self) -> list[RunSummary]:
        """Abandon the running runs whose process is gone; return them (as they were)."""
        now = self.clock()
        recovered: list[RunSummary] = []
        cutoff = now - timedelta(seconds=self.stale_after_s)
        for run in self.store.stale_runs(cutoff):
            last = _stamp(run.heartbeat_at or run.started_at)
            if self._abandon(run.id, now, f"abandoned: no heartbeat since {last}"):
                recovered.append(run)
        done = {run.id for run in recovered}
        for run in self.store.list_runs(status="running", limit=MAX_LIST_RUNS):
            if run.id in done or run.pid is None or run.host is None or run.host != self.host:
                continue
            if not self.is_alive(run.pid):
                why = f"abandoned: its process (pid {run.pid}) is gone"
                if self._abandon(run.id, now, why):
                    recovered.append(run)
        return recovered

    # --- one run ---------------------------------------------------------------------------

    def _pipeline(self, name: str) -> Pipeline:
        try:
            return self.instance.pipelines[name]
        except KeyError:
            raise UnknownPipeline(name) from None

    async def run(
        self, name: str, *, dry_run: bool = False, vars: Mapping[str, str] | None = None
    ) -> RunRecord:
        """Run pipeline `name` once and return its saved record (see the module docstring).

        Raises `UnknownPipeline`, `PipelineLocked` (nothing written), `RunCancelled` (the
        caller cancelled; the partial record is saved), `RunLost`, or what crashed the run
        (the run is then abandoned). `vars` are extra template variables."""
        pipeline = self._pipeline(name)
        self.recover_stale()
        started_at = self.clock()
        run_id = new_run_id(name, started_at)
        trigger = pipeline.trigger
        zone = (
            trigger.timezone
            if trigger is not None and trigger.timezone is not None
            else self.instance.config.timezone
        )
        context = make_context(started_at, zone, vars)
        previous = self.store.last_accepted_llm(name)
        try:
            self.store.begin_run(run_id, name, started_at, dry_run=dry_run, host=self.host)
        except PipelineBusy as busy:
            holder = self.store.run_summary(busy.run_id)
            raise PipelineLocked(busy.pipeline, busy.run_id, holder) from None
        try:
            return await self._drive(pipeline, run_id, started_at, context, dry_run, previous)
        except BaseException as exc:
            # Only the exception's type: its message may hold data from a plugin or the store.
            with suppress(StoreError):
                self.store.abandon_run(
                    run_id, at=self.clock(), error=f"run crashed ({type(exc).__name__})"
                )
            raise

    async def _heartbeat(self, run_id: str, task: asyncio.Task[RunRecord], beat: _Beat) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                self.store.heartbeat(run_id, self.clock())
            except RunNotRunning:
                beat.lost = True
                task.cancel()
                return
            except StoreError:
                continue  # e.g. busy: try again at the next beat

    async def _drive(
        self,
        pipeline: Pipeline,
        run_id: str,
        started_at: datetime,
        context: RunContext,
        dry_run: bool,
        previous: PreviousLlm | None,
    ) -> RunRecord:
        async def on_step(index: int, step: StepRecord) -> None:
            self.store.record_step(run_id, index, step)

        models = {alias: spec.model for alias, spec in self.instance.config.models.items()}
        task = asyncio.create_task(
            execute(
                pipeline,
                run_id=run_id,
                context=context,
                started_at=started_at,
                plugins=self.ports.plugins,
                tools=self.ports.tools,
                llm=self.ports.llm,
                dry_run=dry_run,
                previous=previous,
                models=models,
                on_step=on_step,
                clock=self.clock,
            )
        )
        beat = _Beat()
        beating = asyncio.create_task(self._heartbeat(run_id, task, beat))
        timeout = self.run_timeout_s
        cancelled = stopped_early = False
        try:
            try:
                done, _ = await asyncio.wait({task}, timeout=timeout)
            except asyncio.CancelledError:
                cancelled, done = True, set()
            if not done:
                task.cancel()
            try:
                record = await task
            except RunCancelled as stopped:
                record, stopped_early = stopped.record, True
        finally:
            beating.cancel()
            with suppress(asyncio.CancelledError):
                await beating
        if beat.lost:
            raise RunLost(run_id)
        if stopped_early and not cancelled and not done:
            record = _timed_out(record, timeout)
        self.store.save_run(record)
        if cancelled:
            # The caller cancelled; if the run finished first, its record is saved as is.
            raise RunCancelled(record) if stopped_early else asyncio.CancelledError()
        return record


__all__ = [
    "DEFAULT_HEARTBEAT_S",
    "DEFAULT_STALE_AFTER_S",
    "PipelineLocked",
    "Ports",
    "RunLost",
    "Runner",
    "RunnerError",
    "UnknownPipeline",
    "new_run_id",
    "pid_alive",
]
