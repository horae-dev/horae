# SPDX-License-Identifier: Apache-2.0
"""`--dry-run` writes nothing anywhere (PLAN.md §2.2, #21), enforced.

An autouse guard replaces every Python-level way this process could write a file, change
file metadata, open a network connection, resolve a host name or start a process. Each
attempt is recorded (and raises, unless caught) and fails the test at teardown, so code that
swallows exceptions cannot hide one. The only place a test may write to is its own `scratch`
directory, and Horae's own state database (`<state_dir>/horae.db` and its `-wal`, `-shm` and
`-journal` files: Horae's state, not user data, which `horae run --dry-run` will record its
run in, #26; schemas decision 32, D5). The vault and the instance (with its state database)
are built before the guard is armed, and must be identical afterwards: listing, bytes, modes
and mtimes, and the database's `user_version`. SQLite may only be opened on those paths, or
read-only (`mode=ro` in a `file:` URI).

Out of scope: writes made below Python (`ctypes`, C extensions calling libc directly) and
plugin subprocesses (the #24 host's sandbox covers those; no process can start here).
"""

import _socket
import asyncio
import builtins
import io
import os
import pty
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from apply_fixtures import DAILY, INBOX, RUN_ID, plugins, record, run_pipeline
from horae.apply import ChangeSet, LocalSink, StaticSinks, plan_changes, render_text, to_json
from horae.apply.testing import FixtureSink
from horae.cli import EXIT_OK, app
from horae.config import STORE_FILE
from horae.store import Store

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_TYPES = (str, bytes, os.PathLike)

_OS_PATH_OPS = {
    # name: how many leading arguments are paths
    "replace": 2,
    "rename": 2,
    "renames": 2,
    "link": 2,
    "symlink": 2,
    "unlink": 1,
    "remove": 1,
    "rmdir": 1,
    "removedirs": 1,
    "mkdir": 1,
    "makedirs": 1,
    "truncate": 1,
    "chmod": 1,
    "lchmod": 1,
    "chown": 1,
    "lchown": 1,
    "utime": 1,
    "mkfifo": 1,
    "mknod": 1,
    "setxattr": 1,
    "removexattr": 1,
    "chflags": 1,
    "lchflags": 1,
}
_OS_NEVER = (
    "system",
    "posix_spawn",
    "posix_spawnp",
    "fork",
    "forkpty",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "ftruncate",
    "fchmod",
    "fchown",
)


class Forbidden(Exception):
    """A write, connection or process the dry run must never make."""


def _tree(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    """Everything under `root` (itself included): bytes (`None` for a directory), mode and
    mtime, without following symlinks."""
    entries = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        entries[str(path.relative_to(root))] = (data, info.st_mode, info.st_mtime_ns)
    return entries


def _user_version(db: Path) -> int:
    with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as conn:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / INBOX).write_bytes(b"# Inbox\r\n- something\r\n")
    (root / DAILY).write_bytes(
        b"# Today\n<!-- horae:begin horae-brief -->\nold\n<!-- horae:end horae-brief -->\nend"
    )
    return root


@pytest.fixture
def stored(instance_dir: Path) -> Path:
    """The instance, with a store holding the dry run."""
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record())
    return instance_dir


@pytest.fixture(autouse=True)
def guard(
    tmp_path: Path, vault: Path, stored: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str]]:
    scratch = os.path.realpath(tmp_path / "scratch")
    os.mkdir(scratch)
    db = stored / "state" / STORE_FILE
    before = _tree(vault), _tree(stored), _user_version(db)
    seen: list[str] = []

    def forbid(what: str) -> None:
        seen.append(what)
        raise Forbidden(what)

    state_db = os.path.realpath(db)
    state_files = {state_db + suffix for suffix in ("", "-wal", "-shm", "-journal")}

    def allowed(path: Any) -> bool:
        if not isinstance(path, _PATH_TYPES):
            return False
        real = os.path.realpath(os.fsdecode(path))
        return real in state_files or os.path.commonpath([scratch, real]) == scratch

    real_open, real_os_open, real_connect = builtins.open, os.open, sqlite3.connect

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+") and not allowed(file):
            forbid(f"open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS and not allowed(path):
            forbid(f"os.open({path!r}, {flags:#o})")
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
        uri = kwargs.get("uri", False)
        if uri and isinstance(database, str) and database.startswith("file:"):
            parts = urlsplit(database)
            if parse_qs(parts.query).get("mode") == ["ro"] or allowed(parts.path):
                return real_connect(database, *args, **kwargs)
        elif allowed(database):
            return real_connect(database, *args, **kwargs)
        forbid(f"sqlite3.connect({database!r})")

    def path_op(name: str, real: Any, paths: int) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("dir_fd") is not None or not all(allowed(a) for a in args[:paths]):
                forbid(f"{name}{args!r}")
            return real(*args, **kwargs)

        return guarded

    def never(name: str) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            forbid(f"{name}{args!r}")

        return guarded

    class GuardedSocket(_socket.socket):
        connect = never("_socket.socket.connect")
        connect_ex = never("_socket.socket.connect_ex")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    for name, paths in _OS_PATH_OPS.items():
        if hasattr(os, name):
            monkeypatch.setattr(os, name, path_op(f"os.{name}", getattr(os, name), paths))
    for name in _OS_NEVER:
        if hasattr(os, name):
            monkeypatch.setattr(os, name, never(f"os.{name}"))
    monkeypatch.setattr(pty, "fork", never("pty.fork"))
    monkeypatch.setattr(shutil, "rmtree", never("shutil.rmtree"))
    for name in ("connect", "connect_ex", "sendto", "sendmsg", "bind"):
        monkeypatch.setattr(socket.socket, name, never(f"socket.{name}"))
    # `_socket.socket` is an immutable C type: replace it with a guarded subclass.
    monkeypatch.setattr(_socket, "socket", GuardedSocket)
    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    ):
        monkeypatch.setattr(socket, name, never(f"socket.{name}"))
    monkeypatch.setattr(subprocess, "Popen", never("subprocess.Popen"))
    yield seen
    monkeypatch.undo()
    assert seen == [], f"the dry run tried to write: {seen}"
    assert (_tree(vault), _tree(stored), _user_version(db)) == before


async def _dry_run(sinks: StaticSinks) -> ChangeSet:
    record = await run_pipeline(dry_run=True)
    assert record.dry_run
    assert record.changes == []
    return await plan_changes(record, sinks)


def test_dry_run_with_fixture_sinks_writes_nothing_and_never_applies() -> None:
    notes = FixtureSink(files={INBOX: b"# Inbox\n"})
    hooks, notifier = FixtureSink(), FixtureSink()
    sinks = StaticSinks({"vault.write_note": notes, "vault.tasks": notes, "hooks.post": hooks})
    sinks = StaticSinks({**sinks.sinks, "ntfy.push": notifier})
    changes = asyncio.run(_dry_run(sinks))
    assert changes.problems == ()
    assert [f.path for f in changes.files] == [DAILY, INBOX]
    assert [t.change.action for t in changes.tasks] == ["create", "update_status"]
    assert len(changes.http_calls) == 1
    assert len(changes.notifications) == 1
    render_text(changes, color=True)
    to_json(changes)
    assert len(notes.reads) == 2  # only notes are read; the core describes the rest
    assert hooks.reads == notifier.reads == []
    assert notes.applied == hooks.applied == notifier.applied == []
    assert notes.sent == hooks.sent == notifier.sent == []
    assert notes.files == {INBOX: b"# Inbox\n"}


def test_dry_run_with_the_local_sink_only_reads_the_vault(vault: Path) -> None:
    changes = asyncio.run(_dry_run(StaticSinks(default=LocalSink(vault))))
    assert changes.problems == ()
    daily, inbox = changes.files
    assert daily.status == inbox.status == "modify"
    assert daily.diff is not None
    assert "+- [ ] Review PR 42" in daily.diff
    assert "\\ No newline at end of file" in daily.diff
    assert inbox.diff is not None
    assert "+## Brief\r\n" in inbox.diff  # the note's CRLF line endings are kept


def test_horae_diff_writes_nothing(stored: Path, vault: Path) -> None:
    state = stored / "state"
    listing = _tree(state)
    version = _user_version(state / STORE_FILE)
    runner = CliRunner()
    for extra in ([], ["--json"], ["--color"]):
        result = runner.invoke(
            app, ["diff", RUN_ID, "-i", str(stored), "--vault", str(vault), *extra]
        )
        assert result.exit_code == EXIT_OK, result.output
        assert DAILY in result.output
    assert _tree(state) == listing
    assert _user_version(state / STORE_FILE) == version


def test_the_engine_calls_no_sink_during_a_dry_run() -> None:
    fixture = plugins()
    asyncio.run(run_pipeline(dry_run=True, fixture=fixture))
    assert fixture.refs == ["items.today", "brief.compose"]


def _connect_raw() -> None:
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        sock.connect(("192.0.2.1", 9))
    finally:
        sock.close()


def _sendmsg() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendmsg([b"x"], [], 0, ("192.0.2.1", 9))


def test_the_guard_catches_writes(
    guard: list[str], vault: Path, stored: Path, tmp_path: Path
) -> None:
    db = vault / "x.db"
    attempts: list[Callable[[], object]] = [
        lambda: open(vault / "x.md", "w"),  # noqa: SIM115
        lambda: (vault / DAILY).write_text("changed"),
        lambda: os.open(vault / "y", os.O_WRONLY | os.O_CREAT),
        lambda: os.replace(vault / DAILY, vault / "z"),
        lambda: os.unlink(vault / INBOX),
        lambda: os.mkdir(vault / "new"),
        lambda: os.chmod(vault / INBOX, 0o600),
        lambda: os.chmod(3, 0o600),  # a descriptor is never allowed
        lambda: os.chown(vault / INBOX, os.getuid(), os.getgid()),
        lambda: os.lchown(vault / INBOX, os.getuid(), os.getgid()),
        lambda: os.utime(vault / INBOX, (0, 0)),
        lambda: os.mkfifo(vault / "fifo"),
        lambda: os.mknod(vault / "node"),
        lambda: os.unlink("x", dir_fd=3),
        lambda: os.forkpty(),
        lambda: pty.fork(),
        lambda: sqlite3.connect(db),
        lambda: sqlite3.connect(f"file:{db}?mode=rwc", uri=True),
        lambda: socket.create_connection(("hooks.example.org", 443)),
        lambda: socket.gethostbyname("hooks.example.org"),
        lambda: socket.gethostbyname_ex("hooks.example.org"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getaddrinfo("hooks.example.org", 443),
        _sendmsg,
        _connect_raw,
        lambda: subprocess.Popen([sys.executable, "-c", "pass"]),
    ]
    if hasattr(os, "lchmod"):
        attempts.append(lambda: getattr(os, "lchmod")(vault / INBOX, 0o600))  # noqa: B009
    if hasattr(os, "setxattr"):
        attempts.append(lambda: getattr(os, "setxattr")(vault / INBOX, "user.x", b"1"))  # noqa: B009
        attempts.append(lambda: getattr(os, "removexattr")(vault / INBOX, "user.x"))  # noqa: B009
    for attempt in attempts:
        with pytest.raises(Forbidden):
            attempt()
    assert len(guard) == len(attempts)
    scratch = tmp_path / "scratch"
    (scratch / "ok.txt").write_text("allowed")
    os.chmod(scratch / "ok.txt", 0o600)
    sqlite3.connect(scratch / "ok.db").close()
    assert _user_version(stored / "state" / STORE_FILE) > 0  # read-only: allowed
    for name in ("x.db", "horae.db-other"):
        with pytest.raises(Forbidden):
            sqlite3.connect(stored / "state" / name)
    guard.clear()
    # Only the state database itself may be written (by `horae run --dry-run`, #26); the
    # teardown still checks that nothing in these tests changed it.
    sqlite3.connect(stored / "state" / STORE_FILE).close()
    guard.clear()
