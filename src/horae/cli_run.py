# SPDX-License-Identifier: Apache-2.0
"""`horae run <pipeline> [--dry-run] [--no-cache]` (#26): one run through the plugin host.

The instance is loaded and its plugins checked (`<instance>/plugins`, `horae.cli_host`)
before anything starts; then `open_ports` starts the plugin host and the runner
(`horae.runner.Runner`) runs the pipeline: lock, recovery, execute, apply through the
plugins' sinks (`McpSinks`, prepared by the core), persist. A dry run applies nothing and
shows the change set it would apply (`plan_changes`, rendered like `horae diff`).

**LLM cache** (#32, schemas decision 45). An `llm` step whose cache key (skill, version and
digest, plugin, model, viewed input) matches a live entry of the store's LLM cache reuses that
output without calling the model (`$0`). `--no-cache` skips the lookup, for debugging: the
model is called, and its accepted output still replaces the entry. A dry run reads the cache
but never writes it.

**Signals.** SIGINT (Ctrl-C) and SIGTERM cancel the run cooperatively and the command exits
128 + the signal (130, 143), naming the run. During the engine phase the runner saves its
partial record (`failed`, kind `cancelled`). During the apply phase the apply is shielded
(schemas decision 37): it finishes (each sink call bounded by `call_timeout_s`), the record
is saved with its real status, and the run is then reported interrupted. A second signal
while the run stops is ignored (the runner's grace period still bounds the stop). If the
run is nonetheless abandoned during the apply (a crash), the command says so and names
`horae undo <id>`.

**LLM.** When a pipeline of the instance has an `llm` step, `open_ports` gives the runner the
instance's model aliases (`horae.llm.ModelBackends`, #28): the alias' backend is built, and
its declared secret resolved, on the step's first call. A backend that is not available yet
fails the step with `backend_unavailable`, and a skill whose prompt is not loaded with
`skill_unavailable` (until #27); the run is then `degraded`.

**Push monitor** (#39, schemas decision 49 F). When `horae.yaml` `scheduler.monitors` has
an entry for the pipeline, its push URL (a secret) is pinged once the run has ended: `up` for
`ok`, `noop` and `degraded`, `down` for anything else (failed, timed out, lost, crashed), and
`down` too when the run could not begin (the plugins do not match the lock, a store, lock or
I/O problem, an internal error: the message is the error kind). An interrupted run pings with
the status of the record it saved (an apply that finished: its real status), within
`INTERRUPTED_PING_S`; one stopped before any record was saved pings nothing (the monitor's
own heartbeat window reports it). Not for a dry run, nor when the pipeline is already running
(the run in progress pings). A failed ping is a warning on stderr and never changes the exit
code.

Exit codes (`horae.cli_output`): 0 the run finished `ok`, `noop` or `degraded` (a dry run:
planned without problems), 1 configuration, plugin, store or runtime problem (the run
`failed` or timed out, or was lost; a dry run whose plan could not start a plugin), 2
usage error (a malformed `--var`, more than `MAX_VARS` of them), 3 unknown pipeline, 4 a
dry run planned with problems (some intents cannot be applied), 5 the pipeline is already
running or the apply lock was busy, 70 internal error, 130 / 143
interrupted.
"""

import asyncio
import logging
import re
import signal
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.apply import ChangeSet, plan_changes, render_text, to_dict
from horae.cli_host import live_ports, load
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_INTERNAL,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    SCHEMA_VERSION,
    CommandFailed,
    echo,
    echo_json,
    internal_message,
    io_failed,
    run_command,
    safe,
)
from horae.config import Instance
from horae.contracts import RunRecord
from horae.contracts.types import MAX_ITEMS
from horae.engine.template import BUILTIN_VARS
from horae.runner import LocksUnsupported, PipelineLocked, RunLost, Runner, RunTimedOut
from horae.scheduler import PingResult, RunOutcome, ping
from horae.store import Store, StoreError

_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MAX_VARS = MAX_ITEMS - 1
"""At most this many `--var` options (`RunContext.vars` holds at most `MAX_ITEMS`, the
built-in `date` included)."""
MAX_VAR_CHARS = 4096
"""At most this many characters in a `--var` value."""
_SHOWN_NAME = 64
"""Characters of a malformed `--var`'s name echoed (never its value)."""
PLUGIN_START_KINDS = frozenset(
    {"plugin_unavailable", "spawn_failed", "startup_timeout", "handshake_failed"}
)
"""Plan problem kinds of a sink plugin that could not start: a dry run with one exits 1."""
_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_APPLY_LOCKED = "apply_locked:"
_IN_RECORD = frozenset({"run_failed", "apply_locked"})
"""Error kinds whose message is the record's error (printed with the record)."""
INTERRUPTED_PING_S = 2.0
"""Seconds the push-monitor ping of an interrupted run may take (the process is stopping)."""
_monitor_log = logging.getLogger("horae.scheduler.monitor")


def parse_vars(values: list[str]) -> dict[str, str]:
    """`--var KEY=VALUE` options as template variables (`CommandFailed(usage)` otherwise).

    An error echoes the name part only (before `=`, truncated), never a value."""
    if len(values) > MAX_VARS:
        raise CommandFailed(EXIT_USAGE, "usage", f"--var: at most {MAX_VARS} variables")
    parsed: dict[str, str] = {}
    for value in values:
        key, sep, text = value.partition("=")
        if not sep or not _VAR_NAME.fullmatch(key):
            shown = key[:_SHOWN_NAME] + ("..." if len(key) > _SHOWN_NAME else "")
            raise CommandFailed(
                EXIT_USAGE, "usage", f"--var {shown!r}: expected KEY=VALUE, KEY an identifier"
            )
        if key in BUILTIN_VARS:
            raise CommandFailed(EXIT_USAGE, "usage", f"--var {key}: a built-in variable")
        if len(text) > MAX_VAR_CHARS:
            raise CommandFailed(
                EXIT_USAGE, "usage", f"--var {key}: longer than {MAX_VAR_CHARS} characters"
            )
        parsed[key] = text
    return parsed


class Interrupted(Exception):
    """The run was cancelled by a signal (`signum`)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


async def cancel_on_signals[T](work: Coroutine[Any, Any, T]) -> T:
    """Await `work` in a task that SIGINT and SIGTERM cancel; then raise `Interrupted`.

    Only the first signal cancels: a second one while the work stops is ignored (the
    runner's grace period bounds the stop). Outside the main thread (no signal handlers
    there) `work` is just awaited."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    received: list[int] = []

    def stop(signum: int) -> None:
        received.append(signum)
        if len(received) == 1:
            task.cancel()

    installed: list[signal.Signals] = []
    try:
        for signum in _SIGNALS:
            try:
                loop.add_signal_handler(signum, stop, signum)
            except (RuntimeError, ValueError, NotImplementedError):  # pragma: no cover
                break
            installed.append(signum)
        try:
            return await task
        except asyncio.CancelledError:
            if not received:
                raise
            raise Interrupted(received[0]) from None
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


@dataclass
class RunCapture:
    """What one run produced: `horae run`, or a scheduled run of `horae serve`."""

    saved: list[RunRecord] = field(default_factory=list)
    changes: ChangeSet | None = None
    run_id: str | None = None
    """The run's id, as soon as its row exists (`Runner.run(on_begin=)`)."""
    outcome: str | None = None
    """How the run ended, for its push monitor (`horae.scheduler.RunOutcome.status`; `None`:
    no ping)."""
    interrupted: bool = False

    def begin(self, run_id: str) -> None:
        self.run_id = run_id


async def execute_run(
    instance: Instance,
    name: str,
    dry_run: bool,
    vars: dict[str, str],
    out: RunCapture,
    cache: bool = True,
) -> RunRecord:
    """Run `name` once through the instance's plugins (`live_ports`), its records and id
    captured in `out`. Raises what `Runner.run` raises (a cancellation included)."""
    async with live_ports(instance) as live:
        with Store.open(instance.store_path) as store:
            runner = Runner(instance, store, live.ports)
            record = await runner.run(
                name,
                dry_run=dry_run,
                vars=vars,
                on_record=out.saved.append,
                on_begin=out.begin,
                cache=cache,
            )
            if dry_run:

                def notified(key: str) -> bool:
                    return store.get_notification(key) is not None

                out.changes = await plan_changes(
                    record,
                    live.sinks,
                    notified=notified,
                    call_timeout_s=instance.config.plugin_defaults.call_timeout_s,
                )
            return record


def _run_dict(record: RunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "pipeline": record.pipeline,
        "status": record.status,
        "dry_run": record.dry_run,
        "started_at": record.started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": record.finished_at.isoformat().replace("+00:00", "Z"),
        "intents": len(record.intents),
        "changes": [{"intent": c.intent, "target": c.target} for c in record.changes],
        "cost": {
            "usd": record.cost.usd,
            "tokens_in": record.cost.tokens_in,
            "tokens_out": record.cost.tokens_out,
        },
        "violations": [v.kind for v in record.violations],
        "error_kinds": _error_kinds(record),
        "error": record.error,
    }


def _error_kinds(record: RunRecord) -> list[str]:
    """The error kinds of the run's failed steps, in step order, without duplicates."""
    kinds = [step.error.kind for step in record.steps if step.error is not None]
    return list(dict.fromkeys(kinds))


def _record_code(record: RunRecord, changes: ChangeSet | None) -> tuple[int, str | None]:
    """The exit code and error kind of a saved record."""
    if record.status == "failed":
        error = record.error or ""
        if error.startswith(_APPLY_LOCKED):
            return EXIT_LOCKED, "apply_locked"
        return EXIT_PROBLEMS, "run_failed"
    if changes is not None and any(p.kind in PLUGIN_START_KINDS for p in changes.problems):
        return EXIT_PROBLEMS, "plugin_unavailable"
    if changes is not None and (changes.problems or changes.refused is not None):
        return EXIT_DIFFERENT, "planned_with_problems"
    return EXIT_OK, None


def _render(record: RunRecord, changes: ChangeSet | None) -> str:
    what = "dry run" if record.dry_run else "run"
    lines = [f"{what} {record.id}: {record.status}"]
    for change in record.changes:
        lines.append(f"  changed {safe(change.target)}")
    if record.error:
        kinds = _error_kinds(record)
        suffix = f" ({', '.join(kinds)})" if kinds else ""
        lines.append(f"  error: {safe(record.error)}{suffix}")
    text = "\n".join(lines) + "\n"
    if changes is not None:
        # The change set without its own header line: the run's line above says it.
        text += render_text(changes).split("\n", 1)[1]
    return text


def _emit(
    json_output: bool,
    record: RunRecord | None,
    changes: ChangeSet | None,
    kind: str | None,
    message: str | None,
    run_id: str | None = None,
) -> None:
    """The result of a run that started: its id (when its row exists), its record (if
    saved) and its error (if any)."""
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": record.id if record is not None else run_id,
                "run": None if record is None else _run_dict(record),
                "plan": None if changes is None else to_dict(changes),
                "error": None if kind is None else {"kind": kind, "message": message or ""},
            }
        )
        return
    if record is not None:
        echo(_render(record, changes), nl=False)
    if kind is None:
        return
    if record is not None and kind in _IN_RECORD:
        # The record's error is already printed above: the kind only, not the error twice.
        echo(f"horae run: {kind}", err=True)
    else:
        echo(f"horae run: {safe(message or kind)}", err=True)


def abandoned_during_apply(instance: Instance, run_id: str) -> str | None:
    """ "abandoned during apply; run `horae undo <id>`" when the run's row says so."""
    try:
        with Store.open(instance.store_path, read_only=True) as store:
            summary = store.run_summary(run_id)
    except (StoreError, OSError):
        return None
    if summary is None or summary.abandoned_at is None:
        return None
    if "during apply" not in (summary.error or ""):
        return None
    return f"run {run_id} abandoned during apply; run `horae undo {run_id}`"


def _interrupted(
    instance: Instance, out: RunCapture, signum: int
) -> tuple[RunRecord | None, None, str, str, str | None]:
    """What `_emit` prints for a run interrupted by `signum`."""
    saved = out.saved[-1] if out.saved else None
    signame = signal.Signals(signum).name
    if saved is not None and saved.status != "failed":
        done = "finished its apply" if saved.changes else "had finished"
        what = f"run {saved.id} {done} and was saved ({saved.status})"
    elif saved is not None:
        what = f"run {saved.id} was saved as failed (cancelled)"
    elif out.run_id is not None:
        note = abandoned_during_apply(instance, out.run_id)
        what = note or f"run {out.run_id} was abandoned (see `horae runs`)"
    else:
        what = "the run had not started"
    return saved, None, "cancelled", f"interrupted by {signame}: {what}", out.run_id


def _run(
    instance_dir: Path,
    name: str,
    dry_run: bool,
    var: list[str],
    json_output: bool,
    runner: Callable[[Coroutine[Any, Any, Any]], Any],
    cache: bool = True,
) -> int:
    vars = parse_vars(var)
    instance = load(instance_dir)
    if name not in instance.pipelines:
        known = ", ".join(sorted(instance.pipelines)) or "none"
        raise CommandFailed(
            EXIT_REFUSED, "unknown_pipeline", f"unknown pipeline {name!r} (pipelines: {known})"
        )
    out = RunCapture()
    started = time.monotonic()
    try:
        code = _attempt(instance, name, dry_run, vars, json_output, runner, cache, out)
    except CommandFailed as failed:
        if failed.kind != "pipeline_locked":
            out.outcome = failed.kind
        raise
    except Exception:
        out.outcome = "internal"
        raise
    finally:
        monitored = name in instance.config.scheduler.monitors
        if monitored and not dry_run and out.outcome is not None:
            outcome = RunOutcome(out.outcome, out.run_id)
            work = ping(instance, name, outcome, time.monotonic() - started)
            runner(_bounded_ping(work, name, outcome) if out.interrupted else work)
    return code


async def _bounded_ping(
    work: Coroutine[Any, Any, PingResult], name: str, outcome: RunOutcome
) -> PingResult:
    """`work` (a ping), given at most `INTERRUPTED_PING_S`."""
    try:
        async with asyncio.timeout(INTERRUPTED_PING_S):
            return await work
    except TimeoutError:
        _monitor_log.warning(
            "push monitor of pipeline %s: the %s ping was not sent within %g s of the "
            "interruption; the run is not affected",
            name,
            "up" if outcome.up else "down",
            INTERRUPTED_PING_S,
        )
        return "failed"


def _attempt(
    instance: Instance,
    name: str,
    dry_run: bool,
    vars: dict[str, str],
    json_output: bool,
    runner: Callable[[Coroutine[Any, Any, Any]], Any],
    cache: bool,
    out: RunCapture,
) -> int:
    """One run of `name`; sets `out.outcome` (not when the pipeline is locked, nor for a run
    interrupted before any record was saved)."""
    try:
        record: RunRecord = runner(
            cancel_on_signals(execute_run(instance, name, dry_run, vars, out, cache))
        )
    except Interrupted as stop:
        out.interrupted = True
        if out.saved:  # the status it was saved with (an apply that finished: its own)
            out.outcome, out.run_id = out.saved[-1].status, out.saved[-1].id
        _emit(json_output, *_interrupted(instance, out, stop.signum))
        return 128 + stop.signum
    except RunTimedOut as timed_out:
        out.outcome = "timeout"
        _emit(json_output, timed_out.record, None, "timeout", str(timed_out), out.run_id)
        return EXIT_PROBLEMS
    except PipelineLocked as locked:
        raise CommandFailed(EXIT_LOCKED, "pipeline_locked", str(locked)) from None
    except RunLost as lost:
        out.outcome = "run_lost"
        saved = out.saved[-1] if out.saved else None
        _emit(json_output, saved, None, "run_lost", str(lost), out.run_id)
        return EXIT_PROBLEMS
    except LocksUnsupported as exc:
        raise CommandFailed(EXIT_PROBLEMS, "locks_unsupported", str(exc)) from None
    except StoreError as exc:
        raise CommandFailed(
            EXIT_PROBLEMS, "store_error", f"{instance.store_path}: {type(exc).__name__}: {exc}"
        ) from None
    except OSError as exc:
        raise io_failed(exc, instance.state_dir, store=instance.store_path) from None
    except Exception as exc:
        if out.run_id is None:
            raise
        # A crash after the run began: its id, and how to undo an abandoned apply.
        crashed = internal_message(exc)
        del exc
        out.outcome = "internal"
        note = abandoned_during_apply(instance, out.run_id)
        if note is not None:
            crashed += f"; {note}"
        _emit(json_output, None, None, "internal", crashed, out.run_id)
        return EXIT_INTERNAL
    out.outcome = record.status
    code, kind = _record_code(record, out.changes)
    message = None
    if kind == "planned_with_problems":
        message = "some intents cannot be applied (see the plan)"
    elif kind == "plugin_unavailable":
        message = "a plugin could not start, so the plan is incomplete (see the plan)"
    elif kind is not None:
        kinds = _error_kinds(record)
        message = (record.error or "the run failed") + (f" ({', '.join(kinds)})" if kinds else "")
    _emit(json_output, record, out.changes, kind, message)
    return code


def run(
    pipeline: Annotated[str, typer.Argument(help="The pipeline to run (its file name).")],
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
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Run without applying anything; show the planned diff."),
    ] = False,
    var: Annotated[
        list[str] | None,
        typer.Option("--var", help="A template variable, KEY=VALUE (repeatable)."),
    ] = None,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Call the LLM even on unchanged input (skip the cache lookup; for debugging).",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Run a pipeline once through the instance's plugins, and apply its intents.

    With --dry-run nothing is applied: the run is recorded (dry run) and the change set it
    would apply is shown, as `horae diff` shows it. SIGINT and SIGTERM cancel the run (a
    second signal is ignored): before the apply its partial record is saved failed; during
    the apply, the apply finishes and the record is saved before the run is reported
    interrupted. An llm step calls the backend of its model alias (horae.yaml models), with
    the API key from the alias' declared secret. On unchanged input it reuses the cached
    output instead (no call, $0); --no-cache skips that lookup.

    Exit codes: 0 ok, noop or degraded (dry run: planned without problems), 1 configuration,
    plugin, store or runtime problem (the run failed or timed out; a dry run whose plan
    could not start a plugin), 2 usage error, 3 unknown pipeline, 4 dry run planned with
    problems, 5 pipeline running or apply lock busy, 70 internal error, 130/143 interrupted
    by SIGINT/SIGTERM. With --json the result is {schema_version, run_id, run, plan, error};
    an error before the run is {schema_version, error}."""
    run_command(
        "run",
        json_output,
        lambda: _run(
            instance, pipeline, dry_run, var or [], json_output, asyncio.run, not no_cache
        ),
    )


__all__ = [
    "INTERRUPTED_PING_S",
    "MAX_VARS",
    "PLUGIN_START_KINDS",
    "Interrupted",
    "RunCapture",
    "abandoned_during_apply",
    "cancel_on_signals",
    "execute_run",
    "parse_vars",
    "run",
]
