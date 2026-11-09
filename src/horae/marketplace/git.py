# SPDX-License-Identifier: Apache-2.0
"""Hardened git for marketplace indexes (#43), after `horae-dev/marketplace`'s verifier.

An index is any git URL the user configures, and a clone is a network read of untrusted data:
only the named protocol is allowed (`https`; tests may name `file`), the user's and the
system's git configuration are ignored, nothing may prompt, nothing inherits the environment
(no `GIT_SSH_COMMAND`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`, ...), and every call has a timeout.
No hook ever runs (`core.hooksPath=/dev/null`) and Git LFS never smudges
(`GIT_LFS_SKIP_SMUDGE=1`): a plugin checkout (#44) runs nothing from the repository.
Clones are shallow (the tip of HEAD only) but not partial (`--filter` made a later `git show`
fetch lazily over a protocol the other calls block).
"""

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

GIT_TIMEOUT_S = 120
"""Seconds one git call may take."""
HTTPS = "https"
_ENV_KEPT = frozenset({"PATH", "HOME", "LANG", "TMPDIR"})
MAX_ERROR_CHARS = 300
WATCH_INTERVAL_S = 0.25
"""How often a watched call has the size of its directory measured."""


class GitError(Exception):
    """A git call failed, timed out, or git is missing."""


class GitTooLarge(GitError):
    """A watched git call wrote more than its byte budget; it was killed."""


def git_command(*args: str, allow: str = HTTPS, options: Sequence[str] = ()) -> list[str]:
    """The argv of one git call: every protocol is off except `allow`, no hook runs.
    `options` are global options placed before the subcommand (`-c key=value`, `--git-dir`)."""
    return [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{allow}.allow=always",
        "-c",
        "core.hooksPath=/dev/null",
        *options,
        *args,
    ]


def git_env() -> dict[str, str]:
    """The environment of a git call: a few variables of ours, no config, no prompt."""
    env = {key: value for key, value in os.environ.items() if key in _ENV_KEPT}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_LFS_SKIP_SMUDGE="1",
        LC_ALL="C",
    )
    return env


def _tree_bytes(root: Path) -> int:
    """Bytes in the files under `root` (links are not followed); what vanishes is skipped."""
    total, pending = 0, [str(root)]
    while pending:
        try:
            with os.scandir(pending.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    """Kill git and the helpers it started (`git-remote-https`, `index-pack`): one session."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    proc.communicate()


def run_git(
    cwd: Path,
    *args: str,
    allow: str = HTTPS,
    timeout_s: float = GIT_TIMEOUT_S,
    options: Sequence[str] = (),
    watch: Path | None = None,
    max_bytes: int | None = None,
) -> str:
    """Run git in `cwd` and return its stripped stdout; `GitError` on any failure.
    With `watch` and `max_bytes`, the call is killed (`GitTooLarge`) as soon as the files
    under `watch` exceed `max_bytes`: a download is bounded in size, not only in time."""
    try:
        proc = subprocess.Popen(  # noqa: S603
            git_command(*args, allow=allow, options=options),
            cwd=cwd,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise GitError(f"cannot run git: {exc.strerror or type(exc).__name__}") from exc
    deadline = time.monotonic() + timeout_s
    watching = watch is not None and max_bytes is not None
    while True:
        left = deadline - time.monotonic()
        try:
            stdout, stderr = proc.communicate(
                timeout=max(0.0, min(WATCH_INTERVAL_S, left)) if watching else max(0.0, left)
            )
            break
        except subprocess.TimeoutExpired:
            pass
        except BaseException:
            _kill(proc)
            raise
        if watch is not None and max_bytes is not None and _tree_bytes(watch) > max_bytes:
            _kill(proc)
            raise GitTooLarge(f"git {args[0]} wrote more than {max_bytes} bytes")
        if time.monotonic() >= deadline:
            _kill(proc)
            raise GitError(f"git {args[0]} timed out after {timeout_s:g}s")
    if proc.returncode != 0:
        reason = " ".join(stderr.decode("utf-8", errors="replace").split())[:MAX_ERROR_CHARS]
        raise GitError(f"git {args[0]} failed: {reason}")
    try:
        return stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        # A hostile index file that is not UTF-8 is a failed fetch (the cache stays), not a crash.
        raise GitError(f"git {args[0]} printed output that is not UTF-8") from None
