# SPDX-License-Identifier: Apache-2.0
"""The stdio transport: one plugin process speaking newline-delimited JSON-RPC (#24).

The SDK's `stdio_client` is not used (PR #85 answer 2): it merges the child environment with
`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` from the host and writes stderr to a
file object. Here the process is spawned with:

- an **argument list** (`anyio.open_process` with a sequence: no shell, ever), whose first
  element the caller resolved to an absolute path;
- **exactly** the given environment, and the given (empty) working directory;
- `start_new_session=True`, so the plugin and everything it starts form one process group;
- stdin, stdout and stderr as pipes: stdout carries the JSON-RPC messages, at most
  `MAX_MESSAGE_BYTES` (8 MiB) each (`output_too_large` otherwise), stderr is drained into a
  `StderrCapture` as it arrives.

A line that is not a JSON-RPC message is a `protocol_error`. Either failure is recorded in
`ProcessState.failure` and ends the read stream, so the session's pending requests fail at
once (the caller then kills the process).

**Shutdown** (MCP lifecycle): close stdin, wait `GRACE_S` (2 s) for the process to exit, then
`SIGTERM` the process group, wait `GRACE_S`, then `SIGKILL` it. A forced stop (after a timeout
or a crash) skips the stdin step. Once the leader is gone, the group gets a final `SIGKILL`,
so nothing the plugin started outlives it. Every wait is bounded and runs shielded from
cancellation, so a cancelled run cannot leak a process.
"""

import os
import signal
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import anyio
import mcp_types
from anyio.abc import Process
from mcp.client._transport import TransportStreams
from mcp.shared.message import SessionMessage

from horae.plugins.host.capture import StderrCapture
from horae.plugins.host.errors import HostErrorKind, PluginCallError

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
"""The largest JSON-RPC message a plugin may write on stdout."""
GRACE_S = 2.0
"""Each step of the shutdown escalation waits this long."""
_POLL_S = 0.01
_STDERR_DRAIN_S = 1.0


@dataclass
class ProcessState:
    """What the transport saw of its process."""

    process: Process | None = None
    failure: tuple[HostErrorKind, str] | None = None
    """Why the transport stopped reading (`output_too_large`, `protocol_error`)."""
    forced: bool = False
    """Stop without the graceful stdin step."""
    stdout_closed: bool = False

    @property
    def returncode(self) -> int | None:
        return None if self.process is None else self.process.returncode


def exit_status(returncode: int | None) -> str:
    """`exited with code N`, `killed by signal NAME`, or `still running`."""
    if returncode is None:
        return "still running"
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = str(-returncode)
        return f"killed by signal {name}"
    return f"exited with code {returncode}"


async def _wait_exit(process: Process, timeout: float) -> bool:
    """Whether the process exited within `timeout` (polls `returncode`: `wait()` would also
    wait for the pipes, which a grandchild may hold open)."""
    with anyio.move_on_after(timeout):
        while process.returncode is None:
            await anyio.sleep(_POLL_S)
    return process.returncode is not None


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def terminate(process: Process, *, graceful: bool) -> None:
    """Stop the process and its group (see the module docstring). Never raises."""
    pgid = process.pid  # the leader of its own session: pgid == pid
    if graceful and process.stdin is not None:
        with suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            await process.stdin.aclose()
        if await _wait_exit(process, GRACE_S):
            _signal_group(pgid, signal.SIGKILL)
            return
    if process.returncode is None:
        _signal_group(pgid, signal.SIGTERM)
        if not await _wait_exit(process, GRACE_S):
            _signal_group(pgid, signal.SIGKILL)
            await _wait_exit(process, GRACE_S)
    _signal_group(pgid, signal.SIGKILL)


@asynccontextmanager
async def open_stdio(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    capture: StderrCapture,
    state: ProcessState,
    max_message: int = MAX_MESSAGE_BYTES,
) -> AsyncIterator[TransportStreams]:
    """Spawn the plugin and yield the session's (read, write) streams; stop it on exit.

    Raises `PluginCallError("spawn_failed")` when the process cannot be started."""
    try:
        process = await anyio.open_process(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        reason = error.strerror if isinstance(error, OSError) and error.strerror else "invalid"
        raise PluginCallError("spawn_failed", f"cannot start {argv[0]}: {reason}") from None
    state.process = process
    read_send, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_receive = anyio.create_memory_object_stream[SessionMessage](0)
    stderr_done = anyio.Event()

    def fail(kind: HostErrorKind, message: str) -> None:
        if state.failure is None:
            state.failure = (kind, message)

    async def stdout_reader() -> None:
        assert process.stdout is not None  # noqa: S101 (spawned with pipes)
        buffer = bytearray()
        scanned = 0
        async with read_send:
            try:
                async for chunk in process.stdout:
                    buffer += chunk
                    while True:
                        newline = buffer.find(b"\n", scanned)
                        if newline < 0:
                            scanned = len(buffer)
                            if len(buffer) > max_message:
                                fail("output_too_large", f"a message over {max_message} bytes")
                                return
                            break
                        line, scanned = bytes(buffer[:newline]), 0
                        del buffer[: newline + 1]
                        if len(line) > max_message:
                            fail("output_too_large", f"a message over {max_message} bytes")
                            return
                        if not line.strip():
                            continue
                        try:
                            message = mcp_types.jsonrpc_message_adapter.validate_json(
                                line, by_name=False
                            )
                        except ValueError:
                            fail("protocol_error", "the plugin wrote a line that is not JSON-RPC")
                            return
                        await read_send.send(SessionMessage(message))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                pass
            finally:
                state.stdout_closed = True

    async def stderr_reader() -> None:
        assert process.stderr is not None  # noqa: S101
        try:
            async for chunk in process.stderr:
                capture.feed(chunk)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            pass
        finally:
            stderr_done.set()

    async def stdin_writer() -> None:
        assert process.stdin is not None  # noqa: S101
        try:
            async with write_receive:
                async for item in write_receive:
                    data = item.message.model_dump_json(by_alias=True, exclude_unset=True)
                    await process.stdin.send(data.encode("utf-8") + b"\n")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_send.aclose()

    async with anyio.create_task_group() as group:
        group.start_soon(stdout_reader)
        group.start_soon(stderr_reader)
        group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            with anyio.CancelScope(shield=True):
                write_stream.close()
                read_stream.close()
                await terminate(process, graceful=not state.forced)
                with anyio.move_on_after(_STDERR_DRAIN_S):
                    await stderr_done.wait()
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        with suppress(OSError, anyio.BrokenResourceError):
                            await pipe.aclose()
                _close_transport(process)
            group.cancel_scope.cancel()


def _close_transport(process: Process) -> None:
    """Close asyncio's subprocess transport now (not at garbage collection, when the event
    loop may be closed). Nothing public exposes it; `mcp.client.stdio` does the same."""
    transport = getattr(getattr(process, "_process", None), "_transport", None)
    close = getattr(transport, "close", None)
    if callable(close):
        with suppress(PermissionError, ProcessLookupError):
            close()


__all__ = [
    "GRACE_S",
    "MAX_MESSAGE_BYTES",
    "ProcessState",
    "exit_status",
    "open_stdio",
    "terminate",
]
