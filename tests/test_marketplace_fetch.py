# SPDX-License-Identifier: Apache-2.0
"""Marketplace fetch (#153): proxy and CA variables, private cache tree, one writer per clone,
and the commit patterns shared with the lockfile. Synthetic values only."""

import contextlib
import fcntl
import os
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import MarketplaceIndex
from horae.marketplace import FetchError, LockBusy, MarketplaceClient
from horae.marketplace import git as git_module
from horae.marketplace.cachefs import LOCK_WAIT_S, clone_lock, make_private_dirs
from horae.marketplace.git import GitError, git_env, network_env, run_git
from marketplace_fixtures import A, LocalClient, entry, publish

PROXY = "https://proxy.example.org:3128"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        *("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"),
        *("SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSH_COMMAND", "ALL_PROXY"),
    ):
        monkeypatch.delenv(var, raising=False)


# --- 1. proxy and CA ---------------------------------------------------------------------------


def test_git_env_stays_strict_with_proxy_and_ca_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    monkeypatch.setenv("GIT_SSH_COMMAND", "evil")
    monkeypatch.setenv("ALL_PROXY", "socks5://other.example.org")
    env = git_env()
    assert "HTTPS_PROXY" not in env
    assert "SSL_CERT_FILE" not in env
    assert "GIT_SSH_COMMAND" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_network_env_allows_exactly_the_listed_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_file, ca_dir = tmp_path / "ca.pem", tmp_path / "certs"
    ca_file.write_text("x")
    ca_dir.mkdir()
    values = {
        "HTTPS_PROXY": PROXY,
        "https_proxy": PROXY,
        "HTTP_PROXY": "http://proxy.example.org:3128",
        "http_proxy": "socks5h://proxy.example.org:1080",
        "NO_PROXY": "localhost,.example.org,10.0.0.0/8",
        "no_proxy": "localhost",
        "SSL_CERT_FILE": str(ca_file),
        "SSL_CERT_DIR": str(ca_dir),
    }
    for key, value in {**values, "ALL_PROXY": PROXY, "GIT_PROXY_COMMAND": "x"}.items():
        monkeypatch.setenv(key, value)
    assert network_env() == values


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@proxy.example.org:3128",
        "https://user@proxy.example.org",
        "proxy.example.org:3128",
        "ftp://proxy.example.org",
        "https://",
        "https://proxy.example.org:notaport",
        "https://proxy.example.org/path?x=1",
        "https://proxy.example.org with space",
        "https://proxy.example.org\n",
    ],
)
@pytest.mark.parametrize("var", ["HTTPS_PROXY", "http_proxy"])
def test_a_bad_proxy_is_refused_and_never_echoed(
    var: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(GitError) as raised:
        network_env()
    assert var in str(raised.value)
    assert "secret" not in str(raised.value)
    assert value.strip() not in str(raised.value)


def test_a_bad_no_proxy_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "host;touch x")
    with pytest.raises(GitError, match="NO_PROXY"):
        network_env()


@pytest.mark.parametrize("kind", ["relative", "missing", "directory", "nul"])
def test_a_bad_ca_file_is_refused(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {
        "relative": "ca.pem",
        "missing": str(tmp_path / "absent.pem"),
        "directory": str(tmp_path),
        "nul": str(tmp_path / "a\0b"),
    }[kind]
    with pytest.raises(GitError, match="SSL_CERT_FILE"):
        network_env({"SSL_CERT_FILE": value})


@pytest.mark.parametrize("kind", ["relative", "missing", "file"])
def test_a_bad_ca_dir_is_refused(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "f"
    plain.write_text("x")
    value = {"relative": "certs", "missing": str(tmp_path / "none"), "file": str(plain)}[kind]
    monkeypatch.setenv("SSL_CERT_DIR", value)
    with pytest.raises(GitError, match="SSL_CERT_DIR"):
        network_env()


def test_empty_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.setenv("SSL_CERT_FILE", "")
    assert network_env() == {}


def _spy_popen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    calls: list[tuple[list[str], dict[str, str]]] = []
    real = subprocess.Popen

    def popen(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs["env"]))
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


def test_only_a_call_that_reads_a_remote_gets_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    calls = _spy_popen(monkeypatch)
    run_git(tmp_path, "--version")
    run_git(tmp_path, "--version", network=True)
    assert "HTTPS_PROXY" not in calls[0][1]
    assert calls[1][1]["HTTPS_PROXY"] == PROXY


def test_a_client_fetch_passes_the_proxy_to_clone_and_fetch_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    calls = _spy_popen(monkeypatch)
    LocalClient(tmp_path / "cache", [A], {A: repo}).load()
    seen = {_sub(argv): env for argv, env in calls}
    assert seen["clone"]["HTTPS_PROXY"] == PROXY
    assert seen["fetch"]["HTTPS_PROXY"] == PROXY
    assert "HTTPS_PROXY" not in seen["show"]
    assert "HTTPS_PROXY" not in seen["cat-file"]


def _sub(argv: list[str]) -> str:
    """The git subcommand of an argv (after the global `-c key=value` pairs)."""
    items = iter(argv[1:])
    for item in items:
        if item == "-c":
            next(items)
        elif not item.startswith("-"):
            return item
    raise AssertionError(argv)


def test_an_invalid_proxy_makes_the_fetch_fail_into_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = LocalClient(tmp_path / "cache", [A], {A: repo})
    client.load()
    monkeypatch.setenv("HTTPS_PROXY", "https://user:secret@proxy.example.org")
    loaded = client.load()
    assert loaded.indexes[0].from_cache
    assert "HTTPS_PROXY" in loaded.warnings[0]
    assert "secret" not in loaded.warnings[0]
    with pytest.raises(FetchError) as raised:
        client.refresh(A)
    assert "secret" not in str(raised.value)


def test_hardening_still_applies_with_a_proxy_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    argv = git_module.git_command("clone", "--", "https://example.org/x/y")
    assert argv[:7] == [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    calls = _spy_popen(monkeypatch)
    marker = tmp_path / "ran"
    with pytest.raises(GitError, match="not allowed"):
        run_git(tmp_path, "clone", "--", f"ext::sh -c 'touch {marker}'", network=True)
    assert not marker.exists()
    env = calls[0][1]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["HTTPS_PROXY"] == PROXY
    assert "GIT_ALLOW_PROTOCOL" not in env


def test_the_installer_gives_the_proxy_to_remote_calls_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.marketplace import install as install_module

    installer = install_module.PluginInstaller(tmp_path / "plugins")
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    calls = _spy_popen(monkeypatch)
    for args in (["ls-remote"], ["clone"], ["fetch"], ["rev-parse"], ["ls-tree"], ["checkout"]):
        with contextlib.suppress(GitError):
            installer._git(tmp_path, *args, "--help-nothing")
    got = {_sub(argv): "HTTPS_PROXY" in env for argv, env in calls}
    assert got == {
        "ls-remote": True,
        "clone": True,
        "fetch": True,
        "rev-parse": False,
        "ls-tree": False,
        "checkout": False,
    }


# --- 2. permissions ----------------------------------------------------------------------------


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def open_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.usefixtures("open_umask")
def test_the_cache_tree_is_private_at_every_level_created(tmp_path: Path) -> None:
    existing = tmp_path / "state"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = LocalClient(existing / "deep" / "marketplaces", [A], {A: repo})
    client.load()
    assert _mode(existing) == 0o755  # it existed: untouched
    assert _mode(existing / "deep") == 0o700
    assert _mode(client.cache_dir) == 0o700
    folder = next(client.cache_dir.glob("*/repo.git")).parent
    assert _mode(folder) == 0o700
    for lock in client.cache_dir.glob("*.lock"):
        assert _mode(lock) == 0o600


@pytest.mark.usefixtures("open_umask")
def test_make_private_dirs_leaves_existing_directories_alone(tmp_path: Path) -> None:
    for name, mode in (("a", 0o755), ("b", 0o750)):
        (tmp_path / name).mkdir(mode=mode)
        (tmp_path / name).chmod(mode)
    make_private_dirs(tmp_path / "b" / "c" / "d")
    make_private_dirs(tmp_path / "a")
    assert (_mode(tmp_path / "a"), _mode(tmp_path / "b")) == (0o755, 0o750)
    assert (_mode(tmp_path / "b" / "c"), _mode(tmp_path / "b" / "c" / "d")) == (0o700, 0o700)


def test_make_private_dirs_refuses_a_file_in_the_way(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(OSError, match=r"Not a directory|exists"):
        make_private_dirs(tmp_path / "f" / "sub")


# --- 3. locking --------------------------------------------------------------------------------


def test_a_held_lock_ends_in_lock_busy_after_the_wait(tmp_path: Path) -> None:
    with clone_lock(tmp_path, "x", "clone x", wait_s=5):
        started = time.monotonic()
        with (
            pytest.raises(LockBusy, match="clone x"),
            clone_lock(tmp_path, "x", "clone x", wait_s=0.3),
        ):
            pass
        assert 0.25 <= time.monotonic() - started < 3
    with clone_lock(tmp_path, "x", "clone x", wait_s=0.3):  # free again
        pass


def test_a_waiting_run_gets_the_lock_when_the_holder_finishes(tmp_path: Path) -> None:
    order: list[str] = []

    def holder() -> None:
        with clone_lock(tmp_path, "x", "x", wait_s=5):
            order.append("held")
            time.sleep(0.4)
            order.append("released")

    thread = threading.Thread(target=holder)
    thread.start()
    while "held" not in order:
        time.sleep(0.01)
    with clone_lock(tmp_path, "x", "x", wait_s=5):
        order.append("second")
    thread.join()
    assert order == ["held", "released", "second"]


def test_different_clones_do_not_block_each_other(tmp_path: Path) -> None:
    with clone_lock(tmp_path, "x", "x", wait_s=1), clone_lock(tmp_path, "y", "y", wait_s=0.2):
        pass


def test_the_lock_is_not_inherited_by_git(tmp_path: Path) -> None:
    with clone_lock(tmp_path, "x", "x", wait_s=1):
        fd = os.open(tmp_path / "x.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_refresh_is_refused_with_lock_busy_while_another_run_holds_the_clone(
    tmp_path: Path,
) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    client = LocalClient(tmp_path / "cache", [A], {A: repo})
    client.lock_wait_s = 0.2
    client.load()
    with (
        clone_lock(client.cache_dir, client._folder(A).name, "x", wait_s=1),
        pytest.raises(LockBusy),
    ):
        client.load()


def test_concurrent_clients_both_succeed_on_one_clone(tmp_path: Path) -> None:
    repo = publish(tmp_path / "a", [entry("one", ["1.0.0"])])
    cache = tmp_path / "cache"
    results: list[Any] = []

    def work() -> None:
        try:
            results.append(LocalClient(cache, [A], {A: repo}).load())
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(not r.warnings and not r.indexes[0].from_cache for r in results), results


def test_the_cli_reports_lock_busy_with_the_locked_exit_code() -> None:
    from horae import cli_plugin
    from horae.cli_output import EXIT_LOCKED

    assert (LockBusy, EXIT_LOCKED, "lock_busy") in cli_plugin._KINDS


def test_the_client_wait_defaults_are_bounded() -> None:
    assert 0 < MarketplaceClient(Path("x"), []).lock_wait_s <= 300
    assert LOCK_WAIT_S > 0


# --- 4. commit patterns ------------------------------------------------------------------------


@pytest.mark.parametrize("length", [40, 64])
def test_the_index_accepts_40_and_64_hex(length: int) -> None:
    plugin = entry("one", {"1.0.0": "a" * length})
    assert MarketplaceIndex.model_validate({"plugins": [plugin]}).plugins[0].versions == {
        "1.0.0": "a" * length
    }
