# SPDX-License-Identifier: Apache-2.0
"""Hardened git for marketplace indexes (#43), after `horae-dev/marketplace`'s verifier.

An index is any git URL the user configures, and a clone is a network read of untrusted data:
only the named protocol is allowed (`https`; tests may name `file`), the user's and the
system's git configuration are ignored, nothing may prompt, nothing inherits the environment
(no `GIT_SSH_COMMAND`, `GIT_PROXY_COMMAND`, `GIT_EXEC_PATH`, ...), and every call has a timeout.
Clones are shallow (the tip of HEAD only) but not partial (`--filter` made a later `git show`
fetch lazily over a protocol the other calls block).
"""

import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 120
"""Seconds one git call may take."""
HTTPS = "https"
_ENV_KEPT = frozenset({"PATH", "HOME", "LANG", "TMPDIR"})
MAX_ERROR_CHARS = 300


class GitError(Exception):
    """A git call failed, timed out, or git is missing."""


def git_command(*args: str, allow: str = HTTPS) -> list[str]:
    """The argv of one git call: every protocol is off except `allow`."""
    return ["git", "-c", "protocol.allow=never", "-c", f"protocol.{allow}.allow=always", *args]


def git_env() -> dict[str, str]:
    """The environment of a git call: a few variables of ours, no config, no prompt."""
    env = {key: value for key, value in os.environ.items() if key in _ENV_KEPT}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
    )
    return env


def run_git(cwd: Path, *args: str, allow: str = HTTPS, timeout_s: float = GIT_TIMEOUT_S) -> str:
    """Run git in `cwd` and return its stripped stdout; `GitError` on any failure."""
    try:
        done = subprocess.run(  # noqa: S603
            git_command(*args, allow=allow),
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            timeout=timeout_s,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise GitError(f"cannot run git: {exc.strerror or type(exc).__name__}") from exc
    if done.returncode != 0:
        stderr = done.stderr.decode("utf-8", errors="replace")
        reason = " ".join(stderr.split())[:MAX_ERROR_CHARS]
        raise GitError(f"git {args[0]} failed: {reason}")
    try:
        return done.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        # A hostile index file that is not UTF-8 is a failed fetch (the cache stays), not a crash.
        raise GitError(f"git {args[0]} printed output that is not UTF-8") from None
