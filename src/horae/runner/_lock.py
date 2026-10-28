# SPDX-License-Identifier: Apache-2.0
"""Per-pipeline OS advisory locks: `<state_dir>/locks/<pipeline>.lock` (#19, decision 31).

A run holds `flock(LOCK_EX | LOCK_NB)` on its pipeline's lock file for its whole life. The
kernel releases the lock when the process exits, however it exits (crash, `kill -9`, OOM), so
"the lock is free" means "no process is running this pipeline": liveness does not depend on
pids, host names, heartbeats or clocks.

- The lock directory is created 0700 and the lock files 0600, opened `O_CLOEXEC` (a plugin
  child process never inherits the lock) and `O_NOFOLLOW`.
- `flock` locks are per open file description: two `PipelineLock`s in one process exclude each
  other like two processes do.
- Local filesystems only: `flock` is not reliable on network filesystems (NFS, SMB), which ADR
  0006 already rules out for the state directory.
- POSIX only (Linux, which Horae targets in containers, and macOS for development). On a
  platform without `fcntl` (Windows), taking a lock raises `LocksUnsupported`.
"""

import hashlib
import os
from pathlib import Path
from types import TracebackType
from typing import Self

try:
    import fcntl

    _FLOCK = True
except ImportError:  # pragma: no cover - Windows
    _FLOCK = False

LOCKS_DIR = "locks"
"""The lock directory, inside the state directory."""
APPLY_LOCK = "_apply"
"""The instance-wide apply lock, `<state_dir>/locks/_apply.lock` (#22, decision 34): held by a
run's apply phase and by `horae undo`, in addition to the pipeline lock, so that no two
processes change notes at the same time. A pipeline name never starts with `_`."""


class LocksUnsupported(OSError):
    """This platform has no `fcntl.flock` (Windows): Horae runs on POSIX systems only."""


EVAL_LOCK_PREFIX = "_eval-"
"""The lock of a live skill eval's run row (`eval:<skill>`, schemas decision 48 J) is
`_eval-<sha256 of the row's pipeline, 16 hex>.lock`: a skill name holds `/`, and a pipeline
name never starts with `_`."""


def lock_path(state_dir: Path, pipeline: str) -> Path:
    """The lock file of `pipeline` (a pipeline name is `a-z 0-9 -`, so it is a safe name; an
    eval row's `eval:<skill>` is hashed, `EVAL_LOCK_PREFIX`)."""
    if pipeline.startswith("eval:"):
        digest = hashlib.sha256(pipeline.encode("utf-8")).hexdigest()[:16]
        return state_dir / LOCKS_DIR / f"{EVAL_LOCK_PREFIX}{digest}.lock"
    return state_dir / LOCKS_DIR / f"{pipeline}.lock"


def _require_flock() -> None:
    if not _FLOCK:  # pragma: no cover - Windows
        raise LocksUnsupported("pipeline locks need fcntl.flock: Horae runs on POSIX systems")


def try_acquire_apply_lock(state_dir: Path) -> "PipelineLock | None":
    """Take the instance's apply lock (`APPLY_LOCK`) without waiting; `None` when it is held.
    Raises like `PipelineLock.try_acquire`."""
    return PipelineLock.try_acquire(state_dir, APPLY_LOCK)


class PipelineLock:
    """The exclusive lock of one pipeline, held until `release` (or the process exits)."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd: int | None = fd

    @classmethod
    def try_acquire(cls, state_dir: Path, pipeline: str) -> Self | None:
        """Take the lock without waiting; `None` when another holder has it.

        Creates the lock directory (0700) and file (0600) when missing. Raises `OSError` when
        they cannot be created or opened, and `LocksUnsupported` without `fcntl`."""
        _require_flock()
        path = lock_path(state_dir, pipeline)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return cls(path, fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Release the lock (idempotent). Closing the descriptor releases the `flock`."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def lock_is_free(state_dir: Path, pipeline: str) -> bool | None:
    """Whether no process holds `pipeline`'s lock, checked without waiting and read-only.

    Creates nothing: a missing lock file is a free lock. `None` when it cannot be told (the
    file cannot be opened, or no `fcntl`). The check takes a shared lock for an instant, so a
    run starting at that very moment may see its lock busy: the runner retries briefly."""
    if not _FLOCK:  # pragma: no cover - Windows
        return None
    path = lock_path(state_dir, pipeline)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return True
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        return None
    finally:
        os.close(fd)  # also releases the shared lock
    return True


__all__ = [
    "APPLY_LOCK",
    "LOCKS_DIR",
    "LocksUnsupported",
    "PipelineLock",
    "lock_is_free",
    "lock_path",
    "try_acquire_apply_lock",
]
