# SPDX-License-Identifier: Apache-2.0
"""`horae serve` (#39): the long-running process of a container, with the built-in cron.

It loads the instance and runs `horae.scheduler.Scheduler` until SIGTERM or SIGINT. In
`builtin` mode (`horae.yaml` `scheduler.mode`, overridden by `HORAE_SCHEDULER`) each
pipeline with a `trigger` runs at its fire times, in this process, exactly as `horae run
<pipeline>` would run it (same plugins, store, per-pipeline lock and apply lock), and its push
monitor is pinged after each run. In `external` mode nothing fires: the process only waits
(an external scheduler calls `horae run`); the HTTP API (#37) will be served by this same
process in both modes, and its `/healthz` reads `Scheduler.state()`.

The first SIGTERM or SIGINT stops the scheduler: no new run starts, a run in progress is
cancelled (its record saved as `horae run` does on SIGTERM) and its monitor pinged `down`;
the command then exits 0 (a requested stop is the normal end of a server). A second signal
while it stops is ignored (the runner's grace period bounds the stop).

Scheduler events (a run's outcome, a skipped fire time, a failed ping) are logged on stderr,
one line each, redacted like every log record.

Exit codes: 0 stopped by a signal, 1 configuration problem (an invalid instance, an invalid
`HORAE_SCHEDULER`), 2 usage error, 70 internal error.
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.cli_host import load
from horae.cli_output import EXIT_OK, EXIT_PROBLEMS, CommandFailed, echo, run_command
from horae.cli_run import _execute, _Run
from horae.config import Instance
from horae.runner import LocksUnsupported, PipelineLocked, RunLost, RunTimedOut
from horae.scheduler import (
    LOCKED,
    ModeError,
    RunOutcome,
    Scheduler,
    ping,
    scheduler_mode,
    schedules,
)
from horae.store import StoreError

logger = logging.getLogger("horae.scheduler")

_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def run_scheduled(instance: Instance, name: str) -> RunOutcome:
    """One scheduled run of `name`, as `horae run <name>` runs it, reported as an outcome
    (`LOCKED` when the pipeline is running elsewhere). A cancellation propagates."""
    out = _Run()
    try:
        record = await _execute(instance, name, False, {}, out)
    except PipelineLocked:
        return RunOutcome(LOCKED)
    except RunTimedOut:
        return RunOutcome("timeout", out.run_id)
    except RunLost:
        return RunOutcome("run_lost", out.run_id)
    except CommandFailed as failed:  # the plugins do not match the lock
        return RunOutcome(failed.kind, out.run_id)
    except LocksUnsupported:
        return RunOutcome("locks_unsupported")
    except StoreError:
        return RunOutcome("store_error", out.run_id)
    except OSError:
        return RunOutcome("io_error", out.run_id)
    return RunOutcome(record.status, record.id)


async def serve_until_signal(scheduler: Scheduler) -> None:
    """Run `scheduler` until the first SIGTERM or SIGINT (then `stop()` it and wait).

    Outside the main thread (no signal handlers there) it runs until `stop()`."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    try:
        for signum in _SIGNALS:
            try:
                loop.add_signal_handler(signum, scheduler.stop)
            except (RuntimeError, ValueError, NotImplementedError):  # pragma: no cover
                break
            installed.append(signum)
        await scheduler.run_forever()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def build_scheduler(instance: Instance, environ: Mapping[str, str] | None = None) -> Scheduler:
    """The instance's scheduler (`CommandFailed(config_invalid)` on a bad `HORAE_SCHEDULER`)."""
    try:
        mode = scheduler_mode(instance.config.scheduler.mode, environ)
    except ModeError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", str(exc)) from None

    async def run(name: str) -> RunOutcome:
        return await run_scheduled(instance, name)

    async def report(name: str, outcome: RunOutcome, duration_s: float) -> None:
        await ping(instance, name, outcome, duration_s)

    return Scheduler(schedules(instance), mode=mode, run=run, ping=report)


def _serve(
    instance_dir: Path,
    environ: Mapping[str, str],
    runner: Callable[[Coroutine[Any, Any, None]], None],
) -> int:
    instance = load(instance_dir)
    scheduler = build_scheduler(instance, environ)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        triggered = scheduler.state().pipelines
        if scheduler.mode == "builtin":
            echo(
                f"horae serve: built-in scheduler, {len(triggered)} pipeline(s) with a trigger",
                err=True,
            )
            for entry in triggered:
                echo(f"  {entry.pipeline}: {entry.cron} ({entry.timezone})", err=True)
        else:
            echo("horae serve: external scheduler, no trigger fires (use `horae run`)", err=True)
        runner(serve_until_signal(scheduler))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    echo("horae serve: stopped", err=True)
    return EXIT_OK


def serve(
    instance: Annotated[
        Path,
        typer.Option(
            "--instance",
            "-i",
            envvar="HORAE_INSTANCE",
            help="Instance directory (holds horae.yaml).",
            file_okay=False,
        ),
    ] = Path(),
) -> None:
    """Run the long-lived Horae process: the built-in scheduler, until SIGTERM or SIGINT.

    In builtin mode (horae.yaml scheduler.mode, or HORAE_SCHEDULER=builtin) every pipeline
    with a trigger runs at its cron fire times, in its trigger's time zone (else the
    instance's). Fire times missed while Horae was stopped are not caught up; a fire time
    while the pipeline is still running (here or in a manual `horae run`) is skipped and
    logged. With HORAE_SCHEDULER=external (or scheduler.mode: external) no trigger fires: an
    external scheduler calls `horae run <pipeline>`. Each run pings its push monitor
    (scheduler.monitors) up or down; a failed ping never fails the run.

    SIGTERM or SIGINT stops it: runs in progress are cancelled and saved, then it exits 0.
    Exit codes: 0 stopped, 1 configuration problem, 2 usage error, 70 internal error."""
    run_command("serve", False, lambda: _serve(instance, os.environ, asyncio.run))


__all__ = ["build_scheduler", "run_scheduled", "serve", "serve_until_signal"]
