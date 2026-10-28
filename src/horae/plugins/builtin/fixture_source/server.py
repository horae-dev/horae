# SPDX-License-Identifier: Apache-2.0
"""The `fixture-source` stdio MCP server: deterministic synthetic data (#25).

One source, `fixture-source.serve`, with one optional parameter, `file`: the absolute path of a
JSON fixture file (default: the packaged `data.json`). A `file` must be inside one of the
plugin's `paths.read` entries **as approved in `horae.lock`** (its real path, symbolic links
resolved; the core sends the approved paths with every call, `_meta["dev.horae/paths"]`), and
be a regular file: opened with `O_NONBLOCK` (a FIFO never blocks the call) and checked with
`fstat` (`S_ISREG`). The manifest asks for no path, so a lock that grants none (the walking
skeleton's) can only serve the packaged fixture. The file is an object with two optional
keys:

- `data` (an object, default `{}`): returned as the tool's `structuredContent`;
- `intents` (a list, default `[]`): proposed as they are, through
  `_meta["dev.horae/intents"]`; the core validates them against the plugin's
  `permissions.intents` and routes them (a note intent may omit `path`: the core fills it
  from the `act` entry).

Nothing depends on the clock, the environment or the call order: the same file gives the
same answer, which is what the end-to-end test and `horae replay` rely on. The file is read
once per call, at most `MAX_FIXTURE_BYTES`, and never written. A missing, oversized or
malformed file is a tool error (`isError`, the host's `tool_error`).
"""

import json
import logging
import os
import stat
import sys
import warnings
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "data.json"
MAX_FIXTURE_BYTES = 1024 * 1024
INTENTS_META = "dev.horae/intents"
PATHS_META = "dev.horae/paths"

server = MCPServer("fixture-source")


def _allowed(file: str, paths: Any) -> str:
    """The real path of `file` if it is inside one of `paths["read"]`; `ToolError` otherwise."""
    if not os.path.isabs(file):
        raise ToolError("file must be an absolute path")
    read = paths.get("read") if isinstance(paths, dict) else None
    if not isinstance(read, list):
        read = []
    roots = [root for root in read if isinstance(root, str) and os.path.isabs(root)]
    real = os.path.realpath(file)
    for root in roots:
        top = os.path.realpath(root)
        if os.path.commonpath([top, real]) == top:
            return real
    raise ToolError("file is not inside a paths.read entry approved in horae.lock")


def _read(path: str) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ToolError(f"the fixture cannot be read ({error.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ToolError("the fixture is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_FIXTURE_BYTES:
            chunk = os.read(fd, MAX_FIXTURE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise ToolError(f"the fixture cannot be read ({error.strerror})") from None
    finally:
        os.close(fd)


def load(file: str | None = None, paths: Any = None) -> tuple[dict[str, Any], list[Any]]:
    """The fixture's `data` and `intents` (see the module docstring); `ToolError` otherwise.
    `paths` are the locked path permissions (`{"read": [...], ...}`)."""
    path = str(DEFAULT_FIXTURE) if file is None else _allowed(file, paths)
    raw = _read(path)
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ToolError(f"the fixture is larger than {MAX_FIXTURE_BYTES} bytes")
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise ToolError("the fixture is not JSON") from None
    if not isinstance(document, dict) or not set(document) <= {"data", "intents"}:
        raise ToolError("the fixture must be an object with only `data` and `intents`")
    data = document.get("data", {})
    intents = document.get("intents", [])
    if not isinstance(data, dict) or not isinstance(intents, list):
        raise ToolError("`data` must be an object and `intents` a list")
    return data, intents


@server.tool(name="fixture-source.serve")
def serve(ctx: Context, file: str | None = None) -> CallToolResult:
    """Serve the fixture's `data` and propose its `intents`."""
    meta = ctx.request_context.meta or {}
    data, intents = load(file, meta.get(PATHS_META))
    return CallToolResult(
        content=[TextContent(text=json.dumps(data, sort_keys=True))],
        structured_content=data,
        _meta={INTENTS_META: intents},
    )


def main() -> None:  # pragma: no cover (runs in the plugin's subprocess)
    warnings.simplefilter("ignore")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server.run("stdio")
