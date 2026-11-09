# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search [QUERY]`, `horae plugin resolve NAME[@VERSION]` (#43) and
`horae plugin add NAME[@VERSION]` (#44, approval and `horae.lock` #46).

All read the marketplace indexes of `horae.yaml` (`marketplaces`, highest priority first),
refreshing each into the instance's cache (`<state_dir>/marketplaces`) unless `--offline`.
When an index cannot be refreshed its cached copy is used, with a warning on stderr. `add`
then refuses a plugin whose tier is not `first-party` (before M5), installs the resolved
commit into `<instance>/plugins/<name>` (`horae.marketplace.install`, schemas decision 62),
shows the permissions its manifest asks for and, once the user approves them, writes its
entry to `horae.lock` (`horae.marketplace.approval` and `horae.marketplace.lock`, decision
70). Its dependencies are #45.

Exit codes (`horae.cli_output`): 0 ok; 1 configuration problem, an index that cannot be
fetched and has no cache, or a plugin that cannot be fetched; 2 usage error (a malformed
reference); 3 unknown plugin or version, a name listed by several marketplaces (a conflict),
an install refused (tag and SHA disagree, invalid tree or `plugin.yaml`, a tier that may not be
installed, permissions not approved) or a plugin already installed; 5 another command is
writing `horae.lock`; 70 internal error. An invalid `horae.lock` is 1 and is never rewritten.
With `--json` the report carries `warnings`; an error is {schema_version, error}.
"""

import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from horae.cli_host import PLUGINS_DIR, load, plugins_dir
from horae.cli_output import (
    EXIT_LOCKED,
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
from horae.config import Instance
from horae.contracts.lockfile import LockedPlugin, Lockfile
from horae.contracts.plugin import PluginManifest
from horae.marketplace import (
    AlreadyInstalled,
    ApprovalRefused,
    ConflictError,
    FetchError,
    InstallError,
    InstallRefused,
    InvalidReference,
    Loaded,
    LockBusy,
    LockInvalid,
    MarketplaceClient,
    MarketplaceError,
    NoCacheError,
    PluginInstaller,
    Resolved,
    TierRefused,
    UnknownPlugin,
    UnknownVersion,
    resolve,
    search,
)
from horae.marketplace.approval import (
    describe,
    lock_mismatch,
    parse_grants,
    require_installable,
    with_grants,
)
from horae.marketplace.lock import LOCK_FILE, guard, read_lock, write_lock

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
    (TierRefused, EXIT_REFUSED, "tier_refused"),
    (ApprovalRefused, EXIT_REFUSED, "approval_refused"),
    (InstallRefused, EXIT_REFUSED, "install_refused"),
    (InstallError, EXIT_PROBLEMS, "install_failed"),
    (ConflictError, EXIT_REFUSED, "conflict"),
    (UnknownPlugin, EXIT_REFUSED, "unknown_plugin"),
    (UnknownVersion, EXIT_REFUSED, "unknown_version"),
    (InvalidReference, EXIT_USAGE, "usage"),
    (NoCacheError, EXIT_PROBLEMS, "no_cache"),
    (FetchError, EXIT_PROBLEMS, "fetch_failed"),
    (LockInvalid, EXIT_PROBLEMS, "lock_invalid"),
    (LockBusy, EXIT_LOCKED, "lock_busy"),
)


def make_client(cache_dir: Path, urls: list[str]) -> MarketplaceClient:
    """The client the commands use: https only. Tests replace this to clone local fixtures."""
    return MarketplaceClient(cache_dir, urls)


def make_installer(directory: Path) -> PluginInstaller:
    """The installer `add` uses: https only. Tests replace this to clone local fixtures."""
    return PluginInstaller(directory)


PENDING = (
    "approved: horae.lock pins it with the permissions above. Its dependencies are not "
    "installed by horae yet."
)
NOT_INTERACTIVE = (
    "permissions need your approval: run `horae plugin add` in a terminal to review them, or "
    "pass --yes when horae.lock already approves exactly this version, commit and permissions "
    "(a reproducible re-install); nothing was installed"
)


def interactive() -> bool:
    """Whether the user can be asked to approve (stdin is a terminal). A seam for tests."""
    return sys.stdin.isatty()


def _failed(exc: MarketplaceError) -> CommandFailed:
    for cls, code, kind in _KINDS:
        if isinstance(exc, cls):
            return CommandFailed(code, kind, str(exc))
    return CommandFailed(EXIT_PROBLEMS, "marketplace_error", str(exc))


def _indexes(instance: Path, offline: bool) -> tuple[Any, Loaded]:
    return _load_indexes(load(instance), offline)


def _load_indexes(loaded_instance: Instance, offline: bool) -> tuple[Any, Loaded]:
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


class _Approval:
    """The `prepare` hook of the install: shows what the verified manifest asks for and gets
    it approved (interactively, or by `--yes` against `horae.lock`), before the tree is moved
    into place. A refusal raises `ApprovalRefused`, so nothing is installed."""

    def __init__(
        self,
        lock: Lockfile,
        found: Resolved,
        grants: tuple[list[str], list[str]],
        *,
        yes: bool,
        json_output: bool,
    ) -> None:
        self.lock = lock
        self.found = found
        self.hosts, self.local = grants
        self.yes = yes
        self.json_output = json_output
        self.entry: LockedPlugin | None = None

    def __call__(self, _tree: Path, manifest: PluginManifest) -> None:
        granted = with_grants(manifest.permissions, self.hosts, self.local)
        wanted = LockedPlugin(
            version=self.found.version,
            repo=self.found.repo,
            commit=self.found.sha,
            permissions=granted,
            personal_fields=manifest.provides.personal_fields,
        )
        existing = self.lock.plugins.get(self.found.name)
        err = self.json_output
        for line in describe(
            self.found.name,
            self.found.version,
            manifest.permissions,
            granted,
            manifest.provides.personal_fields,
        ):
            echo(safe(line), err=err)
        if self.yes:
            why = lock_mismatch(existing, wanted)
            if why is not None:
                raise ApprovalRefused(
                    f"--yes approves only what horae.lock already approves: {why}. Run "
                    "without --yes to review and approve; nothing was installed"
                )
        elif not interactive():
            raise ApprovalRefused(NOT_INTERACTIVE)
        else:
            if existing is not None and lock_mismatch(existing, wanted) is not None:
                echo(
                    f"this replaces the horae.lock entry for {self.found.name} "
                    f"({existing.version} at {existing.commit})",
                    err=err,
                )
            try:
                approved = typer.confirm(
                    f"Approve these permissions and install {self.found.name}?",
                    default=False,
                    err=err,
                )
            except (click.exceptions.Abort, typer.Abort):  # end of input, Ctrl-C
                approved = False
            if not approved:
                raise ApprovalRefused("permissions not approved; nothing was installed")
        self.entry = wanted


def _add(
    instance: Path,
    ref: str,
    offline: bool,
    pre: bool,
    json_output: bool,
    yes: bool,
    allow_host: list[str],
    allow_local: list[str],
) -> int:
    try:
        grants = parse_grants(allow_host, allow_local)
    except MarketplaceError as exc:
        raise _failed(exc) from None
    loaded_instance = load(instance)
    config, loaded = _load_indexes(loaded_instance, offline)
    lock_path = instance / LOCK_FILE
    try:
        found = resolve(ref, loaded.indexes, pins=config.marketplace_pins, allow_prerelease=pre)
        require_installable(found.name, found.tier, found.index)
        with guard(loaded_instance.state_dir):
            lock, header = read_lock(lock_path)
            approval = _Approval(lock, found, grants, yes=yes, json_output=json_output)
            installed = make_installer(plugins_dir(instance)).install(
                found.name, found.version, found.repo, found.sha, prepare=approval
            )
            entry = approval.entry
            if entry is None or installed.lock_entry(entry.permissions) != entry:
                # The install record and the approved entry must be the same plugin.
                shutil.rmtree(installed.path, ignore_errors=True)
                raise ApprovalRefused("the installed plugin is not the one approved; removed")
            plugins = {**lock.plugins, installed.name: entry}
            try:
                write_lock(lock_path, Lockfile(schema_version=1, plugins=plugins), header)
            except OSError as exc:
                shutil.rmtree(installed.path, ignore_errors=True)
                raise CommandFailed(
                    EXIT_PROBLEMS,
                    "lock_write_failed",
                    f"cannot write {lock_path}: {exc.strerror or exc}; the install was undone",
                ) from None
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
                "permissions": entry.model_dump(mode="json")["permissions"],
                "approved": True,
                "enabled": False,
                "pending": ["install_dependencies"],
                "warnings": list(loaded.warnings),
            }
        )
        return EXIT_OK
    echo(f"installed {installed.name}@{installed.version} into {where}")
    echo(f"  repo    {installed.repo}")
    echo(f"  commit  {installed.commit} (tag v{installed.version} points to it)")
    echo(f"  tier    {found.tier}")
    echo(PENDING)
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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Approve without asking, only when horae.lock already has an entry for this "
            "exact version and commit with identical permissions (a reproducible re-install).",
        ),
    ] = False,
    allow_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-host",
            metavar="HOST",
            help="Also grant this https host (repeatable), beyond what the plugin declares.",
        ),
    ] = None,
    allow_local: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-local",
            metavar="HOST:PORT",
            help="Also grant this local-network endpoint (repeatable; plain http allowed), "
            "e.g. 192.168.1.10:8123.",
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Install a plugin at the commit its marketplace pins, approve its permissions, and pin
    it in horae.lock.

    The name is resolved as `horae plugin resolve` does. Before M5 only first-party plugins
    install. The repository is cloned over https and the pinned commit checked out; the install
    is refused when the tag v<version> and the pinned commit disagree, or when plugin.yaml is
    invalid or names another plugin or version. The permissions plugin.yaml asks for are then
    shown and must be approved: interactively, or with --yes when horae.lock already approves
    exactly this version, commit and permissions (without a terminal and without --yes, the
    install is refused). --allow-host and --allow-local add grants beyond the manifest (written
    to horae.lock; give them again with --yes). Nothing is half-installed. Its dependencies are
    not installed yet.

    Exit codes: 0 installed and approved, 1 configuration problem, unreachable marketplace
    without cache, a repository that cannot be fetched, or an invalid horae.lock, 2 malformed
    reference or grant, 3 unknown plugin or version, a conflict, a refused install (tier,
    verification, approval) or a plugin already installed, 5 horae.lock is being written by
    another command, 70 internal."""
    run_command(
        "plugin add",
        json_output,
        lambda: _add(
            instance,
            ref,
            offline,
            pre,
            json_output,
            yes,
            list(allow_host or ()),
            list(allow_local or ()),
        ),
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


__all__ = ["MARKETPLACES_DIR", "interactive", "make_client", "make_installer", "plugin_app"]
