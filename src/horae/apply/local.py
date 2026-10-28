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
`horae undo` and the walking skeleton: `apply` and `restore` (#22). It resolves the note the
same way, holding the parent directory's descriptor: missing parent directories are created
(`mkdir` relative to that descriptor, never following a link; undo leaves them in place), the
current bytes are read and compared with the expected SHA-256 (`target_changed`), the new
bytes go to a temporary file created next to the note (`O_CREAT | O_EXCL | O_NOFOLLOW`, the
note's permission bits, or 0644 for a new note), which is `fsync`ed and renamed over the note
(`os.replace` relative to the directory descriptor), then the directory is `fsync`ed. A
removal (`restore` with `None`) unlinks the note relative to its directory. The check and the
rename are not one atomic step: an editor writing the note in between can lose that write
(there is no portable file lock that editors honour).

POSIX only (Linux, macOS): Windows is unsupported (no `O_NOFOLLOW`, no `dir_fd`).
"""

import contextlib
import errno
import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from horae.apply.notes import NoteIntent, note_after
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
    fd = _open(name, _FILE_FLAGS, dir_fd, path)
    if fd is None:
        return None
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
        return b"".join(chunks)
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


_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_MODE = 0o644


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _write_error(path: str, error: OSError) -> SinkError:
    return SinkError("unwritable", f"{path} cannot be written ({error.strerror})")


def _parent(parts: list[str], path: str, fds: list[int], *, create: bool = True) -> int | None:
    """The descriptor of the note's directory, creating missing directories (no links) when
    `create` (else `None` for a missing one)."""
    for part in parts[:-1]:
        directory = _open(part, _DIR_FLAGS, fds[-1], path)
        if directory is None:
            if not create:
                return None
            try:
                os.mkdir(part, 0o755, dir_fd=fds[-1])
            except FileExistsError:
                pass  # created meanwhile: opened (and checked) below
            except OSError as error:
                raise _write_error(path, error) from None
            directory = _open(part, _DIR_FLAGS, fds[-1], path)
            if directory is None:
                raise SinkError("unwritable", f"{path}: a parent directory vanished")
        fds.append(directory)
    return fds[-1]


def _mode(dir_fd: int, name: str) -> int:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _NEW_MODE
    return stat.S_IMODE(info.st_mode)


def _replace(dir_fd: int, name: str, path: str, data: bytes) -> None:
    """Write `data` to a temporary file next to `name`, then rename it over `name`."""
    mode = _mode(dir_fd, name)
    tmp = f".{name[:200]}.horae-{secrets.token_hex(6)}.tmp"
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
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as error:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise _write_error(path, error) from None
    _sync(dir_fd)


def _sync(dir_fd: int) -> None:
    with contextlib.suppress(OSError):  # some filesystems cannot fsync a directory
        os.fsync(dir_fd)


def _changed(path: str) -> SinkError:
    return SinkError("target_changed", f"{path} changed since it was planned")


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
        path = intent.path
        parts = _check_path(path)
        fds = [_open_vault(self._vault())]
        try:
            parent = _parent(parts, path, fds)
            assert parent is not None  # noqa: S101 - created
            before = _read_at(parent, parts[-1], path)
            if _sha(before) != expected_before_sha256:
                raise _changed(path)
            after = note_after(intent, before)
            if len(after) > MAX_NOTE_BYTES:
                raise _too_large(path)
            _replace(parent, parts[-1], path, after)
            return Applied(path, before, after)
        finally:
            for opened in reversed(fds):
                os.close(opened)

    async def read_target(self, target: str) -> bytes | None:
        return read_note(self._vault(), target)

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        parts = _check_path(target)
        fds = [_open_vault(self._vault())]
        try:
            parent = _parent(parts, target, fds, create=content is not None)
            current = None if parent is None else _read_at(parent, parts[-1], target)
            if _sha(current) != expected_sha256:
                raise _changed(target)
            if content is None:
                if parent is None or current is None:
                    return
                try:
                    os.unlink(parts[-1], dir_fd=parent)
                except OSError as error:
                    raise _write_error(target, error) from None
                _sync(parent)
                return
            assert parent is not None  # noqa: S101 - created
            _replace(parent, parts[-1], target, content)
        finally:
            for opened in reversed(fds):
                os.close(opened)
