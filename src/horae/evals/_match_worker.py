# SPDX-License-Identifier: Apache-2.0
"""The worker of `horae.evals._match`: `re.search` of a pattern in a text, in its own process.

Run as `python -I -B _match_worker.py`; it imports nothing of Horae. It reads one JSON object
(`pattern`, `text`, `cpu_s`) from stdin, sets its own CPU and memory ceilings and writes `1`
(found) or `0` (not found). Anything else (a signal from the CPU ceiling, an error) is a
failure for the parent."""

import contextlib
import json
import re
import resource
import sys

MEMORY_BYTES = 512 * 1024 * 1024


def _limit(which: int, value: int) -> None:
    # A platform without the limit: the parent's wall-clock kill still applies.
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(which, (value, value))


def main() -> int:
    request = json.loads(sys.stdin.buffer.read(1024 * 1024))
    _limit(resource.RLIMIT_CPU, int(request["cpu_s"]))
    _limit(getattr(resource, "RLIMIT_AS", resource.RLIMIT_DATA), MEMORY_BYTES)
    found = re.search(request["pattern"], request["text"]) is not None
    sys.stdout.write("1" if found else "0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
