# SPDX-License-Identifier: Apache-2.0
"""`horae plugin add` (#44): install from git at a pinned commit, over local repositories."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import Lockfile
from horae.marketplace import (
    AlreadyInstalled,
    InstallError,
    InstallRefused,
    PluginInstaller,
)
from horae.marketplace import git as git_module
from horae.marketplace import install as install_module
from horae.marketplace.install import INSTALL_RECORD, SCRATCH_PREFIX, STALE_SCRATCH_S
from horae.plugins.host import HostRegistry
from marketplace_fixtures import PLUGIN_URL, LocalInstaller, git, plugin_manifest, plugin_repo

TESTS = Path(__file__).resolve().parent


def _installer(tmp_path: Path, repo: Path | str) -> LocalInstaller:
    return LocalInstaller(tmp_path / "plugins", {PLUGIN_URL: repo})


def _left(plugins: Path) -> list[str]:
    """What an install left in the plugin directory."""
    return sorted(p.name for p in plugins.iterdir()) if plugins.is_dir() else []


# --- the install -------------------------------------------------------------------------------


def test_a_plugin_is_installed_at_the_pinned_commit(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    installer = _installer(tmp_path, repo)
    installed = installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    target = tmp_path / "plugins" / "demo"
    assert installed.path == target
    assert sorted(p.name for p in target.iterdir()) == [INSTALL_RECORD, "plugin.yaml", "server.py"]
    assert json.loads((target / INSTALL_RECORD).read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "name": "demo",
        "version": "1.0.0",
        "repo": PLUGIN_URL,
        "commit": sha,
    }
    assert _left(tmp_path / "plugins") == ["demo"]  # no scratch directory, no .git


def test_an_installed_plugin_is_loaded_by_the_host_once_locked(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    installed = _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    unlocked = HostRegistry.from_directory(Lockfile(schema_version=1), tmp_path / "plugins")
    assert unlocked.plugins() == ()  # not enabled until #46 writes horae.lock
    lock = Lockfile(
        schema_version=1,
        plugins={"demo": installed.lock_entry(installed.manifest.permissions)},
    )
    registry = HostRegistry.from_directory(lock, tmp_path / "plugins")
    assert registry.installed("demo").manifest.version == "1.0.0"
    assert registry.plugin_for_tool("demo.fetch").commit == sha


def test_an_annotated_tag_is_dereferenced(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src", annotated=True)
    assert _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha).commit == sha


def test_an_older_release_is_installed_from_its_own_tag(tmp_path: Path) -> None:
    repo, old = plugin_repo(tmp_path / "src", version="1.0.0")
    plugin_repo(repo, version="1.1.0")
    installed = _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, old)
    assert installed.commit == old


def test_an_installed_name_is_not_overwritten(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    installer = _installer(tmp_path, repo)
    installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    with pytest.raises(AlreadyInstalled):
        installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == ["demo"]


# --- tag and SHA -------------------------------------------------------------------------------


def test_a_tag_moved_to_another_commit_is_refused(tmp_path: Path) -> None:
    repo, pinned = plugin_repo(tmp_path / "src")
    plugin_repo(repo, files={"server.py": "print('evil')\n"})  # moves v1.0.0 (tag -f)
    with pytest.raises(InstallRefused, match=r"tag v1\.0\.0 points to"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, pinned)
    assert _left(tmp_path / "plugins") == []


def test_an_index_sha_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    repo, _ = plugin_repo(tmp_path / "src")
    with pytest.raises(InstallError, match="cannot fetch commit"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, "1" * 40)
    assert _left(tmp_path / "plugins") == []


def test_a_sha_of_another_commit_is_refused(tmp_path: Path) -> None:
    repo, other = plugin_repo(tmp_path / "src", tag=None)
    plugin_repo(repo)  # v1.0.0 on a later commit; the index pins the earlier one
    with pytest.raises(InstallRefused, match=f"but {other} is pinned"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, other)
    assert _left(tmp_path / "plugins") == []


def test_a_missing_tag_is_refused(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src", tag=None)
    with pytest.raises(InstallError, match=r"tag v1\.0\.0"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_a_branch_named_like_the_tag_does_not_stand_in_for_it(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src", tag=None)
    git(repo, "branch", "v1.0.0")
    with pytest.raises(InstallError):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


# --- nothing from the repository runs ----------------------------------------------------------


def _hook_template(tmp_path: Path, marker: Path) -> Path:
    template = tmp_path / "template"
    hooks = template / "hooks"
    hooks.mkdir(parents=True)
    for hook in ("post-checkout", "post-index-change", "reference-transaction"):
        script = hooks / hook
        script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        script.chmod(0o755)
    return template


def test_no_hook_runs_even_from_a_planted_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "hook-ran"
    template = _hook_template(tmp_path, marker)
    repo, sha = plugin_repo(
        tmp_path / "src",
        files={"hooks/post-checkout": f"#!/bin/sh\ntouch {marker}\n", ".githooks/x": "x\n"},
    )
    # Positive control: the same checkout without `core.hooksPath=/dev/null` runs the hook.
    control = tmp_path / "control"
    env = {**git_module.git_env(), "GIT_TEMPLATE_DIR": str(template)}
    subprocess.run(  # noqa: S603
        ["git", "-c", "protocol.file.allow=always", "clone", "-q", "--", repo.as_uri(), control],  # noqa: S607
        env=env,
        check=True,
        capture_output=True,
    )
    assert marker.exists()
    marker.unlink()
    # A template directory reaching git (it never does: the environment is allow-listed).
    real_env = git_module.git_env
    monkeypatch.setattr(
        git_module, "git_env", lambda: {**real_env(), "GIT_TEMPLATE_DIR": str(template)}
    )
    _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert not marker.exists()


def test_a_submodule_is_refused_and_never_fetched(tmp_path: Path) -> None:
    inner, inner_sha = plugin_repo(tmp_path / "inner", files={"secret-payload": "x\n"}, tag=None)
    repo, _ = plugin_repo(tmp_path / "src", tag=None)
    (repo / ".gitmodules").write_text(
        f'[submodule "sub"]\n\tpath = sub\n\turl = {inner.as_uri()}\n', encoding="utf-8"
    )
    git(repo, "add", ".gitmodules")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{inner_sha},sub")
    git(repo, "commit", "-q", "-m", "submodule")
    git(repo, "tag", "-f", "v1.0.0")
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(InstallRefused, match="submodule"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_a_tree_too_deep_is_refused(tmp_path: Path) -> None:
    deep = "/".join(["d"] * (install_module.MAX_DEPTH + 1)) + "/f.txt"
    repo, sha = plugin_repo(tmp_path / "src", files={deep: "x"})
    with pytest.raises(InstallRefused, match="invalid path"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)


def test_losing_the_rename_race_never_replaces_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    plugins = tmp_path / "plugins"
    real = os.path.lexists
    calls = {"n": 0}

    def racy(path: str | os.PathLike[str]) -> bool:
        if Path(path) == plugins / "demo":
            calls["n"] += 1
            if calls["n"] == 2:  # after the last check, before the rename: the winner lands
                (plugins / "demo").mkdir()
                (plugins / "demo" / "plugin.yaml").write_text("winner", encoding="utf-8")
                return False
        return real(path)

    monkeypatch.setattr(os.path, "lexists", racy)
    with pytest.raises(AlreadyInstalled):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert (plugins / "demo" / "plugin.yaml").read_text(encoding="utf-8") == "winner"
    assert _left(plugins) == ["demo"]


def test_a_download_is_killed_when_it_outgrows_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = "head -c 3000000 /dev/zero > big; sleep 60"
    monkeypatch.setattr(git_module, "git_command", lambda *a, **k: ["sh", "-c", script])
    monkeypatch.setattr(git_module, "WATCH_INTERVAL_S", 0.05)
    started = time.monotonic()
    with pytest.raises(git_module.GitTooLarge):
        git_module.run_git(tmp_path, "clone", timeout_s=30, watch=tmp_path, max_bytes=1000)
    assert time.monotonic() - started < 20


def test_a_filter_named_by_gitattributes_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "filter-ran"
    repo, sha = plugin_repo(
        tmp_path / "src",
        files={".gitattributes": "* filter=lfs diff=lfs merge=lfs\n*.py filter=evil\n"},
    )
    # The repository's own config is never cloned; only config could define a filter.
    git(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    installed = _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert (installed.path / "server.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not marker.exists()


def _symlinked_repo(root: Path, target: str) -> tuple[Path, str]:
    repo, _ = plugin_repo(root, tag=None)
    (repo / "data").mkdir()
    os.symlink(target, repo / "data" / "link")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    git(repo, "tag", "-f", "v1.0.0")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside", "../.."])
def test_a_symlink_escaping_the_tree_is_refused(tmp_path: Path, target: str) -> None:
    repo, sha = _symlinked_repo(tmp_path / "src", target)
    with pytest.raises(InstallRefused, match="points outside"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_a_symlink_inside_the_tree_is_written_as_a_plain_file(tmp_path: Path) -> None:
    repo, sha = _symlinked_repo(tmp_path / "src", "../server.py")
    installed = _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    link = installed.path / "data" / "link"
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "../server.py"


def test_tree_limits_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha = plugin_repo(tmp_path / "src", files={f"f{n}.txt": "x" * 100 for n in range(5)})
    installer = _installer(tmp_path, repo)
    monkeypatch.setattr(install_module, "MAX_FILES", 4)
    with pytest.raises(InstallRefused, match="more than 4 files"):
        installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    monkeypatch.setattr(install_module, "MAX_FILES", 5000)
    monkeypatch.setattr(install_module, "MAX_TREE_BYTES", 300)
    with pytest.raises(InstallRefused, match="larger than 300 bytes"):
        installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    monkeypatch.setattr(install_module, "MAX_TREE_BYTES", 10**8)
    monkeypatch.setattr(install_module, "MAX_FETCH_BYTES", 10)
    with pytest.raises(InstallRefused, match="more than 10"):
        installer.install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_a_tree_cannot_forge_the_install_record(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src", files={INSTALL_RECORD: '{"commit": "x"}'})
    with pytest.raises(InstallRefused, match=INSTALL_RECORD):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)


# --- plugin.yaml -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest",
    [
        "name: demo\nversion: 1.0.0\n",  # incomplete
        plugin_manifest(extra="field"),
        "- not\n- a mapping\n",
        "&a [*a]\n",
    ],
)
def test_an_invalid_plugin_yaml_is_refused(tmp_path: Path, manifest: str) -> None:
    repo, sha = plugin_repo(tmp_path / "src", manifest=manifest)
    with pytest.raises(InstallRefused, match=r"plugin\.yaml is invalid"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_a_missing_plugin_yaml_is_refused(tmp_path: Path) -> None:
    repo, _ = plugin_repo(tmp_path / "src", tag=None)
    git(repo, "rm", "-q", "plugin.yaml")
    git(repo, "commit", "-q", "-m", "no manifest")
    git(repo, "tag", "v1.0.0")
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(InstallRefused, match=r"no plugin\.yaml"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)


@pytest.mark.parametrize(
    ("declared_name", "declared_version"), [("other", "1.0.0"), ("demo", "1.0.1")]
)
def test_a_name_or_version_mismatch_is_refused(
    tmp_path: Path, declared_name: str, declared_version: str
) -> None:
    repo, sha = plugin_repo(
        tmp_path / "src", manifest=plugin_manifest(declared_name, declared_version)
    )
    with pytest.raises(InstallRefused, match=f"declares {declared_name}@{declared_version}"):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(tmp_path / "plugins") == []


# --- atomicity ---------------------------------------------------------------------------------


def test_an_interrupted_install_leaves_nothing(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")

    def interrupted(tree: Path, _manifest: Any) -> None:
        assert (tree / "plugin.yaml").is_file()
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha, prepare=interrupted)
    assert _left(tmp_path / "plugins") == []


_KILLED = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from marketplace_fixtures import PLUGIN_URL, LocalInstaller
installer = LocalInstaller(Path(sys.argv[2]), {PLUGIN_URL: Path(sys.argv[3])})
installer.install(
    "demo", "1.0.0", PLUGIN_URL, sys.argv[4],
    prepare=lambda tree, manifest: os.kill(os.getpid(), signal.SIGKILL),
)
"""


def test_a_killed_install_leaves_no_plugin_and_is_cleaned_up_later(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    plugins = tmp_path / "plugins"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _KILLED, str(TESTS), str(plugins), str(repo), sha],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == -signal.SIGKILL
    left = _left(plugins)
    assert "demo" not in left
    assert [name for name in left if not name.startswith(SCRATCH_PREFIX)] == []
    assert HostRegistry.from_directory(Lockfile(schema_version=1), plugins).plugins() == ()
    stale = time.time() - STALE_SCRATCH_S - 60
    for name in left:
        os.utime(plugins / name, (stale, stale))
    _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert _left(plugins) == ["demo"]


def test_a_recent_scratch_directory_is_left_alone(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    busy = tmp_path / "plugins" / f"{SCRATCH_PREFIX}busy"
    busy.mkdir(parents=True)
    _installer(tmp_path, repo).install("demo", "1.0.0", PLUGIN_URL, sha)
    assert busy.is_dir()


# --- URLs and transport -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c id",
        "file:///tmp/plugin",
        "--upload-pack=touch /tmp/x",
        "-uhttps://example.org/a",
        "http://example.org/a/plugin",
        "git@example.org:a/plugin.git",
        "https://user@example.org/a/plugin",
        "https://example.org/a/../b",
    ],
)
def test_the_default_installer_refuses_non_https_urls_before_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(cwd: Path, *args: str, **kwargs: Any) -> str:
        calls.append(args)
        raise AssertionError("git must not run")

    monkeypatch.setattr(install_module, "run_git", fake)
    installer = PluginInstaller(tmp_path / "plugins")
    assert installer.allow_protocol == "https"
    with pytest.raises(InstallRefused, match="not an https git URL"):
        installer.install("demo", "1.0.0", url, "a" * 40)
    assert calls == []
    assert _left(tmp_path / "plugins") == []


def test_every_git_call_allows_https_only_and_puts_urls_after_dashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[tuple[str, ...], str]] = []
    real = git_module.run_git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str:
        seen.append((args, kwargs["allow"]))
        if args[0] == "clone":
            raise git_module.GitError("offline")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(install_module, "run_git", spy)
    with pytest.raises(InstallError, match="cannot clone"):
        PluginInstaller(tmp_path / "plugins").install("demo", "1.0.0", PLUGIN_URL, "a" * 40)
    (args, allow), *_ = seen
    assert allow == "https"
    assert args[args.index("--") + 1] == PLUGIN_URL
    assert "--no-recurse-submodules" in args


class _HttpsOnly(PluginInstaller):
    """The default (https-only) installer, handing git another address for the URL."""

    def __init__(self, plugins_dir: Path, address: str) -> None:
        super().__init__(plugins_dir)
        self.address = address

    def remote(self, url: str) -> str:
        return self.address


@pytest.mark.parametrize(
    "remote",
    ["ext::sh -c 'touch {marker}'", "--upload-pack=touch {marker}", "-u touch {marker}"],
)
def test_real_git_never_runs_a_hostile_remote(tmp_path: Path, remote: str) -> None:
    marker = tmp_path / "pwned"
    installer = _HttpsOnly(tmp_path / "plugins", remote.format(marker=marker))
    with pytest.raises(InstallError):
        installer.install("demo", "1.0.0", PLUGIN_URL, "a" * 40)
    assert not marker.exists()
    assert _left(tmp_path / "plugins") == []


def test_file_urls_are_refused_by_git_without_the_test_seam(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "src")
    installer = _HttpsOnly(tmp_path / "plugins", repo.as_uri())
    with pytest.raises(InstallError, match="cannot clone"):
        installer.install("demo", "1.0.0", PLUGIN_URL, sha)


@pytest.mark.parametrize(
    ("name", "version", "sha"),
    [("../evil", "1.0.0", "a" * 40), ("demo", "1.0", "a" * 40), ("demo", "1.0.0", "abc")],
)
def test_a_name_version_or_sha_that_is_not_valid_is_refused(
    tmp_path: Path, name: str, version: str, sha: str
) -> None:
    with pytest.raises(InstallRefused):
        PluginInstaller(tmp_path / "plugins").install(name, version, PLUGIN_URL, sha)
    assert not (tmp_path / "evil").exists()
    assert _left(tmp_path / "plugins") == []
