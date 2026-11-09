# SPDX-License-Identifier: Apache-2.0
"""Read and write `horae.lock` (#46, schemas decision 70).

`horae.lock` is the list of **approved** plugins (decision 62 F). `horae plugin add` adds or
replaces one entry; this module is how it does so:

- **canonical text** (`dump_lock`): the validated `Lockfile` dumped as JSON data (plugins
  sorted by name, every permission list sorted, empty `network_local` and `personal_fields`
  left out: the contract's serializers), then as block-style YAML with **sorted keys**, no
  line wrapping and a trailing newline. The same lock is always the same bytes, and adding a
  plugin changes only the lines of its own entry;
- **the header**: the comment lines at the top of the existing file are kept as they are
  (`horae init` writes one); a new file gets `DEFAULT_HEADER`;
- **atomic**: the new text is written to a temporary file in the same directory, flushed to
  disk, and renamed over `horae.lock`, so a reader sees the old file or the new one, never
  half of one;
- **one writer**: callers hold `guard()` (an exclusive `flock` on a file of the state
  directory, never on `horae.lock` itself, which the rename replaces) for the whole
  read-modify-write; a second writer is refused at once (`LockBusy`), never queued.

An existing `horae.lock` that does not validate is never overwritten (`LockInvalid`).
"""

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

from horae.config.documents import ProblemsError, read_yaml, validate
from horae.contracts.lockfile import Lockfile
from horae.marketplace.errors import LockBusy, LockInvalid

LOCK_FILE = "horae.lock"
GUARD_FILE = "horae.lock.guard"
"""In the state directory: the file `guard()` locks."""
DEFAULT_HEADER = (
    "# horae.lock: the plugins this instance runs, each pinned to a commit with the permissions\n"
    "# you approved. Written by `horae plugin add`; keep it under version control.\n"
)
_MODE = 0o644


def dump_lock(lock: Lockfile, header: str = "") -> str:
    """The canonical text of `lock`, after `header` (comment lines, each ending in `\\n`)."""
    body = yaml.safe_dump(
        lock.model_dump(mode="json"),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=1 << 16,
    )
    return header + body


def header_of(text: str) -> str:
    """The comment lines at the top of `text` (with their newlines), kept on rewrite."""
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        kept.append(line if line.endswith("\n") else line + "\n")
    return "".join(kept)


def read_lock(path: Path) -> tuple[Lockfile, str]:
    """`(lock, header)` of `path`; an absent file is an empty lock with `DEFAULT_HEADER`.
    `LockInvalid` when it exists but cannot be read or does not validate."""
    if not os.path.lexists(path):
        return Lockfile(schema_version=1), DEFAULT_HEADER
    if path.is_symlink() or not path.is_file():
        raise LockInvalid(f"{path} is not a regular file: refusing to rewrite it")
    try:
        lock = validate(read_yaml(path), Lockfile)
        text = path.read_text(encoding="utf-8")
    except ProblemsError as exc:
        problems = "; ".join(map(str, exc.problems[:3]))
        raise LockInvalid(f"{path} is invalid, fix it first: {problems}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise LockInvalid(f"cannot read {path}: {exc}") from None
    return lock, header_of(text)


def write_lock(path: Path, lock: Lockfile, header: str = DEFAULT_HEADER) -> None:
    """Replace `path` with the canonical text of `lock`, atomically (temporary file in the
    same directory, `fsync`, rename, `fsync` of the directory). The caller holds `guard()`."""
    text = dump_lock(lock, header)
    mode = _MODE
    if path.is_file() and not path.is_symlink():
        mode = path.stat().st_mode & 0o777
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def guard(state_dir: Path) -> Iterator[None]:
    """Hold the instance's lockfile writer lock (`<state_dir>/horae.lock.guard`), or raise
    `LockBusy` when another command holds it."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state_dir / GUARD_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(
                "another horae command is changing horae.lock of this instance; "
                "try again when it has finished"
            ) from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


__all__ = [
    "DEFAULT_HEADER",
    "GUARD_FILE",
    "LOCK_FILE",
    "dump_lock",
    "guard",
    "header_of",
    "read_lock",
    "write_lock",
]
