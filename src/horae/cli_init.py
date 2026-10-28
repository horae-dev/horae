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
"""

import os
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

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


def _target(directory: Path) -> Path:
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise CommandFailed(
            EXIT_REFUSED, "target_invalid", f"{directory} exists and is not a directory"
        )
    if directory.is_dir() and any(directory.iterdir()):
        raise CommandFailed(
            EXIT_REFUSED,
            "target_not_empty",
            f"{directory} is not empty; `horae init` never overwrites (choose a new directory)",
        )
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def create(directory: Path) -> dict[str, Any]:
    """Create the example instance in `directory` (see the module docstring); returns what
    `--json` prints."""
    root = _target(directory)
    vault = root / VAULT_DIR
    for name in _TEMPLATE_FILES:
        _write(root / name, (TEMPLATE / name).read_text(encoding="utf-8"))
    _write(root / ".gitignore", _GITIGNORE)
    _write(root / LOCK_FILE, _LOCK_HEADER + yaml.safe_dump(lock_data(vault), sort_keys=False))
    vault.mkdir()
    (root / STATE_DIR).mkdir(mode=0o700)
    os.chmod(root / STATE_DIR, 0o700)
    install(root / PLUGINS_DIR)
    return {
        "schema_version": SCHEMA_VERSION,
        "instance": str(root),
        "vault": str(vault),
        "pipelines": [PIPELINE],
        "plugins": sorted(BUILTIN),
        "files": sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()),
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
    The directory must be new or empty: nothing is ever overwritten.

    Exit codes: 0 created, 1 the files or plugins cannot be written, 2 usage error, 3 the
    directory is not empty (or not a directory), 70 internal error. With --json the result
    is {schema_version, instance, vault, pipelines, plugins, files}; an error is
    {schema_version, error}."""
    run_command("init", json_output, lambda: _init(directory, json_output))


__all__ = ["LOCK_FILE", "PIPELINE", "TEMPLATE", "VAULT_DIR", "create", "init", "lock_data"]
