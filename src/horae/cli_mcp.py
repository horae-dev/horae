# SPDX-License-Identifier: Apache-2.0
"""`horae mcp serve` (#54, schemas decision 65): the read-only MCP server over stdio.

Started by an MCP client (`claude mcp add horae -- horae mcp serve -i <instance>`), which
talks to it over the process's stdin and stdout; it opens no socket. The tools
(`horae.mcp_server`) only read the store. stdout carries the protocol and nothing else, so
errors go to stderr.

Exit codes (`horae.cli_output`, decision 37): 0 the client closed the connection, 1
configuration problem, 2 usage error, 70 internal error.
"""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError

from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    CommandFailed,
    JsonUsageGroup,
    prime_secrets,
    run_command,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.contracts.types import PipelineName
from horae.mcp_server import DEFAULT_BRIEF_PIPELINE, Reader, build_server

mcp_app = typer.Typer(
    name="mcp",
    help="Horae as an MCP server for other agents (read-only).",
    no_args_is_help=True,
    add_completion=False,
    cls=JsonUsageGroup,
    pretty_exceptions_show_locals=False,
)


def _serve(instance: Path, brief_pipeline: str) -> int:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        message = "; ".join(map(str, exc.problems))
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    prime_secrets(instance, config)
    try:
        _PIPELINE.validate_python(brief_pipeline)
    except ValidationError:
        raise CommandFailed(
            EXIT_PROBLEMS, "config_invalid", "--brief-pipeline is not a pipeline name"
        ) from None
    state_dir = instance / config.state_dir
    reader = Reader(state_dir, state_dir / STORE_FILE, config.timezone, brief_pipeline)
    build_server(reader).run("stdio")
    return EXIT_OK


_PIPELINE: TypeAdapter[str] = TypeAdapter(PipelineName)


@mcp_app.command("serve")
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
    brief_pipeline: Annotated[
        str,
        typer.Option(
            "--brief-pipeline", help="The pipeline `brief.latest` reports (the daily brief)."
        ),
    ] = DEFAULT_BRIEF_PIPELINE,
) -> None:
    """Serve the read-only tools tasks.today, brief.latest and runs.recent over stdio.

    For an MCP client to start (Claude Code: claude mcp add horae -- horae mcp serve -i
    <instance>). It opens no network port, reads the store read-only and can change nothing.
    Task titles, note paths and errors it returns are untrusted text."""
    run_command("mcp serve", False, lambda: _serve(instance, brief_pipeline))


__all__ = ["mcp_app", "serve"]
