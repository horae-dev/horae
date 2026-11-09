# SPDX-License-Identifier: Apache-2.0
"""The `matches` assertion's regular expression, kept safe against ReDoS (#134, schemas
decision 64).

Python's `re` backtracks and cannot be interrupted from inside the process, so a case file
(or a pack's pull request) could hang an eval with a pattern like `(a+)+$`. The core's answer
to the same problem for the output validator (`horae.llm.validate`) is a **killable worker**,
and this module does the same:

- the pattern is at most `MAX_PATTERN_CHARS` characters and must compile (checked when the
  case is loaded, `check_pattern`);
- the text it is searched in is at most `MAX_SUBJECT_CHARS` characters (a longer value fails
  the assertion, it is never searched);
- the search runs in a short-lived child (`python -I -B horae/evals/_match_worker.py`, an
  empty environment, `cwd` `/`, a new session, the request on stdin) with its own CPU and
  memory ceilings, and is killed after `MATCH_TIMEOUT_S` seconds of wall time. A search that
  does not finish is a **failed assertion** (`MatchTimeout`), never a pass.

The semantics is `re.search` (unanchored; use `^` and `$` to anchor), with the flags written
inline in the pattern (`(?i)`, `(?s)`, `(?m)`).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

MAX_PATTERN_CHARS = 256
"""Longest pattern a `matches` assertion may hold."""
MAX_SUBJECT_CHARS = 64 * 1024
"""Longest string a pattern is searched in."""
MATCH_TIMEOUT_S = 2.0
"""Wall time a search may take (the child's start included) before it is killed."""

WORKER = Path(__file__).with_name("_match_worker.py")
WORKER_FLAGS = ("-I", "-B")
WORKER_CWD = "/"


class MatchTimeout(Exception):
    """The search did not finish in time (or the worker failed): the assertion fails."""


def check_pattern(pattern: str) -> str | None:
    """Why `pattern` cannot be used (`None`: it can): too long, or not a valid regex."""
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"the pattern is longer than {MAX_PATTERN_CHARS} characters"
    try:
        re.compile(pattern)
    except (re.error, RecursionError, OverflowError) as error:
        return f"not a valid regular expression ({str(error)[:100]})"
    return None


def search(pattern: str, text: str, *, timeout_s: float = MATCH_TIMEOUT_S) -> bool:
    """Whether `pattern` is found in `text` (`re.search`), in a killable worker. Raises
    `ValueError` for a pattern or text over the bounds (or a pattern that does not compile)
    and `MatchTimeout` when the search is killed or the worker fails."""
    problem = check_pattern(pattern)
    if problem is not None:
        raise ValueError(problem)
    if len(text) > MAX_SUBJECT_CHARS:
        raise ValueError(f"the text is longer than {MAX_SUBJECT_CHARS} characters")
    request = json.dumps({"pattern": pattern, "text": text, "cpu_s": int(timeout_s) + 1})
    try:
        done = subprocess.run(  # noqa: S603 (fixed argument vector, no shell)
            [sys.executable, *WORKER_FLAGS, str(WORKER)],
            input=request.encode("ascii"),
            capture_output=True,
            timeout=timeout_s,
            cwd=WORKER_CWD,
            env={},
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise MatchTimeout(f"the search took more than {timeout_s:g} s") from None
    except OSError:
        raise MatchTimeout("the search worker could not be started") from None
    answer = done.stdout.strip()
    if done.returncode != 0 or answer not in (b"1", b"0"):
        raise MatchTimeout("the search was stopped (CPU or memory limit)")
    return answer == b"1"
