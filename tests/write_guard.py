# SPDX-License-Identifier: Apache-2.0
"""A write guard for dry-run tests (PLAN.md §2.2 "`--dry-run` writes nothing", #21, #25).

`arm(monkeypatch, allowed, allow_process)` replaces every Python-level way this process could
write a file, change file metadata, open a network connection, resolve a host name or start a
process. Each attempt is recorded in the returned list (and raises `Forbidden`, unless caught),
so a test can fail at teardown even when code swallows the exception.

- `allowed(path)` says which paths may be written (the caller's scratch directory, Horae's own
  state); a call relative to a directory descriptor (`dir_fd`) is never allowed;
- `allow_process(argv)` says which processes may start (default: none). The end-to-end test
  allows the built-in plugins' subprocesses, whose own writes are below this guard: it
  compares the vault byte for byte instead (`tree`).

Processes: besides `subprocess.Popen`, `os.system`, `os.posix_spawn*`, `os.fork*`, `os.exec*`
and `pty.fork`, the guard replaces `_posixsubprocess.fork_exec` (and `subprocess._fork_exec`,
the same function imported by `subprocess`), which `multiprocessing` (`spawn` and
`forkserver`, through `multiprocessing.util.spawnv_passfds`) and `Popen` itself use: it is
allowed only inside an allowed `Popen`.

Out of scope: writes made below Python (`ctypes`, C extensions calling libc directly) and the
writes of an allowed subprocess.
"""

import _posixsubprocess
import _socket
import builtins
import io
import os
import pty
import shutil
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
PATH_TYPES = (str, bytes, os.PathLike)

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


def tree(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    """Everything under `root` (itself included): bytes (`None` for a directory), mode and
    mtime, without following symlinks."""
    entries = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        entries[str(path.relative_to(root))] = (data, info.st_mode, info.st_mtime_ns)
    return entries


def user_version(db: Path) -> int:
    with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as conn:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def under(*roots: Path) -> Callable[[str], bool]:
    """`allowed` for real paths inside one of `roots`."""
    reals = [os.path.realpath(root) for root in roots]
    return lambda real: any(os.path.commonpath([root, real]) == root for root in reals)


def arm(
    monkeypatch: pytest.MonkeyPatch,
    allowed_real: Callable[[str], bool],
    allow_process: Callable[[Any], bool] | None = None,
) -> list[str]:
    """Arm the guard (see the module docstring); `allowed_real` gets a real path."""
    seen: list[str] = []

    def forbid(what: str) -> None:
        seen.append(what)
        raise Forbidden(what)

    def allowed(path: Any) -> bool:
        if not isinstance(path, PATH_TYPES):
            return False
        return allowed_real(os.path.realpath(os.fsdecode(path)))

    real_open, real_os_open, real_connect = builtins.open, os.open, sqlite3.connect
    real_popen, real_unlink, real_rmdir = subprocess.Popen, os.unlink, os.rmdir

    spawning: list[bool] = []
    """Non-empty while an allowed `Popen` runs: it wraps its pipes' descriptors with `open`."""

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        pipe = isinstance(file, int) and bool(spawning)
        if any(flag in mode for flag in "wax+") and not pipe and not allowed(file):
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

    def guarded_popen(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if allow_process is None or not allow_process(args):
            forbid(f"subprocess.Popen({args!r})")
        spawning.append(True)
        try:
            return real_popen(args, *rest, **kwargs)
        finally:
            spawning.pop()

    real_fork_exec = _posixsubprocess.fork_exec

    def guarded_fork_exec(*args: Any, **kwargs: Any) -> Any:
        """Only through an allowed `Popen` (which checked its argv)."""
        if not spawning:
            forbid(f"_posixsubprocess.fork_exec({args[0] if args else ()!r})")
        return real_fork_exec(*args, **kwargs)

    def guarded_rmtree(path: Any, ignore_errors: bool = False, **kwargs: Any) -> None:
        """`shutil.rmtree` inside an allowed path, by path (the real one works relative to
        directory descriptors, which the guard never allows)."""
        if not allowed(path):
            forbid(f"shutil.rmtree({path!r})")
        try:
            for top, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    real_unlink(os.path.join(top, name))
                for name in dirs:
                    child = os.path.join(top, name)
                    (real_unlink if os.path.islink(child) else real_rmdir)(child)
            real_rmdir(path)
        except OSError:
            if not ignore_errors:
                raise

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

    # A module first imported while the guard is armed must not write its bytecode cache
    # (`__pycache__` in site-packages: the interpreter's, not the dry run's).
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
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
    monkeypatch.setattr(shutil, "rmtree", guarded_rmtree)
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
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(_posixsubprocess, "fork_exec", guarded_fork_exec)
    if hasattr(subprocess, "_fork_exec"):
        monkeypatch.setattr(subprocess, "_fork_exec", guarded_fork_exec)
    return seen
