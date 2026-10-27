# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point for Horae."""

from typing import Annotated

import typer

from horae import __version__

app = typer.Typer(
    name="horae",
    help="Horae: a predictable, self-hosted personal agent.",
    no_args_is_help=True,
    add_completion=False,
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
