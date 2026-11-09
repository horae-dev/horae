# SPDX-License-Identifier: Apache-2.0
"""`horae plugin upgrade NAME[@VERSION]`, `horae plugin remove NAME` and `horae plugin audit`
(#47, schemas decision 73). Registered on `horae.cli_plugin.plugin_app`.

- **upgrade** resolves the new version as `add` does (marketplace pins, `--pre`, the tier rule),
  installs it through the same hardened path into a staging directory (with its own venv and
  pack pipelines), shows the permission diff against the `horae.lock` entry and needs a new
  approval for anything that widens; it then swaps the trees and writes the lock, undoing every
  step on failure. A direct install (`source: direct`) stays refused before M5.
- **remove** refuses a plugin still listed under `plugins:` in `horae.yaml`, then removes its
  tree (and venv), the pack pipelines it recorded that are still its own, and its lock entry.
  Secrets are kept unless `--delete-secrets`; the files it wrote and the run history always.
- **audit** lists every plugin of `horae.lock` and `plugins/` with its version, source, tier,
  permissions and last activity, and the drift between the lock and the disk (exit 4).

Exit codes (`horae.cli_output`): 0 ok (audit: no drift); 1 configuration problem, a repository
or index that cannot be fetched, an invalid `horae.lock`, a lock that cannot be written; 2
usage error; 3 refused (not installed, enabled, downgrade, tier, approval, verification); 4
drift found (audit); 5 another command is writing `horae.lock`; 70 internal error.
"""

import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from pydantic import TypeAdapter, ValidationError

from horae import cli_plugin
from horae.cli_host import PLUGINS_DIR, load, plugins_dir
from horae.cli_output import (
    EXIT_DIFFERENT,
    EXIT_OK,
    EXIT_PROBLEMS,
    SCHEMA_VERSION,
    CommandFailed,
    JsonUsageCommand,
    echo,
    echo_json,
    prime_secrets,
    run_command,
    safe,
)
from horae.config import CONFIG_FILE, STORE_FILE, Instance, ProblemsError
from horae.config.documents import read_yaml, validate
from horae.contracts.instance import CORE_SECRET_NAMES, EnvSecretRef, InstanceConfig
from horae.contracts.lockfile import LockedPlugin, Lockfile
from horae.contracts.plugin import Permissions, PluginManifest
from horae.contracts.types import PluginName
from horae.marketplace import (
    ApprovalRefused,
    InstallRefused,
    InvalidReference,
    MarketplaceError,
    parse_ref,
    resolve,
)
from horae.marketplace.approval import (
    describe,
    lock_mismatch,
    parse_grants,
    require_installable,
    with_grants,
)
from horae.marketplace.audit import (
    Drift,
    RecordInvalid,
    leftovers,
    pipeline_drift,
    read_record,
    record_mismatch,
)
from horae.marketplace.direct import is_direct
from horae.marketplace.errors import DowngradeRefused, NotInstalled, PluginEnabled
from horae.marketplace.install import MANIFEST_FILE
from horae.marketplace.lock import LOCK_FILE, guard, read_lock, write_lock
from horae.marketplace.pipelines import PIPELINES_DIR, PackPipelines, delete_own, pack_path
from horae.marketplace.semver import compare
from horae.marketplace.upgrade import (
    OLD_PREFIX,
    REMOVED_PREFIX,
    Change,
    Swap,
    delete_tree,
    describe_diff,
    permission_diff,
    user_grants,
    widens,
)
from horae.plugins.host import HostRegistry, RegistryError
from horae.plugins.host.env import plugin_venv
from horae.store import Store, StoreError

plugin_app = cli_plugin.plugin_app
_name: TypeAdapter[str] = TypeAdapter(PluginName)

NOT_INTERACTIVE = (
    "the upgrade needs your approval: run `horae plugin upgrade` in a terminal to review it, or "
    "pass --yes when nothing widens (or horae.lock already approves the target); nothing changed"
)


# --- what is installed -----------------------------------------------------------------------


def _plugin_name(name: str) -> str:
    try:
        return _name.validate_python(name)
    except ValidationError:
        raise InvalidReference(f"{name!r} is not a plugin name") from None


def _tree(root: Path) -> os.stat_result | None:
    """`lstat` of `plugins/<name>`, or `None` when it is absent."""
    try:
        return os.lstat(root)
    except FileNotFoundError:
        return None


def _recorded(name: str, root: Path) -> dict[str, Any]:
    """The install record of `plugins/<name>`, which must be a real directory installed by
    `horae plugin add` for this name; `NotInstalled` or `InstallRefused` otherwise."""
    info = _tree(root)
    if info is None:
        raise NotInstalled(
            f"plugin {name!r} has no tree in {root}: run `horae plugin remove {name}` to drop "
            "its horae.lock entry, then `horae plugin add`"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise InstallRefused(f"{root} is not a directory (a symlink?): refusing to touch it")
    try:
        record = read_record(root)
    except RecordInvalid as exc:
        raise InstallRefused(f"plugin {name}: {exc}") from None
    if record is None:
        raise NotInstalled(
            f"plugin {name!r} was not installed by `horae plugin add` (a built-in plugin, or "
            "one copied by hand): it has no install record"
        )
    if record["name"] != name:
        raise InstallRefused(f"{root} holds the install record of {record['name']!r}, not {name!r}")
    return record


def _manifest_permissions(root: Path) -> Permissions | None:
    """The permissions the installed manifest declares, or `None` when it cannot be read."""
    try:
        return validate(read_yaml(root / MANIFEST_FILE), PluginManifest).permissions
    except (ProblemsError, OSError, ValueError):
        return None


# --- upgrade ---------------------------------------------------------------------------------


class _UpgradeApproval:
    """The `prepare` hook of an upgrade: the permission diff of the verified new manifest
    against the approved lock entry, then the approval (interactive, or `--yes` when nothing
    widens). A refusal raises `ApprovalRefused`: nothing is placed."""

    def __init__(
        self,
        old: LockedPlugin,
        grants: tuple[list[str], list[str]],
        repo: str,
        sha: str,
        *,
        yes: bool,
        json_output: bool,
        pipelines: PackPipelines,
    ) -> None:
        self.old = old
        self.hosts, self.local = grants
        self.repo, self.sha = repo, sha
        self.yes = yes
        self.json_output = json_output
        self.pipelines = pipelines
        self.entry: LockedPlugin | None = None
        self.changes: list[Change] = []

    def __call__(self, _tree: Path, manifest: PluginManifest) -> None:
        name, version = manifest.name, manifest.version
        granted = with_grants(manifest.permissions, self.hosts, self.local)
        wanted = LockedPlugin(
            version=version,
            repo=self.repo,
            commit=self.sha,
            permissions=granted,
            personal_fields=manifest.provides.personal_fields,
            pipelines=self.pipelines.digests,
        )
        changes = permission_diff(self.old, wanted)
        err = self.json_output
        echo(
            f"upgrade {name}: {self.old.version} ({self.old.commit}) -> {version} ({self.sha})",
            err=err,
        )
        for line in describe_diff(changes):
            echo(safe(line), err=err)
        if widens(changes):
            # The full request, so what is new is read in context (pipelines with their calls).
            for line in describe(
                name,
                version,
                manifest.permissions,
                granted,
                manifest.provides.personal_fields,
                self.pipelines.approval_lines(),
            ):
                echo(safe(line), err=err)
        if self.yes:
            if widens(changes) and lock_mismatch(self.old, wanted) is not None:
                new = ", ".join(f"{c.field} {c.item}" for c in changes if c.widens)
                raise ApprovalRefused(
                    f"--yes approves an upgrade only when nothing widens: {new} need your "
                    "approval. Run without --yes to review it; nothing changed"
                )
        elif not cli_plugin.interactive():
            raise ApprovalRefused(NOT_INTERACTIVE)
        else:
            question = f"Approve and upgrade {name} to {version}?"
            try:
                approved = typer.confirm(question, default=False, err=err)
            except (click.exceptions.Abort, typer.Abort):  # end of input, Ctrl-C
                approved = False
            if not approved:
                raise ApprovalRefused("upgrade not approved; nothing changed")
        self.entry, self.changes = wanted, changes


def _same_release(found_sha: str, record: dict[str, Any], version: str, name: str) -> None:
    """Refuse a version the marketplace now pins to another commit than the installed one."""
    if record["version"] == version and record["commit"] != found_sha:
        raise InstallRefused(
            f"the marketplace now pins {name}@{version} to {found_sha}, but {record['commit']} "
            "is installed for that version: refusing (a moved release, or a tampered index)"
        )


def _upgrade(
    instance: Path,
    ref: str,
    offline: bool,
    pre: bool,
    json_output: bool,
    yes: bool,
    allow_host: list[str],
    allow_local: list[str],
    allow_downgrade: bool,
) -> int:
    try:
        if is_direct(ref):
            raise InvalidReference(
                "upgrade takes a plugin name (name or name@version), not a git URL; a direct "
                "install is replaced with `horae plugin remove` then `horae plugin add URL@SHA`"
            )
        name, _version = parse_ref(ref)
        grants = parse_grants(allow_host, allow_local)
    except MarketplaceError as exc:
        raise cli_plugin._failed(exc) from None
    loaded_instance = load(instance)
    try:
        with guard(loaded_instance.state_dir):
            return _upgrade_locked(
                loaded_instance,
                instance,
                name,
                ref,
                grants,
                offline=offline,
                pre=pre,
                yes=yes,
                json_output=json_output,
                allow_downgrade=allow_downgrade,
            )
    except MarketplaceError as exc:
        raise cli_plugin._failed(exc) from None


def _upgrade_locked(
    loaded_instance: Instance,
    instance: Path,
    name: str,
    ref: str,
    grants: tuple[list[str], list[str]],
    *,
    offline: bool,
    pre: bool,
    yes: bool,
    json_output: bool,
    allow_downgrade: bool,
) -> int:
    lock_path = instance / LOCK_FILE
    lock, header = read_lock(lock_path)
    old = lock.plugins.get(name)
    if old is None:
        raise NotInstalled(f"plugin {name!r} is not in horae.lock: install it with `add`")
    root = plugins_dir(instance) / name
    record = _recorded(name, root)
    if old.source == "direct" or record.get("source") == "direct":
        require_installable(name, None, None)  # refused before M5 (docs/security.md T04)
        raise InstallRefused(
            f"{name} is a direct install: replace it with `horae plugin remove {name}` then "
            "`horae plugin add URL@SHA` (an upgrade resolves through a marketplace)"
        )
    config, loaded = cli_plugin._load_indexes(loaded_instance, offline)
    found = resolve(ref, loaded.indexes, pins=config.marketplace_pins, allow_prerelease=pre)
    require_installable(found.name, found.tier, found.index)
    _same_release(found.sha, record, found.version, name)
    if compare(found.version, record["version"]) < 0 and not allow_downgrade:
        raise DowngradeRefused(
            f"{name}@{found.version} is lower than the installed {record['version']}: pass "
            "--allow-downgrade to install it anyway"
        )
    if (
        (found.repo, found.sha) == (old.repo, old.commit)
        and not grants[0]
        and not grants[1]
        and not record_mismatch(record, name, old.version, old.repo, old.commit)
    ):
        return _report_current(name, old, json_output, list(loaded.warnings))
    kept_hosts, kept_local = user_grants(old.permissions, _manifest_permissions(root))
    all_grants = (sorted({*kept_hosts, *grants[0]}), sorted({*kept_local, *grants[1]}))
    for host in kept_hosts:
        echo(f"keeping the host you added: {host}", err=json_output)
    for endpoint in kept_local:
        echo(f"keeping the local endpoint you added: {endpoint}", err=json_output)
    # Only a pack's own directory is replaced; a plugin that installed no pipeline never
    # touches `pipelines/<name>/` (a directory there is the user's, refused like at `add`).
    pipelines = PackPipelines(
        instance, replacing=dict(old.pipelines) if old.pipelines else None, upgrade=True
    )
    approval = _UpgradeApproval(
        old,
        all_grants,
        found.repo,
        found.sha,
        yes=yes,
        json_output=json_output,
        pipelines=pipelines,
    )
    installed = cli_plugin.make_installer(plugins_dir(instance)).install(
        found.name,
        found.version,
        found.repo,
        found.sha,
        prepare=cli_plugin._chain(
            pipelines,
            approval,
            cli_plugin.make_environment(loaded_instance.state_dir / cli_plugin.UV_CACHE_DIR),
        ),
        tier=found.tier,
        index=found.index,
        replace=True,
    )
    staged = installed.path
    entry = approval.entry
    if entry is None or (
        installed.lock_entry(entry.permissions).model_copy(update={"pipelines": entry.pipelines})
        != entry
    ):
        delete_tree(staged)
        raise ApprovalRefused("the staged plugin is not the one approved; nothing changed")
    swap = Swap()
    try:
        old_tree = swap.aside(root, OLD_PREFIX)
        swap.move(staged, root)
        pipelines.write()
        swap.also_undo(pipelines.remove)
        write_lock(
            lock_path, Lockfile(schema_version=1, plugins={**lock.plugins, name: entry}), header
        )
    except BaseException as exc:
        failed = swap.rollback()
        delete_tree(staged)
        if failed:
            echo(f"warning: could not undo everything: {'; '.join(failed)}", err=True)
        if isinstance(exc, OSError):
            raise CommandFailed(
                EXIT_PROBLEMS,
                "upgrade_failed",
                f"cannot upgrade {name}: {exc.strerror or exc}; the previous version is kept",
            ) from None
        raise
    warnings = list(loaded.warnings)
    if not delete_tree(old_tree):
        warnings.append(f"the previous tree is left in {old_tree}; delete it")
    warnings += [f"kept {path} (not this pack's own file)" for path in pipelines.commit()]
    for warning in warnings:
        echo(f"warning: {safe(warning)}", err=True)
    return _report_upgrade(
        name,
        old,
        entry,
        approval.changes,
        found.tier,
        found.index,
        pipelines,
        warnings,
        json_output,
    )


def _report_current(name: str, old: LockedPlugin, json_output: bool, warnings: list[str]) -> int:
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "name": name,
                "version": old.version,
                "commit": old.commit,
                "upgraded": False,
                "warnings": warnings,
            }
        )
        return EXIT_OK
    echo(f"{name} is already at {old.version} ({old.commit}): nothing to upgrade")
    return EXIT_OK


def _report_upgrade(
    name: str,
    old: LockedPlugin,
    entry: LockedPlugin,
    changes: list[Change],
    tier: str,
    index: str,
    pipelines: PackPipelines,
    warnings: list[str],
    json_output: bool,
) -> int:
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "name": name,
                "from": {"version": old.version, "commit": old.commit, "repo": old.repo},
                "to": {"version": entry.version, "commit": entry.commit, "repo": entry.repo},
                "tier": tier,
                "index": index,
                "path": f"{PLUGINS_DIR}/{name}",
                "changes": [change.as_json() for change in changes],
                "widened": widens(changes),
                "permissions": entry.model_dump(mode="json")["permissions"],
                "pipelines": pipelines.paths,
                "upgraded": True,
                "warnings": warnings,
            }
        )
        return EXIT_OK
    echo(f"upgraded {name} from {old.version} to {entry.version}")
    echo(f"  repo    {entry.repo}")
    echo(f"  commit  {entry.commit} (tag v{entry.version} points to it)")
    echo(f"  tier    {tier}")
    for path in pipelines.paths:
        echo(f"  pipeline  {path}")
    echo("approved: horae.lock pins the new version with the permissions above.")
    return EXIT_OK


@plugin_app.command("upgrade", cls=JsonUsageCommand)
def upgrade_command(
    ref: Annotated[
        str,
        typer.Argument(metavar="NAME[@VERSION]", help="Installed plugin to upgrade."),
    ],
    instance: cli_plugin.InstanceOption = Path(),
    offline: cli_plugin.OfflineOption = False,
    pre: Annotated[
        bool, typer.Option("--pre", help="Also consider pre-releases when no version is named.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Approve without asking, only when nothing widens (no new host, secret, "
            "intent, path or pipeline) or horae.lock already approves the target.",
        ),
    ] = False,
    allow_host: Annotated[
        list[str] | None,
        typer.Option("--allow-host", metavar="HOST", help="Also grant this https host."),
    ] = None,
    allow_local: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-local", metavar="HOST:PORT", help="Also grant this local-network endpoint."
        ),
    ] = None,
    allow_downgrade: Annotated[
        bool,
        typer.Option("--allow-downgrade", help="Allow installing a lower version."),
    ] = False,
    json_output: cli_plugin.JsonOption = False,
) -> None:
    """Upgrade an installed plugin, showing the permission diff and asking again for anything new.

    The version is resolved as `horae plugin add` does (highest release, --pre, or NAME@VERSION;
    first-party only before M5). The new version is installed beside the old one with its own
    venv; what it asks for is compared with horae.lock: a new host, local endpoint, secret,
    intent, path or pack pipeline (or a changed one, or a dropped personal field) must be
    approved again. Hosts you added before are kept. Only after approval and a complete install
    are the trees swapped and horae.lock rewritten; on any failure the old version and the lock
    are left as they were. A lower version is refused without --allow-downgrade; a direct install
    is refused (before M5, and replaced by remove then add afterwards).

    Exit codes: 0 upgraded or already current, 1 configuration problem, unreachable marketplace
    or repository, or an invalid or unwritable horae.lock, 2 malformed reference or grant, 3
    not installed, refused (tier, downgrade, verification, approval), 5 horae.lock is being
    written by another command, 70 internal."""
    run_command(
        "plugin upgrade",
        json_output,
        lambda: _upgrade(
            instance,
            ref,
            offline,
            pre,
            json_output,
            yes,
            list(allow_host or ()),
            list(allow_local or ()),
            allow_downgrade,
        ),
    )


# --- remove ----------------------------------------------------------------------------------


def _secret_file(config: InstanceConfig, name: str) -> str | None:
    """The file of secret `name` in the secrets directory, `None` for an environment one."""
    ref = config.secrets.refs.get(name)
    if isinstance(ref, EnvSecretRef):
        return None
    return name if ref is None else ref.file


def _secrets(
    instance: Path,
    config: InstanceConfig,
    removed: LockedPlugin,
    remaining: Lockfile,
    delete: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """`(deleted, kept)`: the secrets of `removed` deleted (with `delete`), and those kept with
    the reason. Only a regular file in the secrets directory that nothing else uses is deleted,
    never through a symlink; values are never read."""
    names = sorted(removed.permissions.all_secrets)
    if not delete:
        return [], [{"name": n, "reason": "pass --delete-secrets to delete it"} for n in names]
    used = set(CORE_SECRET_NAMES)
    for plugin in remaining.plugins.values():
        used.update(plugin.permissions.all_secrets)
    used.update(spec.secret for spec in config.models.values() if spec.secret)
    used.update(monitor.secret for monitor in config.scheduler.monitors.values())
    used_files = {f for f in (_secret_file(config, n) for n in used) if f is not None}
    directory = instance / config.secrets.dir
    deleted: list[str] = []
    kept: list[dict[str, str]] = []
    for name in names:
        file = _secret_file(config, name)
        reason = None
        if name in used:
            reason = "another plugin or the configuration uses it"
        elif file is None:
            reason = "it is read from an environment variable"
        elif file in used_files:
            reason = "its file is shared with another secret"
        else:
            reason = _delete_secret_file(directory, file)
        if reason is None:
            deleted.append(name)
        elif reason != "absent":
            kept.append({"name": name, "reason": reason})
    return deleted, kept


def _delete_secret_file(directory: Path, file: str) -> str | None:
    """Delete `<directory>/<file>` if it is a regular file (checked through a descriptor of the
    directory opened without following a symlink); the reason it was kept, `"absent"`, or
    `None` when deleted."""
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"the secrets directory cannot be opened ({exc.strerror})"
    try:
        try:
            info = os.lstat(file, dir_fd=fd)
        except FileNotFoundError:
            return "absent"
        if not stat.S_ISREG(info.st_mode):
            return "its file is not a regular file (a symlink?)"
        try:
            os.unlink(file, dir_fd=fd)
        except OSError as exc:
            return f"cannot delete its file ({exc.strerror})"
        return None
    finally:
        os.close(fd)


def _remove(instance: Path, name: str, delete_secrets: bool, json_output: bool) -> int:
    try:
        name = _plugin_name(name)
    except MarketplaceError as exc:
        raise cli_plugin._failed(exc) from None
    config = _config(instance)
    try:
        if name in config.plugins:
            raise PluginEnabled(
                f"{name} is enabled (listed under plugins: in {CONFIG_FILE}): remove it from "
                f"there first, then run `horae plugin remove {name}` again; nothing was removed"
            )
        with guard(instance / config.state_dir):
            return _remove_locked(config, instance, name, delete_secrets, json_output)
    except MarketplaceError as exc:
        raise cli_plugin._failed(exc) from None


def _remove_locked(
    config: InstanceConfig, instance: Path, name: str, delete_secrets: bool, json_output: bool
) -> int:
    lock_path = instance / LOCK_FILE
    lock, header = read_lock(lock_path)
    entry = lock.plugins.get(name)
    root = plugins_dir(instance) / name
    record: dict[str, Any] | None = None
    if _tree(root) is not None:
        record = _recorded(name, root)
    elif entry is None:
        raise NotInstalled(f"plugin {name!r} is not installed (not in horae.lock nor {root})")
    approved = dict(entry.pipelines) if entry is not None else {}
    if record is not None:
        recorded = record.get("pipelines")
        listed = set(recorded) if isinstance(recorded, list) else set()
        approved = {p: d for p, d in approved.items() if pack_path(name, p) in listed}
    swap = Swap()
    aside = swap.aside(root, REMOVED_PREFIX) if record is not None else None
    remaining = Lockfile(
        schema_version=1, plugins={k: v for k, v in lock.plugins.items() if k != name}
    )
    if entry is not None:
        try:
            write_lock(lock_path, remaining, header)
        except OSError as exc:
            swap.rollback()
            raise CommandFailed(
                EXIT_PROBLEMS,
                "lock_write_failed",
                f"cannot write {lock_path}: {exc.strerror or exc}; nothing was removed",
            ) from None
    warnings: list[str] = []
    if aside is not None and not delete_tree(aside):
        warnings.append(f"could not delete {aside} entirely; delete it by hand")
    pack = instance / PIPELINES_DIR / name
    has_pack = record is not None and bool(record.get("pipelines"))
    kept_pipelines = delete_own(pack, approved) if approved or has_pack else []
    removed_pipelines = [
        pack_path(name, p)
        for p in sorted(approved)
        if str(pack / f"{p}.yaml") not in kept_pipelines
    ]
    deleted, kept_secrets = (
        _secrets(instance, config, entry, remaining, delete_secrets) if entry else ([], [])
    )
    kept_data = sorted(entry.permissions.paths.write) if entry else []
    for warning in warnings:
        echo(f"warning: {safe(warning)}", err=True)
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "name": name,
                "removed": {
                    "tree": record is not None,
                    "lock_entry": entry is not None,
                    "pipelines": removed_pipelines,
                    "secrets": deleted,
                },
                "kept": {
                    "pipelines": kept_pipelines,
                    "secrets": kept_secrets,
                    "data": kept_data,
                    "run_history": True,
                },
                "warnings": warnings,
            }
        )
        return EXIT_OK
    echo(f"removed {name}")
    if record is not None:
        echo(f"  tree and venv  {PLUGINS_DIR}/{name}")
    if entry is not None:
        echo("  horae.lock entry")
    for path in removed_pipelines:
        echo(f"  pipeline  {path}")
    for secret in deleted:
        echo(f"  secret  {secret}")
    echo("kept:")
    for path in kept_pipelines:
        echo(f"  {safe(path)} (not this pack's own file, or changed after approval)")
    for item in kept_secrets:
        echo(f"  secret {item['name']}: {item['reason']}")
    for path in kept_data:
        echo(f"  files it wrote under {safe(path)}")
    echo("  its run history in the store")
    return EXIT_OK


@plugin_app.command("remove", cls=JsonUsageCommand)
def remove_command(
    name: Annotated[str, typer.Argument(metavar="NAME", help="Installed plugin to remove.")],
    instance: cli_plugin.InstanceOption = Path(),
    delete_secrets: Annotated[
        bool,
        typer.Option(
            "--delete-secrets",
            help="Also delete the plugin's secret files that nothing else uses.",
        ),
    ] = False,
    json_output: cli_plugin.JsonOption = False,
) -> None:
    """Remove an installed plugin: its tree and venv, its pack pipelines, its horae.lock entry.

    Refused while the plugin is listed under plugins: in horae.yaml (disable it first). Only
    the pipelines it installed that still have their approved digest are deleted; a file you
    changed or added is kept. Its secrets are kept unless --delete-secrets (then only files of
    the secrets directory that no other plugin, model or monitor uses); the files it wrote and
    the run history are always kept. What was kept is listed.

    Exit codes: 0 removed, 1 configuration problem or an invalid or unwritable horae.lock, 2
    malformed name, 3 not installed by `horae plugin add`, or enabled, 5 horae.lock is being
    written by another command, 70 internal."""
    run_command(
        "plugin remove",
        json_output,
        lambda: _remove(instance, name, delete_secrets, json_output),
    )


# --- audit -----------------------------------------------------------------------------------


def _config(instance: Path) -> InstanceConfig:
    """`horae.yaml` alone (the audit must run on an instance whose plugins drifted, which the
    full loader refuses)."""
    try:
        config = validate(read_yaml(instance / CONFIG_FILE), InstanceConfig)
    except ProblemsError as exc:
        message = "; ".join(map(str, exc.problems[:3]))
        raise CommandFailed(EXIT_PROBLEMS, "config_invalid", message) from None
    prime_secrets(instance, config)
    return config


def _activity(store_path: Path, warnings: list[str]) -> dict[str, datetime]:
    if not store_path.is_file():
        return {}
    try:
        with Store.open(store_path, read_only=True) as store:
            return store.plugin_activity()
    except StoreError as exc:
        warnings.append(f"last activity unknown: the store cannot be read ({exc})")
        return {}


def _host_problem(name: str, entry: LockedPlugin, directory: Path) -> str | None:
    try:
        HostRegistry.from_directory(Lockfile(schema_version=1, plugins={name: entry}), directory)
    except ProblemsError as exc:
        return "; ".join(map(str, exc.problems[:3]))
    except (RegistryError, OSError) as exc:
        return str(exc)
    return None


def _audit_plugin(
    name: str, entry: LockedPlugin | None, instance: Path
) -> tuple[dict[str, Any], list[Drift]]:
    directory = plugins_dir(instance)
    root = directory / name
    drift: list[Drift] = []
    info = _tree(root)
    record: dict[str, Any] | None = None
    if info is None:
        drift.append(Drift("not_installed", f"{name}: in horae.lock but {root} is missing"))
    elif not stat.S_ISDIR(info.st_mode):
        drift.append(Drift("tree_invalid", f"{name}: {root} is not a directory (a symlink?)"))
        info = None
    else:
        try:
            record = read_record(root)
        except RecordInvalid as exc:
            drift.append(Drift("record_invalid", f"{name}: {exc}"))
    if entry is None:
        drift.append(Drift("not_locked", f"{name}: installed in {root} but not in horae.lock"))
    elif info is not None:
        differs = (
            record_mismatch(record, name, entry.version, entry.repo, entry.commit) if record else ""
        )
        if differs:
            drift.append(Drift("record_mismatch", f"{name}: {differs}"))
        elif problem := _host_problem(name, entry, directory):
            drift.append(Drift("host_refuses", f"{name}: {problem}"))
    if record is not None and info is not None and plugin_venv(root) is None:
        drift.append(Drift("venv_missing", f"{name}: {root}/.venv is missing or incomplete"))
    if entry is not None and entry.pipelines:
        drift += pipeline_drift(name, dict(entry.pipelines), instance / PIPELINES_DIR / name)
    source = (
        (record.get("source") or "marketplace")
        if record is not None
        else ("direct" if entry is not None and entry.source == "direct" else "local")
    )
    row: dict[str, Any] = {
        "name": name,
        "version": entry.version if entry is not None else None,
        "installed_version": record["version"] if record is not None else None,
        "source": source,
        "tier": record.get("tier") if record is not None else None,
        "index": record.get("index") if record is not None else None,
        "repo": entry.repo if entry is not None else None,
        "commit": entry.commit if entry is not None else None,
        "permissions": entry.model_dump(mode="json")["permissions"] if entry else None,
        "personal_fields": entry.model_dump(mode="json").get("personal_fields", {})
        if entry
        else {},
        "pipelines": sorted(entry.pipelines) if entry is not None else [],
    }
    return row, drift


def _trees(directory: Path) -> set[str]:
    """The names of `plugins/` entries that look like installed plugins (not hidden)."""
    try:
        return {
            entry.name
            for entry in directory.iterdir()
            if not entry.name.startswith(".") and _is_plugin_name(entry.name)
        }
    except OSError:
        return set()


def _is_plugin_name(text: str) -> bool:
    try:
        _name.validate_python(text)
    except ValidationError:
        return False
    return True


def _permission_summary(permissions: dict[str, Any] | None) -> list[str]:
    if not permissions:
        return []
    lines: list[str] = []
    for key in ("network", "network_local", "secrets", "optional_secrets", "intents"):
        if permissions.get(key):
            lines.append(f"{key}: {', '.join(permissions[key])}")
    paths = permissions.get("paths") or {}
    for access in ("read", "write"):
        if paths.get(access):
            lines.append(f"paths.{access}: {', '.join(paths[access])}")
    return lines or ["nothing (no network, secret, intent or path)"]


def _audit(instance: Path, json_output: bool) -> int:
    config = _config(instance)
    try:
        lock, _header = read_lock(instance / LOCK_FILE)
    except MarketplaceError as exc:
        raise cli_plugin._failed(exc) from None
    warnings: list[str] = []
    activity = _activity(instance / config.state_dir / STORE_FILE, warnings)
    names = sorted(set(lock.plugins) | _trees(plugins_dir(instance)))
    rows: list[dict[str, Any]] = []
    found: list[Drift] = []
    for name in names:
        row, drift = _audit_plugin(name, lock.plugins.get(name), instance)
        used = activity.get(name)
        row["enabled"] = name in config.plugins
        row["last_activity"] = used.isoformat() if used is not None else None
        row["drift"] = [d.as_json() for d in drift]
        rows.append(row)
        found += drift
    extra = leftovers(plugins_dir(instance), instance / PIPELINES_DIR)
    found += extra
    code = EXIT_DIFFERENT if found else EXIT_OK
    if json_output:
        echo_json(
            {
                "schema_version": SCHEMA_VERSION,
                "plugins": rows,
                "leftovers": [d.as_json() for d in extra],
                "drift": bool(found),
                "warnings": warnings,
            }
        )
        return code
    for warning in warnings:
        echo(f"warning: {safe(warning)}", err=True)
    _table(rows)
    for item in extra:
        echo(f"drift (leftover): {safe(item.message)}")
    echo(f"drift found: {len(found)} problem(s)" if found else "no drift")
    return code


def _table(rows: list[dict[str, Any]]) -> None:
    header = ("NAME", "VERSION", "SOURCE", "TIER", "ENABLED", "LAST ACTIVITY", "DRIFT")
    table = [
        (
            row["name"],
            row["version"] or row["installed_version"] or "-",
            row["source"],
            row["tier"] or "-",
            "yes" if row["enabled"] else "no",
            row["last_activity"] or "never",
            str(len(row["drift"])) if row["drift"] else "-",
        )
        for row in rows
    ]
    widths = [max(len(str(r[i])) for r in (header, *table)) for i in range(len(header))]
    for cells in (header, *table):
        text = "  ".join(str(cell).ljust(w) for cell, w in zip(cells, widths, strict=True))
        echo(safe(text.rstrip()))
    for row in rows:
        details = _permission_summary(row["permissions"])
        details += [f"pipelines: {', '.join(row['pipelines'])}"] if row["pipelines"] else []
        if not details and not row["drift"]:
            continue
        echo(f"{row['name']}:")
        for line in details:
            echo(f"  {safe(line)}")
        for item in row["drift"]:
            echo(f"  drift ({item['kind']}): {safe(item['message'])}")
    if not rows:
        echo("no plugin installed")


@plugin_app.command("audit", cls=JsonUsageCommand)
def audit_command(
    instance: cli_plugin.InstanceOption = Path(),
    json_output: cli_plugin.JsonOption = False,
) -> None:
    """List the installed plugins and check them against horae.lock.

    For each plugin of horae.lock or plugins/: its version, source (marketplace, direct, or
    local for a built-in or hand-copied one), tier, whether it is enabled in horae.yaml, the
    permissions horae.lock grants it, its pipelines and its last activity (the latest stored run
    that used it). Drift is reported: a locked plugin not installed or installed but not
    locked, an install record or manifest that is not the locked one, a missing venv, a pack
    pipeline missing, changed or not approved, and what an interrupted upgrade or remove left.

    Exit codes: 0 no drift, 1 configuration problem or an invalid horae.lock, 4 drift found,
    70 internal."""
    run_command("plugin audit", json_output, lambda: _audit(instance, json_output))


__all__ = ["audit_command", "remove_command", "upgrade_command"]
