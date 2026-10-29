# SPDX-License-Identifier: Apache-2.0
"""The recorded-input test harness: replay recorded calls to a plugin and check its outputs.

A **recording** is a JSON file holding one call as the core makes it, and what the plugin
must answer:

```json
{
  "tool": "hello.greet",
  "params": {"name": "Ada"},
  "input": null,
  "paths": {"read": [], "write": []},
  "expect": {"data": {"greeting": "Hello, Ada!"}, "intents": [{"type": "task.create", ...}]}
}
```

`params` are the trusted parameters, `input` a transform's input (the previous step's data,
as a run record stores it), `paths` the locked path permissions; all optional. `expect` holds
the `data` and `intents` (both compared exactly) or an `error` (a substring of the tool
error's message). A recording is replayed **through MCP** (an in-process client, the same
requests, `_meta` keys and result reading as the core's host), so what passes here is what
the host would see.

- `replay(plugin, path)` replays one recording, raising `ReplayMismatch` (an
  `AssertionError`, with a readable diff) when the output differs;
- `replay_all(plugin, directory)` replays every `*.json` file of `directory` (sorted) and
  returns the paths, so a plugin's test suite is one line;
- with `HORAE_SDK_RECORD=1` in the environment (or `record=True`), the current output is
  **written** into each file's `expect` instead: record once, review the diff, commit;
- `call(plugin, tool, ...)` and `operation(plugin, tool, arguments)` are the async primitives
  (a tool call, a sink operation).

Recordings must be synthetic (no personal data, no secret). A source that talks to a network
service is tested by giving the plugin a fake client in the test and replaying its recorded
answers through the same recordings.
"""

import difflib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.client import Client
from mcp_types import CallToolResult

from horae_sdk.plugin import Plugin
from horae_sdk.protocol import INPUT_META, INTENTS_META, PATHS_META

RECORD_ENV = "HORAE_SDK_RECORD"
_KEYS = {"tool", "params", "input", "paths", "expect"}


class ReplayMismatch(AssertionError):
    """A replayed call's output differs from its recording."""


@dataclass(frozen=True)
class Replayed:
    """What the plugin answered, read as the core's host reads it."""

    data: Any = None
    intents: list[Any] = field(default_factory=list)
    error: str | None = None
    """The tool error's message (`isError`), else `None`."""

    def expectation(self) -> dict[str, Any]:
        if self.error is not None:
            return {"error": self.error}
        return {"data": self.data, "intents": self.intents}


def read_result(result: CallToolResult) -> Replayed:
    """A tool result as the host reads it: `isError`, then `structuredContent`, else a single
    JSON text block; intents from `_meta["dev.horae/intents"]`."""
    texts = [block.text for block in result.content if block.type == "text"]
    if result.is_error:
        return Replayed(error="\n".join(texts) or "the tool reported an error")
    if result.structured_content is not None:
        data = result.structured_content
    elif len(result.content) == 1 and texts:
        data = json.loads(texts[0])
    else:
        raise ReplayMismatch("no structured content and not a single JSON text block")
    intents = (result.meta or {}).get(INTENTS_META, [])
    if not isinstance(intents, list):
        raise ReplayMismatch(f"_meta[{INTENTS_META!r}] is not a list")
    return Replayed(data, intents)


async def _call_tool(
    plugin: Plugin, tool: str, arguments: dict[str, Any], meta: dict[str, Any]
) -> CallToolResult:
    async with Client(plugin.server) as client:
        listed = {item.name for item in (await client.list_tools()).tools}
        if tool not in listed:
            raise ReplayMismatch(f"{plugin.name} does not list {tool} in tools/list")
        return await client.call_tool(tool, arguments, meta=meta or None)  # type: ignore[arg-type]


async def call(
    plugin: Plugin,
    tool: str,
    params: Mapping[str, Any] | None = None,
    *,
    input: Any = None,
    paths: Mapping[str, Any] | None = None,
) -> Replayed:
    """Call `tool` as the core does: `params` as arguments, `input` and `paths` in `_meta`.
    Only a provided source or transform can be called this way."""
    if tool not in plugin.tools:
        raise ReplayMismatch(f"{tool} is not a source or transform of {plugin.name}")
    meta: dict[str, Any] = {PATHS_META: {"read": [], "write": [], **(paths or {})}}
    if input is not None:
        meta[INPUT_META] = input
    return read_result(await _call_tool(plugin, tool, dict(params or {}), meta))


async def operation(plugin: Plugin, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Call sink operation `tool` (`<ns>.apply`…) as the core does; its result object (or
    `{"error": {...}}`). `arguments` should include `paths`."""
    namespace = tool.split(".", 1)[0]
    if namespace not in plugin.sinks:
        raise ReplayMismatch(f"{namespace} is not a sink namespace of {plugin.name}")
    replayed = read_result(await _call_tool(plugin, tool, dict(arguments), {}))
    if replayed.error is not None or not isinstance(replayed.data, dict):
        raise ReplayMismatch(f"{tool} did not return an object: {replayed.error}")
    return replayed.data


def load(path: Path) -> dict[str, Any]:
    """A recording, checked for its keys."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("tool"), str):
        raise ValueError(f"{path}: a recording is an object with a `tool`")
    unknown = sorted(set(document) - _KEYS)
    if unknown:
        raise ValueError(f"{path}: unknown keys {', '.join(unknown)}")
    return document


def _pretty(value: Any) -> list[str]:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def check(expected: Mapping[str, Any], replayed: Replayed, where: str = "recording") -> None:
    """`ReplayMismatch` unless `replayed` matches `expected` (see the module docstring)."""
    if "error" in expected:
        if replayed.error is None or str(expected["error"]) not in replayed.error:
            got = replayed.expectation()
            raise ReplayMismatch(
                f"{where}: expected a tool error with {expected['error']!r}, got {got!r}"
            )
        return
    actual = replayed.expectation()
    wanted = {"data": expected.get("data"), "intents": expected.get("intents", [])}
    if actual != wanted:
        diff = difflib.unified_diff(
            _pretty(wanted), _pretty(actual), "recorded", "replayed", lineterm=""
        )
        raise ReplayMismatch(f"{where}: the output differs\n" + "\n".join(diff))


def replay(plugin: Plugin, path: Path, *, record: bool | None = None) -> Replayed:
    """Replay the recording at `path` (see the module docstring)."""
    document = load(path)
    replayed = anyio.run(
        lambda: call(
            plugin,
            document["tool"],
            document.get("params"),
            input=document.get("input"),
            paths=document.get("paths"),
        )
    )
    if record if record is not None else os.environ.get(RECORD_ENV) == "1":
        document["expect"] = replayed.expectation()
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return replayed
    if "expect" not in document:
        raise ReplayMismatch(f"{path}: no `expect` (record it with {RECORD_ENV}=1)")
    check(document["expect"], replayed, str(path))
    return replayed


def replay_all(plugin: Plugin, directory: Path, *, record: bool | None = None) -> list[Path]:
    """Replay every `*.json` recording in `directory`; the paths replayed (at least one)."""
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ReplayMismatch(f"no recording in {directory}")
    for path in paths:
        replay(plugin, path, record=record)
    return paths


__all__ = [
    "RECORD_ENV",
    "ReplayMismatch",
    "Replayed",
    "call",
    "check",
    "load",
    "operation",
    "read_result",
    "replay",
    "replay_all",
]
