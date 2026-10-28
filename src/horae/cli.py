# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

import json
from pathlib import Path
from typing import Annotated

import typer

from horae import __version__
from horae.config import InstanceError, check_instance, load_instance
from horae.contracts.run import RunRecord
from horae.replay import RESULT_SCHEMA_VERSION, LockRegistry, ReplayResult, replay
from horae.store import Store

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


STORE_FILE = "horae.db"
"""The store's file in the state directory (provisional until the runner, #19, fixes it)."""


class _ReplayUsage(Exception):
    """`horae replay` cannot start: unknown run or missing store (exit 2)."""


class _ReplayProblem(Exception):
    """`horae replay` cannot compare: invalid instance, missing pipeline… (exit 1)."""


def _replay(instance: Path, store: Path | None, run_id: str, against_current: bool) -> ReplayResult:
    try:
        loaded = load_instance(instance)
    except InstanceError as exc:
        raise _ReplayProblem("\n".join(str(problem) for problem in exc.problems)) from None
    path = store if store is not None else loaded.state_dir / STORE_FILE
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
