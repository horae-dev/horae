# SPDX-License-Identifier: Apache-2.0
"""The marketplace cache on disk (#153): private directories and one lock per clone.

- `make_private_dirs` creates a directory and the missing ancestors, each `0700` (the umask
  can only remove more). A directory that already existed is never touched: it may be a
  shared one (`/var/lib`) or one the user set up on purpose.
- `clone_lock` is an exclusive `flock` on a file of the cache directory, held for the fetch
  or the update of one clone, so two `horae plugin` runs never write the same repository. The
  wait is bounded; when it ends, `LockBusy` (exit code 5, kind `lock_busy`) says who to wait for.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from horae.marketplace.errors import LockBusy

LOCK_WAIT_S = 150.0
"""Longest wait for another run to finish its fetch (one clone plus one fetch of 120 s each
fits in a sibling's turn; past this, something is stuck)."""
LOCK_POLL_S = 0.1
_DIR_MODE = 0o700
_FILE_MODE = 0o600


def make_private_dirs(path: Path) -> None:
    """Create `path` and every missing ancestor with mode `0700`; existing ones are left as
    they are. `OSError` when a level cannot be created (and is not a directory)."""
    missing: list[Path] = []
    current = path
    while not current.is_dir():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for level in reversed(missing):
        try:
            os.mkdir(level, _DIR_MODE)
        except FileExistsError:
            if not level.is_dir():
                raise
            continue  # created by a concurrent run: theirs to set up
        os.chmod(level, _DIR_MODE)


@contextmanager
def clone_lock(
    cache_dir: Path, name: str, what: str, *, wait_s: float = LOCK_WAIT_S
) -> Iterator[None]:
    """Hold the exclusive lock of one clone (`<cache_dir>/<name>.lock`) for the `with` body.
    `what` names the clone in the error. `LockBusy` after `wait_s` seconds without the lock."""
    make_private_dirs(cache_dir)
    fd = os.open(
        cache_dir / f"{name}.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        _FILE_MODE,
    )
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(
                        f"another horae command has been fetching {what} for {wait_s:g}s; "
                        "try again when it has finished"
                    ) from None
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock
