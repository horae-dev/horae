# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

import json
from pathlib import Path
from typing import Annotated

import typer

from horae import __version__
from horae.config import check_instance

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
