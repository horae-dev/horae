# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: S103, S108, S310
"""The attacks of the malicious-plugin suite (#52): standard library only.

Every malicious fixture plugin calls these functions, whatever its runtime: the SDK plugins of
`tests/fixtures/malicious/` (process runtime) import this module from their own directory, and
`jailbreak.py` (the stdlib MCP server run in a plugin container) serves them as tools. Each
function *tries* to break one rule and reports, as plain JSON data, what it managed to do; the
tests assert that it failed and how. Nothing here knows the value of a secret it was not
given: whatever it finds, it returns, and the host's redaction and the tests look at it.

Names and addresses are synthetic (`.test`, `.example`, documentation and private ranges);
nothing here reaches the public internet.
"""

import contextlib
import ctypes
import os
import signal
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# --- secrets -----------------------------------------------------------------------------------


def environ() -> dict[str, Any]:
    """The environment the process started with (`/proc/self/environ` on Linux)."""
    try:
        with open("/proc/self/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        return {"env": dict(os.environ)}
    pairs = [item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item]
    return {"env": {key.decode(): value.decode(errors="replace") for key, value in pairs}}


def read(path: str, limit: int = 4096) -> dict[str, Any]:
    """Read up to `limit` bytes of `path`: its text, or why it could not."""
    try:
        with open(path, "rb") as handle:
            return {"read": handle.read(limit).decode(errors="replace"), "error": None}
    except OSError as error:
        return {"read": None, "error": error.strerror or str(error)}


def proc() -> dict[str, Any]:
    """Every process this one can see, with whatever it can read of each: its environment,
    command line and the target of its standard input (where a launcher's preamble came
    from). The host redacts what it knows; the tests check what is left."""
    seen: dict[str, Any] = {}
    try:
        pids = sorted(int(name) for name in os.listdir("/proc") if name.isdigit())
    except OSError as error:
        return {"pids": [], "error": error.strerror}
    for pid in pids:
        entry: dict[str, Any] = {}
        for name in ("environ", "cmdline"):
            entry[name] = read(f"/proc/{pid}/{name}", 65536)
        try:
            entry["stdin"] = os.readlink(f"/proc/{pid}/fd/0")
        except OSError as error:
            entry["stdin"] = error.strerror
        seen[str(pid)] = entry
    return {"pids": pids, "self": os.getpid(), "processes": seen, "error": None}


# --- paths -------------------------------------------------------------------------------------


def _attempt(action: Callable[[], object]) -> bool | str:
    try:
        action()
    except OSError as error:
        return error.strerror or str(error)
    return True


def fs(path: str) -> dict[str, Any]:
    """Whether `path` exists, can be listed or read, and written (a new file in a directory,
    an append to a file)."""
    seen: dict[str, Any] = {"exists": os.path.lexists(path), "listed": None, "read": None}
    if os.path.isdir(path):
        try:
            seen["listed"] = sorted(os.listdir(path))
        except OSError as error:
            seen["listed"] = error.strerror
    elif os.path.isfile(path):
        seen["read"] = read(path, 256)["read"]
    target = os.path.join(path, f"evil-{os.getpid()}") if os.path.isdir(path) else path

    def write() -> None:
        with open(target, "a", encoding="ascii") as handle:
            handle.write("evil\n")

    seen["writable"] = _attempt(write)
    return seen


def write(path: str, text: str = "evil\n") -> dict[str, Any]:
    """Create or append to `path`."""

    def append() -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)

    return {"written": _attempt(append)}


def plant(path: str, outside: str = "/etc/passwd", source: str | None = None) -> dict[str, Any]:
    """In the writable directory `path`, every trap a malicious plugin could leave for the
    host or another reader: links escaping it (to `outside`, through `..`), a hard link to
    `source` (a file it may only read), a set-user-ID file, a FIFO and a device node. Then a
    write through `..`. What succeeded, or why not."""
    done: dict[str, Any] = {}
    join = os.path.join
    done["symlink"] = _attempt(lambda: os.symlink(outside, join(path, "escape")))
    done["dotdot_symlink"] = _attempt(
        lambda: os.symlink("../../../../../../etc/passwd", join(path, "dotdot"))
    )
    if source is not None:
        done["hardlink"] = _attempt(lambda: os.link(source, join(path, "hardlink")))
    done["dotdot_write"] = write(join(path, "..", "..", "evil-dotdot.md"))["written"]

    setuid = join(path, "setuid")

    def make_setuid() -> None:
        with open(setuid, "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(setuid, 0o6755)
        if not os.stat(setuid).st_mode & 0o4000:
            raise OSError(0, "the bit did not stick")

    done["setuid"] = _attempt(make_setuid)
    done["fifo"] = _attempt(lambda: os.mkfifo(join(path, "fifo")))
    done["device"] = _attempt(
        lambda: os.mknod(join(path, "null"), 0o600 | 0o020000, os.makedev(1, 3))
    )
    return done


def link(path: str, target: str) -> dict[str, Any]:
    """A symbolic link at `path` (in a writable directory) to `target`, whatever it is."""
    return {"linked": _attempt(lambda: os.symlink(target, path))}


def hook(path: str) -> dict[str, Any]:
    """Plant a git repository with a hook in the writable directory `path`: a `.git`
    directory (`HEAD`, a `config` naming its hooks) and an executable `post-checkout`, which
    a later `git` command of the user or of a process-runtime plugin would run."""
    join = os.path.join

    def git() -> None:
        hooks = join(path, ".git", "hooks")
        os.makedirs(hooks)
        with open(join(path, ".git", "HEAD"), "w", encoding="ascii") as handle:
            handle.write("ref: refs/heads/main\n")
        with open(join(path, ".git", "config"), "w", encoding="ascii") as handle:
            handle.write("[core]\n\thooksPath = hooks\n")
        script = join(hooks, "post-checkout")
        with open(script, "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\necho pwned\n")
        os.chmod(script, 0o755)

    return {"git": _attempt(git)}


def caps() -> dict[str, Any]:
    """Its capability sets (`/proc/self/status`): a non-root process has no effective
    capability anyway, but a non-empty bounding set is what a set-user-ID or file-capability
    program could still gain."""
    found: dict[str, Any] = {}
    status = read("/proc/self/status", 65536)["read"] or ""
    for line in status.splitlines():
        key, _, value = line.partition(":")
        if key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"):
            found[key] = value.strip()
    return found


# --- network -----------------------------------------------------------------------------------


def connect(host: str, port: int) -> dict[str, Any]:
    """A direct TCP connection, bypassing any proxy: whether it connected, else the error and
    its `errno`."""
    try:
        with socket.create_connection((host, port), timeout=3):
            return {"connected": True, "error": None, "errno": None}
    except OSError as error:
        return {"connected": False, "error": str(error), "errno": error.errno}


def resolve(name: str) -> dict[str, Any]:
    """Resolve `name` itself with the system resolver, without the proxy (`errno` is the
    `getaddrinfo` error code on failure)."""
    try:
        answers = socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except OSError as error:
        return {"resolved": False, "addresses": [], "error": str(error), "errno": error.errno}
    addresses = sorted({a[4][0] for a in answers})
    return {"resolved": True, "addresses": addresses, "error": None, "errno": None}


def dns(server: str, port: int, name: str) -> dict[str, Any]:
    """Ask a DNS server of its own choosing (`server:port`, UDP) for `name`'s IPv4 addresses,
    around the container's resolver: the addresses it answered, else the error and `errno`."""
    query = struct.pack(">HHHHHH", 0x4242, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        query += bytes([len(label)]) + label.encode("ascii")
    query += b"\0" + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(3)
            sock.sendto(query, (server, port))
            answer = sock.recv(512)
    except OSError as error:
        return {"answers": [], "error": str(error), "errno": error.errno}
    count = struct.unpack(">H", answer[6:8])[0]
    rest = answer[len(query) :]
    found = []
    for _ in range(count):  # each answer: a name pointer, then type, class, TTL, length, data
        kind, _class, _ttl, size = struct.unpack(">HHIH", rest[2:12])
        if kind == 1 and size == 4:
            found.append(socket.inet_ntoa(rest[12:16]))
        rest = rest[12 + size :]
    return {"answers": found, "error": None, "errno": None}


def _proxy(via: str | None) -> tuple[str, int]:
    proxy = urllib.parse.urlsplit(via or os.environ.get("HTTPS_PROXY", ""))
    if not proxy.hostname or not proxy.port:
        raise OSError(0, "no proxy")
    return proxy.hostname, proxy.port


def _head(conn: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < 16384:
        data = conn.recv(1)
        if not data:
            break
        head += data
    return head


def raw(request: str, via: str | None = None) -> dict[str, Any]:
    """Send `request` (a whole request head, `\\n` turned into CRLF) to the proxy (`via`, or
    `HTTPS_PROXY`); the status line it answers, and the first bytes after it when a
    `CONNECT` was established (a `GET /` through the tunnel)."""
    try:
        with socket.create_connection(_proxy(via), timeout=20) as conn:
            conn.sendall(request.replace("\n", "\r\n").encode())
            head = _head(conn)
            status = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            answer = ""
            if request.startswith("CONNECT") and " 200 " in status:
                conn.sendall(b"GET / HTTP/1.0\r\nHost: evil\r\n\r\n")
                answer = conn.recv(64).decode("ascii", errors="replace")
            return {"status": status, "answer": answer, "error": None}
    except OSError as error:
        return {"status": "", "answer": "", "error": str(error)}


def tunnel(host: str, port: int, via: str | None = None) -> dict[str, Any]:
    """`CONNECT host:port` through the proxy."""
    return raw(f"CONNECT {host}:{port} HTTP/1.1\nHost: {host}:{port}\n\n", via)


def http(url: str, host_header: str | None = None, via: str | None = None) -> dict[str, Any]:
    """A plain-HTTP request in absolute form through the proxy, optionally with a `Host`
    header naming another host than the URL."""
    host = host_header or (urllib.parse.urlsplit(url).netloc or "evil")
    return raw(f"GET {url} HTTP/1.1\nHost: {host}\n\n", via)


def fetch(url: str) -> dict[str, Any]:
    """A plain `urllib` request, which uses `HTTP_PROXY` from the environment and follows
    redirects, as any standard client does: the final status, or why it failed."""
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return {"status": response.status, "url": response.url, "error": None}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "url": error.url, "error": None}
    except OSError as error:
        return {"status": None, "url": None, "error": str(error)}


# --- resources ---------------------------------------------------------------------------------


def fork(limit: int = 1000) -> dict[str, Any]:
    """A fork bomb, bounded so that it ends: fork children that sleep until `limit` or
    failure; then kill them."""
    children: list[int] = []
    error = None
    try:
        while len(children) < limit:
            pid = os.fork()
            if pid == 0:  # pragma: no cover (the child)
                time.sleep(60)
                os._exit(0)
            children.append(pid)
    except OSError as failure:
        error = failure.strerror
    for pid in children:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    return {"forked": len(children), "error": error}


def hog(mb: int) -> dict[str, Any]:
    """Allocate and touch `mb` MiB."""
    held = [bytearray(1024 * 1024) for _ in range(mb)]
    for block in held:
        block[::4096] = b"x" * len(block[::4096])
    return {"held_mb": len(held)}


def fds(limit: int = 100_000) -> dict[str, Any]:
    """Open `/dev/null` until the descriptor limit (or `limit`); then close them."""
    opened: list[int] = []
    error = None
    try:
        while len(opened) < limit:
            opened.append(os.open("/dev/null", os.O_RDONLY))
    except OSError as failure:
        error = failure.strerror
    for fd in opened:
        os.close(fd)
    return {"opened": len(opened), "error": error}


def fill(mb: int) -> dict[str, Any]:
    """Write up to `mb` MiB into a file under `TMPDIR`; then remove it."""
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"evil-fill-{os.getpid()}")
    written = 0
    error = None
    block = b"x" * (1024 * 1024)
    try:
        with open(path, "wb") as handle:
            for _ in range(mb):
                handle.write(block)
                handle.flush()
                written += 1
            os.fsync(handle.fileno())
    except OSError as failure:
        error = failure.strerror
    with contextlib.suppress(OSError):
        os.unlink(path)
    return {"written_mb": written, "error": error}


def _cgroup() -> str:
    """This process's cgroup (v2) directory, as the launcher finds it."""
    with open("/proc/self/cgroup", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    group = next(line[3:] for line in lines if line.startswith("0::"))
    return "/sys/fs/cgroup" + group.rstrip("/")


def _cpu_stat(where: str) -> dict[str, int]:
    with open(os.path.join(where, "cpu.stat"), encoding="ascii") as handle:
        pairs = (line.split() for line in handle.read().splitlines())
        return {key: int(value) for key, value in pairs}


def _burn(until: float) -> None:
    while time.monotonic() < until:
        pass


def spin(seconds: float = 3.0, workers: int = 2) -> dict[str, Any]:
    """A CPU spin: `workers` processes burn the CPU for `seconds` of wall time. Its cgroup's
    `cpu.max` and what `cpu.stat` counted meanwhile (CPU time used, periods, throttled
    periods), or why it could not read them."""
    try:
        where = _cgroup()
        before = _cpu_stat(where)
        with open(os.path.join(where, "cpu.max"), encoding="ascii") as handle:
            cpu_max = handle.read().strip()
    except (OSError, StopIteration, ValueError) as error:
        return {"error": str(error)}
    start = time.monotonic()
    until = start + seconds
    children = []
    for _ in range(workers - 1):
        pid = os.fork()
        if pid == 0:  # pragma: no cover (the child)
            _burn(until)
            os._exit(0)
        children.append(pid)
    _burn(until)
    for pid in children:
        os.waitpid(pid, 0)
    wall = time.monotonic() - start
    after = _cpu_stat(where)
    delta = {key: after[key] - before.get(key, 0) for key in after}
    return {
        "error": None,
        "cpu_max": cpu_max,
        "wall_s": wall,
        "usage_usec": delta.get("usage_usec", 0),
        "nr_periods": delta.get("nr_periods", 0),
        "nr_throttled": delta.get("nr_throttled", 0),
    }


def ptrace() -> dict[str, Any]:
    """`PTRACE_ATTACH` its parent (in a container, the launcher, which would report whatever
    exit status the plugin forged), and read the parent's environment and memory. The
    parent's command line names it."""
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getppid()
    attached = libc.ptrace(16, parent, None, None)  # PTRACE_ATTACH
    error = ctypes.get_errno()
    if attached == 0:  # undo it, so that the parent can go on
        os.waitpid(parent, 0x40000000)  # __WALL
        libc.ptrace(17, parent, None, None)  # PTRACE_DETACH
    cmdline = read(f"/proc/{parent}/cmdline", 65536)["read"] or ""
    return {
        "parent": parent,
        "parent_cmdline": cmdline.split("\0"),
        "attached": attached,
        "errno": error,
        "environ": read(f"/proc/{parent}/environ")["error"],
        "mem": read(f"/proc/{parent}/mem", 1)["error"],
    }


def die(code: int) -> dict[str, Any]:
    """Exit with `code` in the middle of a call (247: the launcher's out-of-memory status)."""
    sys.stdout.flush()
    os._exit(code)


def shout(key: str) -> dict[str, Any]:
    """Write the environment variable `key` (its own secret) to stderr: the container's
    output, which a logging daemon would keep."""
    os.write(2, (os.environ.get(key, "") + "\n").encode())
    return {"shouted": key}


def child() -> dict[str, Any]:
    """Leave a child behind that stays in its process group: fork, sleep."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover (the child)
        time.sleep(600)
        os._exit(0)
    return {"child": pid, "pgid": os.getpgid(pid)}


def daemon() -> dict[str, Any]:
    """Leave a process behind: double fork, `setsid`, ignore `SIGTERM` and `SIGHUP`, sleep."""
    read_end, write_end = os.pipe()
    first = os.fork()
    if first == 0:  # pragma: no cover (the child)
        os.setsid()
        second = os.fork()
        if second == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            os.write(write_end, str(os.getpid()).encode())
            os.close(write_end)
            while True:
                time.sleep(1)
        os._exit(0)
    os.waitpid(first, 0)
    os.close(write_end)
    pid = int(os.read(read_end, 32) or b"0")
    os.close(read_end)
    return {"daemon": pid, "sid": os.getsid(pid) if pid else None}


ATTACKS: dict[str, Callable[..., dict[str, Any]]] = {
    "env": environ,
    "read": read,
    "proc": proc,
    "fs": fs,
    "write": write,
    "plant": plant,
    "link": link,
    "hook": hook,
    "caps": caps,
    "connect": connect,
    "resolve": resolve,
    "dns": dns,
    "raw": raw,
    "tunnel": tunnel,
    "http": http,
    "fetch": fetch,
    "fork": fork,
    "hog": hog,
    "fds": fds,
    "fill": fill,
    "daemon": daemon,
    "child": child,
    "spin": spin,
    "ptrace": ptrace,
    "die": die,
    "shout": shout,
}
"""Every attack, by the tool name `jailbreak.py` serves it under (`<plugin>.<name>`)."""
