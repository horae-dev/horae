# SPDX-License-Identifier: Apache-2.0
"""`horae mcp serve` (#54, schemas decision 65): the read-only MCP server.

**stdio** (the default): started by an MCP client (`claude mcp add horae -- horae mcp serve -i
<instance>`), which talks to it over the process's stdin and stdout; it opens no socket and
asks for no token (decision 75 B). stdout carries the protocol and nothing else, so errors go
to stderr.

**HTTP** (`--http`, #56, decision 75): streamable HTTP at `http://<host>:<port>/mcp`, every
request behind a bearer token (`horae.mcp_auth`; tokens are managed by `horae mcp token`,
`horae.cli_mcp_token`). It binds to `127.0.0.1:8765` by default and **refuses to start** while
no token is usable (none created, all revoked or expired). The listening address goes to
stderr. It stops on SIGINT or SIGTERM. The tools (`horae.mcp_server`) only read the store.

Exit codes (`horae.cli_output`, decision 37): 0 the client closed the connection (stdio) or
the server was stopped (HTTP), 1 configuration problem (no usable token, a port in use), 2
usage error, 70 internal error.
"""

import asyncio
import re
import signal
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError

from horae.cli_mcp_token import token_app
from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_USAGE,
    CommandFailed,
    JsonUsageGroup,
    echo,
    prime_secrets,
    run_command,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.contracts.types import PipelineName
from horae.mcp_auth import TokenVerifier
from horae.mcp_server import DEFAULT_BRIEF_PIPELINE, MCP_PATH, Reader, build_http_app, build_server
from horae.store import StoreError

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

mcp_app = typer.Typer(
    name="mcp",
    help="Horae as an MCP server for other agents (read-only).",
    no_args_is_help=True,
    add_completion=False,
    cls=JsonUsageGroup,
    pretty_exceptions_show_locals=False,
)
mcp_app.add_typer(token_app, name="token")


def _reader(instance: Path, brief_pipeline: str) -> Reader:
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
    return Reader(state_dir, state_dir / STORE_FILE, config.timezone, brief_pipeline)


def _serve(instance: Path, brief_pipeline: str) -> int:
    build_server(_reader(instance, brief_pipeline), stdio=True).run("stdio")
    return EXIT_OK


def _serve_http(
    instance: Path, brief_pipeline: str, host: str, port: int, allowed_hosts: list[str]
) -> int:
    bad = [h for h in allowed_hosts if _HOSTNAME.fullmatch(h) is None]
    if bad:
        raise CommandFailed(EXIT_USAGE, "usage", "--allowed-host takes a host name or address")
    reader = _reader(instance, brief_pipeline)
    verifier = TokenVerifier(reader.store_path)
    try:
        usable = verifier.usable_count()
    except (StoreError, OSError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        raise CommandFailed(
            EXIT_PROBLEMS, "store_error", f"{reader.store_path}: {reason}"
        ) from None
    if usable == 0:
        raise CommandFailed(
            EXIT_PROBLEMS,
            "no_token",
            "no usable MCP token: the HTTP server refuses to start without one "
            "(create one with `horae mcp token create NAME`)",
        )
    import uvicorn  # only the HTTP transport needs it

    from horae.api._server import ApiServer, BindError, bind

    try:
        sock = bind(host, port)
    except BindError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "bind_failed", str(exc)) from None
    bound_host, bound_port = sock.getsockname()[:2]
    shown = f"[{bound_host}]" if ":" in str(bound_host) else str(bound_host)
    echo(f"horae mcp serve: listening on http://{shown}:{bound_port}{MCP_PATH}", err=True)
    if host not in _LOOPBACK:
        echo(
            "horae mcp serve: warning: not a loopback address; tokens travel in clear text "
            "unless a TLS reverse proxy is in front",
            err=True,
        )
    config = uvicorn.Config(
        build_http_app(reader, verifier, tuple(allowed_hosts)),
        log_config=None,  # Horae's redacting log record factory stays in place
        access_log=False,
        server_header=False,
        proxy_headers=False,
        lifespan="on",  # the SDK's session manager runs in the app's lifespan
    )
    # Horae's `ApiServer` leaves the signals alone (uvicorn would re-raise them on exit):
    # the first SIGINT / SIGTERM stops it gracefully, a second one at once; exit 0.
    server = ApiServer(config, sock)

    def stop(signum: int, frame: object) -> None:
        server.request_stop(force=server.should_exit)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        asyncio.run(server.run_until_stopped())
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
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
    http: Annotated[
        bool,
        typer.Option("--http", help="Serve streamable HTTP with bearer tokens instead of stdio."),
    ] = False,
    host: Annotated[
        str,
        typer.Option("--host", help="With --http: the address to listen on (loopback)."),
    ] = DEFAULT_HTTP_HOST,
    port: Annotated[
        int,
        typer.Option("--port", min=0, max=65535, help="With --http: the port (0: any free one)."),
    ] = DEFAULT_HTTP_PORT,
    allowed_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            help="With --http: a Host header to accept besides loopback names (repeatable).",
        ),
    ] = None,
) -> None:
    """Serve the read-only tools tasks.today, brief.latest and runs.recent.

    Over stdio by default, for an MCP client to start (Claude Code: claude mcp add horae --
    horae mcp serve -i <instance>); no port, no token. With --http, streamable HTTP at
    http://127.0.0.1:8765/mcp where every request needs a token from `horae mcp token
    create` (Authorization: Bearer hmcp_...); it refuses to start without a usable token.
    The tools read the store read-only and change nothing (over HTTP, only a token's last
    use is recorded). Task titles, note paths and errors they return are untrusted text.

    Exit codes: 0 stopped, 1 configuration problem (no usable token, port in use), 2 usage
    error, 70 internal error."""
    if http:
        hosts = allowed_host or []
        run_command(
            "mcp serve", False, lambda: _serve_http(instance, brief_pipeline, host, port, hosts)
        )
    if host != DEFAULT_HTTP_HOST or port != DEFAULT_HTTP_PORT or allowed_host:
        run_command("mcp serve", False, _http_only)
    run_command("mcp serve", False, lambda: _serve(instance, brief_pipeline))


def _http_only() -> int:
    raise CommandFailed(EXIT_USAGE, "usage", "--host, --port and --allowed-host need --http")


__all__ = ["mcp_app", "serve"]
