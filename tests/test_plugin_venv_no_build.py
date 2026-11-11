# SPDX-License-Identifier: Apache-2.0
"""A plugin that is not first-party is installed without building its project (#191,
schemas decision 79 H): its build backend (`setup.py`, a hatch build hook) would run plugin
code on the host, outside its container. Local `file://` index, synthetic names, no network:
uv never needs setuptools or hatchling here, which is itself the proof that no backend ran."""

import json
from pathlib import Path
from typing import Any

import pytest

from horae import cli_plugin
from horae.contracts import PluginManifest
from horae.contracts.instance import DEFAULT_MARKETPLACE
from horae.marketplace import InstallRefused, PluginEnvironment
from horae.marketplace.approval import describe_origin
from horae.marketplace.environment import (
    CONTAINER_ROOT,
    PTH_FILE,
    check_module_command,
    check_project,
)
from horae.plugins.host.container import PLUGIN_MOUNT
from marketplace_fixtures import A, LocalInstaller, plugin_repo
from venv_fixtures import UV, local_index, lock, pyproject, venv_plugin_files

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")
URL = "https://git.example.test/someone/plugin-alpha"

SETUPTOOLS = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
HATCHLING = (
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
    "[tool.hatch.build.hooks.custom]\n"
)


def _repo(tmp_path: Path, index: str, build: str, marker: Path, *, src: bool = True) -> Any:
    """`alpha`, whose build would write `marker` (`setup.py` and `hatch_build.py` both do)."""
    root = tmp_path / "repo"
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0", build=build)
    if not src:
        files = {k.removeprefix("src/"): v for k, v in files.items() if k != "plugin.yaml"} | {
            "plugin.yaml": files["plugin.yaml"]
        }
    files["setup.py"] = f"open({str(marker)!r}, 'w').close()\n"
    files["hatch_build.py"] = f"open({str(marker)!r}, 'w').close()\n"
    root.mkdir()
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    lock(root, index, tmp_path / "lock-cache")
    files["uv.lock"] = (root / "uv.lock").read_text(encoding="utf-8")
    manifest = files.pop("plugin.yaml")
    return plugin_repo(root, name="alpha", manifest=manifest, files=files, tag=None)


def _site_packages(tree: Path) -> Path:
    (found,) = (tree / ".venv" / "lib").glob("python*/site-packages")
    return found


@needs_uv
@pytest.mark.parametrize("build", [SETUPTOOLS, HATCHLING], ids=["setuptools", "hatchling"])
def test_a_third_party_install_never_runs_the_build_backend(tmp_path: Path, build: str) -> None:
    index = local_index(tmp_path / "index")
    marker = tmp_path / "BUILT"
    repo, sha = _repo(tmp_path, index, build, marker)
    environment = PluginEnvironment(tmp_path / "uv-cache", index_url=index, install_project=False)
    installed = LocalInstaller(tmp_path / "plugins", {URL: repo}).install_commit(
        URL, sha, prepare=environment
    )
    assert not marker.exists()  # neither setup.py nor the hatch hook ran
    site = _site_packages(installed.path)
    assert (site / PTH_FILE).read_text(encoding="utf-8") == f"{CONTAINER_ROOT}/src\n"
    assert (site / "tinydep").is_dir()  # the dependencies are installed
    assert not list(site.glob("alpha*"))  # the project itself is not


@needs_uv
def test_without_a_src_directory_the_pth_names_the_tree_root(tmp_path: Path) -> None:
    index = local_index(tmp_path / "index")
    repo, sha = _repo(tmp_path, index, SETUPTOOLS, tmp_path / "BUILT", src=False)
    environment = PluginEnvironment(tmp_path / "uv-cache", index_url=index, install_project=False)
    installed = LocalInstaller(tmp_path / "plugins", {URL: repo}).install_commit(
        URL, sha, prepare=environment
    )
    pth = _site_packages(installed.path) / PTH_FILE
    assert pth.read_text(encoding="utf-8") == f"{CONTAINER_ROOT}\n"


@needs_uv
def test_a_local_path_dependency_is_refused_and_never_built(tmp_path: Path) -> None:
    """A dependency from the plugin's own tree (`tool.uv.sources` path) whose `setup.py` would
    write a marker: refused before uv runs, the marker never written."""
    index = local_index(tmp_path / "index")
    marker = tmp_path / "BUILT"
    root = tmp_path / "repo"
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0")
    files["pyproject.toml"] = pyproject("alpha", "1.0.0", ["localdep"], build=SETUPTOOLS) + (
        '\n[tool.uv.sources]\nlocaldep = { path = "localdep" }\n'
    )
    files["localdep/pyproject.toml"] = pyproject("localdep", "1.0.0", [], build=SETUPTOOLS)
    files["localdep/setup.py"] = f"open({str(marker)!r}, 'w').close()\n"
    files["localdep/src/localdep/__init__.py"] = ""
    root.mkdir()
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    lock(root, index, tmp_path / "lock-cache")
    assert not marker.exists()
    files["uv.lock"] = (root / "uv.lock").read_text(encoding="utf-8")
    manifest = files.pop("plugin.yaml")
    repo, sha = plugin_repo(root, name="alpha", manifest=manifest, files=files, tag=None)
    environment = PluginEnvironment(tmp_path / "uv-cache", index_url=index, install_project=False)
    with pytest.raises(InstallRefused, match=r"tool\.uv\.sources\.localdep"):
        LocalInstaller(tmp_path / "plugins", {URL: repo}).install_commit(
            URL, sha, prepare=environment
        )
    assert not marker.exists()
    assert not (tmp_path / "plugins" / "alpha").exists()


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ('[tool.uv.workspace]\nmembers = ["pkg/*"]\n', "workspace"),
        ('[tool.uv.sources]\ntinydep = { git = "https://git.example.test/t" }\n', "sources"),
        ('[tool.uv.sources]\ntinydep = { url = "https://example.test/t.whl" }\n', "sources"),
        ("[tool.uv.sources]\ntinydep = { workspace = true }\n", "sources"),
        ('[dependency-groups]\nlocal = ["localdep @ file:///srv/localdep"]\n', "direct"),
    ],
)
def test_whatever_uv_would_build_is_refused_for_a_third_party_plugin(
    tmp_path: Path, extra: str, why: str
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    text = pyproject("alpha", "1.0.0", ["tinydep==1.0.0"], build=SETUPTOOLS) + "\n" + extra
    (tree / "pyproject.toml").write_text(text, encoding="utf-8")
    with pytest.raises(InstallRefused, match=why):
        check_project(tree, "alpha", builds=False)
    assert check_project(tree, "alpha") == "alpha"  # first-party: checked as before
    index_only = pyproject("alpha", "1.0.0", ["tinydep"], build=SETUPTOOLS)
    (tree / "pyproject.toml").write_text(
        index_only + '\n[tool.uv.sources]\ntinydep = { index = "mine" }\n', encoding="utf-8"
    )
    assert check_project(tree, "alpha", builds=False) == "alpha"


def test_the_pth_names_the_container_mount() -> None:
    assert CONTAINER_ROOT == PLUGIN_MOUNT


@needs_uv
def test_dynamic_metadata_is_refused_before_uv_runs(tmp_path: Path) -> None:
    """uv would call the backend to compute dynamic metadata, even with
    `--no-install-project`."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "pyproject.toml").write_text(
        pyproject("alpha", "1.0.0", [], build=SETUPTOOLS).replace(
            'version = "1.0.0"', 'dynamic = ["version"]'
        ),
        encoding="utf-8",
    )
    with pytest.raises(InstallRefused, match="dynamic"):
        check_project(tree, "alpha", builds=False)
    assert check_project(tree, "alpha") == "alpha"  # first-party: built as before


@pytest.mark.parametrize(
    ("command", "ok"),
    [
        (["python", "-m", "alpha"], True),
        (["python3", "-m", "alpha"], True),
        (["alpha-server"], False),  # a console script: never generated
        (["/usr/bin/python3", "-m", "alpha"], False),
        (["bin/run"], False),
    ],
)
def test_a_console_script_only_plugin_is_refused(command: list[str], ok: bool) -> None:
    manifest = PluginManifest.model_validate(
        {
            "name": "alpha",
            "version": "1.0.0",
            "horae": ">=0",
            "provides": {"sources": ["alpha.info"]},
            "permissions": {},
            "runtime": {"protocol": "mcp-stdio", "command": command},
        }
    )
    if ok:
        check_module_command(manifest)
    else:
        with pytest.raises(InstallRefused, match=r"python, -m, <module>"):
            check_module_command(manifest)


@pytest.mark.parametrize(
    ("tier", "index", "builds"),
    [
        ("first-party", "default", True),
        ("community", "default", False),
        ("first-party", A, False),
        (None, None, False),
    ],
)
def test_only_a_first_party_plugin_builds_its_project(
    tmp_path: Path, tier: Any, index: str | None, builds: bool
) -> None:
    where = DEFAULT_MARKETPLACE if index == "default" else index
    environment = cli_plugin.environment_for(tmp_path, tier, where)
    assert isinstance(environment, PluginEnvironment)
    assert environment.install_project is builds


def test_the_approval_says_the_build_does_not_run() -> None:
    lines = describe_origin(None, None, URL, runtime="container")
    assert any("installed without running its build" in line for line in lines)
    first = describe_origin("first-party", DEFAULT_MARKETPLACE, URL, runtime="process")
    assert "without running its build" not in json.dumps(first)
