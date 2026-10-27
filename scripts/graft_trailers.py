# SPDX-License-Identifier: Apache-2.0
"""commit-msg hook: add Graft provenance trailers to agent commits.

Reads ``GRAFT_AGENT``, ``GRAFT_MODEL`` and ``GRAFT_TASK``. When all three are set,
appends ``Graft-Agent``, ``Graft-Model`` and ``Graft-Task: #<n>`` through
``git interpret-trailers`` so they join the existing trailer block. Runs at the
``commit-msg`` stage, when the message is final. Does nothing when a variable is
missing, for merge/squash commits, when the message has no real content (an aborted
editor commit), or per key when a ``Graft-*`` trailer is already present.

Fails open: an unexpected error prints a warning to stderr and exits 0, so this hook
never blocks a commit.

Usage: ``graft_trailers.py <commit-msg-file>``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

GIT = shutil.which("git") or "git"


def normalise_task(raw: str) -> str | None:
    """Return ``#<n>`` for ``15``, ``#15`` or ``  #15 ``; None if not a number."""
    match = re.fullmatch(r"#?(\d+)", raw.strip())
    return f"#{match.group(1)}" if match else None


def build_trailers(env: Mapping[str, str]) -> list[str] | None:
    """Return ``Key: value`` trailers, or None when the hook should do nothing."""
    agent = env.get("GRAFT_AGENT", "").strip()
    model = env.get("GRAFT_MODEL", "").strip()
    task = normalise_task(env.get("GRAFT_TASK", ""))
    if not agent or not model or task is None:
        return None
    return [f"Graft-Agent: {agent}", f"Graft-Model: {model}", f"Graft-Task: {task}"]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [GIT, *args], capture_output=True, text=True, check=False
    )


def is_merge_or_squash() -> bool:
    """True while a merge (MERGE_HEAD) or squash (SQUASH_MSG) is being committed."""
    if _git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        return True
    git_dir = _git("rev-parse", "--git-dir").stdout.strip()
    return bool(git_dir) and (Path(git_dir) / "SQUASH_MSG").exists()


def has_content(text: str) -> bool:
    """True if the message has a non-blank, non-comment line."""
    return any(line.strip() and not line.startswith("#") for line in text.splitlines())


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    if not argv:
        return 0
    trailers = build_trailers(env)
    if trailers is None:
        return 0
    msg_file = Path(argv[0])
    if not msg_file.is_file() or not has_content(msg_file.read_text(encoding="utf-8")):
        return 0
    if is_merge_or_squash():
        return 0
    cmd = [
        GIT,
        "interpret-trailers",
        "--in-place",
        "--if-exists",
        "doNothing",
    ]
    for trailer in trailers:
        cmd += ["--trailer", trailer]
    cmd.append(str(msg_file))
    subprocess.run(cmd, check=True)  # noqa: S603
    return 0


def run() -> int:
    try:
        return main(sys.argv[1:], os.environ)
    except Exception as exc:  # fail open: never block a commit
        print(f"graft_trailers: skipped ({exc})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(run())
