# SPDX-License-Identifier: Apache-2.0
"""Per-plugin virtual environments (#45, schemas decision 69).

`PluginEnvironment` is the `prepare` hook of `PluginInstaller.install` (decision 62 G): it runs
on the verified tree in the scratch directory, before the tree is renamed into place, and
builds the plugin's own venv in `<tree>/.venv`. A failure aborts the install, which leaves
nothing in `plugins/`. Plugins never share dependencies: each venv holds exactly what the
plugin's lock file pins.

**What a plugin must ship** (else the install is refused, before uv runs):

- `pyproject.toml` with a `[project]` table and a `[build-system]` whose `build-backend` is in
  `BUILD_BACKENDS` (hatchling, setuptools, uv_build, flit_core, pdm-backend), whose `requires`
  names only that backend's distribution (and `wheel` for setuptools), without a direct URL,
  and without `backend-path` (an in-tree backend); not `tool.uv.package = false`;
- `uv.lock`, committed, in which every package from an index or a URL carries a `sha256` hash
  for each of its files, and every other package is a directory, editable or path inside the
  plugin tree (a `git` source has no hash and is refused).

**What runs** (`uv`, found on the host's `PATH` unless given):

1. `uv venv --relocatable --no-project --python <python> <tree>/.venv`, where `<python>` is
   Horae's own interpreter unless given: the venv lives offline with what is already on the
   machine, and a relocatable venv keeps working once the tree is renamed;
2. `uv sync --locked --no-dev --no-editable --python <tree>/.venv/bin/python`: the lock is
   only read, never re-resolved or rewritten (`--locked` fails when it no longer matches
   `pyproject.toml`); every file is checked against its hash; the plugin's own project is
   built (by its backend, the one place plugin code can run at install time, see
   `docs/security.md` T22) and installed non-editable, so the tree's sources are never imported
   at run time. `requires-python` is honoured: a plugin that does not accept `<python>` is
   refused (no interpreter is ever downloaded, `UV_PYTHON_DOWNLOADS=never`).

**The uv environment** is built from scratch, nothing inherited but the proxy and CA settings
(`PASSTHROUGH`): `PATH` is `BASE_PATH`, `HOME` and `TMPDIR` an empty directory removed after,
`UV_NO_CONFIG=1` (and `--no-config`: no `uv.toml` or user config), `UV_CACHE_DIR` the
instance's cache (`<state_dir>/uv-cache` from the CLI), `UV_LINK_MODE=copy` (the venv does not
depend on the cache), `UV_PROJECT_ENVIRONMENT=<tree>/.venv`. No `UV_*` or `PIP_*` index
setting of the caller's reaches uv: the index is uv's default (PyPI) or the plugin's own
`pyproject.toml`; the hashes pin the bytes either way. `index_url` replaces the default index
and is a constructor argument only, never a setting of `horae.yaml` (tests pass a local
`file://` index).

**Limits**: each uv call at most `timeout_s` (default `UV_TIMEOUT_S`), the venv at most
`max_bytes` (`MAX_VENV_BYTES`, measured while uv runs: it is killed past it), `pyproject.toml`
and `uv.lock` at most `MAX_PROJECT_BYTES` each.

**At run time** the host starts the plugin from that venv (`horae.plugins.host.env`): a bare
`runtime.command[0]` (`python`, `python3`, a console script) is looked up in
`<plugin>/.venv/bin` first, the child's `PATH` starts with it, `VIRTUAL_ENV` names it, and
every plugin child has `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never` and `PIP_NO_INDEX=1`.
Neither uv nor the network is needed to run a plugin.

**Upgrade** (#47): a new version is a new tree, installed by the same `install(...,
prepare=PluginEnvironment(...))` into its own scratch directory, so it gets a fresh venv built
from its own lock; a venv is never upgraded in place.
"""

import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horae.contracts.plugin import PluginManifest
from horae.marketplace.errors import InstallError, InstallRefused
from horae.marketplace.git import WATCH_INTERVAL_S, kill_session, tree_bytes
from horae.plugins.host.env import BASE_PATH, VENV_DIR

PROJECT_FILE = "pyproject.toml"
LOCK_FILE = "uv.lock"
UV_TIMEOUT_S = 600
"""Seconds one uv call may take (a sync downloads and may build)."""
MAX_VENV_BYTES = 1024 * 1024 * 1024
MAX_PROJECT_BYTES = 4 * 1024 * 1024
MAX_ERROR_CHARS = 600
BUILD_BACKENDS: dict[str, str] = {
    "hatchling.build": "hatchling",
    "setuptools.build_meta": "setuptools",
    "uv_build": "uv-build",
    "flit_core.buildapi": "flit-core",
    "pdm.backend": "pdm-backend",
}
"""The build backends a plugin may declare, with the one distribution its `requires` names."""
_EXTRA_REQUIRES = {"setuptools.build_meta": frozenset({"wheel"})}
PASSTHROUGH = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)
"""What uv inherits from Horae's environment: how to reach the network, never what to fetch."""
_HASHED = ("registry", "url")
_IN_TREE = ("editable", "directory", "virtual", "path")
_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_LOCK_HINTS = ("lockfile needs to be updated", "--locked", "hash mismatch", "requires-python")


def normalize(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_toml(path: Path, plugin: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InstallRefused(f"plugin {plugin} has no {path.name}: its dependencies need one")
    if path.stat().st_size > MAX_PROJECT_BYTES:
        raise InstallRefused(f"plugin {plugin}: {path.name} is larger than {MAX_PROJECT_BYTES}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallRefused(f"plugin {plugin}: {path.name} is not valid TOML: {exc}") from None


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def check_project(tree: Path, plugin: str) -> str:
    """The project name of `<tree>/pyproject.toml`; `InstallRefused` unless it builds with an
    allowed backend and nothing else (see the module docstring)."""
    data = _read_toml(tree / PROJECT_FILE, plugin)
    name = _table(data, "project").get("name")
    if not isinstance(name, str) or not name:
        raise InstallRefused(f"plugin {plugin}: {PROJECT_FILE} has no [project] name")
    if _table(_table(data, "tool"), "uv").get("package") is False:
        raise InstallRefused(
            f"plugin {plugin}: tool.uv.package = false (a virtual project) is not installable"
        )
    build = _table(data, "build-system")
    backend = build.get("build-backend")
    if not isinstance(backend, str) or backend not in BUILD_BACKENDS:
        allowed = ", ".join(sorted(BUILD_BACKENDS))
        raise InstallRefused(
            f"plugin {plugin}: build-backend {backend!r} is not allowed (one of: {allowed})"
        )
    if "backend-path" in build:
        raise InstallRefused(f"plugin {plugin}: an in-tree build backend (backend-path) is refused")
    requires = build.get("requires", [])
    if not isinstance(requires, list) or not requires:
        raise InstallRefused(f"plugin {plugin}: build-system.requires must name {backend}")
    allowed_names = {BUILD_BACKENDS[backend]} | _EXTRA_REQUIRES.get(backend, frozenset())
    for requirement in requires:
        match = _NAME.match(requirement) if isinstance(requirement, str) else None
        if match is None or "@" in requirement or normalize(match[1]) not in allowed_names:
            raise InstallRefused(
                f"plugin {plugin}: build-system.requires may only name "
                f"{', '.join(sorted(allowed_names))}, not {requirement!r}"
            )
    return name


def _in_tree(where: object) -> bool:
    if not isinstance(where, str) or not where or "\0" in where or "\\" in where:
        return False
    if where.startswith("/") or re.match(r"^[A-Za-z]:", where):
        return False
    resolved = posixpath.normpath(where)
    return resolved != ".." and not resolved.startswith("../")


def _artifacts(package: dict[str, Any]) -> list[Any]:
    found: list[Any] = []
    if "sdist" in package:
        found.append(package["sdist"])
    wheels = package.get("wheels", [])
    found.extend(wheels if isinstance(wheels, list) else [wheels])
    return found


def check_lock(tree: Path, plugin: str) -> None:
    """`InstallRefused` unless `<tree>/uv.lock` pins every package by hash or to the tree."""
    data = _read_toml(tree / LOCK_FILE, plugin)
    packages = data.get("package", [])
    if not isinstance(packages, list) or not packages:
        raise InstallRefused(f"plugin {plugin}: {LOCK_FILE} lists no package")
    for package in packages:
        name = package.get("name", "?") if isinstance(package, dict) else "?"
        source = package.get("source") if isinstance(package, dict) else None
        if not isinstance(source, dict) or len(source) != 1:
            raise InstallRefused(f"plugin {plugin}: {LOCK_FILE}: {name} has no single source")
        ((kind, where),) = source.items()
        if kind in _HASHED:
            artifacts = _artifacts(package)
            if not artifacts or not all(
                isinstance(a, dict) and str(a.get("hash", "")).startswith("sha256:")
                for a in artifacts
            ):
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} has a file without a sha256 hash"
                )
        elif kind in _IN_TREE:
            if not _in_tree(where):
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} comes from {where!r}, outside the plugin"
                )
        else:
            raise InstallRefused(
                f"plugin {plugin}: {LOCK_FILE}: {name} comes from a {kind} source, which has "
                "no hash"
            )


class UvTooLarge(InstallRefused):
    """The venv grew past its budget; uv was killed."""


@dataclass(frozen=True)
class PluginEnvironment:
    """Builds a plugin's venv in its verified tree: the `prepare` hook of `install`."""

    cache_dir: Path
    uv: str | None = None
    """uv's path; default: `uv` on Horae's `PATH`, needed at install time only."""
    python: str = sys.executable
    """The base interpreter of every plugin venv (default: Horae's own)."""
    timeout_s: float = UV_TIMEOUT_S
    max_bytes: int = MAX_VENV_BYTES
    index_url: str | None = None
    """Replaces the default index (tests only: a local `file://` index). Never a setting."""

    def __call__(self, tree: Path, manifest: PluginManifest) -> None:
        self.prepare(tree, manifest.name)

    def prepare(self, tree: Path, plugin: str) -> Path:
        """Check the project and its lock, then build `<tree>/.venv`; returns it."""
        check_project(tree, plugin)
        check_lock(tree, plugin)
        venv = tree / VENV_DIR
        if os.path.lexists(venv):
            raise InstallRefused(f"plugin {plugin}: the tree holds a {VENV_DIR}; it must not")
        uv = self.uv or shutil.which("uv")
        if uv is None:
            raise InstallError(
                f"cannot install the dependencies of {plugin}: uv is not installed "
                "(it is needed to install a plugin, not to run one)"
            )
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise InstallError(f"cannot write {self.cache_dir}: {exc.strerror}") from None
        with tempfile.TemporaryDirectory(prefix="horae-uv-") as home:
            env = self.env(tree, Path(home))
            self._run(
                plugin,
                [uv, "venv", "--relocatable", "--no-project", "--python", self.python],
                [str(venv)],
                tree,
                env,
                venv,
            )
            sync = ["sync", "--locked", "--no-dev", "--no-editable"]
            self._run(
                plugin,
                [uv, *sync, "--python", str(venv / "bin" / "python")],
                self._index_args(),
                tree,
                env,
                venv,
            )
        self._check_venv(venv, plugin)
        return venv

    def env(self, tree: Path, home: Path) -> dict[str, str]:
        """uv's environment (see the module docstring)."""
        env = {key: value for key, value in os.environ.items() if key in PASSTHROUGH}
        env.update(
            PATH=BASE_PATH,
            HOME=str(home),
            TMPDIR=str(home),
            LANG="C.UTF-8",
            UV_NO_CONFIG="1",
            UV_CACHE_DIR=str(self.cache_dir),
            UV_PYTHON_DOWNLOADS="never",
            UV_PROJECT_ENVIRONMENT=str(tree / VENV_DIR),
            UV_LINK_MODE="copy",
            UV_NO_PROGRESS="1",
        )
        return env

    def _index_args(self) -> list[str]:
        return ["--default-index", self.index_url] if self.index_url else []

    def _check_venv(self, venv: Path, plugin: str) -> None:
        cfg = venv / "pyvenv.cfg"
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            raise InstallError(f"plugin {plugin}: uv wrote no {VENV_DIR}/pyvenv.cfg") from None
        if not re.search(r"^relocatable\s*=\s*true\s*$", text, re.MULTILINE):
            # uv replaced the venv (another interpreter): its scripts name the scratch path.
            raise InstallRefused(f"plugin {plugin}: uv rebuilt the venv with another interpreter")
        size = tree_bytes(venv)
        if size > self.max_bytes:
            raise UvTooLarge(
                f"plugin {plugin}: its venv is {size} bytes, more than {self.max_bytes}"
            )

    def _run(
        self,
        plugin: str,
        command: Sequence[str],
        extra: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        watch: Path,
    ) -> None:
        argv = [command[0], "--no-config", *command[1:], *extra]
        try:
            proc = subprocess.Popen(  # noqa: S603 (argv only, a fixed uv command)
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise InstallError(f"cannot run uv: {exc.strerror or type(exc).__name__}") from None
        deadline = time.monotonic() + self.timeout_s
        while True:
            left = deadline - time.monotonic()
            try:
                _stdout, stderr = proc.communicate(timeout=max(0.0, min(WATCH_INTERVAL_S, left)))
                break
            except subprocess.TimeoutExpired:
                pass
            except BaseException:
                kill_session(proc)
                raise
            if tree_bytes(watch) > self.max_bytes:
                kill_session(proc)
                raise UvTooLarge(f"plugin {plugin}: its venv grew past {self.max_bytes} bytes")
            if time.monotonic() >= deadline:
                kill_session(proc)
                raise InstallError(f"plugin {plugin}: uv timed out after {self.timeout_s:g}s")
        if proc.returncode != 0:
            reason = " ".join(stderr.decode("utf-8", errors="replace").split())
            reason = reason[-MAX_ERROR_CHARS:]
            message = f"cannot install the dependencies of {plugin}: uv {command[1]}: {reason}"
            if any(hint in reason.lower() for hint in _LOCK_HINTS):
                raise InstallRefused(message)
            raise InstallError(message)


__all__ = [
    "BUILD_BACKENDS",
    "LOCK_FILE",
    "MAX_VENV_BYTES",
    "PROJECT_FILE",
    "UV_TIMEOUT_S",
    "PluginEnvironment",
    "check_lock",
    "check_project",
]
