# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from horae import __version__
from horae.apply import LocalSink, StaticSinks, plan_changes, render_text, to_json
from horae.config import CONFIG_FILE, ProblemsError, check_instance, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.store import Store, StoreError

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
"""The state database, in the instance's `state_dir` (also defined by the runner, #19)."""


def _diff(instance: Path, run_id: str, vault: Path | None, json_output: bool, color: bool) -> int:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        for problem in exc.problems:
            typer.echo(str(problem))
        typer.echo(_plural(len(exc.problems), "problem") + " found")
        return EXIT_PROBLEMS
    path = instance / config.state_dir / STORE_FILE
    if not path.exists():  # never create the store just to read it
        typer.echo(f"horae diff: unknown run {run_id!r} (no store at {path})", err=True)
        return EXIT_PROBLEMS
    sinks = StaticSinks(default=LocalSink(vault))
    try:
        with Store.open(path) as store:
            record = store.get_run(run_id)
            if record is None:
                typer.echo(f"horae diff: unknown or unfinished run {run_id!r}", err=True)
                return EXIT_PROBLEMS

            def notified(key: str) -> bool:
                return store.get_notification(key) is not None

            changes = asyncio.run(plan_changes(record, sinks, notified=notified))
    except StoreError as exc:
        typer.echo(f"horae diff: {path}: {exc}", err=True)
        return EXIT_PROBLEMS
    if json_output:
        typer.echo(to_json(changes))
    else:
        typer.echo(render_text(changes, color=color), nl=False, color=color or None)
    return EXIT_PROBLEMS if changes.refused is not None or changes.problems else EXIT_OK


@app.command()
def diff(
    run_id: Annotated[str, typer.Argument(help="The run to show.")],
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
    vault: Annotated[
        Path | None,
        typer.Option(
            "--vault",
            envvar="HORAE_VAULT",
            help="Vault directory to preview note changes against (read only).",
            file_okay=False,
            exists=True,
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the diff as JSON.")] = False,
    color: Annotated[bool, typer.Option("--color", help="Colour the text diff.")] = False,
) -> None:
    """Show what applying a stored run would change, without applying anything.

    Notes are previewed against --vault with the built-in read-only local sink until plugin
    sinks are available (#24). A failed run is never applied, so nothing is shown for it.

    Exit codes: 0 planned, 1 unknown run, failed run, unplannable intents, or a configuration
    or store problem, 2 usage error, 70 internal error."""
    try:
        code = _diff(instance, run_id, vault, json_output, color)
    except Exception as exc:
        # Never print the exception, its traceback or its locals: a frame may hold a secret.
        name = type(exc).__name__
        del exc
        typer.echo(f"horae diff: internal error ({name}); please report it", err=True)
        code = EXIT_INTERNAL
    raise typer.Exit(code)
