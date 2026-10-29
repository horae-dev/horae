# SPDX-License-Identifier: Apache-2.0
"""`horae serve` (#39): the long-running process of a container, with the built-in cron.

It loads the instance **once** (a change to `horae.yaml`, a pipeline or the lock needs a
restart) and runs `horae.scheduler.Scheduler` until SIGTERM or SIGINT. In `builtin` mode
(`horae.yaml` `scheduler.mode`, overridden by `HORAE_SCHEDULER`) each pipeline with a
`trigger` runs at its fire times, in this process, exactly as `horae run <pipeline>` would
run it (same plugins, store, per-pipeline lock and apply lock), at most
`scheduler.max_concurrent` at once, and its push monitor is pinged after each run. In
`external` mode nothing fires: the process only waits (an external scheduler calls `horae
run`). In both modes it also serves the HTTP API (#37, schemas decision 51, `horae.api`) in
the same event loop: `GET /healthz` (store and `Scheduler.state()`), `GET /runs`, `GET
/runs/<id>`, read-only and without authentication of its own, on `--host:--port`
(`HORAE_HOST`, `HORAE_PORT`; `127.0.0.1:8080` by default; the image sets `0.0.0.0` for the
reverse proxy, whose SSO middleware guards it). The store is created and migrated once at
start, so `/healthz` reports it from the first request.

**Stopping.** The first SIGTERM or SIGINT stops the scheduler and the HTTP server (which
waits up to 5 s for requests in progress): no new run starts, a run in
progress is cancelled as `horae run` cancels one (its record saved: `failed` before the
apply, its real status once an apply in progress has finished) and reported with the status
it was saved with, its monitor pinged accordingly; a run stopped before any record was saved
is reported `cancelled` and pings nothing. The command then exits 0 (a requested stop is the
normal end of a server). A second signal while it stops no longer waits for the reports and
pings: the runs still finish saving their records (bounded by the runner's grace period, 5 s,
or for an apply by its sink calls' timeouts), then the command exits 128 + that signal (130,
143). A container's stop timeout should leave room for both: at least 20 s (compose
`stop_grace_period: 20s`), more when applies are long.

Scheduler events (a run's outcome, a crashed run, a skipped fire time, a failed ping) are
logged on stderr, one line each, redacted like every log record. `horae serve` has no
`--json`: it is a process, not a report.

Exit codes: 0 stopped by a signal, 1 configuration problem (an invalid instance, an invalid
`HORAE_SCHEDULER`, an address that cannot be bound: `bind_failed`), 2 usage error, 70
internal error (also: the HTTP server ended on its own), 130 / 143 forced by a second SIGINT
/ SIGTERM.
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from horae.cli_host import load
from horae.cli_output import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CommandFailed,
    echo,
    internal_message,
    run_command,
)
from horae.cli_run import RunCapture, abandoned_during_apply, execute_run
from horae.config import Instance
from horae.runner import LocksUnsupported, PipelineLocked, RunLost, RunTimedOut
from horae.scheduler import (
    CANCELLED,
    LOCKED,
    ModeError,
    PingResult,
    RunOutcome,
    Scheduler,
    ping,
    scheduler_mode,
    schedules,
)
from horae.store import Store, StoreError

if TYPE_CHECKING:  # imported when serving only: the other commands never load FastAPI
    from horae.api import ApiServer

logger = logging.getLogger("horae.scheduler")

DEFAULT_HOST = "127.0.0.1"
"""The HTTP API listens on loopback unless told otherwise (`--host`, `HORAE_HOST`)."""
DEFAULT_PORT = 8080

_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def run_scheduled(instance: Instance, name: str) -> RunOutcome:
    """One scheduled run of `name`, as `horae run <name>` runs it, reported as an outcome
    (`LOCKED` when the pipeline is running elsewhere).

    When cancelled (`Scheduler.stop()`), it returns rather than raise: the status and id of
    the record the run saved (a run cancelled during its shielded apply finishes it and is
    saved with its real status), or `CANCELLED` (with the run's id, if it began) when no
    record was saved."""
    out = RunCapture()
    try:
        record = await execute_run(instance, name, False, {}, out)
    except asyncio.CancelledError:
        if out.saved:
            return RunOutcome(out.saved[-1].status, out.saved[-1].id)
        return RunOutcome(CANCELLED, out.run_id)
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
    except Exception as exc:
        # As `horae run` reports a crash: the run's id, never the exception's message.
        crashed = internal_message(exc)
        del exc
        if out.run_id is not None and (note := abandoned_during_apply(instance, out.run_id)):
            crashed += f"; {note}"
        logger.error("scheduler: run %s of %s crashed: %s", out.run_id or "-", name, crashed)
        return RunOutcome("internal", out.run_id)
    return RunOutcome(record.status, record.id)


async def serve_until_signal(
    scheduler: Scheduler,
    on_ready: Callable[[], object] | None = None,
    *,
    api: "ApiServer | None" = None,
) -> int:
    """Run `scheduler`, and the HTTP API server `api` if given, until SIGTERM or SIGINT; the
    exit code.

    The first signal `stop()`s the scheduler and asks the API server to stop (it closes its
    socket and waits up to `GRACEFUL_SHUTDOWN_S` for requests in progress); exit 0 once both
    have stopped. A second `force_stop()`s the scheduler and the server (exit 128 + that
    signal); later ones are ignored. `on_ready` is called once the handlers are installed
    (tests). Outside the main thread (no signal handlers there) it runs until `stop()`. If
    the API server ends on its own (it should not), the scheduler is stopped too and the exit
    code is 70."""
    loop = asyncio.get_running_loop()
    received: list[int] = []

    def handle(signum: int) -> None:
        received.append(signum)
        name = signal.Signals(signum).name
        if len(received) == 1:
            logger.info("scheduler: %s: stopping (runs in progress are cancelled)", name)
            scheduler.stop()
            if api is not None:
                api.request_stop()
        elif len(received) == 2:
            logger.warning("scheduler: second %s: not waiting for the reports and pings", name)
            scheduler.force_stop()
            if api is not None:
                api.request_stop(force=True)

    installed: list[signal.Signals] = []
    try:
        for signum in _SIGNALS:
            try:
                loop.add_signal_handler(signum, handle, signum)
            except (RuntimeError, ValueError, NotImplementedError):  # pragma: no cover
                break
            installed.append(signum)
        if not await _run(scheduler, api, on_ready):
            return EXIT_INTERNAL
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
    return 128 + received[1] if len(received) > 1 else EXIT_OK


async def _run(
    scheduler: Scheduler, api: "ApiServer | None", on_ready: Callable[[], object] | None
) -> bool:
    """Run the scheduler and the API server until both have stopped; `False` when the server
    ended on its own (it was not asked to stop) or crashed. `on_ready` is called once both
    are scheduled to start."""
    scheduling = asyncio.create_task(scheduler.run_forever())
    if api is None:
        if on_ready is not None:
            on_ready()
        await scheduling
        return True
    serving = asyncio.create_task(api.run_until_stopped())
    if on_ready is not None:
        on_ready()
    try:
        await asyncio.wait([scheduling, serving], return_when=asyncio.FIRST_COMPLETED)
        unexpected = serving.done() and not api.should_exit
        if serving.done() and (unexpected or serving.exception() is not None):
            failure = serving.exception()
            logger.error(
                "api: the HTTP server stopped unexpectedly (%s); stopping",
                type(failure).__name__ if failure is not None else "no error",
            )
            del failure
            scheduler.stop()
            await scheduling
            return False
        if scheduling.done():  # stopped by `stop()`: the server follows
            api.request_stop()
        await asyncio.gather(scheduling, serving)
        return True
    finally:
        for task in (scheduling, serving):
            if not task.done():
                task.cancel()
        await asyncio.gather(scheduling, serving, return_exceptions=True)


def build_scheduler(instance: Instance, environ: Mapping[str, str] | None = None) -> Scheduler:
    """The instance's scheduler (`CommandFailed(config_invalid)` on a bad `HORAE_SCHEDULER`)."""
    config = instance.config.scheduler
    try:
        mode = scheduler_mode(config.mode, environ)
    except ModeError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", str(exc)) from None

    async def run(name: str) -> RunOutcome:
        return await run_scheduled(instance, name)

    async def report(name: str, outcome: RunOutcome, duration_s: float) -> PingResult:
        return await ping(instance, name, outcome, duration_s)

    return Scheduler(
        schedules(instance),
        mode=mode,
        run=run,
        ping=report,
        max_concurrent=config.max_concurrent,
        misfire_grace_s=config.misfire_grace_s,
    )


def prepare_store(instance: Instance) -> None:
    """Create and migrate the store once at start, so that `/healthz` reports it from the
    first request (each run opens it again). A failure is a warning: `/healthz` then reports
    the store as an error and the runs fail with their own `store_error`."""
    try:
        Store.open(instance.store_path).close()
    except StoreError as exc:
        logger.warning("api: the store cannot be opened (%s)", type(exc).__name__)
        del exc
    except OSError as exc:
        logger.warning("api: the store cannot be opened (%s)", exc.strerror or type(exc).__name__)
        del exc


def build_api(
    instance: Instance,
    scheduler: Scheduler,
    host: str,
    port: int,
    allowed_hosts: Sequence[str] = (),
) -> "ApiServer":
    """The API server, bound to `host:port` (`CommandFailed(bind_failed)` if it cannot be),
    accepting the `Host` values `allowed_hosts` besides loopback (`CommandFailed(usage)` for
    one that is not a DNS name or an IP address). A bind address other than a wildcard
    (`0.0.0.0`, `::`) is accepted as a `Host` too."""
    from horae.api import ApiContext, BindError, allowed_host, bind, build_server, create_app

    extra = [*allowed_hosts]
    if host not in ("0.0.0.0", "::", ""):  # noqa: S104 (compared, not bound)
        extra.append(host)
    try:
        hosts = frozenset(allowed_host(value) for value in extra)
    except ValueError as exc:
        raise CommandFailed(EXIT_USAGE, "usage", f"--allowed-host: {exc}") from None
    try:
        sock = bind(host, port)
    except BindError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "bind_failed", str(exc)) from None
    context = ApiContext(instance.store_path, instance.state_dir, scheduler.state, hosts)
    return build_server(create_app(context), sock)


def _serve(
    instance_dir: Path,
    environ: Mapping[str, str],
    runner: Callable[[Coroutine[Any, Any, int]], int],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allowed_hosts: Sequence[str] = (),
) -> int:
    instance = load(instance_dir)
    scheduler = build_scheduler(instance, environ)
    api = build_api(instance, scheduler, host, port, allowed_hosts)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        triggered = scheduler.state().pipelines
        if scheduler.mode == "builtin":
            echo(
                f"horae serve: built-in scheduler, {len(triggered)} pipeline(s) with a trigger, "
                f"at most {scheduler.max_concurrent} run(s) at once",
                err=True,
            )
            for entry in triggered:
                echo(f"  {entry.pipeline}: {entry.cron} ({entry.timezone})", err=True)
        else:
            echo("horae serve: external scheduler, no trigger fires (use `horae run`)", err=True)
        prepare_store(instance)
        bound_host, bound_port = api.address
        shown = f"[{bound_host}]" if ":" in bound_host else bound_host
        echo(f"horae serve: HTTP API on http://{shown}:{bound_port}", err=True)
        code = runner(serve_until_signal(scheduler, api=api))
    finally:
        api.sock.close()
        logger.removeHandler(handler)
        logger.setLevel(previous)
    echo("horae serve: stopped" + ("" if code == EXIT_OK else " (forced)"), err=True)
    return code


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
    host: Annotated[
        str,
        typer.Option(
            "--host",
            envvar="HORAE_HOST",
            help="Address the HTTP API listens on (0.0.0.0 in a container, behind the proxy).",
        ),
    ] = DEFAULT_HOST,
    port: Annotated[
        int,
        typer.Option("--port", envvar="HORAE_PORT", min=0, max=65535, help="Port of the HTTP API."),
    ] = DEFAULT_PORT,
    allowed_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            envvar="HORAE_ALLOWED_HOSTS",
            help=(
                "A Host the HTTP API accepts besides localhost, 127.0.0.1 and ::1: the "
                "proxy's public hostname (repeatable; HORAE_ALLOWED_HOSTS: space-separated)."
            ),
        ),
    ] = None,
) -> None:
    """Run the long-lived Horae process: the built-in scheduler and the HTTP API, until
    SIGTERM or SIGINT.

    The HTTP API (GET /healthz, GET /runs, GET /runs/<id>; read-only, no authentication of
    its own) listens on --host:--port, 127.0.0.1:8080 by default. Never expose it without
    the reverse proxy and its SSO middleware. A request whose Host header is not localhost,
    127.0.0.1, ::1 or an --allowed-host is refused (400): set the proxy's public hostname.

    In builtin mode (horae.yaml scheduler.mode, or HORAE_SCHEDULER=builtin) every pipeline
    with a trigger runs at its cron fire times, in its trigger's time zone (else the
    instance's), at most scheduler.max_concurrent at once (default 1; the others wait their
    turn). Fire times missed while Horae was stopped are not caught up; a fire time while the
    pipeline is still running (here or in a manual `horae run`), or more than
    scheduler.misfire_grace_s late, is skipped and logged. With HORAE_SCHEDULER=external (or
    scheduler.mode: external) no trigger fires: an external scheduler calls `horae run
    <pipeline>`. Each run pings its push monitor (scheduler.monitors) up or down; a failed
    ping never fails the run. The instance is read once: restart after changing it.

    SIGTERM or SIGINT stops it: runs in progress are cancelled and saved, then it exits 0; a
    second signal stops waiting for the pings (exit 130/143). Allow it at least 20 s to stop.
    Exit codes: 0 stopped, 1 configuration problem (also: the address cannot be bound), 2
    usage error, 70 internal error, 130/143 forced by a second SIGINT/SIGTERM."""
    run_command(
        "serve",
        False,
        lambda: _serve(instance, os.environ, asyncio.run, host, port, allowed_host or ()),
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "build_api",
    "build_scheduler",
    "prepare_store",
    "run_scheduled",
    "serve",
    "serve_until_signal",
]
