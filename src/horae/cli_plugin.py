# SPDX-License-Identifier: Apache-2.0
"""`horae plugin search [QUERY]`, `horae plugin resolve NAME[@VERSION]` (#43) and
`horae plugin add NAME[@VERSION]` (#44, approval and `horae.lock` #46), or
`horae plugin add https://HOST/OWNER/REPO@SHA` (a direct install, #48).

All read the marketplace indexes of `horae.yaml` (`marketplaces`, highest priority first),
refreshing each into the instance's cache (`<state_dir>/marketplaces`) unless `--offline`.
When an index cannot be refreshed its cached copy is used, with a warning on stderr. `add`
then refuses a plugin whose tier is not `first-party` (before M5), installs the resolved
commit into `<instance>/plugins/<name>` (`horae.marketplace.install`, schemas decision 62)
with its own venv built from its committed `uv.lock` (`horae.marketplace.environment`,
decision 69; uv's cache is `<state_dir>/uv-cache`), shows the permissions its manifest asks
for and, once the user approves them, writes its entry to `horae.lock`
(`horae.marketplace.approval` and `horae.marketplace.lock`, decision 70). Approval runs
before the venv is built, so a refused plugin's dependencies are never installed.

A direct install (a git URL pinned to a full commit SHA, `horae.marketplace.direct`) reads no
marketplace and has no tier: before M5 it is refused before anything is fetched
(`require_installable`, docs/security.md T04). Behind that gate it fetches the one commit
(`PluginInstaller.install_commit`, the same hardening) and goes through the same approval, venv
and `horae.lock` entry (its repo and commit), with `--yes` only for an identical entry.

Exit codes (`horae.cli_output`): 0 ok; 1 configuration problem, an index that cannot be
fetched and has no cache, or a plugin that cannot be fetched; 2 usage error (a malformed
reference); 3 unknown plugin or version, a name listed by several marketplaces (a conflict),
an install refused (tag and SHA disagree, invalid tree or `plugin.yaml`, no `uv.lock`, a
lock that does not match `pyproject.toml` or lacks a hash, a build backend not allowed, a
tier that may not be installed, permissions not approved) or a plugin already installed; 5
another command is writing `horae.lock`; 70 internal error. An invalid `horae.lock` is 1 and
is never rewritten. With `--json` the report carries `warnings`; an error is
{schema_version, error}.
"""

import shutil
import sys
from collections.abc import Callable
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
    Installed,
    InstallError,
    InstallRefused,
    InvalidReference,
    Loaded,
    LockBusy,
    LockInvalid,
    MarketplaceClient,
    MarketplaceError,
    NoCacheError,
    PluginEnvironment,
    PluginInstaller,
    Prepare,
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
from horae.marketplace.direct import is_direct, parse_direct
from horae.marketplace.lock import LOCK_FILE, guard, read_lock, write_lock

MARKETPLACES_DIR = "marketplaces"
"""Under the state directory: one folder per index."""
UV_CACHE_DIR = "uv-cache"
"""Under the state directory: uv's cache for the plugin venvs (#45)."""

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


def make_environment(cache_dir: Path) -> Prepare | None:
    """What builds the plugin's venv (#45). Tests replace this."""
    return PluginEnvironment(cache_dir)


PENDING = (
    "approved: horae.lock pins it with the permissions above, and its dependencies are "
    "installed in its own venv."
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


def _chain(*steps: Prepare | None) -> Prepare:
    """Run several `prepare` hooks in order (approval first, then the venv): the first that
    raises stops the install, and nothing is moved into place."""

    def prepare(tree: Path, manifest: PluginManifest) -> None:
        for step in steps:
            if step is not None:
                step(tree, manifest)

    return prepare


class _Approval:
    """The `prepare` hook of the install: shows what the verified manifest asks for and gets
    it approved (interactively, or by `--yes` against `horae.lock`), before the tree is moved
    into place. A refusal raises `ApprovalRefused`, so nothing is installed. The name and
    version are the manifest's (the installer has checked them against what was resolved);
    `repo` and `sha` are what is being installed."""

    def __init__(
        self,
        lock: Lockfile,
        repo: str,
        sha: str,
        grants: tuple[list[str], list[str]],
        *,
        yes: bool,
        json_output: bool,
    ) -> None:
        self.lock = lock
        self.repo = repo
        self.sha = sha
        self.hosts, self.local = grants
        self.yes = yes
        self.json_output = json_output
        self.entry: LockedPlugin | None = None

    def __call__(self, _tree: Path, manifest: PluginManifest) -> None:
        name, version = manifest.name, manifest.version
        granted = with_grants(manifest.permissions, self.hosts, self.local)
        wanted = LockedPlugin(
            version=version,
            repo=self.repo,
            commit=self.sha,
            permissions=granted,
            personal_fields=manifest.provides.personal_fields,
        )
        existing = self.lock.plugins.get(name)
        err = self.json_output
        for line in describe(
            name, version, manifest.permissions, granted, manifest.provides.personal_fields
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
                    f"this replaces the horae.lock entry for {name} "
                    f"({existing.version} at {existing.commit})",
                    err=err,
                )
            try:
                approved = typer.confirm(
                    f"Approve these permissions and install {name}?", default=False, err=err
                )
            except (click.exceptions.Abort, typer.Abort):  # end of input, Ctrl-C
                approved = False
            if not approved:
                raise ApprovalRefused("permissions not approved; nothing was installed")
        self.entry = wanted


Install = Callable[[PluginInstaller, Prepare], Installed]
"""How `add` installs, given the installer and the `prepare` chain: at a marketplace pin
(`install`), or a direct commit (`install_commit`, #48)."""


def _install_and_lock(
    loaded_instance: Instance,
    instance: Path,
    repo: str,
    sha: str,
    grants: tuple[list[str], list[str]],
    install: Install,
    *,
    yes: bool,
    json_output: bool,
) -> tuple[Installed, LockedPlugin]:
    """Under the lock guard: install with approval then the venv (`_chain`) as `prepare`,
    and write the approved entry to `horae.lock`. Raises `MarketplaceError`."""
    lock_path = instance / LOCK_FILE
    with guard(loaded_instance.state_dir):
        lock, header = read_lock(lock_path)
        approval = _Approval(lock, repo, sha, grants, yes=yes, json_output=json_output)
        installed = install(
            make_installer(plugins_dir(instance)),
            _chain(approval, make_environment(loaded_instance.state_dir / UV_CACHE_DIR)),
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
    return installed, entry


def _report(
    installed: Installed,
    entry: LockedPlugin,
    tier: str | None,
    index: str | None,
    warnings: list[str],
    json_output: bool,
) -> int:
    where = f"{PLUGINS_DIR}/{installed.name}"
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                **installed.record(),
                "tier": tier,
                "index": index,
                "path": where,
                "permissions": entry.model_dump(mode="json")["permissions"],
                "approved": True,
                "enabled": False,
                "pending": [],
                "warnings": warnings,
            }
        )
        return EXIT_OK
    echo(f"installed {installed.name}@{installed.version} into {where}")
    echo(f"  repo    {installed.repo}")
    if tier is None:
        echo(f"  commit  {installed.commit} (direct install, pinned by its SHA)")
    else:
        echo(f"  commit  {installed.commit} (tag v{installed.version} points to it)")
        echo(f"  tier    {tier}")
    echo(PENDING)
    return EXIT_OK


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
    direct = is_direct(ref)
    try:
        grants = parse_grants(allow_host, allow_local)
        if direct:
            repo, sha = parse_direct(ref)
            if pre:
                raise InvalidReference("--pre applies to marketplace names, not to a git URL")
    except MarketplaceError as exc:
        raise _failed(exc) from None
    if direct:
        return _add_direct(instance, repo, sha, grants, yes=yes, json_output=json_output)
    loaded_instance = load(instance)
    config, loaded = _load_indexes(loaded_instance, offline)
    try:
        found = resolve(ref, loaded.indexes, pins=config.marketplace_pins, allow_prerelease=pre)
        require_installable(found.name, found.tier, found.index)
        installed, entry = _install_and_lock(
            loaded_instance,
            instance,
            found.repo,
            found.sha,
            grants,
            lambda installer, prepare: installer.install(
                found.name, found.version, found.repo, found.sha, prepare=prepare
            ),
            yes=yes,
            json_output=json_output,
        )
    except MarketplaceError as exc:
        raise _failed(exc) from None
    return _report(installed, entry, found.tier, found.index, list(loaded.warnings), json_output)


def _add_direct(
    instance: Path,
    repo: str,
    sha: str,
    grants: tuple[list[str], list[str]],
    *,
    yes: bool,
    json_output: bool,
) -> int:
    """A direct install from a git URL (#48): no marketplace is read. Refused before M5
    (`require_installable` with no tier, docs/security.md T04) before anything is fetched;
    otherwise the same approval, venv and `horae.lock` as a marketplace install."""
    loaded_instance = load(instance)
    try:
        require_installable(repo, None, None)
        installed, entry = _install_and_lock(
            loaded_instance,
            instance,
            repo,
            sha,
            grants,
            lambda installer, prepare: installer.install_commit(repo, sha, prepare=prepare),
            yes=yes,
            json_output=json_output,
        )
    except MarketplaceError as exc:
        raise _failed(exc) from None
    return _report(installed, entry, None, None, [], json_output)


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
    ref: Annotated[
        str,
        typer.Argument(
            metavar="NAME[@VERSION] | URL@SHA",
            help="Plugin to install: a marketplace name, or an https git URL pinned to a full "
            "commit SHA (a direct install, refused before M5).",
        ),
    ],
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
    to horae.lock; give them again with --yes). Nothing is half-installed. Once approved, its
    dependencies are installed in its own venv.

    A direct install, https://HOST/OWNER/REPO@SHA with the full commit SHA (no tag, branch or
    short SHA; no credentials, http, ssh or file URL), reads no marketplace and is refused
    before M5 (no tier).

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
