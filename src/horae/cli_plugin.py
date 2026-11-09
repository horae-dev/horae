# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search [QUERY]` and `horae plugin resolve NAME[@VERSION]` (#43).

Both read the marketplace indexes of `horae.yaml` (`marketplaces`, highest priority first),
refreshing each into the instance's cache (`<state_dir>/marketplaces`) unless `--offline`.
When an index cannot be refreshed its cached copy is used, with a warning on stderr. They
install nothing (`horae plugin add` is #44).

Exit codes (`horae.cli_output`): 0 ok; 1 configuration problem, or an index that cannot be
fetched and has no cache; 2 usage error (a malformed reference); 3 unknown plugin or
version, or a name listed by several marketplaces (a conflict); 70 internal error. With
`--json` the report carries `warnings`; an error is {schema_version, error}.
"""

from pathlib import Path
from typing import Annotated, Any

import typer

from horae.cli_host import load
from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_USAGE,
    SCHEMA_VERSION,
    CommandFailed,
    JsonUsageCommand,
    JsonUsageGroup,
    echo,
    echo_json,
    run_command,
    safe,
)
from horae.marketplace import (
    ConflictError,
    FetchError,
    InvalidReference,
    Loaded,
    MarketplaceClient,
    MarketplaceError,
    NoCacheError,
    UnknownPlugin,
    UnknownVersion,
    resolve,
    search,
)

MARKETPLACES_DIR = "marketplaces"
"""Under the state directory: one folder per index."""

plugin_app = typer.Typer(
    name="plugin",
    help="Find plugins in the configured marketplaces.",
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
OfflineOption = Annotated[
    bool,
    typer.Option("--offline", help="Do not fetch: use the cached copy of every marketplace."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the result as JSON.")]

_KINDS: tuple[tuple[type[MarketplaceError], int, str], ...] = (
    (ConflictError, EXIT_REFUSED, "conflict"),
    (UnknownPlugin, EXIT_REFUSED, "unknown_plugin"),
    (UnknownVersion, EXIT_REFUSED, "unknown_version"),
    (InvalidReference, EXIT_USAGE, "usage"),
    (NoCacheError, EXIT_PROBLEMS, "no_cache"),
    (FetchError, EXIT_PROBLEMS, "fetch_failed"),
)


def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
    """The client the commands use: https only. Tests replace this to clone local fixtures."""
    return MarketplaceClient(cache_dir, urls)


def _failed(exc: MarketplaceError) -> CommandFailed:
    for cls, code, kind in _KINDS:
        if isinstance(exc, cls):
            return CommandFailed(code, kind, str(exc))
    return CommandFailed(EXIT_PROBLEMS, "marketplace_error", str(exc))


def _indexes(instance: Path, offline: bool) -> tuple[Any, Loaded]:
    loaded_instance = load(instance)
    config = loaded_instance.config
    client = make_client(loaded_instance.state_dir / MARKETPLACES_DIR, list(config.marketplaces))
    try:
        loaded = client.load(offline=offline)
    except MarketplaceError as exc:
        raise _failed(exc) from None
    for warning in loaded.warnings:
        echo(f"warning: {warning}", err=True)
    return config, loaded


def _resolve(instance: Path, ref: str, offline: bool, pre: bool, json_output: bool) -> int:
    config, loaded = _indexes(instance, offline)
    try:
        found = resolve(ref, loaded.indexes, pins=config.marketplace_pins, allow_prerelease=pre)
    except MarketplaceError as exc:
        raise _failed(exc) from None
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "name": found.name,
                "version": found.version,
                "repo": found.repo,
                "sha": found.sha,
                "tier": found.tier,
                "index": found.index,
                "warnings": list(loaded.warnings),
            }
        )
        return EXIT_OK
    echo(f"{found.name}@{found.version}")
    echo(f"  repo   {found.repo}")
    echo(f"  sha    {found.sha}")
    echo(f"  tier   {found.tier}")
    echo(f"  index  {found.index}")
    return EXIT_OK


def _search(instance: Path, query: str, offline: bool, json_output: bool) -> int:
    _config, loaded = _indexes(instance, offline)
    hits = search(loaded.indexes, query)
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "plugins": [
                    {
                        "name": h.name,
                        "repo": h.repo,
                        "tier": h.tier,
                        "versions": list(h.versions),
                        "index": h.index,
                    }
                    for h in hits
                ],
                "warnings": list(loaded.warnings),
            }
        )
        return EXIT_OK
    for hit in hits:
        echo(f"{hit.name}  {', '.join(hit.versions)}  [{hit.tier}]  {safe(hit.index)}")
    if not hits:
        echo("no plugin found")
    return EXIT_OK


@plugin_app.command("resolve", cls=JsonUsageCommand)
def resolve_command(
    ref: Annotated[str, typer.Argument(metavar="NAME[@VERSION]", help="Plugin to resolve.")],
    instance: InstanceOption = Path(),
    offline: OfflineOption = False,
    pre: Annotated[
        bool, typer.Option("--pre", help="Also consider pre-releases when no version is named.")
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Resolve a plugin name (and version) to its repository and pinned commit.

    Marketplaces are searched in the order of `marketplaces` in horae.yaml. Without a version
    the highest semantic version wins (pre-releases only with --pre). A name that two
    marketplaces list under different repositories is an error naming both: pick one with
    `marketplace_pins` in horae.yaml.

    Exit codes: 0 resolved, 1 configuration problem or an unreachable marketplace without
    cache, 2 malformed reference, 3 unknown plugin or version, or a conflict, 70 internal."""
    run_command(
        "plugin resolve",
        json_output,
        lambda: _resolve(instance, ref, offline, pre, json_output),
    )


@plugin_app.command("search", cls=JsonUsageCommand)
def search_command(
    query: Annotated[str, typer.Argument(help="Part of a plugin name (default: all).")] = "",
    instance: InstanceOption = Path(),
    offline: OfflineOption = False,
    json_output: JsonOption = False,
) -> None:
    """List the plugins of the configured marketplaces, by priority, with their versions.

    Exit codes: 0 listed (possibly nothing), 1 configuration problem or an unreachable
    marketplace without cache, 70 internal."""
    run_command(
        "plugin search", json_output, lambda: _search(instance, query, offline, json_output)
    )


__all__ = ["MARKETPLACES_DIR", "make_client", "plugin_app"]
