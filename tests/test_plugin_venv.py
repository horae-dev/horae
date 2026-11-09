# SPDX-License-Identifier: Apache-2.0
"""Per-plugin virtual environments (#45, schemas decision 69): built at install from the
plugin's committed `uv.lock`, used by the host at run time, with no network then.

Everything runs against a local `file://` index of hand-made wheels (`venv_fixtures`), and the
plugins build with `uv_build`, which uv carries: no test touches the network.
"""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import Lockfile, PluginManifest
from horae.marketplace import InstallError, InstallRefused, PluginEnvironment
from horae.marketplace.environment import BUILD_BACKENDS, check_lock, check_project
from horae.marketplace.install import SCRATCH_PREFIX
from horae.plugins.host import HostRegistry, PluginHost
from horae.plugins.host.env import BASE_PATH, OFFLINE_ENV, build_env, plugin_venv
from horae.plugins.host.host import resolve_command
from horae.secrets import SecretResolver
from marketplace_fixtures import PLUGIN_URL, LocalInstaller, plugin_repo
from plugin_fixtures import lock_for
from venv_fixtures import (
    UV,
    local_index,
    pyproject,
    venv_manifest,
    venv_plugin_files,
    venv_plugin_repo,
)

needs_uv = pytest.mark.skipif(UV is None, reason="uv is not installed")
ALPHA_URL = "https://example.org/x/plugin-alpha"
BETA_URL = "https://example.org/x/plugin-beta"


@pytest.fixture
def index(tmp_path: Path) -> str:
    return local_index(tmp_path / "index")


def _environment(tmp_path: Path, index: str, **kwargs: Any) -> PluginEnvironment:
    return PluginEnvironment(tmp_path / "state" / "uv-cache", index_url=index, **kwargs)


def _repo(tmp_path: Path, index: str, name: str, version: str, tinydep: str, **kw: Any) -> Any:
    return venv_plugin_repo(
        tmp_path / f"repo-{name}",
        name,
        version,
        tinydep,
        index_url=index,
        cache=tmp_path / "lock-cache",
        **kw,
    )


def _left(plugins: Path) -> list[str]:
    return sorted(p.name for p in plugins.iterdir()) if plugins.exists() else []


def _run_info(plugins: Path, *names: str) -> dict[str, Any]:
    manifests = {
        name: PluginManifest.model_validate(venv_manifest(name, "1.0.0")) for name in names
    }
    lock: Lockfile = lock_for(*(venv_manifest(name, "1.0.0") for name in names))
    registry = HostRegistry(lock, manifests, {name: plugins / name for name in names})
    (plugins.parent / "secrets").mkdir(exist_ok=True)
    resolver = SecretResolver({}, plugins.parent / "secrets")

    async def scenario() -> dict[str, Any]:
        async with PluginHost(registry, secrets=resolver, work_dir=plugins.parent / "work") as h:
            return {name: (await h.call(f"{name}.info", {}, None)).data for name in names}

    return asyncio.run(scenario())


# --- install and run --------------------------------------------------------------------------


@needs_uv
def test_two_plugins_with_conflicting_dependencies_both_install_and_run(
    tmp_path: Path, index: str
) -> None:
    alpha, alpha_sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0")
    beta, beta_sha = _repo(tmp_path, index, "beta", "1.0.0", "2.0.0")
    plugins = tmp_path / "instance" / "plugins"
    installer = LocalInstaller(plugins, {ALPHA_URL: alpha, BETA_URL: beta})
    prepare = _environment(tmp_path, index)
    installer.install("alpha", "1.0.0", ALPHA_URL, alpha_sha, prepare=prepare)
    installer.install("beta", "1.0.0", BETA_URL, beta_sha, prepare=prepare)
    assert _left(plugins) == ["alpha", "beta"]
    # The project is installed non-editable: the venv does not point back into the tree.
    for name in ("alpha", "beta"):
        site = next((plugins / name / ".venv").glob("lib/python*/site-packages"))
        assert (site / name / "__main__.py").is_file()
        assert not [p for p in site.glob("*.pth") if name in p.name]

    # No network at run time: the index and uv's cache are gone, and uv is not needed.
    shutil.rmtree(tmp_path / "index")
    shutil.rmtree(tmp_path / "state" / "uv-cache")
    info = _run_info(plugins, "alpha", "beta")

    assert info["alpha"]["tinydep"] == "1.0.0"
    assert info["beta"]["tinydep"] == "2.0.0"
    for name in ("alpha", "beta"):
        venv = (plugins / name / ".venv").resolve()
        data = info[name]
        assert Path(data["prefix"]).resolve() == venv
        env = data["env"]
        for key, value in OFFLINE_ENV.items():
            assert env[key] == value
        assert Path(env["VIRTUAL_ENV"]).resolve() == venv
        assert env["PATH"].endswith(":" + BASE_PATH)
        assert Path(env["PATH"].split(":")[0]).resolve() == venv / "bin"


@needs_uv
def test_an_upgrade_installs_a_fresh_venv_from_the_new_lock(tmp_path: Path, index: str) -> None:
    repo, sha1 = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0")
    plugins = tmp_path / "instance" / "plugins"
    installer = LocalInstaller(plugins, {ALPHA_URL: repo})
    prepare = _environment(tmp_path, index)
    installer.install("alpha", "1.0.0", ALPHA_URL, sha1, prepare=prepare)
    old = plugins / "alpha" / ".venv"
    site = next(old.glob("lib/python*/site-packages"))
    assert (site / "tinydep" / "__init__.py").read_text() == "VERSION = '1.0.0'\n"

    # Version 2.0.0 pins tinydep 2.0.0. What #47 will do: install the new tree (with its own
    # venv, built in the scratch directory) and swap it in; here the old one is removed first.
    _, sha2 = _repo(tmp_path, index, "alpha", "2.0.0", "2.0.0")
    shutil.rmtree(plugins / "alpha")
    installer.install("alpha", "2.0.0", ALPHA_URL, sha2, prepare=prepare)
    site = next((plugins / "alpha" / ".venv").glob("lib/python*/site-packages"))
    assert (site / "tinydep" / "__init__.py").read_text() == "VERSION = '2.0.0'\n"
    assert [p.name for p in site.glob("tinydep-*.dist-info")] == ["tinydep-2.0.0.dist-info"]


# --- refusals: nothing is left ----------------------------------------------------------------


@needs_uv
def test_a_plugin_without_a_lock_file_is_refused_and_leaves_nothing(
    tmp_path: Path, index: str
) -> None:
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0", locked=False)
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match=r"no uv\.lock"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index)
        )
    assert _left(plugins) == []


def test_a_plugin_without_a_pyproject_is_refused(tmp_path: Path) -> None:
    repo, sha = plugin_repo(tmp_path / "repo")  # plugin.yaml and server.py only
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match=r"no pyproject\.toml"):
        LocalInstaller(plugins, {PLUGIN_URL: repo}).install(
            "demo", "1.0.0", PLUGIN_URL, sha, prepare=PluginEnvironment(tmp_path / "cache")
        )
    assert _left(plugins) == []


@needs_uv
def test_a_lock_that_no_longer_matches_the_project_is_refused(tmp_path: Path, index: str) -> None:
    # Locked with tinydep 1.0.0, then pyproject.toml asks for 2.0.0: --locked never re-resolves.
    changed = {"pyproject.toml": pyproject("alpha", "1.0.0", ["tinydep==2.0.0"])}
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0", files=changed)
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match="needs to be updated, but `--locked`"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index)
        )
    assert _left(plugins) == []


@needs_uv
def test_a_file_that_does_not_match_its_locked_hash_is_refused(tmp_path: Path, index: str) -> None:
    repo, _ = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0")
    text = (repo / "uv.lock").read_text()
    good = text.split('hash = "sha256:')[1][:64]
    tampered = {"uv.lock": text.replace(good, "0" * 64)}
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0", files=tampered)
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match=r"Hash mismatch for `tinydep==1\.0\.0`"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index)
        )
    assert _left(plugins) == []


@needs_uv
def test_a_plugin_that_does_not_accept_the_interpreter_is_refused(
    tmp_path: Path, index: str
) -> None:
    files = {"pyproject.toml": pyproject("alpha", "1.0.0", [], requires_python=">=3.99")}
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0", files=files, locked=False)
    (repo / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.99"\n\n[[package]]\nname = "alpha"\n'
        'version = "1.0.0"\nsource = { editable = "." }\n'
    )
    _, sha = plugin_repo(repo, name="alpha", manifest=(repo / "plugin.yaml").read_text())
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match="incompatible with the project's Python"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index)
        )
    assert _left(plugins) == []


BAD_BUILDS = {
    "unknown backend": '[build-system]\nrequires = ["evil"]\nbuild-backend = "evil.backend"\n',
    "no backend": '[build-system]\nrequires = ["hatchling"]\n',
    "in-tree backend": (
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        'backend-path = ["."]\n'
    ),
    "extra requirement": (
        '[build-system]\nrequires = ["hatchling", "hatch-evil"]\n'
        'build-backend = "hatchling.build"\n'
    ),
    "direct URL": (
        '[build-system]\nrequires = ["hatchling @ https://example.org/h.whl"]\n'
        'build-backend = "hatchling.build"\n'
    ),
    "no build-system": "",
    "virtual project": "[tool.uv]\npackage = false\n"
    + ('[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'),
}


@pytest.mark.parametrize("case", sorted(BAD_BUILDS))
def test_a_build_backend_that_is_not_allowed_is_refused_before_uv_runs(
    tmp_path: Path, case: str
) -> None:
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0")
    files["pyproject.toml"] = pyproject("alpha", "1.0.0", [], build=BAD_BUILDS[case])
    if case == "no build-system":
        files["pyproject.toml"] = '[project]\nname = "alpha"\nversion = "1.0.0"\n'
    files["uv.lock"] = 'version = 1\n[[package]]\nname = "alpha"\nsource = { editable = "." }\n'
    manifest = files.pop("plugin.yaml")
    repo, sha = plugin_repo(tmp_path / "repo", name="alpha", manifest=manifest, files=files)
    plugins = tmp_path / "plugins"
    never = PluginEnvironment(tmp_path / "cache", uv=str(tmp_path / "no-such-uv"))
    with pytest.raises(InstallRefused):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=never
        )
    assert _left(plugins) == []


def test_every_allowed_backend_with_its_own_requirement_passes(tmp_path: Path) -> None:
    names = {
        "hatchling.build": "hatchling>=1.25",
        "setuptools.build_meta": "setuptools",
        "uv_build": "uv_build>=0.7,<1",
        "flit_core.buildapi": "flit_core >=3.2,<4",
        "pdm.backend": "pdm-backend",
    }
    assert set(names) == set(BUILD_BACKENDS)
    for backend, requirement in names.items():
        build = f'[build-system]\nrequires = ["{requirement}"]\nbuild-backend = "{backend}"\n'
        (tmp_path / "pyproject.toml").write_text(pyproject("alpha", "1.0.0", [], build=build))
        assert check_project(tmp_path, "alpha") == "alpha"


LOCK_HEAD = 'version = 1\n\n[[package]]\nname = "alpha"\nsource = { editable = "." }\n\n'
BAD_LOCKS = {
    "no hash": (
        '[[package]]\nname = "x"\nsource = { registry = "https://pypi.org/simple" }\n'
        'wheels = [{ url = "https://example.org/x.whl" }]\n'
    ),
    "no file": '[[package]]\nname = "x"\nsource = { registry = "https://pypi.org/simple" }\n',
    "git": '[[package]]\nname = "x"\nsource = { git = "https://example.org/x?rev=1#1" }\n',
    "outside": '[[package]]\nname = "x"\nsource = { directory = "../elsewhere" }\n',
    "absolute": '[[package]]\nname = "x"\nsource = { path = "/tmp/x.whl" }\n',
    "sdist only": (
        '[[package]]\nname = "x"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://example.org/x.tar.gz", hash = "sha256:' + "a" * 64 + '" }\n'
    ),
    "another in-tree package": '[[package]]\nname = "x"\nsource = { editable = "libs/x" }\n',
    "md5 only": (
        '[[package]]\nname = "x"\nsource = { url = "https://example.org/x.tar.gz" }\n'
        'sdist = { url = "https://example.org/x.tar.gz", hash = "md5:00" }\n'
    ),
}


@pytest.mark.parametrize("case", sorted(BAD_LOCKS))
def test_a_lock_entry_without_a_hash_or_from_outside_the_tree_is_refused(
    tmp_path: Path, case: str
) -> None:
    (tmp_path / "uv.lock").write_text(LOCK_HEAD + BAD_LOCKS[case])
    with pytest.raises(InstallRefused):
        check_lock(tmp_path, "alpha", "alpha")


def test_an_sdist_only_dependency_is_refused_by_name(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(LOCK_HEAD + BAD_LOCKS["sdist only"])
    with pytest.raises(InstallRefused, match=r"x has no wheel"):
        check_lock(tmp_path, "alpha", "alpha")


def test_a_hashed_lock_passes_and_lists_what_is_never_built(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        LOCK_HEAD + '[[package]]\nname = "x"\nsource = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://example.org/x.tar.gz", hash = "sha256:' + "a" * 64 + '" }\n'
        'wheels = [{ url = "https://example.org/x.whl", hash = "sha256:' + "b" * 64 + '" }]\n'
    )
    assert check_lock(tmp_path, "alpha", "alpha") == ["x"]


def test_a_dependency_with_only_an_sdist_is_refused_before_anything_runs(tmp_path: Path) -> None:
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0")
    files["uv.lock"] = LOCK_HEAD + BAD_LOCKS["sdist only"]
    manifest = files.pop("plugin.yaml")
    repo, sha = plugin_repo(tmp_path / "repo", name="alpha", manifest=manifest, files=files)
    plugins = tmp_path / "plugins"
    never = PluginEnvironment(tmp_path / "cache", uv=str(tmp_path / "no-such-uv"))
    with pytest.raises(InstallRefused, match="x has no wheel"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=never
        )
    assert _left(plugins) == []


@needs_uv
def test_a_dependency_is_never_built_even_when_no_wheel_fits(tmp_path: Path) -> None:
    # The index has a wheel uv cannot use here and an sdist whose build would leave a marker:
    # the lock passes check_lock (it has a hashed wheel), and uv must refuse to build it.
    index = local_index(tmp_path / "index", foreign_sdist=True)
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "3.0.0")
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallError, match="cannot install the dependencies of alpha"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index)
        )
    assert _left(plugins) == []
    assert not (tmp_path / "BUILT").exists()


def test_a_committed_venv_is_refused(tmp_path: Path) -> None:
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0")
    files["uv.lock"] = LOCK_HEAD
    files[".venv/bin/python"] = "#!/bin/sh\n"
    files[".venv/pyvenv.cfg"] = "home = /usr/bin\n"
    manifest = files.pop("plugin.yaml")
    repo, sha = plugin_repo(tmp_path / "repo", name="alpha", manifest=manifest, files=files)
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match=r"holds a \.venv"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=PluginEnvironment(tmp_path / "cache")
        )
    assert _left(plugins) == []


def test_without_uv_the_install_fails_and_leaves_nothing(tmp_path: Path) -> None:
    files = venv_plugin_files("alpha", "1.0.0", "1.0.0")
    files["uv.lock"] = LOCK_HEAD
    manifest = files.pop("plugin.yaml")
    repo, sha = plugin_repo(tmp_path / "repo", name="alpha", manifest=manifest, files=files)
    plugins = tmp_path / "plugins"
    environment = PluginEnvironment(tmp_path / "cache", uv=str(tmp_path / "no-such-uv"))
    with pytest.raises(InstallError, match="cannot run uv"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=environment
        )
    assert not [p for p in _left(plugins) if p.startswith(SCRATCH_PREFIX)]
    assert _left(plugins) == []


@needs_uv
def test_a_venv_over_its_size_budget_is_refused(tmp_path: Path, index: str) -> None:
    repo, sha = _repo(tmp_path, index, "alpha", "1.0.0", "1.0.0")
    plugins = tmp_path / "plugins"
    with pytest.raises(InstallRefused, match="bytes"):
        LocalInstaller(plugins, {ALPHA_URL: repo}).install(
            "alpha", "1.0.0", ALPHA_URL, sha, prepare=_environment(tmp_path, index, max_bytes=10)
        )
    assert _left(plugins) == []


# --- uv's environment ----------------------------------------------------------------------


def test_uv_inherits_no_index_setting_only_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "UV_INDEX_URL": "https://evil.example/simple",
        "UV_EXTRA_INDEX_URL": "https://evil.example/simple",
        "UV_FIND_LINKS": "/evil",
        "UV_INSECURE_HOST": "evil.example",
        "PIP_INDEX_URL": "https://evil.example/simple",
        "PIP_EXTRA_INDEX_URL": "https://evil.example/simple",
        "UV_CONFIG_FILE": "/evil/uv.toml",
        "XDG_CONFIG_HOME": "/evil",
        "HTTPS_PROXY": "http://proxy.lan:3128",
    }.items():
        monkeypatch.setenv(name, value)
    environment = PluginEnvironment(tmp_path / "cache")
    env = environment.env(tmp_path / "tree", tmp_path / "home")
    assert env == {
        "HTTPS_PROXY": "http://proxy.lan:3128",
        "PATH": BASE_PATH,
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
        "UV_NO_CONFIG": "1",
        "UV_CACHE_DIR": str(tmp_path / "cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "tree" / ".venv"),
        "UV_LINK_MODE": "copy",
        "UV_NO_PROGRESS": "1",
    }
    assert environment.index_url is None  # the default index is uv's, never a setting


# --- run time: how runtime.command maps onto the venv ---------------------------------------


def _fake_venv(root: Path) -> Path:
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nrelocatable = true\n")
    for name in ("python", "my-plugin"):
        script = venv / "bin" / name
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    return venv


def test_a_bare_command_resolves_in_the_plugin_venv_first(tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    venv = _fake_venv(root)
    assert plugin_venv(root) == venv
    python = str(venv / "bin" / "python")
    assert resolve_command(["python", "-m", "alpha"], root) == [python, "-m", "alpha"]
    assert resolve_command(["my-plugin"], root) == [str(venv / "bin" / "my-plugin")]
    # A name the venv does not have is looked up on the base PATH, as before.
    assert resolve_command(["env"], root) == [shutil.which("env", path=BASE_PATH)]
    # An absolute command is kept as written (the contract before #45).
    assert resolve_command(["/bin/sh"], root) == ["/bin/sh"]


def test_the_venv_python_symlink_is_kept_as_found(tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    venv = _fake_venv(root)
    (venv / "bin" / "python").unlink()
    target = shutil.which("sh", path=BASE_PATH)
    assert target is not None
    os.symlink(target, venv / "bin" / "python")
    assert resolve_command(["python"], root) == [str(venv / "bin" / "python")]


def test_a_plugin_without_a_venv_keeps_the_old_contract(tmp_path: Path) -> None:
    root = tmp_path / "old"
    root.mkdir()
    assert plugin_venv(root) is None
    assert plugin_venv(None) is None
    env = build_env("old", home=tmp_path, tmpdir=tmp_path, timezone="UTC", secrets={})
    assert env["PATH"] == BASE_PATH
    assert "VIRTUAL_ENV" not in env
    assert {k: env[k] for k in OFFLINE_ENV} == OFFLINE_ENV


def test_the_venv_env(tmp_path: Path) -> None:
    venv = _fake_venv(tmp_path / "alpha")
    env = build_env("alpha", home=tmp_path, tmpdir=tmp_path, timezone="UTC", secrets={}, venv=venv)
    assert env["PATH"] == f"{venv / 'bin'}:{BASE_PATH}"
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["UV_OFFLINE"] == "1"
    assert env["PIP_NO_INDEX"] == "1"


def test_a_plugin_child_stays_offline_whether_or_not_uv_is_on_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image ships uv for `horae plugin add` (#169); a plugin child must not gain a way to
    reach the network from it, and the caller's `UV_*` settings never reach the child."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uv").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("UV_OFFLINE", "0")
    monkeypatch.setenv("PIP_NO_INDEX", "0")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.org/simple")
    env = build_env("demo", home=tmp_path, tmpdir=tmp_path, timezone="UTC", secrets={})
    assert env["PATH"] == BASE_PATH
    assert {k: env[k] for k in OFFLINE_ENV} == OFFLINE_ENV
    assert "UV_INDEX_URL" not in env
