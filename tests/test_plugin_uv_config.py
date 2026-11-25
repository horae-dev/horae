# SPDX-License-Identifier: Apache-2.0
"""A plugin's `[tool.uv]` under `uv --no-config` (#199, T22).

Horae installs with `uv --no-config`, which reads no uv *setting*, the plugin's own
`pyproject.toml` included. A setting that shaped the lock (`exclude-newer`, recorded in
`uv.lock [options]`) made `uv sync --locked` fail with "needs to be updated"; others would be
lost silently. `check_project` now refuses each such key by name before uv runs, and keeps
what uv still reads as project metadata (`sources`, an explicit index, `dev-dependencies`, ...).
"""

from pathlib import Path

import pytest

from horae.marketplace import InstallRefused, PluginEnvironment
from horae.marketplace.environment import UV_INERT_KEYS, UV_PROJECT_KEYS, check_project
from marketplace_fixtures import LocalInstaller, plugin_repo
from venv_fixtures import UV, local_index, lock, pyproject, venv_plugin_repo

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")
ALPHA_URL = "https://example.org/x/plugin-alpha"
TEMPLATE_UV = '\n[tool.uv]\nrequired-version = ">=0.13"\nexclude-newer = "7 days"\n'
"""The `[tool.uv]` table every plugin scaffolded from the template had (#199)."""


def _repo(tmp_path: Path, index: str, *, tool_uv: str, after_lock: bool) -> tuple[Path, str]:
    """Plugin `alpha` with `tool_uv` appended to its `pyproject.toml`, before the lock (so the
    lock is resolved with it) or after it."""
    text = pyproject("alpha", "1.0.0", ["tinydep==1.0.0"]) + tool_uv
    repo, sha = venv_plugin_repo(
        tmp_path / "repo", "alpha", "1.0.0", "1.0.0", index_url=index,
        cache=tmp_path / "lock-cache", locked=after_lock, files={"pyproject.toml": text},
    )  # fmt: skip
    if after_lock:
        return repo, sha
    lock(repo, index, tmp_path / "lock-cache")
    return plugin_repo(repo, name="alpha", manifest=(repo / "plugin.yaml").read_text())


def _project(tmp_path: Path, tool_uv: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject("alpha", "1.0.0", []) + tool_uv)
    return tmp_path


def test_the_template_exclude_newer_is_refused_by_name_before_uv_runs(tmp_path: Path) -> None:
    """The fixture of the issue: the template's `exclude-newer = "7 days"`. `uv` points nowhere,
    so the refusal comes from `check_project`, not from a uv error."""
    index = local_index(tmp_path / "index")
    repo, sha = _repo(tmp_path, index, tool_uv=TEMPLATE_UV, after_lock=True)
    plugins = tmp_path / "plugins"
    never = PluginEnvironment(tmp_path / "cache", uv=str(tmp_path / "no-uv"), index_url=index)
    with pytest.raises(InstallRefused) as refused:
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=never
        )
    message = str(refused.value)
    assert "pyproject.toml sets tool.uv.exclude-newer" in message
    assert "uv --no-config" in message
    assert "Remove tool.uv.exclude-newer, then run `uv lock`" in message
    assert "minimumReleaseAge" in message
    assert "needs to be updated" not in message
    assert not plugins.exists() or list(plugins.iterdir()) == []


@needs_uv
def test_a_plugin_with_only_honoured_tool_uv_keys_installs(tmp_path: Path) -> None:
    """`required-version` (inert) and project metadata uv reads under `--no-config`
    (`constraint-dependencies`, `dev-dependencies`): the lock matches, the install works."""
    index = local_index(tmp_path / "index")
    tool_uv = (
        '\n[tool.uv]\nrequired-version = ">=0.5"\n'
        'constraint-dependencies = ["tinydep<3"]\ndev-dependencies = []\n'
    )
    repo, sha = _repo(tmp_path, index, tool_uv=tool_uv, after_lock=False)
    plugins = tmp_path / "plugins"
    environment = PluginEnvironment(tmp_path / "state" / "uv-cache", index_url=index)
    LocalInstaller(plugins, {ALPHA_URL: repo}).install(
        "alpha", "1.0.0", ALPHA_URL, sha, prepare=environment
    )
    site = next((plugins / "alpha" / ".venv").glob("lib/python*/site-packages"))
    assert (site / "tinydep" / "__init__.py").read_text() == "VERSION = '1.0.0'\n"


REFUSED = {
    "exclude-newer": 'exclude-newer = "7 days"',
    "exclude-newer-package": 'exclude-newer-package = { tinydep = "2024-01-01T00:00:00Z" }',
    "resolution": 'resolution = "lowest-direct"',
    "prerelease": 'prerelease = "allow"',
    "fork-strategy": 'fork-strategy = "fewest"',
    "index-url": 'index-url = "https://example.org/simple"',
    "extra-index-url": 'extra-index-url = ["https://example.org/simple"]',
    "find-links": 'find-links = ["https://example.org/links"]',
    "no-binary-package": 'no-binary-package = ["tinydep"]',
    "cache-keys": 'cache-keys = [{ file = "pyproject.toml" }]',
    "pip": "pip = { universal = true }",
}


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_a_uv_setting_in_tool_uv_is_refused_by_name(tmp_path: Path, key: str) -> None:
    project = _project(tmp_path, f"\n[tool.uv]\n{REFUSED[key]}\n")
    with pytest.raises(InstallRefused, match=rf"sets tool\.uv\.{key}, a uv setting") as refused:
        check_project(project, "alpha")
    assert ("minimumReleaseAge" in str(refused.value)) == key.startswith("exclude-newer")
    # The same for a plugin that is not first-party.
    with pytest.raises(InstallRefused, match=rf"tool\.uv\.{key}"):
        check_project(project, "alpha", builds=False)


@pytest.mark.parametrize(
    "index",
    [
        '[[tool.uv.index]]\nname = "corp"\nurl = "https://example.org/simple"\n',
        '[[tool.uv.index]]\nname = "corp"\nurl = "https://example.org/simple"\ndefault = true\n',
    ],
)
def test_an_index_that_is_not_explicit_is_refused(tmp_path: Path, index: str) -> None:
    """Under `--no-config` uv never uses a plugin's index as a default or extra one."""
    with pytest.raises(InstallRefused, match=r"tool\.uv\.index 'corp' is not `explicit = true`"):
        check_project(_project(tmp_path, "\n" + index), "alpha")


@pytest.mark.parametrize("key", ["package", "managed"])
def test_an_unmanaged_or_virtual_project_is_refused(tmp_path: Path, key: str) -> None:
    with pytest.raises(InstallRefused, match=rf"tool\.uv\.{key} = false is not installable"):
        check_project(_project(tmp_path, f"\n[tool.uv]\n{key} = false\n"), "alpha")


def test_project_metadata_uv_reads_under_no_config_passes(tmp_path: Path) -> None:
    tool_uv = (
        '\n[tool.uv]\nrequired-version = ">=0.13"\nmanaged = true\npackage = true\n'
        'dev-dependencies = ["pytest"]\noverride-dependencies = ["tinydep==1.0.0"]\n'
        "environments = [\"sys_platform == 'linux'\"]\n"
        '\n[tool.uv.sources]\ntinydep = { index = "corp" }\n'
        '\n[[tool.uv.index]]\nname = "corp"\nurl = "https://example.org/simple"\n'
        "explicit = true\n"
    )
    assert check_project(_project(tmp_path, tool_uv), "alpha") == "alpha"


def test_the_allowed_keys_are_the_documented_ones() -> None:
    assert {"required-version"} == UV_INERT_KEYS
    assert "exclude-newer" not in UV_PROJECT_KEYS
    assert not UV_INERT_KEYS & UV_PROJECT_KEYS
