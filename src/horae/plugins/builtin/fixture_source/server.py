# SPDX-License-Identifier: Apache-2.0
"""The `fixture-source` stdio MCP server: deterministic synthetic data (#25).

One source, `fixture-source.serve`, with one optional parameter, `file`: the absolute path of a
JSON fixture file (default: the packaged `data.json`). The file is an object with two
optional keys:

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
import sys
import warnings
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

DEFAULT_FIXTURE = Path(__file__).resolve().parent / "data.json"
MAX_FIXTURE_BYTES = 1024 * 1024
INTENTS_META = "dev.horae/intents"

server = MCPServer("fixture-source")


def load(file: str | None = None) -> tuple[dict[str, Any], list[Any]]:
    """The fixture's `data` and `intents` (see the module docstring); `ToolError` otherwise."""
    if file is not None and not os.path.isabs(file):
        raise ToolError("file must be an absolute path")
    path = DEFAULT_FIXTURE if file is None else Path(file)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FIXTURE_BYTES + 1)
    except OSError as error:
        raise ToolError(f"the fixture cannot be read ({error.strerror})") from None
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
def serve(file: str | None = None) -> CallToolResult:
    """Serve the fixture's `data` and propose its `intents`."""
    data, intents = load(file)
    return CallToolResult(
        content=[TextContent(text=json.dumps(data, sort_keys=True))],
        structured_content=data,
        _meta={INTENTS_META: intents},
    )


def main() -> None:  # pragma: no cover (runs in the plugin's subprocess)
    warnings.simplefilter("ignore")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server.run("stdio")
