# SPDX-License-Identifier: Apache-2.0
"""A third-party plugin, installed without building it, runs under `runtime: container` (#194,
schemas decision 79 G): the end-to-end proof of the `.pth` path that unit tests only describe.

The plugin is synthetic: a src layout, static metadata, `python -m <module>`, one dependency
(`tinydep`) from a local `file://` index, and a `setup.py` and a hatch build hook that would each
write a marker if anything ever built the project. It is installed the way a plugin that is not
first-party is (`PluginEnvironment(install_project=False)`): the project is checked with
`check_project(builds=False)`, `check_lock` and `check_module_command`, the dependencies come from
the lock's hashed wheels with uv refusing to build (`--no-build`, a private cache), and Horae
writes `_horae_plugin.pth` naming `/plugin/src`. The venv is created inside the digest-pinned
plugin image (offline, the instance mounted), because a venv links to the interpreter that built
it (`home = /usr/local/bin`, decision 74): that is also where a real install builds it, in the
Horae image. Then the plugin runs in its container and one real tool call is made.

Marked `malicious`, `container` and `live`: needs a Docker engine and the plugin image, and in
the CI malicious job it must run, never skip (`HORAE_SKIP_CONTAINER_TESTS=1` skips it anyway).
Nothing reaches the public internet.
"""

import asyncio
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import PluginManifest
from horae.contracts.instance import (
    DEFAULT_CONTAINER_IMAGE,
    ContainerConfig,
    ContainerLimits,
    PluginDefaults,
)
from horae.contracts.lockfile import Lockfile
from horae.marketplace.environment import (
    PTH_FILE,
    check_lock,
    check_module_command,
    check_project,
    write_pth,
)
from horae.plugins.host import HostRegistry, PluginCallError, PluginHost
from horae.plugins.host.container import PLUGIN_MOUNT
from horae.plugins.host.env import BASE_PATH, VENV_DIR
from horae.secrets import SecretResolver
from plugin_fixtures import lock_for, secrets_dir
from venv_fixtures import UV, local_index, lock, venv_manifest, venv_plugin_files

pytestmark = [pytest.mark.malicious, pytest.mark.container, pytest.mark.live]

NAME = f"nobuild{secrets.token_hex(3)}"
"""Also the module (`python -m <NAME>`) and, with a prefix, the plugin's Docker network."""
SETUPTOOLS = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
PLATFORMS = {
    "x86_64": "x86_64-manylinux_2_28",
    "amd64": "x86_64-manylinux_2_28",
    "aarch64": "aarch64-manylinux_2_28",
    "arm64": "aarch64-manylinux_2_28",
}
COMMUNITY = {"tier": "community", "index": "https://git.example.test/someone/marketplace"}


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker", path=BASE_PATH)
    assert docker is not None
    return subprocess.run(  # noqa: S603 (the Docker CLI, fixed arguments)
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[str]:
    """Skips without an engine; yields the engine's architecture as a uv platform."""
    if os.environ.get("HORAE_SKIP_CONTAINER_TESTS"):
        pytest.skip("HORAE_SKIP_CONTAINER_TESTS is set")
    if shutil.which("docker", path=BASE_PATH) is None:
        pytest.skip("no Docker CLI on the base PATH")
    info = _docker("info", "--format", "{{.Architecture}}", timeout=30)
    if info.returncode != 0:
        pytest.skip("the Docker daemon is not reachable")
    if _docker("image", "inspect", DEFAULT_CONTAINER_IMAGE).returncode != 0:
        pulled = _docker("pull", "--quiet", DEFAULT_CONTAINER_IMAGE, timeout=600)
        if pulled.returncode != 0:
            pytest.skip("the plugin image cannot be pulled")
    if UV is None:
        pytest.skip("uv is not installed")
    platform = PLATFORMS.get(info.stdout.strip().lower())
    if platform is None:
        pytest.skip(f"unknown engine architecture {info.stdout.strip()!r}")
    yield platform
    _docker("network", "rm", f"horae-plugin-{NAME}")


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")


class Installed:
    def __init__(self, tree: Path, markers: list[Path], manifest: dict[str, Any]) -> None:
        self.tree = tree
        self.markers = markers
        self.manifest = manifest


@pytest.fixture(scope="module")
def installed(engine: str, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """The plugin, installed without a build into a venv made inside the plugin image."""
    base = tmp_path_factory.mktemp("nobuild")
    index = local_index(base / "index")
    tree = base / "plugins" / NAME
    markers = [base / "BUILT-setup-py", base / "BUILT-hatch-hook"]
    files = venv_plugin_files(NAME, "1.0.0", "1.0.0", build=SETUPTOOLS)
    files.pop("plugin.yaml")
    # What a source file reports (this one is imported through the `.pth`, from /plugin/src).
    server = f"src/{NAME}/__main__.py"
    files[server] = files[server].replace(
        '"prefix": sys.prefix,', '"prefix": sys.prefix,\n            "module": __file__,'
    )
    files["setup.py"] = f"open({str(markers[0])!r}, 'w').close()\n"
    files["hatch_build.py"] = f"open({str(markers[1])!r}, 'w').close()\n"
    tree.mkdir(parents=True)
    _write(tree, files)
    lock(tree, index, base / "lock-cache")
    manifest = venv_manifest(NAME, "1.0.0")
    plugin = PluginManifest.model_validate(manifest)

    # The checks `PluginEnvironment(install_project=False).prepare` makes, as for a plugin that
    # is not first-party: no build, no build backend, index packages only, a module command.
    project = check_project(tree, NAME, builds=False)
    dependencies = check_lock(tree, NAME, project, trusted=False, index_url=index)
    check_module_command(plugin)
    assert dependencies == ["tinydep"]

    # The lock's hashed requirements (the venv is not there yet for uv to find).
    assert UV is not None
    env = {
        "PATH": BASE_PATH,
        "HOME": str(base),
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    env["UV_CACHE_DIR"] = str(base / "private-cache")
    export = [
        UV,
        "export",
        "--locked",
        "--offline",
        "--no-python-downloads",
        "--python",
        sys.executable,
        "--no-dev",
        "--no-emit-project",
    ]
    export += ["--no-header", "--format", "requirements-txt", "--default-index", index]
    exported = subprocess.run(  # noqa: S603 (uv, fixed arguments)
        export, cwd=tree, env=env, capture_output=True, text=True, check=False
    )
    assert exported.returncode == 0, exported.stderr
    # The venv, made by the plugin image's own interpreter, offline, as the user that runs it.
    built = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,source={tree.resolve()},target={PLUGIN_MOUNT}",
        DEFAULT_CONTAINER_IMAGE,
        "python",
        "-m",
        "venv",
        "--without-pip",
        f"{PLUGIN_MOUNT}/{VENV_DIR}",
    )
    assert built.returncode == 0, built.stderr
    venv = tree / VENV_DIR
    (site,) = (venv / "lib").glob("python*/site-packages")

    # The dependencies: the lock's hashed wheels only, never built, from uv's own private cache
    # (not the one first-party builds share), installed for the image's platform.
    assert UV is not None
    env = {
        "PATH": BASE_PATH,
        "HOME": str(base),
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    env["UV_CACHE_DIR"] = str(base / "private-cache")
    requirements = base / "requirements.txt"
    requirements.write_text(exported.stdout, encoding="utf-8")
    install = [UV, "pip", "install", "--offline", "--no-build", "--require-hashes"]
    install += ["--python-platform", engine, "--python-version", "3.12", "--target", str(site)]
    install += ["--default-index", index, "-r", str(requirements)]
    done = subprocess.run(  # noqa: S603 (uv, fixed arguments)
        install, cwd=base, env=env, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert (site / "tinydep").is_dir()
    assert not list(site.glob(f"{NAME}*"))  # the project itself is not installed
    assert write_pth(tree, venv, NAME) == f"{PLUGIN_MOUNT}/src"
    assert (site / PTH_FILE).read_text(encoding="utf-8") == f"{PLUGIN_MOUNT}/src\n"
    return Installed(tree, markers, manifest)


def _host(tmp_path: Path, found: Installed, runtime: str) -> PluginHost:
    lock_entry = lock_for(found.manifest).plugins[NAME].model_dump(mode="json")
    locked = Lockfile.model_validate(
        {"schema_version": 1, "plugins": {NAME: lock_entry | COMMUNITY}}
    )
    registry = HostRegistry(
        locked, {NAME: PluginManifest.model_validate(found.manifest)}, {NAME: found.tree}
    )
    assert registry.installed(NAME).first_party is False  # the lock says so, not the manifest
    defaults = PluginDefaults(
        runtime=runtime,  # type: ignore[arg-type]
        startup_timeout_s=120.0,
        call_timeout_s=60.0,
        limits=ContainerLimits(),
    )
    return PluginHost(
        registry,
        secrets=SecretResolver({}, secrets_dir(tmp_path)),
        work_dir=tmp_path / "work",
        timeouts=lambda _name: defaults,
        container=ContainerConfig(image=DEFAULT_CONTAINER_IMAGE),
    )


def test_a_plugin_installed_without_a_build_runs_in_its_container(
    tmp_path: Path, installed: Installed
) -> None:
    async def go() -> Any:
        async with _host(tmp_path, installed, "container") as host:
            return (await host.call(f"{NAME}.info", {}, None)).data

    data = asyncio.run(go())
    assert data["tinydep"] == "1.0.0"  # the dependency from the venv's site-packages
    assert data["prefix"] == f"{PLUGIN_MOUNT}/{VENV_DIR}"  # the venv, valid in the image
    assert data["executable"].startswith(f"{PLUGIN_MOUNT}/{VENV_DIR}/bin/")
    # Importable only through the `.pth`: the project was never installed into the venv.
    assert data["module"] == f"{PLUGIN_MOUNT}/src/{NAME}/__main__.py"
    for marker in installed.markers:
        assert not marker.exists()  # neither `setup.py` nor the hatch hook ever ran
    assert not list(installed.tree.rglob("*.egg-info"))
    assert not (installed.tree / "build").exists()


def test_the_same_plugin_is_refused_as_a_process(tmp_path: Path, installed: Installed) -> None:
    async def go() -> PluginCallError:
        async with _host(tmp_path, installed, "process") as host:
            with pytest.raises(PluginCallError) as caught:
                await host.call(f"{NAME}.info", {}, None)
            return caught.value

    assert asyncio.run(go()).kind == "runtime_refused"
