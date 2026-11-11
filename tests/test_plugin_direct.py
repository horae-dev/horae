# SPDX-License-Identifier: Apache-2.0
"""Direct install from a git URL (#48), over local fixture repositories (no network).

A direct install has no marketplace tier, so it is not first-party: it installs (and runs) only
with `runtime: container` in horae.yaml (#191, docs/security.md T04). The rest of the path is
tested with `plugins.demo.runtime: container`; the upgrade of a direct install to another
commit is at the end."""

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_plugin
from horae.cli import app
from horae.contracts.plugin import PluginManifest
from horae.marketplace import (
    AlreadyInstalled,
    InstallError,
    InstallRefused,
    InvalidReference,
    PluginInstaller,
    TierRefused,
    approval,
)
from horae.marketplace import git as git_module
from horae.marketplace import install as install_module
from horae.marketplace.direct import is_direct, parse_direct
from horae.marketplace.install import INSTALL_RECORD
from marketplace_fixtures import LocalInstaller, git, plugin_manifest, plugin_repo, set_runtime

runner = CliRunner()
URL = "https://git.example.test/someone/plugin-demo"
OTHER_URL = "https://git.example.test/someone-else/plugin-demo"
SHA = "0123456789abcdef0123456789abcdef01234567"


# --- the reference ---------------------------------------------------------------------------


def test_a_url_pinned_to_a_full_sha_is_a_direct_reference() -> None:
    assert parse_direct(f"{URL}@{SHA}") == (URL, SHA)
    assert parse_direct(f"{URL}.git@{SHA}") == (f"{URL}.git", SHA)
    assert parse_direct(f"{URL}@{'a' * 64}") == (URL, "a" * 64)  # a SHA-256 repository
    assert is_direct(f"{URL}@{SHA}")
    assert is_direct("git@git.example.test:someone/plugin-demo")
    assert not is_direct("demo")
    assert not is_direct("demo@1.0.0")


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        (URL, "pinned to a commit"),
        (f"{URL}@v1.0.0", "not a full commit SHA"),
        (f"{URL}@main", "not a full commit SHA"),
        (f"{URL}@{SHA[:12]}", "not a full commit SHA"),
        (f"{URL}@{SHA.upper()}", "not a full commit SHA"),
        (f"{URL}@{SHA}0", "not a full commit SHA"),
        (f"{URL}@", "not a full commit SHA"),
        (f"http://git.example.test/someone/plugin-demo@{SHA}", "https:// git URL only"),
        (f"file:///srv/plugin-demo@{SHA}", "https:// git URL only"),
        (f"ssh://git@git.example.test/someone/plugin-demo@{SHA}", "https:// git URL only"),
        (f"git://git.example.test/someone/plugin-demo@{SHA}", "https:// git URL only"),
        (f"git@git.example.test:someone/plugin-demo@{SHA}", "https:// git URL only"),
        (f"ext::sh -c touch@{SHA}", "https:// git URL only"),
        (f"HTTPS://git.example.test/someone/plugin-demo@{SHA}", "https:// git URL only"),
        (f"https://git.example.test:8443/someone/plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://192.0.2.10/someone/plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://localhost/someone/plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://git.localhost/someone/plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://[2001:db8::1]/someone/plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://git.example.test/someone/../plugin-demo@{SHA}", "not an allowed git URL"),
        (f"https://git.example.test/someone/plugin-demo?x=1@{SHA}", "not an allowed git URL"),
        (f"https://git.example.test/@{SHA}", "not an allowed git URL"),
    ],
)
def test_anything_but_an_https_url_at_a_full_sha_is_refused(ref: str, why: str) -> None:
    with pytest.raises(InvalidReference, match=why):
        parse_direct(ref)


@pytest.mark.parametrize(
    "ref",
    [
        f"https://user:s3cr3t-token@git.example.test/someone/plugin-demo@{SHA}",
        f"https://s3cr3t-token@git.example.test/someone/plugin-demo@{SHA}",
        "https://s3cr3t-token@git.example.test/someone/plugin-demo",
    ],
)
def test_credentials_in_the_url_are_refused_and_never_repeated(ref: str) -> None:
    with pytest.raises(InvalidReference, match="credentials") as caught:
        parse_direct(ref)
    assert "s3cr3t" not in str(caught.value)


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A plugin repository with no tag at all, served for `URL` (and `OTHER_URL`)."""
    source, sha = plugin_repo(tmp_path / "plugin-demo", tag=None)
    monkeypatch.setattr(
        cli_plugin,
        "make_installer",
        lambda where: LocalInstaller(where, {URL: source, OTHER_URL: source}),
    )
    monkeypatch.setattr(cli_plugin, "interactive", lambda: True)
    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: None)
    return source, sha


@pytest.fixture
def gate_open(instance_dir: Path) -> None:
    """#191: horae.yaml runs `demo` in a container, so its direct install may proceed."""
    set_runtime(instance_dir, "container", "demo")


def _add(instance: Path, *args: str, answer: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", "add", *args, "-i", str(instance)], input=answer)
    return result.exit_code, result.stdout, result.stderr


def _json(out: str) -> Any:
    return json.loads(out[out.index("{") :])


def _lock(instance: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((instance / "horae.lock").read_text(encoding="utf-8"))
    return data


def _nothing_installed(instance: Path) -> bool:
    plugins = instance / "plugins"
    return not plugins.exists() or not list(plugins.iterdir())


def test_a_direct_install_is_refused_under_the_process_runtime_before_anything_is_fetched(
    instance_dir: Path, repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat model T04 (#191): no marketplace tier, so not first-party: no install while
    horae.yaml runs every plugin as a plain subprocess, and nothing is fetched."""

    def never(where: Path) -> PluginInstaller:
        raise AssertionError("nothing may be fetched")

    monkeypatch.setattr(cli_plugin, "make_installer", never)
    before = (instance_dir / "horae.lock").read_bytes()
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 3, out
    error = _json(out)["error"]
    assert error["kind"] == "tier_refused"
    assert "direct install" in error["message"]
    assert "runtime: container" in error["message"]
    assert "Docker" in error["message"]
    assert _nothing_installed(instance_dir)
    assert (instance_dir / "horae.lock").read_bytes() == before
    with pytest.raises(TierRefused, match="direct install"):
        approval.require_installable("demo", None, None, runtime="process")
    approval.require_installable("demo", None, None, runtime="container")


def test_a_direct_install_whose_name_runs_as_a_process_is_refused_once_the_name_is_known(
    instance_dir: Path, repo: tuple[Path, str]
) -> None:
    """Another plugin runs in a container, so the fetch happens; the commit's plugin.yaml names
    `demo`, which horae.yaml runs as a process: refused before approval, nothing installed."""
    set_runtime(instance_dir, "container", "other")
    before = (instance_dir / "horae.lock").read_bytes()
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 3, out
    error = _json(out)["error"]
    assert error["kind"] == "tier_refused"
    assert "plugins.demo.runtime: container" in error["message"]
    assert "asks for these permissions" not in out
    assert _nothing_installed(instance_dir)
    assert (instance_dir / "horae.lock").read_bytes() == before


def test_a_direct_install_under_the_default_container_runtime_is_installed(
    instance_dir: Path, repo: tuple[Path, str]
) -> None:
    set_runtime(instance_dir, "container")  # plugin_defaults.runtime
    code, out, err = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 0, out
    report = _json(out)
    assert (report["runtime"], report["first_party"], report["enabled"]) == (
        "container",
        False,
        False,
    )
    assert "runs in a container" in err
    assert "source: direct install" in err


@pytest.mark.parametrize(
    "ref",
    [
        f"{URL}@v1.0.0",
        f"{URL}@{SHA[:7]}",
        f"file:///srv/plugin-demo@{SHA}",
        f"https://tok@git.example.test/someone/plugin-demo@{SHA}",
    ],
)
def test_a_malformed_direct_reference_is_a_usage_error(
    instance_dir: Path, repo: tuple[Path, str], ref: str
) -> None:
    code, out, _ = _add(instance_dir, ref, "--json")
    assert code == 2, out
    assert _json(out)["error"]["kind"] == "usage"
    assert "tok@" not in out
    assert _nothing_installed(instance_dir)


@pytest.mark.parametrize("flag", ["--pre", "--offline"])
def test_pre_and_offline_do_not_apply_to_a_direct_install(
    instance_dir: Path, repo: tuple[Path, str], flag: str
) -> None:
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", flag, "--json")
    assert code == 2, out
    error = _json(out)["error"]
    assert error["kind"] == "usage"
    assert flag in error["message"]
    assert _nothing_installed(instance_dir)


@pytest.mark.parametrize(
    "ref",
    [
        f"https://git.example.test/a/plugin?token=s3cr3t-token@{SHA}",
        f"https://git.example.test/a/plugin#s3cr3t-token@{SHA}",
        f"https://git.example.test:8443/s3cr3t-token/plugin@{SHA}",
    ],
)
def test_a_refused_url_is_never_repeated(ref: str) -> None:
    with pytest.raises(InvalidReference, match="not an allowed git URL") as caught:
        parse_direct(ref)
    assert "s3cr3t" not in str(caught.value)


def test_under_the_container_runtime_a_direct_install_is_approved_and_locked(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", answer="y\n")
    assert code == 0, out
    assert "demo@1.0.0 asks for these permissions:" in out
    assert "Approve these permissions and install demo?" in out
    assert "runs in a container (runtime: container)" in out  # #191: shown at approval
    assert f"commit  {repo[1]} (direct install, pinned by its SHA)" in out
    assert "tier" not in out
    installed = instance_dir / "plugins" / "demo"
    record = json.loads((installed / INSTALL_RECORD).read_text(encoding="utf-8"))
    assert (record["repo"], record["source"]) == (URL, "direct")
    assert "tier" not in record
    assert _lock(instance_dir)["plugins"]["demo"] == {
        "source": "direct",  # decision 73 A: the lock says where it came from
        "commit": repo[1],
        "permissions": {
            "intents": [],
            "network": ["api.example.org"],
            "optional_secrets": [],
            "paths": {"read": [], "write": []},
            "secrets": [],
        },
        "repo": URL,
        "version": "1.0.0",
    }


def test_under_the_container_runtime_json_reports_no_tier(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    code, out, err = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 0, out
    report = _json(out)
    assert (report["name"], report["repo"], report["commit"]) == ("demo", URL, repo[1])
    assert (report["tier"], report["index"]) == (None, None)
    assert (report["runtime"], report["first_party"], report["enabled"]) == (
        "container",
        False,
        True,  # `plugins.demo` was configured before the install (#191)
    )
    assert "asks for these permissions" in err


def test_the_pin_is_the_sha_not_the_tip(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    source, first = repo
    plugin_repo(source, version="2.0.0", tag=None)  # a newer commit on the branch
    code, out, _ = _add(instance_dir, f"{URL}@{first}", answer="y\n")
    assert code == 0, out
    assert _lock(instance_dir)["plugins"]["demo"]["commit"] == first
    assert _lock(instance_dir)["plugins"]["demo"]["version"] == "1.0.0"


def test_approval_comes_before_the_venv(
    instance_dir: Path,
    repo: tuple[Path, str],
    gate_open: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    def build(tree: Path, manifest: PluginManifest) -> None:
        built.append(manifest.name)

    monkeypatch.setattr(cli_plugin, "make_environment", lambda cache_dir: build)
    before = (instance_dir / "horae.lock").read_bytes()
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="n\n")
    assert code == 3, out
    assert _json(out)["error"]["kind"] == "approval_refused"
    assert built == []  # declined: no dependency was ever installed
    assert _nothing_installed(instance_dir)
    assert (instance_dir / "horae.lock").read_bytes() == before
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", answer="y\n")
    assert code == 0, out
    assert built == ["demo"]


def test_without_a_terminal_and_without_yes_a_direct_install_is_refused(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_plugin, "interactive", lambda: False)
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 3
    assert _json(out)["error"]["kind"] == "approval_refused"
    assert _nothing_installed(instance_dir)


def test_yes_passes_only_for_the_same_repo_sha_and_permissions(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    source, first = repo
    code, out, _ = _add(instance_dir, f"{URL}@{first}", "--yes", "--json")
    assert code == 3, out
    assert "no entry" in _json(out)["error"]["message"]
    assert _add(instance_dir, f"{URL}@{first}", answer="y\n")[0] == 0
    approved = (instance_dir / "horae.lock").read_bytes()
    shutil.rmtree(instance_dir / "plugins" / "demo")
    # The same repository, commit and permissions: a reproducible re-install.
    code, out, _ = _add(instance_dir, f"{URL}@{first}", "--yes")
    assert code == 0, out
    assert "Approve these permissions" not in out
    assert (instance_dir / "horae.lock").read_bytes() == approved
    shutil.rmtree(instance_dir / "plugins" / "demo")
    _, second = plugin_repo(source, tag=None, files={"more.py": "x = 1\n"})
    for ref, args, why in (
        (f"{URL}@{second}", (), "pins commit"),
        (f"{OTHER_URL}@{first}", (), "pins repository"),
        (f"{URL}@{first}", ("--allow-host", "extra.example.org"), "network"),
    ):
        code, out, _ = _add(instance_dir, ref, *args, "--yes", "--json")
        assert code == 3, out
        error = _json(out)["error"]
        assert error["kind"] == "approval_refused"
        assert why in error["message"]
        assert _nothing_installed(instance_dir)
        assert (instance_dir / "horae.lock").read_bytes() == approved


def test_a_direct_install_of_a_name_already_installed_is_refused_before_approval(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    assert _add(instance_dir, f"{URL}@{repo[1]}", answer="y\n")[0] == 0
    code, out, _ = _add(instance_dir, f"{URL}@{repo[1]}", "--json", answer="y\n")
    assert code == 3, out
    assert _json(out)["error"]["kind"] == "already_installed"
    assert "asks for these permissions" not in out


# --- upgrading a direct install to another commit (#191) -------------------------------------


def _upgrade(instance: Path, *args: str, answer: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["plugin", "upgrade", *args, "-i", str(instance)], input=answer)
    return result.exit_code, result.stdout, result.stderr


def _installed_direct(instance: Path, repo: tuple[Path, str]) -> tuple[Path, str]:
    """`demo` installed directly at the first commit, then a newer commit 1.1.0 on `main`."""
    assert _add(instance, f"{URL}@{repo[1]}", answer="y\n")[0] == 0
    return plugin_repo(repo[0], version="1.1.0", tag=None)


def _state(instance: Path) -> tuple[bytes, str]:
    record = (instance / "plugins" / "demo" / INSTALL_RECORD).read_text(encoding="utf-8")
    return (instance / "horae.lock").read_bytes(), record


def test_a_direct_install_is_upgraded_to_a_reachable_commit_under_the_container_runtime(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    _, second = _installed_direct(instance_dir, repo)
    code, out, err = _upgrade(instance_dir, f"{URL}@{second}", "--yes", "--json")
    assert code == 0, out
    report = _json(out)
    assert (report["upgraded"], report["tier"], report["to"]["commit"]) == (True, None, second)
    assert "runs in a container" in err
    entry = _lock(instance_dir)["plugins"]["demo"]
    assert (entry["source"], entry["commit"], entry["version"]) == ("direct", second, "1.1.0")
    record = json.loads(_state(instance_dir)[1])
    assert (record["source"], record["commit"]) == ("direct", second)
    # The same commit again: nothing to do.
    code, out, _ = _upgrade(instance_dir, f"{URL}@{second}", "--yes", "--json")
    assert (code, _json(out)["upgraded"]) == (0, False)


def test_a_direct_upgrade_is_refused_once_horae_yaml_runs_it_as_a_process(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    _, second = _installed_direct(instance_dir, repo)
    set_runtime(instance_dir, "process", "demo")
    before = _state(instance_dir)
    code, out, _ = _upgrade(instance_dir, f"{URL}@{second}", "--yes", "--json")
    assert code == 3, out
    error = _json(out)["error"]
    assert error["kind"] == "tier_refused"
    assert "plugins.demo.runtime: container" in error["message"]
    assert _state(instance_dir) == before


def test_a_direct_upgrade_to_a_commit_no_branch_or_tag_reaches_is_refused(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    _installed_direct(instance_dir, repo)
    stray = _unreachable_commit(repo[0])
    before = _state(instance_dir)
    code, out, _ = _upgrade(instance_dir, f"{URL}@{stray}", "--yes", "--json")
    assert code == 3, out
    assert _json(out)["error"]["kind"] == "install_refused"
    assert "not reachable from a branch or tag" in _json(out)["error"]["message"]
    assert _state(instance_dir) == before


def test_a_direct_upgrade_that_names_another_plugin_or_a_lower_version_is_refused(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    _installed_direct(instance_dir, repo)
    _, other = plugin_repo(repo[0], name="other", version="2.0.0", tag=None)
    _, lower = plugin_repo(repo[0], version="0.9.0", tag=None)
    before = _state(instance_dir)
    for sha, kind, why in (
        (other, "install_refused", "names 'other', not 'demo'"),
        (lower, "downgrade_refused", "--allow-downgrade"),
    ):
        code, out, _ = _upgrade(instance_dir, f"{URL}@{sha}", "--yes", "--json")
        assert code == 3, out
        error = _json(out)["error"]
        assert (error["kind"], why in error["message"]) == (kind, True), error
        assert _state(instance_dir) == before
        assert sorted(p.name for p in (instance_dir / "plugins").iterdir()) == ["demo"]


def test_a_direct_upgrade_needs_the_url_the_lock_records_and_a_name_upgrade_points_to_it(
    instance_dir: Path, repo: tuple[Path, str], gate_open: None
) -> None:
    _, second = _installed_direct(instance_dir, repo)
    code, out, _ = _upgrade(instance_dir, f"{OTHER_URL}@{second}", "--yes", "--json")
    assert code == 3, out
    error = _json(out)["error"]
    assert error["kind"] == "not_installed"
    assert OTHER_URL not in error["message"]  # decision 71 A: never repeated
    code, out, _ = _upgrade(instance_dir, "demo", "--yes", "--json")
    assert code == 3, out
    error = _json(out)["error"]
    assert error["kind"] == "install_refused"
    assert "horae plugin upgrade URL@SHA" in error["message"]
    for flag in ("--pre", "--offline"):
        code, out, _ = _upgrade(instance_dir, f"{URL}@{second}", flag, "--json")
        assert (code, _json(out)["error"]["kind"]) == (2, "usage")


# --- the installer: the same hardening as #44 ------------------------------------------------


def _installer(tmp_path: Path, source: Path | str) -> LocalInstaller:
    return LocalInstaller(tmp_path / "plugins", {URL: source})


def _left(plugins: Path) -> list[str]:
    return sorted(p.name for p in plugins.iterdir()) if plugins.is_dir() else []


def test_install_commit_needs_no_tag_and_takes_the_name_from_the_manifest(tmp_path: Path) -> None:
    source, sha = plugin_repo(tmp_path / "src", name="other", version="0.3.0", tag=None)
    installed = _installer(tmp_path, source).install_commit(URL, sha)
    assert (installed.name, installed.version, installed.repo, installed.commit) == (
        "other",
        "0.3.0",
        URL,
        sha,
    )
    assert _left(tmp_path / "plugins") == ["other"]  # no scratch directory, no .git
    assert sorted(p.name for p in installed.path.iterdir()) == [
        INSTALL_RECORD,
        "plugin.yaml",
        "server.py",
    ]


def test_install_commit_refuses_a_sha_that_is_not_in_the_repository(tmp_path: Path) -> None:
    source, _ = plugin_repo(tmp_path / "src", tag=None)
    with pytest.raises(InstallRefused, match="not reachable from a branch or tag"):
        _installer(tmp_path, source).install_commit(URL, SHA)
    assert _left(tmp_path / "plugins") == []


def _unreachable_commit(source: Path) -> str:
    """A commit in the object store of `source` that no branch or tag reaches, kept alive by
    a pull-request ref only: what a forge serves for a fork's commit through the parent URL."""
    stray = plugin_repo(source, version="6.6.6", tag=None, files={"evil.py": "x = 1\n"})[1]
    git(source, "update-ref", "refs/pull/1/head", stray)
    git(source, "reset", "-q", "--hard", "HEAD~1")
    return stray


def test_a_commit_no_branch_or_tag_reaches_is_refused(tmp_path: Path) -> None:
    """Security review of #171: a fork's commit, served through the trusted repository's URL,
    must never install under that repository's name."""
    source, _ = plugin_repo(tmp_path / "src", tag=None)
    stray = _unreachable_commit(source)
    assert git(source, "cat-file", "-t", stray) == "commit"  # in the object store
    with pytest.raises(InstallRefused, match="not reachable from a branch or tag"):
        _installer(tmp_path, source).install_commit(URL, stray)
    assert _left(tmp_path / "plugins") == []


@pytest.mark.parametrize("kind", ["branch", "tag", "annotated-tag"])
def test_a_commit_a_branch_or_tag_reaches_is_installed(tmp_path: Path, kind: str) -> None:
    source, _ = plugin_repo(tmp_path / "src", tag=None)
    stray = _unreachable_commit(source)
    if kind == "branch":
        git(source, "branch", "feature", stray)
    elif kind == "tag":
        git(source, "tag", "keep", stray)
    else:
        git(source, "tag", "-a", "keep", "-m", "keep", stray)
    installed = _installer(tmp_path, source).install_commit(URL, stray)
    assert (installed.commit, installed.version) == (stray, "6.6.6")


def test_a_commit_beyond_the_reach_depth_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, first = plugin_repo(tmp_path / "src", tag=None)
    for n in range(3):
        plugin_repo(source, tag=None, files={f"f{n}.txt": "x\n"})
    monkeypatch.setattr(install_module, "MAX_REACH_DEPTH", 2)
    with pytest.raises(InstallRefused, match="within 2 commits"):
        _installer(tmp_path, source).install_commit(URL, first)
    monkeypatch.setattr(install_module, "MAX_REACH_DEPTH", 10)
    assert _installer(tmp_path, source).install_commit(URL, first).commit == first


def test_too_many_branches_and_tags_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = plugin_repo(tmp_path / "src", tag=None)
    for n in range(3):
        git(source, "tag", f"t{n}")
    monkeypatch.setattr(install_module, "MAX_DIRECT_REFS", 3)
    with pytest.raises(InstallRefused, match="more than 3 branches and tags"):
        _installer(tmp_path, source).install_commit(URL, sha)


@pytest.mark.parametrize("sha", ["abc", SHA[:12], SHA.upper(), "v1.0.0"])
def test_install_commit_refuses_anything_but_a_full_sha(tmp_path: Path, sha: str) -> None:
    with pytest.raises(InstallRefused, match="full commit SHA"):
        PluginInstaller(tmp_path / "plugins").install_commit(URL, sha)
    assert _left(tmp_path / "plugins") == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///srv/plugin",
        "http://git.example.test/a/plugin",
        "https://user@git.example.test/a/plugin",
        "https://localhost/a/plugin",
    ],
)
def test_install_commit_refuses_non_https_urls_before_running_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    def fake(cwd: Path, *args: str, **kwargs: Any) -> str:
        raise AssertionError("git must not run")

    monkeypatch.setattr(install_module, "run_git", fake)
    with pytest.raises(InstallRefused, match="not an https git URL"):
        PluginInstaller(tmp_path / "plugins").install_commit(url, SHA)


def test_install_commit_fetches_only_branches_and_tags_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[tuple[str, ...], str]] = []
    real = git_module.run_git

    def spy(cwd: Path, *args: str, **kwargs: Any) -> str:
        seen.append((args, kwargs["allow"]))
        if args[0] == "ls-remote":
            return (
                f"{SHA}\trefs/heads/main\n{SHA}\trefs/tags/v1.0.0\n"
                f"{SHA}\trefs/pull/7/head\n{SHA}\trefs/heads/odd name\n"
            )
        if args[0] == "fetch":
            raise git_module.GitError("offline")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(install_module, "run_git", spy)
    with pytest.raises(InstallError, match="cannot fetch the branches and tags"):
        PluginInstaller(tmp_path / "plugins").install_commit(URL, SHA)
    listing = next(args for args, _ in seen if args[0] == "ls-remote")
    fetch = next(args for args, _ in seen if args[0] == "fetch")
    assert {allow for _, allow in seen} == {"https"}
    assert listing[listing.index("--") + 1 :] == (URL, "refs/heads/*", "refs/tags/*")
    # Never the SHA itself, never a pull-request ref: only the repository's own tips.
    assert fetch[fetch.index("--") + 1 :] == (
        URL,
        "+refs/heads/main:refs/heads/main",
        "+refs/tags/v1.0.0:refs/tags/v1.0.0",
    )
    assert "--no-recurse-submodules" in fetch
    assert "--no-tags" in fetch
    assert _left(tmp_path / "plugins") == []


def test_install_commit_refuses_a_submodule(tmp_path: Path) -> None:
    inner, inner_sha = plugin_repo(tmp_path / "inner", tag=None)
    source, _ = plugin_repo(tmp_path / "src", tag=None)
    (source / ".gitmodules").write_text(
        f'[submodule "sub"]\n\tpath = sub\n\turl = {inner.as_uri()}\n', encoding="utf-8"
    )
    git(source, "add", ".gitmodules")
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{inner_sha},sub")
    git(source, "commit", "-q", "-m", "submodule")
    with pytest.raises(InstallRefused, match="submodule"):
        _installer(tmp_path, source).install_commit(URL, git(source, "rev-parse", "HEAD"))
    assert _left(tmp_path / "plugins") == []


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside"])
def test_install_commit_refuses_an_escaping_symlink(tmp_path: Path, target: str) -> None:
    source, _ = plugin_repo(tmp_path / "src", tag=None)
    os.symlink(target, source / "link")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "link")
    with pytest.raises(InstallRefused, match="points outside"):
        _installer(tmp_path, source).install_commit(URL, git(source, "rev-parse", "HEAD"))
    assert _left(tmp_path / "plugins") == []


def test_install_commit_enforces_the_size_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sha = plugin_repo(tmp_path / "src", tag=None, files={"big.txt": "x" * 400})
    installer = _installer(tmp_path, source)
    monkeypatch.setattr(install_module, "MAX_TREE_BYTES", 300)
    with pytest.raises(InstallRefused, match="larger than 300 bytes"):
        installer.install_commit(URL, sha)
    monkeypatch.setattr(install_module, "MAX_TREE_BYTES", 10**8)
    monkeypatch.setattr(install_module, "MAX_FETCH_BYTES", 10)
    with pytest.raises(InstallRefused, match="more than 10"):
        installer.install_commit(URL, sha)
    assert _left(tmp_path / "plugins") == []


def test_install_commit_runs_no_hook_and_no_filter(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    source, sha = plugin_repo(
        tmp_path / "src",
        tag=None,
        files={
            ".gitattributes": "* filter=lfs diff=lfs merge=lfs\n*.py filter=evil\n",
            "hooks/post-checkout": f"#!/bin/sh\ntouch {marker}\n",
        },
    )
    git(source, "config", "filter.evil.smudge", f"touch {marker}; cat")
    installed = _installer(tmp_path, source).install_commit(URL, sha)
    assert (installed.path / "server.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not marker.exists()


def test_install_commit_refuses_an_invalid_manifest_and_a_taken_name(tmp_path: Path) -> None:
    bad, bad_sha = plugin_repo(
        tmp_path / "bad", tag=None, manifest=plugin_manifest(name="Not_A_Name")
    )
    with pytest.raises(InstallRefused, match=r"plugin\.yaml is invalid"):
        _installer(tmp_path, bad).install_commit(URL, bad_sha)
    source, sha = plugin_repo(tmp_path / "src", tag=None)
    (tmp_path / "plugins" / "demo").mkdir(parents=True)
    called: list[str] = []
    with pytest.raises(AlreadyInstalled):
        _installer(tmp_path, source).install_commit(
            URL, sha, prepare=lambda tree, manifest: called.append(manifest.name)
        )
    assert called == []
    assert _left(tmp_path / "plugins") == ["demo"]
