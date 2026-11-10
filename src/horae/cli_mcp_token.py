# SPDX-License-Identifier: Apache-2.0
"""`horae mcp token create|list|revoke` (#56, schemas decision 75): the bearer tokens of
`horae mcp serve --http`.

- **`create NAME [--scope S]... [--expires-in DAYS] [--json]`**: a new random token
  (`horae.mcp_auth.new_token`), printed **once** on stdout; the store keeps its digest only.
  Scopes default to `read`. Opens the store writable (creating and migrating it if needed).
- **`list [--json]`**: every token, revoked ones included: name, scopes, created, expires, last
  used, status (`active`, `expired`, `revoked`). Never a token or a digest. A missing store
  lists nothing.
- **`revoke NAME [--json]`**: the token is refused from the server's next request on (the
  server reads the store on every request); its row is kept, its name free again.

Exit codes (`horae.cli_output`, decision 37): 0 done, 1 configuration or store problem, 2
usage error, 3 unknown name (revoke) or a name already in use (create), 70 internal error.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    CommandFailed,
    JsonUsageCommand,
    JsonUsageGroup,
    echo,
    echo_json,
    io_failed,
    plural,
    prime_secrets,
    run_command,
)
from horae.config import CONFIG_FILE, STORE_FILE, ProblemsError, read_yaml, validate
from horae.contracts import InstanceConfig
from horae.mcp_auth import (
    DEFAULT_SCOPES,
    MAX_EXPIRES_DAYS,
    NAME,
    SCOPES,
    new_token,
    status,
    token_digest,
)
from horae.store import McpTokenExists, McpTokenRow, Store, StoreError

TOKEN_SCHEMA_VERSION = 1
"""Version of the `horae mcp token ... --json` documents."""

token_app = typer.Typer(
    name="token",
    help="Manage the bearer tokens of `horae mcp serve --http`.",
    no_args_is_help=True,
    add_completion=False,
    cls=JsonUsageGroup,
    pretty_exceptions_show_locals=False,
)

InstanceOption = Annotated[
    Path,
    typer.Option(
        "--instance",
        "-i",
        envvar="HORAE_INSTANCE",
        help="Instance directory (holds horae.yaml).",
        file_okay=False,
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the result as JSON.")]


def _now() -> datetime:
    return datetime.now(UTC)


def _store_path(instance: Path) -> Path:
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        message = "; ".join(map(str, exc.problems))
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    prime_secrets(instance, config)
    return instance / config.state_dir / STORE_FILE


def _with_store[T](path: Path, body: Callable[[Store], T]) -> T:
    try:
        with Store.open(path) as store:
            return body(store)
    except McpTokenExists as exc:
        raise CommandFailed(EXIT_REFUSED, "token_exists", str(exc)) from None
    except StoreError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "store_error", f"{path}: {exc}") from None
    except OSError as exc:
        raise io_failed(exc, path, store=path) from None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _row_json(row: McpTokenRow, now: datetime) -> dict[str, Any]:
    """A token as `list` shows it: never the token or its digest."""
    return {
        "name": row.name,
        "scopes": list(row.scopes),
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "revoked_at": _iso(row.revoked_at),
        "status": status(row, now),
    }


def _check_name(name: str) -> None:
    if NAME.fullmatch(name) is None:
        message = "a token name is 1 to 64 letters, digits, '.', '_' or '-', not starting with"
        raise CommandFailed(EXIT_USAGE, "usage", f"{message} '.', '_' or '-'")


def _create(
    instance: Path, name: str, scopes: list[str], expires_in: int | None, json_output: bool
) -> int:
    _check_name(name)
    chosen = tuple(sorted(set(scopes or DEFAULT_SCOPES)))
    unknown = [s for s in chosen if s not in SCOPES]
    if unknown:
        known = ", ".join(sorted(SCOPES))
        raise CommandFailed(EXIT_USAGE, "usage", f"unknown scope (known: {known})")
    path = _store_path(instance)
    now = _now()
    expires = None if expires_in is None else now + timedelta(days=expires_in)
    token = new_token()

    def add(store: Store) -> None:
        store.mcp_token_add(name, token_digest(token), chosen, at=now, expires_at=expires)

    _with_store(path, add)
    if json_output:
        echo_json(
            {
                "schema_version": TOKEN_SCHEMA_VERSION,
                "name": name,
                "scopes": list(chosen),
                "created_at": _iso(now),
                "expires_at": _iso(expires),
                "token": token,
            }
        )
        return EXIT_OK
    until = "never expires" if expires is None else f"expires {_iso(expires)}"
    echo(f"created MCP token {name} (scopes {' '.join(chosen)}; {until})")
    echo(token)
    echo("Copy it now: it is shown only once and cannot be recovered.", err=True)
    return EXIT_OK


def _list(instance: Path, json_output: bool) -> int:
    path = _store_path(instance)
    rows = _with_store(path, lambda store: store.mcp_tokens()) if path.exists() else []
    now = _now()
    shown = [_row_json(row, now) for row in rows]
    if json_output:
        echo_json({"schema_version": TOKEN_SCHEMA_VERSION, "tokens": shown})
        return EXIT_OK
    if not shown:
        echo("no MCP token (create one with `horae mcp token create NAME`)")
        return EXIT_OK
    for item in shown:
        echo(
            f"{item['name']}  {item['status']}  scopes={','.join(item['scopes'])}  "
            f"created={item['created_at']}  expires={item['expires_at'] or 'never'}  "
            f"last_used={item['last_used_at'] or 'never'}"
        )
    active = sum(item["status"] == "active" for item in shown)
    echo(f"{plural(len(shown), 'token')}, {active} active")
    return EXIT_OK


def _revoke(instance: Path, name: str, json_output: bool) -> int:
    _check_name(name)
    path = _store_path(instance)
    now = _now()
    found = path.exists() and _with_store(path, lambda s: s.mcp_token_revoke(name, at=now))
    if not found:
        raise CommandFailed(EXIT_REFUSED, "unknown_token", f"no unrevoked MCP token named {name}")
    if json_output:
        echo_json({"schema_version": TOKEN_SCHEMA_VERSION, "name": name, "revoked_at": _iso(now)})
    else:
        echo(f"revoked MCP token {name}: refused from the next request on")
    return EXIT_OK


@token_app.command("create", cls=JsonUsageCommand)
def create(
    name: Annotated[str, typer.Argument(help="A name for the token (e.g. laptop-claude).")],
    instance: InstanceOption = Path(),
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help="A scope to grant (repeatable): read (default) or pipeline.run (#55).",
        ),
    ] = None,
    expires_in: Annotated[
        int | None,
        typer.Option(
            "--expires-in",
            metavar="DAYS",
            min=1,
            max=MAX_EXPIRES_DAYS,
            help="Expire the token after DAYS days (default: never).",
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Create a bearer token for `horae mcp serve --http` and print it once.

    Only its SHA-256 digest is stored: copy the token now, it cannot be shown again. A name
    is unique among the tokens not revoked.

    Exit codes: 0 created, 1 configuration or store problem, 2 usage error, 3 name in use,
    70 internal error."""
    run_command(
        "mcp token create",
        json_output,
        lambda: _create(instance, name, scope or [], expires_in, json_output),
    )


@token_app.command("list", cls=JsonUsageCommand)
def list_tokens(instance: InstanceOption = Path(), json_output: JsonOption = False) -> None:
    """List the MCP tokens (revoked ones too) with their status; never a token.

    Exit codes: 0 listed, 1 configuration or store problem, 70 internal error."""
    run_command("mcp token list", json_output, lambda: _list(instance, json_output))


@token_app.command("revoke", cls=JsonUsageCommand)
def revoke(
    name: Annotated[str, typer.Argument(help="The name of the token to revoke.")],
    instance: InstanceOption = Path(),
    json_output: JsonOption = False,
) -> None:
    """Revoke an MCP token: the server refuses it from its next request on.

    Exit codes: 0 revoked, 1 configuration or store problem, 2 usage error, 3 no unrevoked
    token has that name, 70 internal error."""
    run_command("mcp token revoke", json_output, lambda: _revoke(instance, name, json_output))


__all__ = ["TOKEN_SCHEMA_VERSION", "create", "list_tokens", "revoke", "token_app"]
