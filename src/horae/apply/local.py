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

POSIX only (Linux, macOS): Windows is unsupported (no `O_NOFOLLOW`, no `dir_fd`).
"""

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from horae.apply.notes import NoteIntent
from horae.apply.sink import MAX_NOTE_BYTES, Applied, SinkCall, SinkError

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


def read_note(vault: Path, path: str) -> bytes | None:
    """The bytes of note `path` in `vault` (`None` if it does not exist), read-only."""
    parts = path.split("/")
    if (
        ":" in path
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise SinkError("path_invalid", f"{path!r} is not a local note path")
    try:
        root = os.open(vault, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise SinkError("unreadable", f"the vault cannot be opened ({error.strerror})") from None
    fds = [root]
    try:
        for part in parts[:-1]:
            directory = _open(part, _DIR_FLAGS, fds[-1], path)
            if directory is None:
                return None
            fds.append(directory)
        fd = _open(parts[-1], _FILE_FLAGS, fds[-1], path)
        if fd is None:
            return None
        fds.append(fd)
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
        return b"".join(chunks)
    finally:
        for opened in reversed(fds):
            os.close(opened)


@dataclass(frozen=True)
class LocalSink:
    """Read-only access to the notes of a local vault directory (`None`: no vault)."""

    vault: Path | None = None

    async def read(self, call: SinkCall) -> bytes | None:
        intent = call.intent
        if not isinstance(intent, NoteIntent):
            raise SinkError("not_supported", f"the local sink reads no {intent.type} target")
        if self.vault is None:
            raise SinkError("vault_unset", "no vault directory to read notes from")
        return read_note(self.vault, intent.path)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        raise SinkError("not_supported", "the built-in local sink only reads")
