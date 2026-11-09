# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search [QUERY]`, `horae plugin resolve NAME[@VERSION]` (#43) and
`horae plugin add NAME[@VERSION]` (#44).

All read the marketplace indexes of `horae.yaml` (`marketplaces`, highest priority first),
refreshing each into the instance's cache (`<state_dir>/marketplaces`) unless `--offline`.
When an index cannot be refreshed its cached copy is used, with a warning on stderr. `add`
then installs the resolved commit into `<instance>/plugins/<name>`
(`horae.marketplace.install`, schemas decision 62); the plugin is not enabled until it is in
`horae.lock` with approved permissions (#46) and its dependencies are installed (#45).

Exit codes (`horae.cli_output`): 0 ok; 1 configuration problem, an index that cannot be
fetched and has no cache, or a plugin that cannot be fetched; 2 usage error (a malformed
reference); 3 unknown plugin or version, a name listed by several marketplaces (a conflict),
an install refused (tag and SHA disagree, invalid tree or `plugin.yaml`) or a plugin already
installed; 70 internal error. With `--json` the report carries `warnings`; an error is
{schema_version, error}.
"""

from pathlib import Path
from typing import Annotated, Any

import typer

from horae.cli_host import PLUGINS_DIR, load, plugins_dir
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
    AlreadyInstalled,
    ConflictError,
    FetchError,
    InstallError,
    InstallRefused,
    InvalidReference,
    Loaded,
    MarketplaceClient,
    MarketplaceError,
    NoCacheError,
    PluginInstaller,
    UnknownPlugin,
    UnknownVersion,
    resolve,
    search,
)

MARKETPLACES_DIR = "marketplaces"
"""Under the state directory: one folder per index."""

plugin_app = typer.Typer(
    name="plugin",
    help="Find and install plugins from the configured marketplaces.",
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
    (AlreadyInstalled, EXIT_REFUSED, "already_installed"),
    (InstallRefused, EXIT_REFUSED, "install_refused"),
    (InstallError, EXIT_PROBLEMS, "install_failed"),
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


def make_installer(directory: Path) -> PluginInstaller:
    """The installer `add` uses: https only. Tests replace this to clone local fixtures."""
    return PluginInstaller(directory)


PENDING = (
    "not enabled yet: Horae loads a plugin only once horae.lock lists it with the permissions "
    "you approved, and its dependencies are not installed. Review the permissions in "
    "{manifest} before enabling it (permission approval and dependency install are not "
    "automated yet)."
)


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


def _add(instance: Path, ref: str, offline: bool, pre: bool, json_output: bool) -> int:
    config, loaded = _indexes(instance, offline)
    try:
        found = resolve(ref, loaded.indexes, pins=config.marketplace_pins, allow_prerelease=pre)
        installed = make_installer(plugins_dir(instance)).install(
            found.name, found.version, found.repo, found.sha
        )
    except MarketplaceError as exc:
        raise _failed(exc) from None
    where = f"{PLUGINS_DIR}/{installed.name}"
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                **installed.record(),
                "tier": found.tier,
                "index": found.index,
                "path": where,
                "enabled": False,
                "pending": ["approve_permissions", "install_dependencies"],
                "warnings": list(loaded.warnings),
            }
        )
        return EXIT_OK
    echo(f"installed {installed.name}@{installed.version} into {where}")
    echo(f"  repo    {installed.repo}")
    echo(f"  commit  {installed.commit} (tag v{installed.version} points to it)")
    echo(f"  tier    {found.tier}")
    echo(PENDING.format(manifest=f"{where}/plugin.yaml"))
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


@plugin_app.command("add", cls=JsonUsageCommand)
def add_command(
    ref: Annotated[str, typer.Argument(metavar="NAME[@VERSION]", help="Plugin to install.")],
    instance: InstanceOption = Path(),
    offline: OfflineOption = False,
    pre: Annotated[
        bool, typer.Option("--pre", help="Also consider pre-releases when no version is named.")
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Install a plugin at the commit its marketplace pins, into the instance's plugins/.

    The name is resolved as `horae plugin resolve` does. The repository is cloned over https
    and the pinned commit checked out; the install is refused when the tag v<version> and the
    pinned commit disagree, or when plugin.yaml is invalid or names another plugin or version.
    Nothing is half-installed. The plugin is not enabled: it must still be added to horae.lock
    with the permissions you approve, and its dependencies installed.

    Exit codes: 0 installed, 1 configuration problem, unreachable marketplace without cache,
    or a repository that cannot be fetched, 2 malformed reference, 3 unknown plugin or
    version, a conflict, a refused install or a plugin already installed, 70 internal."""
    run_command(
        "plugin add",
        json_output,
        lambda: _add(instance, ref, offline, pre, json_output),
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


__all__ = ["MARKETPLACES_DIR", "make_client", "make_installer", "plugin_app"]
