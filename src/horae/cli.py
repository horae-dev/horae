# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from horae import __version__
from horae.config import (
    CONFIG_FILE,
    STORE_FILE,
    InstanceError,
    ProblemsError,
    check_instance,
    load_instance,
    read_yaml,
    validate,
)
from horae.contracts import InstanceConfig, RunRecord
from horae.replay import RESULT_SCHEMA_VERSION, LockRegistry, ReplayResult, replay
from horae.runner import lock_is_free
from horae.store import MAX_LIST_RUNS, RunSummary, Store, StoreError

app = typer.Typer(
    name="horae",
    help="Horae: a predictable, self-hosted personal agent.",
    no_args_is_help=True,
    add_completion=False,
    # Never print local variables in tracebacks: they may hold secret values.
    pretty_exceptions_show_locals=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"horae {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Horae: a predictable, self-hosted personal agent."""


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 70
"""`EX_SOFTWARE` (sysexits.h): an unexpected internal error."""

REPORT_SCHEMA_VERSION = 1
"""Version of the `horae doctor --json` report."""


def _report(instance: Path, json_output: bool) -> int:
    result = check_instance(instance)
    if json_output:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": result.ok,
            "instance": str(instance),
            "problems": [problem.to_dict() for problem in result.problems],
        }
        typer.echo(json.dumps(report, indent=2))
    else:
        for problem in result.problems:
            typer.echo(str(problem))
        if result.ok and result.instance is not None:
            loaded = result.instance
            typer.echo(
                f"OK: {_plural(len(loaded.pipelines), 'pipeline')}, "
                f"{_plural(len(loaded.lock.plugins), 'plugin')}, "
                f"{_plural(len(loaded.config.models), 'model alias', 'model aliases')}"
            )
        else:
            typer.echo(_plural(len(result.problems), "problem") + " found")
    return EXIT_OK if result.ok else EXIT_PROBLEMS


@app.command()
def doctor(
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
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Check the instance configuration, pipelines, lockfile and secrets.

    Exit codes: 0 no problem, 1 problems found, 2 usage error, 70 internal error.
    Secret values are never printed."""
    try:
        code = _report(instance, json_output)
    except Exception as exc:
        # Never print the exception, its traceback or its locals: a frame may hold a secret.
        name = type(exc).__name__
        del exc
        typer.echo(f"horae doctor: internal error ({name}); please report it", err=True)
        code = EXIT_INTERNAL
    raise typer.Exit(code)


RUNS_SCHEMA_VERSION = 1
"""Version of the `horae runs --json` report."""


class RunStatusFilter(StrEnum):
    running = "running"
    ok = "ok"
    noop = "noop"
    degraded = "degraded"
    failed = "failed"
    undone = "undone"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(value: timedelta | None) -> str:
    if value is None:
        return "-"
    seconds = value.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


MAX_ERROR_COLUMN = 60
"""Characters of a run's error shown in the `horae runs` table (`--json` has all of it)."""
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]+")


def _reason(error: str | None) -> str:
    """A run's error for one table cell: control and bidi characters collapsed, capped."""
    if not error:
        return ""
    text = " ".join(_UNSAFE.sub(" ", error).split())
    return text if len(text) <= MAX_ERROR_COLUMN else text[: MAX_ERROR_COLUMN - 1] + "…"


def _run_json(run: RunSummary, stale: bool) -> dict[str, Any]:
    duration = run.duration
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
        "stale": stale,
        "dry_run": run.dry_run,
        "abandoned": run.abandoned_at is not None,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_s": None if duration is None else duration.total_seconds(),
        "cost": {
            "usd": run.cost.usd,
            "tokens_in": run.cost.tokens_in,
            "tokens_out": run.cost.tokens_out,
        },
        "error": run.error,
    }


def _run_row(run: RunSummary, stale: bool) -> list[str]:
    status = str(run.status)
    if stale:
        status += " (stale?)"
    if run.abandoned_at is not None:
        status += " (abandoned)"
    if run.dry_run:
        status += " (dry run)"
    return [
        run.id,
        run.pipeline,
        status,
        _iso(run.started_at) or "",
        _duration(run.duration),
        f"${run.cost.usd:.4f}",
        f"{run.cost.tokens_in}/{run.cost.tokens_out}",
        _reason(run.error),
    ]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
        for row in [header, *rows]
    ]


def _list_runs(
    instance: Path,
    pipeline: str | None,
    status: RunStatusFilter | None,
    limit: int,
    json_output: bool,
) -> int:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        for problem in exc.problems:
            typer.echo(str(problem))
        typer.echo(_plural(len(exc.problems), "problem") + " found")
        return EXIT_PROBLEMS
    state_dir = instance / config.state_dir
    path = state_dir / STORE_FILE
    runs: list[RunSummary] = []
    try:
        if path.exists():  # never create the store just to list it
            # Read-only: no migration, no WAL switch, no new file.
            with Store.open(path, read_only=True) as store:
                runs = store.list_runs(
                    pipeline=pipeline,
                    status=None if status is None else status.value,
                    limit=limit,
                )
    except (StoreError, OSError) as exc:
        typer.echo(f"horae runs: {path}: {exc}", err=True)
        return EXIT_PROBLEMS
    # A `running` row whose pipeline lock is free has no live process: the next run of the
    # pipeline will abandon it. Checked without waiting and without creating anything.
    stale = [
        run.status == "running" and lock_is_free(state_dir, run.pipeline) is True for run in runs
    ]
    if json_output:
        report = {
            "schema_version": RUNS_SCHEMA_VERSION,
            "instance": str(instance.resolve()),
            "runs": [_run_json(run, s) for run, s in zip(runs, stale, strict=True)],
        }
        typer.echo(json.dumps(report, indent=2))
    elif not runs:
        typer.echo("no runs")
    else:
        header = [
            "RUN",
            "PIPELINE",
            "STATUS",
            "STARTED",
            "DURATION",
            "COST",
            "TOKENS IN/OUT",
            "ERROR",
        ]
        rows = [_run_row(run, s) for run, s in zip(runs, stale, strict=True)]
        for line in _table(header, rows):
            typer.echo(line)
    return EXIT_OK


@app.command()
def runs(
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
    pipeline: Annotated[
        str | None, typer.Option("--pipeline", "-p", help="Only runs of this pipeline.")
    ] = None,
    status: Annotated[
        RunStatusFilter | None, typer.Option("--status", "-s", help="Only runs with this status.")
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", min=0, max=MAX_LIST_RUNS, help="At most this many runs."),
    ] = 20,
    json_output: Annotated[bool, typer.Option("--json", help="Print the runs as JSON.")] = False,
) -> None:
    """List runs, newest first, with status, duration and cost.

    Exit codes: 0 listed (possibly none), 1 configuration or store problem, 2 usage error,
    70 internal error."""
    try:
        code = _list_runs(instance, pipeline, status, limit, json_output)
    except Exception as exc:
        name = type(exc).__name__
        del exc
        typer.echo(f"horae runs: internal error ({name}); please report it", err=True)
        code = EXIT_INTERNAL
    raise typer.Exit(code)


class _ReplayUsage(Exception):
    """`horae replay` cannot start: unknown run or missing store (exit 2)."""


class _ReplayProblem(Exception):
    """`horae replay` cannot compare: invalid instance, missing pipeline… (exit 1)."""


def _replay(instance: Path, store: Path | None, run_id: str, against_current: bool) -> ReplayResult:
    try:
        loaded = load_instance(instance)
    except InstanceError as exc:
        raise _ReplayProblem("\n".join(str(problem) for problem in exc.problems)) from None
    path = store if store is not None else loaded.store_path
    if not path.is_file():
        raise _ReplayUsage(f"no store at {path}")
    with Store.open(path) as opened:
        record: RunRecord | None = opened.get_run(run_id)
    if record is None:
        raise _ReplayUsage(f"unknown run {run_id!r} (or still running, or abandoned)")
    pipeline = loaded.pipelines.get(record.pipeline)
    if pipeline is None:
        raise _ReplayProblem(f"pipeline {record.pipeline!r} is not in the instance")
    registry = LockRegistry(loaded.lock, pipeline)
    if registry.unresolved:
        raise _ReplayProblem(
            "no plugin in horae.lock is named after the namespace of "
            + ", ".join(registry.unresolved)
            + " (namespaces resolve by plugin name until the plugin host, #24)"
        )
    models = {alias: spec.model for alias, spec in loaded.config.models.items()}
    return replay(
        record, pipeline, plugins=registry, models=models, against_current=against_current
    )


def _replay_error(run_id: str, message: str, json_output: bool) -> None:
    if json_output:
        report = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": run_id,
            "outcome": "error",
            "identical": False,
            "error": message,
        }
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"horae replay: {message}", err=True)


@app.command("replay")
def replay_command(
    run_id: Annotated[str, typer.Argument(help="The run to replay.")],
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
    store: Annotated[
        Path | None,
        typer.Option(
            "--store", help=f"SQLite store (default: <state_dir>/{STORE_FILE}).", dir_okay=False
        ),
    ] = None,
    against_current: Annotated[
        bool,
        typer.Option(
            "--against-current",
            help="Replay the recorded data through the current pipeline even if it changed.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Re-execute a recorded run from its inputs and LLM responses, and compare its intents.

    Nothing external is called: sources, transforms and the LLM are answered from the record.
    Exit codes: 0 identical, 1 differences (or not comparable), 2 usage error, 70 internal."""
    try:
        result = _replay(instance, store, run_id, against_current)
    except _ReplayUsage as exc:
        _replay_error(run_id, str(exc), json_output)
        raise typer.Exit(EXIT_USAGE) from None
    except _ReplayProblem as exc:
        _replay_error(run_id, str(exc), json_output)
        raise typer.Exit(EXIT_PROBLEMS) from None
    except Exception as exc:
        # As for doctor: only the exception type, never its message or locals.
        name = type(exc).__name__
        del exc
        typer.echo(f"horae replay: internal error ({name}); please report it", err=True)
        raise typer.Exit(EXIT_INTERNAL) from None
    if json_output:
        typer.echo(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        typer.echo(result.render())
    raise typer.Exit(EXIT_OK if result.identical else EXIT_PROBLEMS)
