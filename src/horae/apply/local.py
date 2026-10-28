# SPDX-License-Identifier: Apache-2.0
"""A built-in, **read-only** sink for notes in a local vault, until the plugin host (#24)
provides real ones.

`LocalSink.read` returns the current bytes of a `note.*` intent's target in a local vault
directory, and nothing else: it never writes, and `apply` raises `SinkError("not_supported")`.

**How a note is read.** The vault directory is opened, then the note path is walked one
component at a time relative to it (`dir_fd`), every component opened with `O_NOFOLLOW` (so a
symlink anywhere in the path is refused, `symlink`: a link can neither lead out of the vault
nor make a user note look owned), the last one with
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` (a FIFO cannot block the read). The open file
is checked with `fstat`: a regular file (`not_a_file`), with a single hard link (`hard_link`),
of at most `MAX_NOTE_BYTES` (`note_too_large`, also checked while reading, for a file that
grows). A missing note is `None`; a component that is not a directory or a name that is too
long is `not_a_file`, a permission or other OS error `unreadable`. A path holding `:` is
refused (`path_invalid`). Without a vault, `vault_unset`.

`LocalFileSink` (**provisional**, until the plugin host's sinks, #24) adds writes for
`horae undo` and the walking skeleton: `apply`, `restore` and `parent_exists` (#22). It
resolves the note the same way, without creating anything first: the existing directories are
walked (a missing one means the note does not exist), the current bytes are read and compared
with the expected SHA-256 (`target_changed`, and nothing at all is created). Only then are the
missing parent directories created (`mkdir` relative to the parent descriptor, never following
a link, the directory holding each new entry `fsync`ed; undo leaves them in place). The new
bytes go to a temporary file `.horae-<12 hex>.tmp` next to the note
(`O_CREAT | O_EXCL | O_NOFOLLOW`, the note's permission bits, or 0644 for a new note), which
is `fsync`ed; just before it is renamed over the note (`os.replace` relative to the directory
descriptor) the note's device, inode, size and modification time are checked against what was
read (`target_changed` otherwise), then the directory is `fsync`ed. A removal (`restore` with
`None`) first renames the note to a private name (`.horae-<12 hex>.old`) in the same
directory, checks and hashes what it renamed, then unlinks it, or puts it back (`link` then
`unlink`, never over a note created meanwhile) if it is not what was expected. Temporary files
older than `STALE_TMP_S` (left by a crash) are removed from every directory a write touches.
These checks narrow, but do not close, the window in which an editor writing the note at that
very moment can lose its write (there is no portable file lock that editors honour).

`LocalSink.identity` (`local:<st_dev>:<st_ino>:<real path>` of the vault) lets `horae undo`
tell one vault from another. All file system work runs in a worker thread
(`asyncio.to_thread`), so a slow disk never blocks the event loop.

POSIX only (Linux, macOS): Windows is unsupported (no `O_NOFOLLOW`, no `dir_fd`).
"""

import asyncio
import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from horae.apply.notes import NoteIntent, note_after
from horae.apply.sink import MAX_IDENTITY, MAX_NOTE_BYTES, Applied, SinkCall, SinkError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CHUNK = 1024 * 1024


def _is_symlink(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open(name: str, flags: int, dir_fd: int, path: str) -> int | None:
    """Open `name` relative to `dir_fd`; `None` if it does not exist."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except NotADirectoryError:
        if _is_symlink(name, dir_fd):  # `O_DIRECTORY | O_NOFOLLOW` on a link (macOS)
            raise SinkError("symlink", f"{path} goes through a symbolic link") from None
        raise SinkError("not_a_file", f"{path}: a parent is not a directory") from None
    except PermissionError:
        raise SinkError("unreadable", f"{path} cannot be read (permission denied)") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SinkError("symlink", f"{path} goes through a symbolic link") from None
        if error.errno == errno.ENAMETOOLONG:
            raise SinkError("not_a_file", f"{path}: a name is too long") from None
        raise SinkError("unreadable", f"{path} cannot be read ({error.strerror})") from None


def _too_large(path: str) -> SinkError:
    return SinkError("note_too_large", f"{path} is larger than {MAX_NOTE_BYTES} bytes")


def _check_path(path: str) -> list[str]:
    parts = path.split("/")
    if (
        ":" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SinkError("path_invalid", f"{path!r} is not a local note path")
    return parts


def _open_vault(vault: Path) -> int:
    try:
        return os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise SinkError("unreadable", f"the vault cannot be opened ({error.strerror})") from None


def _read_at(dir_fd: int, name: str, path: str) -> bytes | None:
    """The bytes of regular file `name` in `dir_fd` (`None`: it does not exist)."""
    return _read_stat_at(dir_fd, name, path)[0]


def _read_stat_at(dir_fd: int, name: str, path: str) -> tuple[bytes | None, os.stat_result | None]:
    """The bytes and `fstat` of regular file `name` in `dir_fd` (`None`s: it does not exist)."""
    fd = _open(name, _FILE_FLAGS, dir_fd, path)
    if fd is None:
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SinkError("not_a_file", f"{path} is not a regular file")
        if info.st_nlink > 1:
            raise SinkError("hard_link", f"{path} has more than one hard link")
        if info.st_size > MAX_NOTE_BYTES:
            raise _too_large(path)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, _CHUNK):
            size += len(chunk)
            if size > MAX_NOTE_BYTES:
                raise _too_large(path)
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def read_note(vault: Path, path: str) -> bytes | None:
    """The bytes of note `path` in `vault` (`None` if it does not exist), read-only."""
    parts = _check_path(path)
    fds = [_open_vault(vault)]
    try:
        for part in parts[:-1]:
            directory = _open(part, _DIR_FLAGS, fds[-1], path)
            if directory is None:
                return None
            fds.append(directory)
        return _read_at(fds[-1], parts[-1], path)
    finally:
        _close(fds)


def _close(fds: list[int]) -> None:
    for opened in reversed(fds):
        os.close(opened)


@dataclass(frozen=True)
class LocalSink:
    """Read-only access to the notes of a local vault directory (`None`: no vault)."""

    vault: Path | None = None

    @property
    def identity(self) -> str:
        """Where the notes are: `local:<st_dev>:<st_ino>:<real path>` of the vault (the path
        replaced by its SHA-256 when too long). Raises `SinkError` without a vault."""
        if self.vault is None:
            raise SinkError("vault_unset", "no vault directory")
        try:
            real = os.path.realpath(self.vault)
            info = os.stat(real)
        except OSError as error:
            message = f"the vault cannot be opened ({error.strerror})"
            raise SinkError("unreadable", message) from None
        identity = f"local:{info.st_dev}:{info.st_ino}:{real}"
        if len(identity) > MAX_IDENTITY:
            digest = hashlib.sha256(os.fsencode(real)).hexdigest()
            identity = f"local:{info.st_dev}:{info.st_ino}:sha256:{digest}"
        return identity

    async def read(self, call: SinkCall) -> bytes | None:
        intent = call.intent
        if not isinstance(intent, NoteIntent):
            raise SinkError("not_supported", f"the local sink reads no {intent.type} target")
        if self.vault is None:
            raise SinkError("vault_unset", "no vault directory to read notes from")
        return await asyncio.to_thread(read_note, self.vault, intent.path)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        raise SinkError("not_supported", "the built-in local sink only reads")


_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_MODE = 0o644
_TMP = re.compile(r"^\.horae-[0-9a-f]{12}\.tmp$")
STALE_TMP_S = 3600.0
"""Temporary files (`.horae-<hex>.tmp`) older than this were left by a crash: removed."""


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _hidden(suffix: str) -> str:
    return f".horae-{secrets.token_hex(6)}{suffix}"


def _write_error(path: str, error: OSError) -> SinkError:
    return SinkError("unwritable", f"{path} cannot be written ({error.strerror})")


def _changed(path: str) -> SinkError:
    return SinkError("target_changed", f"{path} changed since it was planned")


def _walk(parts: list[str], path: str, fds: list[int]) -> list[str]:
    """Open the note's existing parent directories (appended to `fds`, no link followed,
    nothing created); the names of the missing ones (empty: the note's directory exists)."""
    for position, part in enumerate(parts[:-1]):
        directory = _open(part, _DIR_FLAGS, fds[-1], path)
        if directory is None:
            return parts[position:-1]
        fds.append(directory)
    return []


def _make_dirs(missing: list[str], path: str, fds: list[int]) -> None:
    """Create the `missing` directories below `fds[-1]` (appended to `fds`); the directory
    holding each new entry is `fsync`ed."""
    for part in missing:
        created = True
        try:
            os.mkdir(part, 0o755, dir_fd=fds[-1])
        except FileExistsError:
            created = False  # created meanwhile: opened (and checked) below
        except OSError as error:
            raise _write_error(path, error) from None
        directory = _open(part, _DIR_FLAGS, fds[-1], path)
        if directory is None:
            raise SinkError("unwritable", f"{path}: a parent directory vanished")
        if created:
            _sync(fds[-1])
        fds.append(directory)


def _sync(dir_fd: int) -> None:
    with contextlib.suppress(OSError):  # some filesystems cannot fsync a directory
        os.fsync(dir_fd)


def _sweep(dir_fd: int) -> None:
    """Remove the temporary files a crash left in `dir_fd` (older than `STALE_TMP_S`)."""
    names: list[str] = []
    with contextlib.suppress(OSError), os.scandir(dir_fd) as entries:
        names = [entry.name for entry in entries if _TMP.fullmatch(entry.name)]
    cutoff = time.time() - STALE_TMP_S
    for name in names:
        with contextlib.suppress(OSError):
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                os.unlink(name, dir_fd=dir_fd)


_SAME = ("st_dev", "st_ino", "st_size", "st_mtime_ns")


def _unchanged(dir_fd: int, name: str, seen: os.stat_result | None) -> bool:
    """Whether `name` is still the file `seen` (`None`: still missing)."""
    try:
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return seen is None
    if seen is None:
        return False
    return all(getattr(now, field) == getattr(seen, field) for field in _SAME)


def _replace(dir_fd: int, name: str, path: str, data: bytes, seen: os.stat_result | None) -> None:
    """Write `data` to a temporary file next to `name`, then rename it over `name` if `name`
    is still the file `seen` (`None`: still missing)."""
    mode = _NEW_MODE if seen is None else stat.S_IMODE(seen.st_mode)
    tmp = _hidden(".tmp")
    try:
        fd = os.open(tmp, _TMP_FLAGS, 0o600, dir_fd=dir_fd)
    except OSError as error:
        raise _write_error(path, error) from None
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        if not _unchanged(dir_fd, name, seen):
            raise _changed(path)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except (OSError, SinkError) as error:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        if isinstance(error, SinkError):
            raise
        raise _write_error(path, error) from None
    _sync(dir_fd)


def _put_back(dir_fd: int, hidden: str, name: str, path: str) -> None:
    """Undo the rename of `name` to `hidden`, never over a file created at `name` meanwhile."""
    try:
        os.link(hidden, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        message = f"{path} changed during the undo; what was there is kept as {hidden}"
        raise SinkError("target_changed", message) from None
    with contextlib.suppress(OSError):
        os.unlink(hidden, dir_fd=dir_fd)


def _remove(dir_fd: int, name: str, path: str, expected: str) -> None:
    """Remove `name` if it holds bytes of SHA-256 `expected`: rename it to a private name,
    check what was renamed, then unlink it (or put it back)."""
    hidden = _hidden(".old")
    try:
        os.rename(name, hidden, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except FileNotFoundError:
        raise _changed(path) from None
    except OSError as error:
        raise _write_error(path, error) from None
    try:
        if _sha(_read_at(dir_fd, hidden, path)) != expected:
            raise _changed(path)
        os.unlink(hidden, dir_fd=dir_fd)
    except (OSError, SinkError) as error:
        _put_back(dir_fd, hidden, name, path)
        if isinstance(error, SinkError):
            raise
        raise _write_error(path, error) from None
    _sync(dir_fd)


def _put(
    vault: Path, path: str, expected: str | None, make: Callable[[bytes | None], bytes]
) -> tuple[bytes | None, bytes]:
    """Compare-and-swap note `path`: if its bytes have SHA-256 `expected` (`None`: missing),
    write `make(before)`. Returns the bytes before and after."""
    parts = _check_path(path)
    fds = [_open_vault(vault)]
    try:
        missing = _walk(parts, path, fds)
        before, seen = (None, None) if missing else _read_stat_at(fds[-1], parts[-1], path)
        if _sha(before) != expected:
            raise _changed(path)
        after = make(before)
        if len(after) > MAX_NOTE_BYTES:
            raise _too_large(path)
        _make_dirs(missing, path, fds)
        _sweep(fds[-1])
        _replace(fds[-1], parts[-1], path, after, seen)
        return before, after
    finally:
        _close(fds)


def _delete(vault: Path, path: str, expected: str | None) -> None:
    """Remove note `path` if its bytes have SHA-256 `expected` (`None`: it must be missing)."""
    parts = _check_path(path)
    fds = [_open_vault(vault)]
    try:
        missing = _walk(parts, path, fds)
        current = None if missing else _read_at(fds[-1], parts[-1], path)
        if _sha(current) != expected:
            raise _changed(path)
        if expected is None:
            return
        _sweep(fds[-1])
        _remove(fds[-1], parts[-1], path, expected)
    finally:
        _close(fds)


def _parent_exists(vault: Path, path: str) -> bool:
    parts = _check_path(path)
    fds = [_open_vault(vault)]
    try:
        return not _walk(parts, path, fds)
    finally:
        _close(fds)


@dataclass(frozen=True)
class LocalFileSink(LocalSink):
    """**Provisional** (until #24): a local vault sink that also writes notes (see the module
    docstring). Only for `note.*` intents; anything else is `not_supported`."""

    def _vault(self) -> Path:
        if self.vault is None:
            raise SinkError("vault_unset", "no vault directory to write notes to")
        return self.vault

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        intent = call.intent
        if not isinstance(intent, NoteIntent):
            raise SinkError("not_supported", f"the local sink applies no {intent.type} intent")

        def make(before: bytes | None) -> bytes:
            return note_after(intent, before)

        vault, path = self._vault(), intent.path
        before, after = await asyncio.to_thread(_put, vault, path, expected_before_sha256, make)
        return Applied(path, before, after)

    async def read_target(self, target: str) -> bytes | None:
        return await asyncio.to_thread(read_note, self._vault(), target)

    async def parent_exists(self, target: str) -> bool:
        return await asyncio.to_thread(_parent_exists, self._vault(), target)

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        vault = self._vault()
        if content is None:
            await asyncio.to_thread(_delete, vault, target, expected_sha256)
            return

        def make(before: bytes | None) -> bytes:
            return content

        await asyncio.to_thread(_put, vault, target, expected_sha256, make)
