# SPDX-License-Identifier: Apache-2.0
"""`--dry-run` writes nothing anywhere (PLAN.md §2.2, #21), enforced.

An autouse guard (`write_guard.arm`, shared with the end-to-end test) replaces every
Python-level way this process could write a file, change file metadata, open a network
connection, resolve a host name or start a process. Each attempt is recorded (and raises,
unless caught) and fails the test at teardown, so code that swallows exceptions cannot hide
one. The only place a test may write to is its own `scratch`
directory, and Horae's own state database (`<state_dir>/horae.db` and its `-wal`, `-shm` and
`-journal` files: Horae's state, not user data, which `horae run --dry-run` will record its
run in, #26; schemas decision 32, D5). The vault and the instance (with its state database)
are built before the guard is armed, and must be identical afterwards: listing, bytes, modes
and mtimes, and the database's `user_version`. SQLite may only be opened on those paths, or
read-only (`mode=ro` in a `file:` URI).

Out of scope: writes made below Python (`ctypes`, C extensions calling libc directly) and
plugin subprocesses (no process can start here; `tests/test_e2e.py` allows the built-in
plugins' and compares the vault byte for byte).
"""

import _socket
import asyncio
import os
import pty
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apply_fixtures import DAILY, INBOX, RUN_ID, plugins, record, run_pipeline
from horae.apply import ChangeSet, LocalSink, StaticSinks, plan_changes, render_text, to_json
from horae.apply.testing import FixtureSink
from horae.cli import EXIT_OK, app
from horae.config import STORE_FILE
from horae.store import Store
from write_guard import Forbidden, arm, tree, under, user_version


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
    before = tree(vault), tree(stored), user_version(db)
    state_db = os.path.realpath(db)
    state_files = {state_db + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    in_scratch = under(Path(scratch))
    seen = arm(monkeypatch, lambda real: real in state_files or in_scratch(real))
    yield seen
    monkeypatch.undo()
    assert seen == [], f"the dry run tried to write: {seen}"
    assert (tree(vault), tree(stored), user_version(db)) == before


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
    listing = tree(state)
    version = user_version(state / STORE_FILE)
    runner = CliRunner()
    for extra in ([], ["--json"], ["--color"]):
        result = runner.invoke(
            app, ["diff", RUN_ID, "-i", str(stored), "--vault", str(vault), *extra]
        )
        assert result.exit_code == EXIT_OK, result.output
        assert DAILY in result.output
    assert tree(state) == listing
    assert user_version(state / STORE_FILE) == version


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
    assert user_version(stored / "state" / STORE_FILE) > 0  # read-only: allowed
    for name in ("x.db", "horae.db-other"):
        with pytest.raises(Forbidden):
            sqlite3.connect(stored / "state" / name)
    guard.clear()
    # Only the state database itself may be written (by `horae run --dry-run`, #26); the
    # teardown still checks that nothing in these tests changed it.
    sqlite3.connect(stored / "state" / STORE_FILE).close()
    guard.clear()
