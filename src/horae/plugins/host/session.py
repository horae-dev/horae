# SPDX-License-Identifier: Apache-2.0
"""One plugin process: spawn, MCP handshake, `tools/list`, tool calls, stop (#24).

A `PluginProcess` is owned by a **serve task** that the host starts in its own task group:
that task enters the transport and the SDK's `ClientSession` (both anyio scopes, which must be
entered and left by the same task), signals `ready`, then waits for `stop`. Calls come from the
engine's tasks and only use the session's streams.

**Client capabilities: none.** The session is built with the SDK's default callbacks, so it
advertises no `sampling`, `roots` or `elicitation` capability and answers any such request with
an error: a plugin asking the core's LLM would be an undeclared LLM call (ADR 0001). Server
`notifications/message` log lines go to the same redacted capture as stderr.

**Tool names.** The host calls tools by their full ref (`probe.env`), the name the manifest
lists in `provides`; the server must list that exact name in `tools/list`.

**Data channels.** The trusted parameters are the tool's `arguments`. The untrusted input of a
`transform` (the previous step's data) travels in the request's `_meta["dev.horae/input"]`
(absent for sources), so it can never shadow a parameter. Intents a tool proposes travel in
the result's `_meta["dev.horae/intents"]` (PR #85 answer 5), a key plain MCP servers never set.
Every request also carries the plugin's path permissions **as approved in `horae.lock`** in
`_meta["dev.horae/paths"]` (`{"read": [...], "write": [...]}`, #25): trusted configuration
from the core (v1 has no per-plugin configuration in `horae.yaml`), e.g. which files
`fixture-source` may serve. A plugin must never take paths from its arguments' untrusted data.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import mcp_types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from horae.plugins.host.capture import StderrCapture
from horae.plugins.host.errors import HostErrorKind, PluginCallError
from horae.plugins.host.transport import ProcessState, exit_status, open_stdio

INPUT_META = "dev.horae/input"
"""Request `_meta` key of a transform's input data."""
INTENTS_META = "dev.horae/intents"
"""Result `_meta` key of the intents a tool proposes."""
PATHS_META = "dev.horae/paths"
"""Request `_meta` key of the plugin's locked path permissions."""
MAX_TOOLS = 1024
"""Most tools (over all `tools/list` pages) a plugin may list."""
_MAX_PAGES = 64
_CRASH_WAIT_S = 2.0
_STOP_WAIT_S = 10.0


@dataclass(frozen=True)
class Spawn:
    """How to start one plugin process."""

    plugin: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    cwd: Path
    startup_timeout_s: float


class CallFailed(Exception):
    """A call failed in a way that leaves the process unusable (it is being stopped)."""

    def __init__(self, kind: HostErrorKind, message: str) -> None:
        super().__init__(kind, message)
        self.kind: HostErrorKind = kind
        self.message = message


class PluginProcess:
    """One running plugin (see the module docstring)."""

    def __init__(self, spawn: Spawn, capture: StderrCapture) -> None:
        self.spawn = spawn
        self.capture = capture
        self.state = ProcessState()
        self.tools: frozenset[str] = frozenset()
        self.session: ClientSession | None = None
        self.error: PluginCallError | None = None
        """Why it could not start."""
        self.last_stderr = 0.0
        """When stderr (or a log notification) last arrived (`anyio.current_time()`)."""
        capture.listeners.append(self._heard)
        self.ready = anyio.Event()
        self.stopped = anyio.Event()
        self._stop = anyio.Event()

    def _heard(self) -> None:
        self.last_stderr = anyio.current_time()

    @property
    def alive(self) -> bool:
        state = self.state
        return (
            self.session is not None
            and not self._stop.is_set()
            and state.returncode is None
            and not state.stdout_closed
            and state.failure is None
        )

    async def serve(self) -> None:
        """The serve task: start, handshake, wait for `stop`, then stop the process."""
        try:
            async with open_stdio(
                self.spawn.argv,
                env=self.spawn.env,
                cwd=self.spawn.cwd,
                capture=self.capture,
                state=self.state,
            ) as (read, write):
                self.spawn.env.clear()  # the child has its copy; keep no secret around
                async with ClientSession(read, write, logging_callback=self._log) as session:
                    # Never raise inside these scopes: anyio task groups would wrap the error.
                    self.error = await self._handshake(session)
                    if self.error is None:
                        self.session = session
                        self.ready.set()
                        await self._stop.wait()
        except PluginCallError as error:  # spawn_failed, raised before any task group
            self.error = error
        except Exception:  # an SDK or transport bug: never crash the run
            self.error = PluginCallError("host_error", f"{self.spawn.plugin}: unexpected error")
        finally:
            self.session = None
            self.ready.set()
            self.stopped.set()

    async def _handshake(self, session: ClientSession) -> PluginCallError | None:
        name = self.spawn.plugin
        timeout = self.spawn.startup_timeout_s
        failure: tuple[HostErrorKind, str] | None = None
        with anyio.move_on_after(timeout) as scope:
            try:
                await session.initialize()
                self.tools = await _list_tools(session)
            except (MCPError, RuntimeError, ValueError) as error:
                failure = ("handshake_failed", self._why(error, "the handshake failed"))
        if scope.cancelled_caught:
            failure = ("startup_timeout", f"{name} did not start within {timeout:g} s")
        if failure is None:
            return None
        self.state.forced = True
        return PluginCallError(failure[0], f"{name}: {failure[1]}")

    def _why(self, error: Exception, default: str) -> str:
        if self.state.failure is not None:
            return self.state.failure[1]
        if isinstance(error, MCPError) and error.code == CONNECTION_CLOSED:
            return f"the process {exit_status(self.state.returncode)} or closed its stdout"
        return default

    async def _log(self, params: mcp_types.LoggingMessageNotificationParams) -> None:
        # JSON (not `str()`/`repr()`), so a value is written in one of the forms the redactor
        # knows (its JSON escaping): `repr` would write a control character as `\x01`.
        source = f" {json.dumps(params.logger, ensure_ascii=False)}" if params.logger else ""
        data = params.data if isinstance(params.data, str) else _json(params.data)
        line = f"[log {params.level}{source}] {data}\n"
        self.capture.feed(line.encode("utf-8", errors="replace"))

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        input: Any,
        timeout_s: float,
        *,
        paths: dict[str, list[str]] | None = None,
    ) -> mcp_types.CallToolResult:
        """Call tool `name`; `CallFailed` (process unusable) or `PluginCallError` (it is fine)."""
        session = self.session
        if session is None:
            raise CallFailed("plugin_crashed", "the process is not running")
        meta: Any = {} if input is None else {INPUT_META: input}
        if paths is not None:
            meta[PATHS_META] = paths
        meta = meta or None
        crashed: MCPError | None = None
        result: mcp_types.CallToolResult | None = None
        with anyio.move_on_after(timeout_s) as scope:
            try:
                result = await session.call_tool(name, arguments, meta=meta)
            except MCPError as error:
                state = self.state
                gone = (
                    state.stdout_closed or state.failure is not None or state.returncode is not None
                )
                if error.code != CONNECTION_CLOSED or not gone:
                    message = f"JSON-RPC error {error.code}: {error.message}"
                    raise PluginCallError("protocol_error", message) from None
                crashed = error
            except RuntimeError as error:  # output schema mismatch, input_required…
                raise PluginCallError("output_invalid", str(error)) from None
            except ValueError:  # a result that does not fit the protocol (pydantic)
                message = "the result is not a valid MCP result"
                raise PluginCallError("protocol_error", message) from None
        if scope.cancelled_caught:
            raise CallFailed("timeout", f"the call took longer than {timeout_s:g} s")
        if crashed is not None or result is None:
            if self.state.failure is not None:
                raise CallFailed(*self.state.failure)
            process = self.state.process
            if process is not None:
                with anyio.move_on_after(_CRASH_WAIT_S):
                    while process.returncode is None:
                        await anyio.sleep(0.01)
            status = exit_status(self.state.returncode)
            raise CallFailed("plugin_crashed", f"the process {status} during the call")
        return result

    async def stop(self, *, forced: bool = False) -> None:
        """Stop the process (forced: no graceful stdin step) and wait until it is gone."""
        if forced:
            self.state.forced = True
        self._stop.set()
        with anyio.move_on_after(_STOP_WAIT_S), anyio.CancelScope(shield=True):
            await self.stopped.wait()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (ValueError, RecursionError):  # pragma: no cover (the data came from JSON)
        return "<unprintable log data>"


async def _list_tools(session: ClientSession) -> frozenset[str]:
    names: set[str] = set()
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params = None if cursor is None else mcp_types.PaginatedRequestParams(cursor=cursor)
        listing = await session.list_tools(params=params)
        names.update(tool.name for tool in listing.tools)
        if len(names) > MAX_TOOLS:
            raise ValueError(f"more than {MAX_TOOLS} tools")
        cursor = listing.next_cursor
        if cursor is None:
            return frozenset(names)
    raise ValueError("too many tools/list pages")


Factory = Callable[[Spawn, StderrCapture], PluginProcess]

__all__ = ["INPUT_META", "INTENTS_META", "PATHS_META", "CallFailed", "PluginProcess", "Spawn"]
