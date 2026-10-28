# SPDX-License-Identifier: Apache-2.0
"""`--dry-run` writes nothing anywhere (PLAN.md §2.2, #21), enforced.

An autouse guard replaces every way this process could write a file, change the filesystem,
open a network connection or start a process. Each attempt is recorded (and raises, unless
caught) and fails the test at teardown, so code that swallows exceptions cannot hide one.
The only place a test may write to is its own `scratch` directory; the vault the local sink
reads is built before the guard is armed, and must be byte-identical afterwards.
"""

import asyncio
import builtins
import io
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from apply_fixtures import DAILY, INBOX, plugins, run_pipeline
from horae.apply import ChangeSet, LocalSink, StaticSinks, plan_changes, render_text, to_json
from horae.apply.testing import FixtureSink

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class Forbidden(Exception):
    """A write, connection or process the dry run must never make."""


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / INBOX).write_bytes(b"# Inbox\r\n- something\r\n")
    (root / DAILY).write_bytes(
        b"# Today\n<!-- horae:begin horae-brief -->\nold\n<!-- horae:end horae-brief -->\nend"
    )
    return root


@pytest.fixture(autouse=True)
def guard(tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    scratch = os.path.realpath(tmp_path / "scratch")
    os.mkdir(scratch)
    before = _tree(vault)
    seen: list[str] = []

    def forbid(what: str) -> None:
        seen.append(what)
        raise Forbidden(what)

    def allowed(path: Any) -> bool:
        if isinstance(path, int):
            return False
        real = os.path.realpath(os.fsdecode(path))
        return os.path.commonpath([scratch, real]) == scratch

    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in "wax+") and not allowed(file):
            forbid(f"open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & _WRITE_FLAGS and not allowed(path):
            forbid(f"os.open({path!r}, {flags:#o})")
        return real_os_open(path, flags, *args, **kwargs)

    def path_op(name: str, real: Any) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if not all(allowed(arg) for arg in args[:2] if isinstance(arg, str | bytes | Path)):
                forbid(f"{name}{args!r}")
            return real(*args, **kwargs)

        return guarded

    def never(name: str) -> Any:
        def guarded(*args: Any, **kwargs: Any) -> Any:
            forbid(f"{name}{args!r}")

        return guarded

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for name in (
        "replace",
        "rename",
        "unlink",
        "remove",
        "rmdir",
        "mkdir",
        "makedirs",
        "symlink",
        "link",
        "truncate",
    ):
        monkeypatch.setattr(os, name, path_op(f"os.{name}", getattr(os, name)))
    monkeypatch.setattr(shutil, "rmtree", never("shutil.rmtree"))
    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, never(f"socket.{name}"))
    monkeypatch.setattr(socket, "create_connection", never("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", never("socket.getaddrinfo"))
    monkeypatch.setattr(subprocess, "Popen", never("subprocess.Popen"))
    for name in ("system", "posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
        monkeypatch.setattr(os, name, never(f"os.{name}"))
    yield seen
    monkeypatch.undo()
    assert seen == [], f"the dry run tried to write: {seen}"
    assert _tree(vault) == before


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
    assert len(notes.previews) == 4
    assert len(hooks.previews) == len(notifier.previews) == 1
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
    assert "+## Brief\n" in inbox.diff


def test_the_engine_calls_no_sink_during_a_dry_run() -> None:
    fixture = plugins()
    asyncio.run(run_pipeline(dry_run=True, fixture=fixture))
    assert fixture.refs == ["items.today", "brief.compose"]


def test_the_guard_catches_writes(guard: list[str], vault: Path, tmp_path: Path) -> None:
    attempts: list[Callable[[], object]] = [
        lambda: open(vault / "x.md", "w"),  # noqa: SIM115
        lambda: (vault / DAILY).write_text("changed"),
        lambda: os.open(vault / "y", os.O_WRONLY | os.O_CREAT),
        lambda: os.replace(vault / DAILY, vault / "z"),
        lambda: os.unlink(vault / INBOX),
        lambda: os.mkdir(vault / "new"),
        lambda: socket.create_connection(("hooks.example.org", 443)),
        lambda: subprocess.Popen([sys.executable, "-c", "pass"]),
    ]
    for attempt in attempts:
        with pytest.raises(Forbidden):
            attempt()
    assert len(guard) == len(attempts)
    (tmp_path / "scratch" / "ok.txt").write_text("allowed")
    guard.clear()
