# SPDX-License-Identifier: Apache-2.0
"""A stdlib-only stdio MCP server that reports what it can do from inside its sandbox (#49).

Used by `tests/test_plugin_container_live.py`, installed into a venv built by the plugin image
and run with `runtime: container`. `python -m jail_server [<mode> [<prefix>]]`; modes: `normal`,
`stubborn` (ignores stdin EOF and `SIGTERM`, so only a forced removal stops it). Tools, named
`<prefix>.<tool>` (prefix `jail` by default, so one server serves several plugin names):

- `jail.env`: the process environment;
- `jail.probe`: uid, capabilities, `no_new_privs`, seccomp mode, and whether the root
  filesystem, `/plugin` and `/tmp` are writable and `/tmp` executable;
- `jail.connect` (`host`, `port`): whether a TCP connection succeeds (2 s);
- `jail.resolve` (`name`): whether a name resolves;
- `jail.tunnel` (`host`, `port`, optional `via`): a `CONNECT` through `HTTPS_PROXY` (or the
  proxy URL `via`, #50), then, once
  established, `GET /` through the tunnel; returns the proxy's status line and the answer's
  first bytes;
- `jail.fetch` (`url`): a plain `urllib` request, which uses `HTTP_PROXY` from the
  environment as any standard client does; returns the HTTP status.

Synthetic names and documentation addresses only.
"""

import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOOLS = ["env", "probe", "connect", "resolve", "tunnel", "fetch"]
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "jail"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def result(request_id: Any, value: dict[str, Any]) -> None:
    send(
        {"jsonrpc": "2.0", "id": request_id, "result": {"content": [], "structuredContent": value}}
    )


def _status() -> dict[str, str]:
    fields: dict[str, str] = {}
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    return fields


def _writable(directory: str) -> bool:
    path = os.path.join(directory, f"jail-probe-{os.getpid()}")
    try:
        with open(path, "w", encoding="ascii") as handle:
            handle.write("x")
    except OSError:
        return False
    os.unlink(path)
    return True


def _tmp_executable() -> bool:
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "jail-exec")  # noqa: S108 (in the container)
    with open(path, "w", encoding="ascii") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o700)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.execv(path, [path])  # noqa: S606 (the probe's own file)
            finally:
                os._exit(126)
        _, code = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(code) == 0
    finally:
        os.unlink(path)


def probe() -> dict[str, Any]:
    status = _status()
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cap_eff": status.get("CapEff"),
        "cap_bnd": status.get("CapBnd"),
        "no_new_privs": status.get("NoNewPrivs"),
        "seccomp": status.get("Seccomp"),
        "root_writable": _writable("/"),
        "plugin_writable": _writable("/plugin"),
        "venv_writable": _writable("/plugin/.venv"),
        "tmp_writable": _writable(os.environ.get("TMPDIR", "/tmp")),  # noqa: S108
        "tmp_executable": _tmp_executable(),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "visible_pids": sorted(int(name) for name in os.listdir("/proc") if name.isdigit()),
    }


def connect(host: str, port: int) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=2):
            return {"connected": True, "error": None}
    except OSError as error:
        return {"connected": False, "error": str(error)}


def resolve(name: str) -> dict[str, Any]:
    try:
        socket.getaddrinfo(name, 443)
    except OSError as error:
        return {"resolved": False, "error": str(error)}
    return {"resolved": True, "error": None}


def tunnel(host: str, port: int, via: str | None = None) -> dict[str, Any]:
    proxy = urllib.parse.urlsplit(via or os.environ.get("HTTPS_PROXY", ""))
    try:
        with socket.create_connection((proxy.hostname, proxy.port), timeout=20) as conn:
            conn.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            head = b""
            while b"\r\n\r\n" not in head:
                data = conn.recv(1)
                if not data:
                    break
                head += data
            status = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            answer = ""
            if " 200 " in status:
                conn.sendall(b"GET / HTTP/1.0\r\nHost: jail\r\n\r\n")
                answer = conn.recv(64).decode("ascii", errors="replace")
            return {"status": status, "answer": answer, "error": None}
    except OSError as error:
        return {"status": None, "answer": "", "error": str(error)}


def fetch(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 (a test URL)
            return {"status": response.status, "error": None}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "error": None}
    except OSError as error:
        return {"status": None, "error": str(error)}


def call(request_id: Any, name: str, arguments: dict[str, Any]) -> None:
    name = name.removeprefix(PREFIX + ".")
    if name == "env":
        result(request_id, {"env": dict(os.environ)})
    elif name == "probe":
        result(request_id, probe())
    elif name == "connect":
        result(request_id, connect(arguments["host"], int(arguments["port"])))
    elif name == "resolve":
        result(request_id, resolve(arguments["name"]))
    elif name == "tunnel":
        result(request_id, tunnel(arguments["host"], int(arguments["port"]), arguments.get("via")))
    elif name == "fetch":
        result(request_id, fetch(arguments["url"]))


def main(mode: str) -> None:
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize":
            version = message["params"]["protocolVersion"]
            info = {"name": "jail", "version": "1"}
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": info,
                    },
                }
            )
        elif method == "tools/list":
            tools = [
                {"name": f"{PREFIX}.{name}", "inputSchema": {"type": "object"}} for name in TOOLS
            ]
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        elif method == "tools/call":
            params = message["params"]
            call(request_id, params["name"], params.get("arguments") or {})
        elif request_id is not None and method is not None:
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    if mode == "stubborn":
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "normal")
