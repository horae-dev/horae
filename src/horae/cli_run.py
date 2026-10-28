# SPDX-License-Identifier: Apache-2.0
"""`horae run <pipeline> [--dry-run]` (#26): one run through the plugin host.

The instance is loaded and its plugins checked (`<instance>/plugins`, `horae.cli_host`)
before anything starts; then `open_ports` starts the plugin host and the runner
(`horae.runner.Runner`) runs the pipeline: lock, recovery, execute, apply through the
plugins' sinks (`McpSinks`, prepared by the core), persist. A dry run applies nothing and
shows the change set it would apply (`plan_changes`, rendered like `horae diff`).

**Signals.** SIGINT (Ctrl-C) and SIGTERM cancel the run cooperatively: the runner saves its
partial record (`failed`, kind `cancelled`) and the command exits 128 + the signal (130,
143). A second signal while it stops is the same cancellation (the runner's grace period
still bounds it).

**LLM.** M1 wires no LLM backend: a pipeline with an `llm` step fails with
`llm_unavailable` (exit 1). M2 adds the backends.

Exit codes (`horae.cli_output`): 0 the run finished `ok`, `noop` or `degraded` (a dry run:
planned without problems), 1 configuration, plugin, store or runtime problem (the run
`failed` or timed out, or was lost), 2 usage error (a malformed `--var`), 3 unknown
pipeline, 4 a dry run planned with problems (some intents cannot be applied), 5 the
pipeline is already running or the apply lock was busy, 70 internal error, 130 / 143
interrupted.
"""

import asyncio
import re
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.apply import ChangeSet, plan_changes, render_text, to_dict
from horae.cli_host import live_ports, load
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    SCHEMA_VERSION,
    CommandFailed,
    echo_json,
    run_command,
    safe,
)
from horae.config import Instance
from horae.contracts import RunRecord
from horae.engine.template import BUILTIN_VARS
from horae.runner import LocksUnsupported, PipelineLocked, RunLost, Runner, RunTimedOut
from horae.store import Store, StoreError

_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_APPLY_LOCKED = "apply_locked:"
_IN_RECORD = frozenset({"run_failed", "apply_locked"})
"""Error kinds whose message is the record's error (printed with the record)."""


def parse_vars(values: list[str]) -> dict[str, str]:
    """`--var KEY=VALUE` options as template variables (`CommandFailed(usage)` otherwise)."""
    parsed: dict[str, str] = {}
    for value in values:
        key, sep, text = value.partition("=")
        if not sep or not _VAR_NAME.fullmatch(key):
            raise CommandFailed(
                EXIT_USAGE, "usage", f"--var {value!r}: expected KEY=VALUE, KEY an identifier"
            )
        if key in BUILTIN_VARS:
            raise CommandFailed(EXIT_USAGE, "usage", f"--var {key}: a built-in variable")
        if len(text) > 4096:
            raise CommandFailed(EXIT_USAGE, "usage", f"--var {key}: longer than 4096 characters")
        parsed[key] = text
    return parsed


class Interrupted(Exception):
    """The run was cancelled by a signal (`signum`)."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


async def cancel_on_signals[T](work: Coroutine[Any, Any, T]) -> T:
    """Await `work` in a task that SIGINT and SIGTERM cancel; then raise `Interrupted`.

    Outside the main thread (no signal handlers there) `work` is just awaited."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    received: list[int] = []

    def stop(signum: int) -> None:
        received.append(signum)
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
class _Run:
    """What one `horae run` produced."""

    saved: list[RunRecord] = field(default_factory=list)
    changes: ChangeSet | None = None


async def _execute(
    instance: Instance, name: str, dry_run: bool, vars: dict[str, str], out: _Run
) -> RunRecord:
    async with live_ports(instance) as live:
        with Store.open(instance.store_path) as store:
            runner = Runner(instance, store, live.ports)
            record = await runner.run(name, dry_run=dry_run, vars=vars, on_record=out.saved.append)
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
        text += render_text(changes)
    return text


def _emit(
    json_output: bool,
    record: RunRecord | None,
    changes: ChangeSet | None,
    kind: str | None,
    message: str | None,
) -> None:
    """The result of a run that started: its record (if saved) and its error (if any)."""
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "run": None if record is None else _run_dict(record),
                "plan": None if changes is None else to_dict(changes),
                "error": None if kind is None else {"kind": kind, "message": message or ""},
            }
        )
        return
    if record is not None:
        typer.echo(_render(record, changes), nl=False)
    if kind is None:
        return
    if record is not None and kind in _IN_RECORD:
        # The record's error is already printed above: the kind only, not the error twice.
        typer.echo(f"horae run: {kind}", err=True)
    else:
        typer.echo(f"horae run: {safe(message or kind)}", err=True)


def _run(
    instance_dir: Path,
    name: str,
    dry_run: bool,
    var: list[str],
    json_output: bool,
    runner: Callable[[Coroutine[Any, Any, RunRecord]], RunRecord],
) -> int:
    vars = parse_vars(var)
    instance = load(instance_dir)
    if name not in instance.pipelines:
        known = ", ".join(sorted(instance.pipelines)) or "none"
        raise CommandFailed(
            EXIT_REFUSED, "unknown_pipeline", f"unknown pipeline {name!r} (pipelines: {known})"
        )
    out = _Run()
    try:
        record = runner(cancel_on_signals(_execute(instance, name, dry_run, vars, out)))
    except Interrupted as stop:
        saved = out.saved[-1] if out.saved else None
        what = "saved as failed (cancelled)" if saved is not None else "before it started"
        name_ = signal.Signals(stop.signum).name
        _emit(json_output, saved, None, "cancelled", f"interrupted by {name_}: the run {what}")
        return 128 + stop.signum
    except RunTimedOut as timed_out:
        _emit(json_output, timed_out.record, None, "timeout", str(timed_out))
        return EXIT_PROBLEMS
    except PipelineLocked as locked:
        raise CommandFailed(EXIT_LOCKED, "pipeline_locked", str(locked)) from None
    except RunLost as lost:
        saved = out.saved[-1] if out.saved else None
        _emit(json_output, saved, None, "run_lost", str(lost))
        return EXIT_PROBLEMS
    except LocksUnsupported as exc:
        raise CommandFailed(EXIT_PROBLEMS, "locks_unsupported", str(exc)) from None
    except (StoreError, OSError) as exc:
        raise CommandFailed(
            EXIT_PROBLEMS, "store_error", f"{instance.store_path}: {type(exc).__name__}: {exc}"
        ) from None
    code, kind = _record_code(record, out.changes)
    message = None
    if kind == "planned_with_problems":
        message = "some intents cannot be applied (see the plan)"
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
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Run a pipeline once through the instance's plugins, and apply its intents.

    With --dry-run nothing is applied: the run is recorded (dry run) and the change set it
    would apply is shown, as `horae diff` shows it. SIGINT and SIGTERM cancel the run; its
    partial record is saved. No LLM backend in M1: a pipeline with an llm step fails with
    llm_unavailable.

    Exit codes: 0 ok, noop or degraded (dry run: planned without problems), 1 configuration,
    plugin, store or runtime problem (the run failed or timed out), 2 usage error, 3 unknown
    pipeline, 4 dry run planned with problems, 5 pipeline running or apply lock busy, 70
    internal error, 130/143 interrupted by SIGINT/SIGTERM. With --json the result is
    {schema_version, run, plan, error}; an error before the run is {schema_version, error}."""
    run_command(
        "run",
        json_output,
        lambda: _run(instance, pipeline, dry_run, var or [], json_output, asyncio.run),
    )


__all__ = ["Interrupted", "cancel_on_signals", "parse_vars", "run"]
