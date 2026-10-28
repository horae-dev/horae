# SPDX-License-Identifier: Apache-2.0
"""`horae init [DIR]` (#26): a working example instance, the walking skeleton.

It writes into `DIR` (default: the current directory), which must not exist or be an empty
directory (anything else is refused, exit 3; nothing is ever overwritten):

- `horae.yaml` and `pipelines/daily-fixture.yaml`, the files of `examples/walking-skeleton`
  (packaged in `horae/init_template`; a test keeps them identical);
- `horae.lock`, generated: both built-in plugins (`horae.plugins.builtin`) pinned at their
  version with the placeholder commit (decision 36) and the permissions their manifests ask
  for; `file-sink`'s `paths.write` is the instance's own `vault/` (an absolute path);
- `vault/`, the vault `file-sink` writes to (move it, or point the lock elsewhere, later);
- `state/` (mode 0700), where the store and the locks go;
- `plugins/`, the built-in plugins installed (`install`, pinned to this interpreter);
- `.gitignore` for `state/`, `plugins/` and `vault/`.

Afterwards `horae doctor` passes and `horae run daily-fixture` writes today's note in
`vault/Daily/`.

**Atomic.** The lock is validated first (a path it cannot hold, such as one with a
backslash, is `target_invalid`, exit 1, nothing written). The instance is then built in a temporary
directory and moved into place: for a new `DIR`, a hidden sibling (`.horae-init-*` in its
parent) renamed to `DIR` in one `rename`; for an existing empty `DIR` (opened with
`O_NOFOLLOW`, so a symbolic link is refused), a hidden directory inside it whose entries are
then renamed up one by one (the directory itself is kept: a shell whose current directory it
is stays there). On any failure the temporary directory, the entries already moved and the
parent directories `init` created are removed: nothing is left behind.

**Not relocatable** (v1): `horae.lock` holds the vault's absolute path and the installed
plugins the absolute path of this interpreter, so a copied or moved instance must be fixed
by hand (`horae doctor` reports it) or created again.
"""

import contextlib
import errno
import os
import secrets
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError

from horae.cli_host import PLUGINS_DIR
from horae.cli_output import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    SCHEMA_VERSION,
    CommandFailed,
    echo_json,
    run_command,
    safe,
)
from horae.config import CONFIG_FILE
from horae.contracts import Lockfile, PluginManifest
from horae.plugins.builtin import BUILTIN, InstallError, install, manifest_data

TEMPLATE = Path(__file__).resolve().parent / "init_template"
LOCK_FILE = "horae.lock"
VAULT_DIR = "vault"
STATE_DIR = "state"
PIPELINE = "daily-fixture"
REPO = "https://github.com/horae-dev/horae.git"
PLACEHOLDER_COMMIT = "0" * 40
_TEMPLATE_FILES = (CONFIG_FILE, f"pipelines/{PIPELINE}.yaml")
_GITIGNORE = """\
# Created by `horae init`: local state, installed plugins and the vault.
state/
plugins/
vault/
"""
_LOCK_HEADER = f"""\
# horae.lock written by `horae init`: the two built-in plugins of the walking skeleton.
# `commit` is a placeholder (the plugins ship inside the core; nothing fetches it).
# file-sink writes to its single paths.write entry: this instance's {VAULT_DIR}/ directory.
"""


def lock_data(vault: Path) -> dict[str, Any]:
    """`horae.lock` pinning the built-in plugins, with `file-sink` writing to `vault`."""
    plugins: dict[str, Any] = {}
    for name in sorted(BUILTIN):
        manifest = PluginManifest.model_validate(manifest_data(name))
        permissions = manifest.permissions.model_dump(mode="json")
        if name == "file-sink":
            permissions["paths"] = {"write": [str(vault)]}
        plugins[name] = {
            "version": manifest.version,
            "repo": REPO,
            "commit": PLACEHOLDER_COMMIT,
            "permissions": permissions,
        }
    data = {"schema_version": 1, "plugins": plugins}
    Lockfile.model_validate(data)  # what `horae doctor` will check
    return data


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as out:  # never overwrite
        out.write(text)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _invalid(directory: Path, why: str = "exists and is not a directory") -> CommandFailed:
    return CommandFailed(EXIT_REFUSED, "target_invalid", f"{directory} {why}")


def _split(directory: Path) -> tuple[Path, str]:
    """The target's (absolute, lexically normalised) parent and name."""
    absolute = Path(os.path.abspath(directory))
    if absolute.parent == absolute:
        raise _invalid(directory, "is the root directory")
    return absolute.parent, absolute.name


def _make_parents(parent: Path) -> list[Path]:
    """Create `parent` and its missing ancestors; returns those created, deepest first."""
    missing: list[Path] = []
    current = parent
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    parent.mkdir(parents=True, exist_ok=True)
    return missing


def _open_target(parent_fd: int, name: str, directory: Path) -> int | None:
    """`None` when the target does not exist, else a descriptor of it (an empty directory,
    opened with `O_NOFOLLOW`); refuses anything else."""
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(status.st_mode):  # a file, a symbolic link…
        raise _invalid(directory)
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _invalid(directory) from None
        raise
    if os.listdir(fd):
        os.close(fd)
        raise _not_empty(directory)
    return fd


def _not_empty(directory: Path) -> CommandFailed:
    return CommandFailed(
        EXIT_REFUSED,
        "target_not_empty",
        f"{directory} is not empty; `horae init` never overwrites (choose a new directory)",
    )


def _build(where: Path, lock: dict[str, Any]) -> list[str]:
    """Write the instance's files into the (empty, temporary) directory `where`; returns
    the files written, relative to it."""
    for name in _TEMPLATE_FILES:
        _write(where / name, (TEMPLATE / name).read_text(encoding="utf-8"))
    _write(where / ".gitignore", _GITIGNORE)
    _write(where / LOCK_FILE, _LOCK_HEADER + yaml.safe_dump(lock, sort_keys=False))
    (where / VAULT_DIR).mkdir()
    (where / STATE_DIR).mkdir(mode=0o700)
    os.chmod(where / STATE_DIR, 0o700)
    install(where / PLUGINS_DIR)
    return sorted(str(path.relative_to(where)) for path in where.rglob("*") if path.is_file())


def _default_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o777 & ~mask


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _create_new(parent: Path, parent_fd: int, name: str, directory: Path, lock: Any) -> list[str]:
    """A new target: built in a hidden sibling, renamed into place in one `rename`."""
    temporary = Path(tempfile.mkdtemp(prefix=".horae-init-", dir=parent))
    try:
        files = _build(temporary, lock)
        os.chmod(temporary, _default_mode())
        try:
            os.rename(temporary.name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except OSError as error:
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise _not_empty(directory) from None  # created meanwhile
            if error.errno == errno.ENOTDIR:
                raise _invalid(directory) from None
            raise
    except BaseException:
        _remove(temporary)
        raise
    return files


def _fill_empty(root: Path, target_fd: int, directory: Path, lock: Any) -> list[str]:
    """An existing empty target: built in a hidden directory inside it, whose entries are
    renamed up into it (the target itself is kept)."""
    hidden = f".horae-init-{secrets.token_hex(6)}"
    os.mkdir(hidden, 0o700, dir_fd=target_fd)
    moved: list[str] = []
    try:
        files = _build(root / hidden, lock)
        hidden_fd = os.open(hidden, _DIR_FLAGS, dir_fd=target_fd)
        try:
            entries = sorted(os.listdir(hidden_fd))
            if set(os.listdir(target_fd)) != {hidden}:
                raise _not_empty(directory)  # something appeared meanwhile
            for entry in entries:
                os.rename(entry, entry, src_dir_fd=hidden_fd, dst_dir_fd=target_fd)
                moved.append(entry)
        finally:
            os.close(hidden_fd)
        os.rmdir(hidden, dir_fd=target_fd)
    except BaseException:
        for entry in moved:
            _remove(root / entry)
        _remove(root / hidden)
        raise
    return files


def create(directory: Path) -> dict[str, Any]:
    """Create the example instance in `directory` (see the module docstring); returns what
    `--json` prints."""
    parent, name = _split(directory)
    root = parent.resolve() / name
    vault = root / VAULT_DIR
    try:
        lock = lock_data(vault)
    except ValidationError:
        raise CommandFailed(
            EXIT_PROBLEMS,
            "target_invalid",
            f"{directory}: horae.lock cannot hold this path (it must be a normalised absolute "
            "POSIX path, without `\\` or control characters); nothing was written",
        ) from None
    created = _make_parents(parent)
    try:
        parent_fd = os.open(parent, _DIR_FLAGS)
        try:
            target_fd = _open_target(parent_fd, name, directory)
            if target_fd is None:
                files = _create_new(parent, parent_fd, name, directory, lock)
            else:
                try:
                    files = _fill_empty(root, target_fd, directory, lock)
                finally:
                    os.close(target_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        for path in created:  # deepest first; only if still empty
            with contextlib.suppress(OSError):
                path.rmdir()
        raise
    return {
        "schema_version": SCHEMA_VERSION,
        "instance": str(root),
        "vault": str(vault),
        "pipelines": [PIPELINE],
        "plugins": sorted(BUILTIN),
        "files": files,
    }


def _init(directory: Path, json_output: bool) -> int:
    try:
        report = create(directory)
    except InstallError as exc:
        raise CommandFailed(EXIT_PROBLEMS, "install_failed", str(exc)) from None
    except OSError as exc:
        raise CommandFailed(
            EXIT_PROBLEMS, "init_failed", f"{directory}: {exc.strerror or type(exc).__name__}"
        ) from None
    if json_output:
        echo_json(report)
    else:
        root = report["instance"]
        typer.echo(f"created a Horae instance in {safe(root)}")
        typer.echo(f"  vault: {safe(report['vault'])}")
        typer.echo(f"  pipeline: {PIPELINE}; plugins: {', '.join(report['plugins'])}")
        typer.echo(
            "  not relocatable: horae.lock holds absolute paths and plugins/ this "
            "interpreter; to move it, run `horae init` again (`horae doctor` checks)"
        )
        typer.echo("next:")
        for step in ("doctor", f"run {PIPELINE} --dry-run", f"run {PIPELINE}", "runs"):
            typer.echo(f"  horae {step} -i {safe(root)}")
    return EXIT_OK


def init(
    directory: Annotated[
        Path, typer.Argument(help="Where to create the instance (new or empty directory).")
    ] = Path(),
    json_output: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Create a working example instance: the walking skeleton with its plugins installed.

    Writes horae.yaml, pipelines/daily-fixture.yaml, horae.lock (the built-in plugins
    pinned, file-sink writing to the instance's vault/), state/, plugins/ and .gitignore.
    The directory must be new or empty: nothing is ever overwritten, and on a failure
    nothing is left behind (the instance is built aside, then moved into place). The
    instance is not relocatable: horae.lock and plugins/ hold absolute paths.

    Exit codes: 0 created, 1 the files or plugins cannot be written, or a path horae.lock
    cannot hold (target_invalid), 2 usage error, 3 the directory is not empty, or not a
    directory (a file, a symbolic link), 70 internal error. With --json the result
    is {schema_version, instance, vault, pipelines, plugins, files}; an error is
    {schema_version, error}."""
    run_command("init", json_output, lambda: _init(directory, json_output))


__all__ = ["LOCK_FILE", "PIPELINE", "TEMPLATE", "VAULT_DIR", "create", "init", "lock_data"]
