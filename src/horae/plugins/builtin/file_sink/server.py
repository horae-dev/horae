# SPDX-License-Identifier: Apache-2.0
"""The `file-sink` stdio MCP server: notes in a local vault (#25).

It implements the sink operations of `horae.plugins.host.sink` for the `file-sink.write_note`
entry, and nothing else: it never decides what a note becomes. **The core computes every
note's bytes** (`horae.apply.notes`, ownership included); this server only reads and writes
bytes, with the resolution and the atomic compare-and-swap of `horae.apply.local`
(`LocalFileSink`: no symlink on any component, a single hard link, a regular file, a
temporary file then `os.replace` relative to the directory descriptor, every
file-system call in a worker thread). So its safety rules are exactly the built-in sink's.

- **The vault** is the single `paths.write` entry the core passes with every operation: the
  plugin's path permission as approved in `horae.lock` (`vault_unset` without one,
  `config_invalid` with several or a relative one). A plugin process serves one instance,
  and trusts the core, never a proposer, for it.
- **Paths** must be note paths of the intent contract (`NotePath`: relative, POSIX, no empty,
  `.` or `..` segment, no hidden segment), else `path_invalid`, before anything is opened.
- `file-sink.read` returns the note's bytes (base64) or `null`; `file-sink.apply` writes the
  core's `content` if the note still has `expected_before_sha256` (`null`: it must not exist)
  and returns the digest of what it wrote; `file-sink.restore` does the same for `horae undo`,
  where `content: null` removes the note; `file-sink.parent_exists` and `file-sink.identity`
  (`local:<st_dev>:<st_ino>:<real path>` of the vault) are read-only. Only `note.*` intents
  are applied (`not_supported` otherwise).
- **Size.** A note travels over MCP only up to `MAX_MCP_NOTE_BYTES` (about 5.95 MiB, the
  host's message cap less base64 and the envelope), below `LocalFileSink`'s 16 MiB:
  `file-sink.read` of a larger note returns `note_too_large` before encoding it (never an
  oversized message, which would cost the process), and `content` above it is refused.
  Each result is sent once, as `structuredContent` with a short text block.
- A `SinkError` is returned as `{"error": {"kind", "message"}}`: the core maps it to its
  kinds (`target_changed`, `symlink`, `hard_link`, `not_a_file`, `note_too_large`…).
"""

import hashlib
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import TypeAdapter, ValidationError

from horae.apply.local import LocalFileSink
from horae.apply.sink import SinkError
from horae.contracts.types import NotePath
from horae.plugins.host.sink import decode, encode

server = MCPServer("file-sink")
_NOTE_PATH: TypeAdapter[str] = TypeAdapter(NotePath)
_NOTE_TYPES = ("note.create", "note.append_section", "note.replace_owned_block")

Result = dict[str, Any]


def _vault(paths: dict[str, Any]) -> LocalFileSink:
    write = paths.get("write")
    if not write:
        raise SinkError("vault_unset", "no paths.write entry approved for file-sink")
    if not isinstance(write, list) or len(write) != 1:
        raise SinkError("config_invalid", "file-sink needs exactly one paths.write entry")
    root = write[0]
    if not isinstance(root, str) or not os.path.isabs(root):
        raise SinkError("config_invalid", "the vault must be an absolute path")
    return LocalFileSink(Path(root))


def _path(path: str) -> str:
    try:
        return _NOTE_PATH.validate_python(path)
    except ValidationError:
        raise SinkError("path_invalid", "not a note path") from None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _failed(error: SinkError) -> Result:
    return {"error": {"kind": error.kind, "message": error.message}}


async def read_op(paths: dict[str, Any], path: str) -> Result:
    try:
        content = await _vault(paths).read_target(_path(path))
        return {"content": None if content is None else encode(content)}
    except SinkError as error:
        return _failed(error)


async def apply_op(
    paths: dict[str, Any],
    intent: dict[str, Any],
    content: str | None = None,
    expected_before_sha256: str | None = None,
) -> Result:
    try:
        if intent.get("type") not in _NOTE_TYPES:
            raise SinkError("not_supported", "file-sink applies note intents only")
        if content is None:
            raise SinkError("content_missing", "the core sent no note bytes")
        path = _path(intent.get("path", ""))
        data = decode(content)
        assert data is not None  # noqa: S101 - content is not None
        await _vault(paths).restore(path, data, expected_sha256=expected_before_sha256)
        return {"target": path, "after_sha256": _sha(data)}
    except SinkError as error:
        return _failed(error)


async def restore_op(
    paths: dict[str, Any], path: str, content: str | None, expected_sha256: str | None
) -> Result:
    try:
        target = _path(path)
        await _vault(paths).restore(target, decode(content), expected_sha256=expected_sha256)
        return {}
    except SinkError as error:
        return _failed(error)


async def parent_exists_op(paths: dict[str, Any], path: str) -> Result:
    try:
        return {"exists": await _vault(paths).parent_exists(_path(path))}
    except SinkError as error:
        return _failed(error)


def identity_op(paths: dict[str, Any]) -> Result:
    try:
        return {"identity": _vault(paths).identity}
    except SinkError as error:
        return _failed(error)


# The MCP tools (the core passes more arguments to `apply` than this sink needs: accepted).
# Each returns its result once, as `structuredContent`, with a short text block: never a
# second copy of a note's base64 (the host caps each message at `MAX_MESSAGE_BYTES`).


def _result(data: Result) -> CallToolResult:
    failure = data.get("error")
    summary = f"error: {failure['kind']}" if isinstance(failure, dict) else "ok"
    return CallToolResult(content=[TextContent(text=summary)], structured_content=data)


@server.tool(name="file-sink.read")
async def read(paths: dict[str, Any], path: str) -> CallToolResult:
    """The note's bytes (base64), or `null` when it does not exist."""
    return _result(await read_op(paths, path))


@server.tool(name="file-sink.apply")
async def apply(
    paths: dict[str, Any],
    intent: dict[str, Any],
    content: str | None = None,
    expected_before_sha256: str | None = None,
    tool: str | None = None,
    params: dict[str, Any] | None = None,
    run_id: str | None = None,
    index: int | None = None,
    input_hash: str | None = None,
    dedupe_key: str | None = None,
) -> CallToolResult:
    """Write the core's bytes for a note intent (compare-and-swap)."""
    return _result(await apply_op(paths, intent, content, expected_before_sha256))


@server.tool(name="file-sink.restore")
async def restore(
    paths: dict[str, Any], path: str, content: str | None = None, expected_sha256: str | None = None
) -> CallToolResult:
    """Make the note hold `content` (`null`: remove it), compare-and-swap (`horae undo`)."""
    return _result(await restore_op(paths, path, content, expected_sha256))


@server.tool(name="file-sink.parent_exists")
async def parent_exists(paths: dict[str, Any], path: str) -> CallToolResult:
    """Whether the note's directory exists."""
    return _result(await parent_exists_op(paths, path))


@server.tool(name="file-sink.identity")
def identity(paths: dict[str, Any]) -> CallToolResult:
    """Where this sink writes: the vault's device, inode and real path."""
    return _result(identity_op(paths))


def main() -> None:  # pragma: no cover (runs in the plugin's subprocess)
    warnings.simplefilter("ignore")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server.run("stdio")
