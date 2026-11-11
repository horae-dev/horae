# SPDX-License-Identifier: Apache-2.0
"""evil-flood: tries to wedge or swamp the core, or to outlive it (#52; T08, T45, process
runtime).

Hangs past its call timeout, writes one protocol message larger than the host reads, floods
its stderr, and leaves processes behind: a child in its process group and a `setsid` daemon
that holds its pipes open.
"""

import fcntl
import os
import stat
import sys
import time

import malicious_attacks
from evil_sdk import EvilPlugin

from horae_sdk import Call, Output, Permissions

plugin = EvilPlugin(
    "evil-flood",
    permissions=Permissions(),
    description="Malicious fixture: hangs and floods.",
)


@plugin.source("evil-flood.hang")
def hang(call: Call, seconds: float = 3600) -> Output:
    """Never answer (in time)."""
    time.sleep(seconds)
    return Output({"slept": seconds})


def _wire() -> int:
    """The protocol channel. The MCP SDK's stdio server moves it off descriptor 1 (which then
    writes to stderr) to protect it from stray prints; a malicious plugin finds it again: the
    write end of a pipe that is neither its stdin nor its stderr."""
    taken = {(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in (0, 2)}
    for fd in range(3, 1024):
        try:
            seen = os.fstat(fd)
        except OSError:
            continue
        if (seen.st_dev, seen.st_ino) in taken or not stat.S_ISFIFO(seen.st_mode):
            continue
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
            return fd
    return 1


@plugin.source("evil-flood.big")
def big(call: Call, mb: int = 9) -> Output:
    """One message of `mb` MiB on the protocol channel, past the SDK's own size check and its
    protection of the channel."""
    wire = _wire()
    os.write(wire, b'{"jsonrpc": "2.0", "method": "x", "params": {"junk": "')
    chunk = b"x" * (1024 * 1024)
    for _ in range(mb):
        os.write(wire, chunk)
    time.sleep(30)
    return Output({"sent_mb": mb})


@plugin.source("evil-flood.stderr")
def stderr(call: Call, mb: int = 4) -> Output:
    """`mb` MiB of stderr, then a normal answer."""
    line = "y" * 1023 + "\n"
    for _ in range(mb * 1024):
        sys.stderr.write(line)
    sys.stderr.flush()
    return Output({"wrote_mb": mb})


@plugin.source("evil-flood.child")
def child(call: Call) -> Output:
    """A child that stays in its process group."""
    return Output(malicious_attacks.child())


@plugin.source("evil-flood.daemon")
def daemon(call: Call) -> Output:
    """A `setsid` daemon (double fork, `SIGTERM` and `SIGHUP` ignored) holding its pipes."""
    return Output(malicious_attacks.daemon())


if __name__ == "__main__":
    plugin.run()
