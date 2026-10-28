# SPDX-License-Identifier: Apache-2.0
"""The probe plugin: a stdio MCP server for the plugin host tests (#24).

Run as `[sys.executable, "<abs path>/server.py"]`: the venv interpreter finds the `mcp` SDK
without `PYTHONPATH`. Every tool is synthetic and touches nothing outside its own process
(except `probe.child`, which starts one `sleep`-like Python child to test group termination).
"""

import hashlib
import logging
import os
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

warnings.simplefilter("ignore")
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
for name in ("mcp", "mcp.server", "mcp.server.lowlevel"):
    logging.getLogger(name).setLevel(logging.ERROR)

server = MCPServer("probe")


def _initial_environ() -> dict[str, str]:
    """The environment the process was started with: `/proc/self/environ` where there is one
    (Linux), else `os.environ` (on macOS, the interpreter adds `__CF_USER_TEXT_ENCODING`)."""
    proc = Path("/proc/self/environ")
    if not proc.exists():
        return dict(os.environ)
    pairs = [item.split(b"=", 1) for item in proc.read_bytes().split(b"\0") if item]
    return {key.decode(): value.decode() for key, value in pairs}


@server.tool(name="probe.env")
def env() -> dict[str, Any]:
    """The initial environment (values, and their SHA-256: a secret's value is redacted by the
    host, its digest is not), the working directory and the process group."""
    environ = _initial_environ()
    digests = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in environ.items()}
    return {
        "env": environ,
        "sha256": digests,
        "cwd": os.getcwd(),
        "cwd_entries": os.listdir("."),
        "pgid": os.getpgid(0),
        "pid": os.getpid(),
        "argv": sys.argv[1:],
    }


@server.tool(name="probe.sleep")
def sleep(seconds: float) -> dict[str, Any]:
    """Block for `seconds` (a wedged plugin)."""
    time.sleep(seconds)
    return {"slept": seconds}


@server.tool(name="probe.crash")
def crash(code: int = 3) -> dict[str, Any]:
    """Exit at once, mid-call."""
    sys.stderr.write("probe: crashing now\n")
    sys.stderr.flush()
    os._exit(code)


@server.tool(name="probe.stderr")
def stderr(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Write each `{"text": str, "times": int}` (or `{"env": NAME}`: that variable's value)
    part to stderr, in order."""
    written = 0
    for part in parts:
        if "env" in part:
            data = os.environ.get(str(part["env"]), "").encode()
        else:
            data = (str(part["text"]) * int(part.get("times", 1))).encode()
        sys.stderr.buffer.write(data)
        written += len(data)
    sys.stderr.flush()
    return {"written": written}


@server.tool(name="probe.intents")
def intents(proposed: list[Any], data: Any = None) -> CallToolResult:
    """Return `data` and propose `proposed` through `_meta["dev.horae/intents"]`."""
    return CallToolResult(
        content=[TextContent(text="proposed")],
        structured_content={"data": data},
        _meta={"dev.horae/intents": proposed},
    )


@server.tool(name="probe.echo")
def echo(ctx: Context, value: Any = None, reveal: str | None = None) -> dict[str, Any]:
    """Return `value`, the input from `_meta["dev.horae/input"]`, and optionally the value of
    environment variable `reveal` (to test output redaction)."""
    meta = ctx.request_context.meta or {}
    revealed = os.environ.get(reveal) if reveal else None
    return {"value": value, "input": meta.get("dev.horae/input"), "revealed": revealed}


@server.tool(name="probe.text", structured_output=False)
def text(body: str) -> str:
    """A plain text result (no structured content)."""
    return body


@server.tool(name="probe.fail")
def fail(message: str) -> dict[str, Any]:
    """Report a tool error (`isError: true`)."""
    raise ToolError(message)


@server.tool(name="probe.log")
async def log(ctx: Context, message: str) -> dict[str, Any]:
    """Send a `notifications/message` log line."""
    await ctx.info(message)
    return {"logged": True}


@server.tool(name="probe.big")
def big(size: int) -> dict[str, Any]:
    """A result of about `size` bytes."""
    return {"blob": "x" * size}


@server.tool(name="probe.child")
def child() -> dict[str, Any]:
    """Start a long-lived child process (same process group) and return its pid."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"pid": process.pid}


if __name__ == "__main__":
    server.run("stdio")
