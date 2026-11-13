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
- `uv.lock`, committed, in which every package but the plugin's own project comes from an
  index or a URL, has at least one **wheel**, and carries a `sha256` hash for each of its files
  (an sdist-only package, a `git` source, another in-tree package are refused, by name). The
  project itself is the tree (`.`).

**What runs** (`uv`, found on the host's `PATH` unless given):

1. `uv venv --relocatable --no-project --python <python> <tree>/.venv`, where `<python>` is
   Horae's own interpreter unless given: the venv lives offline with what is already on the
   machine, and a relocatable venv keeps working once the tree is renamed;
2. `uv sync --locked --no-dev --no-editable --python <tree>/.venv/bin/python
   --no-build-package=<each dependency>`: dependencies install from their hashed wheels only,
   never built (no wheel fits this machine: refused, not built from the sdist); the lock is
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

**Limits**: each uv call at most `timeout_s` (default `UV_TIMEOUT_S`); what the install
writes, the venv plus uv's temporary directory plus what it adds to the cache, at most
`max_bytes` (`MAX_VENV_BYTES`, measured while uv runs: it is killed past it);
`pyproject.toml` and `uv.lock` at most `MAX_PROJECT_BYTES` each.

**A plugin without a runtime** (a pipeline pack: `provides.pipelines` / `provides.skills`
only, decision 18) is never started, so it gets **no venv** (#200): `__call__` returns at once,
whatever its `pyproject.toml` and `uv.lock` say (a pack's are for its own tests), and uv never
runs. `horae plugin audit` does not expect a venv for it either.

**At run time** the host starts the plugin from that venv (`horae.plugins.host.env`): a bare
`runtime.command[0]` (`python`, `python3`, a console script) is looked up in
`<plugin>/.venv/bin` first, the child's `PATH` starts with it, `VIRTUAL_ENV` names it, and
every plugin child has `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never` and `PIP_NO_INDEX=1`.
Neither uv nor the network is needed to run a plugin.

**Without the build (#191)**: a plugin that is not first-party gets `install_project=False`.
Its own project is **never built**: a build backend runs plugin code (`setup.py`, a hatch
build hook, `pdm_build.py`) on the host, outside any container. Then:

- `check_project` also refuses `dynamic` in `[project]` (uv would call the backend for the
  metadata, even with `--no-install-project`) and keeps refusing `backend-path`;
- also refused: a uv workspace, a `tool.uv.sources` entry that is not an index (path, git,
  url, workspace, editable), a direct reference (`name @ ...`) in the dependencies, optional
  dependencies or dependency groups; and, as for every plugin, a lock entry that is not a
  hashed index or URL package (path, git, directory, editable, virtual), but the root project;
- `uv sync --locked --no-dev --no-install-project --no-build --no-build-package=<each
  dependency>`: the dependencies only, from their hashed wheels, uv refusing to build anything
  at all; the lock is still checked against a static `pyproject.toml`;
- Horae writes `site-packages/_horae_plugin.pth` (`PTH_FILE`) into the venv, naming the
  project's source directory **as the container sees it** (`CONTAINER_ROOT`, the plugin's
  mount): `/plugin/src` when the tree has a real `src/` directory (not a symlink), else
  `/plugin`. Such a plugin only ever runs in a container (decision 79);
- its `runtime.command` must be the venv's interpreter running a module (`[python, -m,
  <module>]`, `python3` too): its console scripts are never generated (`check_module_command`,
  refused before approval otherwise).

**Trust classes never share a cache (#191, review of PR #192)**: a plugin that is not
first-party gets its own uv cache inside the install's temporary home, deleted with it; the
persistent `cache_dir` serves first-party installs only. Otherwise a third-party lock naming a
build backend's wheel (a `flit-core`, say) under the real index but on another host would put
the attacker's wheel in the index's cache bucket, and the next first-party build, whose backend
is resolved without hashes, would import it on the host. `check_lock` also requires every file
of a registry package to come from that registry's own host over https (or `FILES_HOSTS`: PyPI
serves from `files.pythonhosted.org/packages/`), or from inside a local directory index; for a
plugin that is not first-party the registry must be `DEFAULT_INDEX` and a `url` source is
refused (no fetch of any URL a third-party lock names).

**Upgrade** (#47): a new version is a new tree, installed by the same `install(...,
prepare=PluginEnvironment(...))` into its own scratch directory, so it gets a fresh venv built
from its own lock; a venv is never upgraded in place.
"""

import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from horae.contracts.plugin import PluginManifest
from horae.marketplace.cachefs import make_private_dirs
from horae.marketplace.errors import InstallError, InstallRefused
from horae.marketplace.git import WATCH_INTERVAL_S, kill_session, tree_bytes
from horae.plugins.host.env import BASE_PATH, VENV_DIR

PROJECT_FILE = "pyproject.toml"
LOCK_FILE = "uv.lock"
PTH_FILE = "_horae_plugin.pth"
"""Written into the venv's `site-packages` for a plugin installed without its build (#191)."""
CONTAINER_ROOT = "/plugin"
"""Where the plugin's tree is mounted in its container (`horae.plugins.host.container.
PLUGIN_MOUNT`, kept equal by a test; not imported, to keep this module free of the host)."""
MODULE_COMMANDS = frozenset({"python", "python3"})
"""The `runtime.command[0]` a plugin installed without its build may use (#191)."""
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


def check_project(tree: Path, plugin: str, *, builds: bool = True) -> str:
    """The project name of `<tree>/pyproject.toml`; `InstallRefused` unless it builds with an
    allowed backend and nothing else (see the module docstring). `builds=False` (#191): also
    refused when uv would still call the backend (`dynamic` metadata)."""
    data = _read_toml(tree / PROJECT_FILE, plugin)
    project = _table(data, "project")
    name = project.get("name")
    if not isinstance(name, str) or not name:
        raise InstallRefused(f"plugin {plugin}: {PROJECT_FILE} has no [project] name")
    if not builds:
        _check_nothing_to_build(data, project, plugin)
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


_SOURCE_KEYS = frozenset({"index", "marker", "extra", "group"})
"""What a `tool.uv.sources` entry may say for a plugin that is not first-party: which index,
for which marker, extra or group. Never `path`, `git`, `url`, `workspace` or `editable`."""


def _requirements(data: dict[str, Any], project: dict[str, Any]) -> list[Any]:
    found: list[Any] = list(project.get("dependencies") or [])
    optional = project.get("optional-dependencies")
    for group in (optional or {}).values() if isinstance(optional, dict) else ():
        found.extend(group if isinstance(group, list) else [group])
    groups = data.get("dependency-groups")
    for group in (groups or {}).values() if isinstance(groups, dict) else ():
        found.extend(group if isinstance(group, list) else [group])
    return found


def _check_nothing_to_build(data: dict[str, Any], project: dict[str, Any], plugin: str) -> None:
    """For a plugin that is not first-party (#191): refuse what would make uv build or call a
    backend despite `--no-install-project --no-build` (decision 79 G)."""
    refused = (
        f"plugin {plugin}: refused for a plugin that is not first-party (its code is never "
        "built on the host):"
    )
    if project.get("dynamic"):
        raise InstallRefused(
            f"{refused} [project] dynamic metadata (uv would run the build backend to compute "
            "it); declare every field statically"
        )
    uv = _table(_table(data, "tool"), "uv")
    if "workspace" in uv:
        raise InstallRefused(f"{refused} a uv workspace (tool.uv.workspace)")
    sources = uv.get("sources", {})
    for name, entries in sources.items() if isinstance(sources, dict) else [("?", sources)]:
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict) or set(entry) - _SOURCE_KEYS:
                raise InstallRefused(
                    f"{refused} tool.uv.sources.{name} is not an index source (a path, git, url "
                    "or workspace source would be built)"
                )
    for requirement in _requirements(data, project):
        if isinstance(requirement, dict):
            if set(requirement) != {"include-group"}:
                raise InstallRefused(f"{refused} a dependency group entry {requirement!r}")
        elif not isinstance(requirement, str) or "@" in requirement.split(";", 1)[0]:
            raise InstallRefused(
                f"{refused} the direct reference {requirement!r} (a local or remote project "
                "would be built)"
            )


def check_module_command(manifest: PluginManifest) -> None:
    """`InstallRefused` unless the plugin's command runs the venv's interpreter (`[python, -m,
    <module>]`): installed without its build (#191), it has no console script."""
    runtime = manifest.runtime
    command = list(getattr(runtime, "command", None) or [])
    if not command or command[0] not in MODULE_COMMANDS:
        raise InstallRefused(
            f"plugin {manifest.name}: a plugin that is not first-party is installed without "
            "building its project, so it has no console script: its runtime.command must run "
            "a module with the venv's interpreter, e.g. [python, -m, <module>]"
        )


def write_pth(tree: Path, venv: Path, plugin: str) -> str:
    """Make the project importable without installing it (#191): `PTH_FILE` in the venv's
    `site-packages`, naming `src/` (a real directory of the tree) or the tree's root, as the
    container sees it. Returns the path written into it."""
    try:
        source = stat_is_dir(tree / "src")
    except OSError:
        source = False
    target = posixpath.join(CONTAINER_ROOT, "src") if source else CONTAINER_ROOT
    found = [
        p
        for p in sorted((venv / "lib").glob("python*/site-packages"))
        if not p.is_symlink() and p.is_dir()
    ]
    if len(found) != 1:
        raise InstallError(f"plugin {plugin}: the venv has no single site-packages directory")
    pth = found[0] / PTH_FILE
    if os.path.lexists(pth):
        raise InstallRefused(f"plugin {plugin}: a dependency ships {PTH_FILE}; refused")
    fd = os.open(pth, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(target + "\n")
    return target


def stat_is_dir(path: Path) -> bool:
    """Whether `path` is a real directory (`lstat`: a symlink is not)."""
    return stat.S_ISDIR(os.lstat(path).st_mode)


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


DEFAULT_INDEX = "https://pypi.org/simple"
"""uv's default index: the only registry a plugin that is not first-party may lock against
(besides `PluginEnvironment.index_url`, tests only)."""
FILES_HOSTS: dict[str, tuple[str, str]] = {"pypi.org": ("files.pythonhosted.org", "/packages/")}
"""Per registry host, the one other host (and path prefix) its files may be served from."""


def _index_key(index: str) -> str:
    """`index` as uv.lock writes a registry: a `file://` index as its path, no trailing `/`."""
    parts = urlsplit(index)
    where = unquote(parts.path) if parts.scheme == "file" else index
    return where.rstrip("/")


def _artifact_problem(registry: str, artifact: dict[str, Any], test_index: str | None) -> str:
    """Why a registry package's file may not be fetched (`""` when it may): it must be served
    by its registry's own host (or its files host, `FILES_HOSTS`) over https, or lie inside a
    local directory index. Anything else would make uv fetch any URL a lock names (#191)."""
    url, path = artifact.get("url"), artifact.get("path")
    if "://" not in registry:  # a local directory index (uv writes paths)
        if not isinstance(path, str) or url is not None:
            return "a file that is not in its local index"
        base, where = posixpath.normpath(registry), posixpath.normpath(path)
        return "" if where.startswith(base.rstrip("/") + "/") else "a file outside its index"
    if not isinstance(url, str) or path is not None:
        return "a file without a URL"
    index, file = urlsplit(registry), urlsplit(url)
    http_ok = test_index is not None and _index_key(registry) == _index_key(test_index)
    if file.scheme != index.scheme or file.scheme not in (
        {"https", "http"} if http_ok else {"https"}
    ):
        return f"a file served over {file.scheme or 'no scheme'} (https only)"
    if file.username or file.password or file.query or file.fragment:
        return "a file URL with credentials, a query or a fragment"
    try:
        same = (file.hostname, file.port) == (index.hostname, index.port)
    except ValueError:
        return "a file URL with an invalid port"
    files = FILES_HOSTS.get(index.hostname or "")
    if same or (
        files is not None
        and file.hostname == files[0]
        and file.port is None
        and file.path.startswith(files[1])
    ):
        return ""
    return f"a file on host {file.hostname!r}, not its registry's"


def check_lock(
    tree: Path,
    plugin: str,
    project: str,
    *,
    trusted: bool = True,
    index_url: str | None = None,
) -> list[str]:
    """The names of the packages `<tree>/uv.lock` installs besides `project`, the plugin's own
    project; `InstallRefused` unless each comes from an index or URL with at least one wheel
    and a `sha256` hash on every file, and `project` itself is the tree (`.`). Only `project`
    is ever built: the others are installed from their wheels (`--no-build-package`).

    Every file of a registry package must be served by that registry (`_artifact_problem`).
    `trusted=False` (a plugin that is not first-party, #191): the registry must be the default
    index (or `index_url`), and a `url` source is refused."""
    allowed = {_index_key(DEFAULT_INDEX)} | ({_index_key(index_url)} if index_url else set())
    data = _read_toml(tree / LOCK_FILE, plugin)
    packages = data.get("package", [])
    if not isinstance(packages, list) or not packages:
        raise InstallRefused(f"plugin {plugin}: {LOCK_FILE} lists no package")
    dependencies: list[str] = []
    for package in packages:
        name = package.get("name", "?") if isinstance(package, dict) else "?"
        source = package.get("source") if isinstance(package, dict) else None
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise InstallRefused(f"plugin {plugin}: {LOCK_FILE}: a package has no valid name")
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
            wheels = package.get("wheels")
            if not isinstance(wheels, list) or not wheels:
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} has no wheel (an sdist only would "
                    "run its build backend at install): refused"
                )
            if kind == "registry":
                if not isinstance(where, str):
                    raise InstallRefused(f"plugin {plugin}: {LOCK_FILE}: {name} has no registry")
                if not trusted and _index_key(where) not in allowed:
                    raise InstallRefused(
                        f"plugin {plugin}: {LOCK_FILE}: {name} comes from another index than "
                        f"{DEFAULT_INDEX}, refused for a plugin that is not first-party"
                    )
                for artifact in artifacts:
                    if why := _artifact_problem(where, artifact, index_url):
                        raise InstallRefused(
                            f"plugin {plugin}: {LOCK_FILE}: {name} lists {why}: refused"
                        )
            elif not trusted:
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} comes from a direct URL, refused for "
                    "a plugin that is not first-party (index packages only)"
                )
            dependencies.append(name)
        elif kind in _IN_TREE:
            if not _in_tree(where):
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} comes from {where!r}, outside the plugin"
                )
            if normalize(name) != normalize(project) or posixpath.normpath(str(where)) != ".":
                raise InstallRefused(
                    f"plugin {plugin}: {LOCK_FILE}: {name} comes from {where!r} in the tree; "
                    "only the plugin's own project may be built"
                )
        else:
            raise InstallRefused(
                f"plugin {plugin}: {LOCK_FILE}: {name} comes from a {kind} source, which has "
                "no hash"
            )
    return sorted(set(dependencies))


class UvTooLarge(InstallRefused):
    """The venv grew past its budget; uv was killed."""


@dataclass(frozen=True)
class _Budget:
    """Bytes an install wrote: under `paths`, less what was there before (`baseline`)."""

    paths: tuple[Path, ...]
    baseline: int

    def used(self) -> int:
        return sum(tree_bytes(path) for path in self.paths) - self.baseline


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
    install_project: bool = True
    """`False` for a plugin that is not first-party (#191): its project is never built, only
    its dependencies are installed, and a `.pth` makes its sources importable."""

    def __call__(self, tree: Path, manifest: PluginManifest) -> None:
        if manifest.runtime is None:
            # A pack (#200): files only (pipelines, skills), never started, so it gets no venv
            # and nothing of its tree is checked, resolved or built: no uv, no build backend.
            return
        if not self.install_project:
            check_module_command(manifest)
        self.prepare(tree, manifest.name)

    def prepare(self, tree: Path, plugin: str) -> Path:
        """Check the project and its lock, then build `<tree>/.venv`; returns it."""
        project = check_project(tree, plugin, builds=self.install_project)
        trusted = self.install_project
        dependencies = check_lock(tree, plugin, project, trusted=trusted, index_url=self.index_url)
        venv = tree / VENV_DIR
        if os.path.lexists(venv):
            raise InstallRefused(f"plugin {plugin}: the tree holds a {VENV_DIR}; it must not")
        uv = self.uv or shutil.which("uv")
        if uv is None:
            raise InstallError(
                f"cannot install the dependencies of {plugin}: uv is not installed "
                "(it is needed to install a plugin, not to run one)"
            )
        with tempfile.TemporaryDirectory(prefix="horae-uv-") as home:
            # #191: a plugin that is not first-party never shares uv's cache: its own, inside
            # the temporary home, deleted with it. What it fetched can never be reused by a
            # first-party build (a build backend would import it on the host).
            cache = self.cache_dir if trusted else Path(home) / "cache"
            try:
                make_private_dirs(cache)
            except OSError as exc:
                raise InstallError(f"cannot write {cache}: {exc.strerror}") from None
            baseline = tree_bytes(cache)
            env = self.env(tree, Path(home), cache)
            # The budget covers what this install writes: the venv, uv's temporary directory
            # (sdist and build environments) and what it adds to the cache.
            watch = _Budget((venv, Path(home), cache) if trusted else (venv, Path(home)), baseline)
            self._run(
                plugin,
                [uv, "venv", "--relocatable", "--no-project", "--python", self.python],
                [str(venv)],
                tree,
                env,
                watch,
            )
            sync = ["sync", "--locked", "--no-dev"]
            # #191: nothing at all is built for a plugin that is not first-party.
            sync += (
                ["--no-editable"]
                if self.install_project
                else ["--no-install-project", "--no-build"]
            )
            self._run(
                plugin,
                [uv, *sync, "--python", str(venv / "bin" / "python")],
                [*self._index_args(), *(f"--no-build-package={name}" for name in dependencies)],
                tree,
                env,
                watch,
            )
            self._check_venv(venv, plugin, watch)
            if not self.install_project:
                write_pth(tree, venv, plugin)
        return venv

    def env(self, tree: Path, home: Path, cache: Path | None = None) -> dict[str, str]:
        """uv's environment (see the module docstring); `cache`: default `cache_dir`."""
        env = {key: value for key, value in os.environ.items() if key in PASSTHROUGH}
        env.update(
            PATH=BASE_PATH,
            HOME=str(home),
            TMPDIR=str(home),
            LANG="C.UTF-8",
            UV_NO_CONFIG="1",
            UV_CACHE_DIR=str(self.cache_dir if cache is None else cache),
            UV_PYTHON_DOWNLOADS="never",
            UV_PROJECT_ENVIRONMENT=str(tree / VENV_DIR),
            UV_LINK_MODE="copy",
            UV_NO_PROGRESS="1",
        )
        return env

    def _index_args(self) -> list[str]:
        return ["--default-index", self.index_url] if self.index_url else []

    def _check_venv(self, venv: Path, plugin: str, watch: "_Budget") -> None:
        cfg = venv / "pyvenv.cfg"
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            raise InstallError(f"plugin {plugin}: uv wrote no {VENV_DIR}/pyvenv.cfg") from None
        if not re.search(r"^relocatable\s*=\s*true\s*$", text, re.MULTILINE):
            # uv replaced the venv (another interpreter): its scripts name the scratch path.
            raise InstallRefused(f"plugin {plugin}: uv rebuilt the venv with another interpreter")
        used = watch.used()
        if used > self.max_bytes:
            raise UvTooLarge(
                f"plugin {plugin}: its install wrote {used} bytes, more than {self.max_bytes}"
            )

    def _run(
        self,
        plugin: str,
        command: Sequence[str],
        extra: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        watch: "_Budget",
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
            if watch.used() > self.max_bytes:
                kill_session(proc)
                raise UvTooLarge(
                    f"plugin {plugin}: its install wrote more than {self.max_bytes} bytes "
                    "(venv, temporary files and cache)"
                )
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
    "CONTAINER_ROOT",
    "LOCK_FILE",
    "MAX_VENV_BYTES",
    "MODULE_COMMANDS",
    "PROJECT_FILE",
    "PTH_FILE",
    "UV_TIMEOUT_S",
    "PluginEnvironment",
    "check_lock",
    "check_module_command",
    "check_project",
    "write_pth",
]
