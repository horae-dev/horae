# SPDX-License-Identifier: Apache-2.0
"""prepare-commit-msg hook: add Graft provenance trailers to agent commits.

Reads ``GRAFT_AGENT``, ``GRAFT_MODEL`` and ``GRAFT_TASK``. When all three are set,
appends ``Graft-Agent``, ``Graft-Model`` and ``Graft-Task: #<n>`` through
``git interpret-trailers`` so they join the existing trailer block. Does nothing
when a variable is missing, for merge/squash commits, or when already present.

Usage: ``graft_trailers.py <commit-msg-file> [<source> [<sha>]]``. Under pre-commit
the source comes from ``PRE_COMMIT_COMMIT_MSG_SOURCE``.
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

SKIP_SOURCES = frozenset({"merge", "squash"})


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


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    if not argv:
        return 0
    source = argv[1] if len(argv) > 1 else env.get("PRE_COMMIT_COMMIT_MSG_SOURCE", "")
    trailers = build_trailers(env)
    if trailers is None or source in SKIP_SOURCES:
        return 0
    msg_file = Path(argv[0])
    if not msg_file.is_file():
        return 0
    cmd = [
        shutil.which("git") or "git",
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
