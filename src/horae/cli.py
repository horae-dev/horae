# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from horae import __version__
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, check_instance, read_yaml, validate
from horae.contracts import InstanceConfig
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


def _run_json(run: RunSummary) -> dict[str, Any]:
    duration = run.duration
    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "status": run.status,
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


def _run_row(run: RunSummary) -> list[str]:
    status = str(run.status)
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
    path = instance / config.state_dir / STORE_FILE
    runs: list[RunSummary] = []
    if path.exists():  # never create the store just to list it
        try:
            with Store.open(path) as store:
                runs = store.list_runs(
                    pipeline=pipeline,
                    status=None if status is None else status.value,
                    limit=limit,
                )
        except StoreError as exc:
            typer.echo(f"horae runs: {path}: {exc}", err=True)
            return EXIT_PROBLEMS
    if json_output:
        report = {
            "schema_version": RUNS_SCHEMA_VERSION,
            "instance": str(instance),
            "runs": [_run_json(run) for run in runs],
        }
        typer.echo(json.dumps(report, indent=2))
    elif not runs:
        typer.echo("no runs")
    else:
        header = ["RUN", "PIPELINE", "STATUS", "STARTED", "DURATION", "COST", "TOKENS IN/OUT"]
        for line in _table(header, [_run_row(run) for run in runs]):
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
