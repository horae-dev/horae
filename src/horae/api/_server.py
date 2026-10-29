# SPDX-License-Identifier: Apache-2.0
"""Serving the API in `horae serve`'s event loop (#37, schemas decision 51 A).

uvicorn's programmatic `Server`, with three changes from its defaults:

- **Signals are `horae serve`'s.** uvicorn would install its own SIGTERM / SIGINT handlers
  (and re-raise the signal when it stops); `horae serve` owns them (the two-step stop of
  decision 49 E2) and asks the server to stop (`should_exit`, then `force_exit`).
- **The socket is bound by Horae** (`bind`) before anything starts, so a port in use or a
  bad host is a configuration error with Horae's message (uvicorn calls `sys.exit` itself).
- **Quiet and minimal.** No access log (scheduler events stay readable; the proxy logs
  requests), uvicorn's log configuration is not applied (Horae's redacting log record
  factory stays in place), no `Server` header, no proxy-header rewriting, no lifespan.
"""

import contextlib
import socket
from collections.abc import Iterator

import uvicorn
from starlette.types import ASGIApp

GRACEFUL_SHUTDOWN_S = 5.0
"""How long the server waits for requests in progress when it stops."""


class BindError(Exception):
    """The API's socket could not be bound (the message is Horae's: host, port, reason)."""


class ApiServer(uvicorn.Server):
    """`uvicorn.Server` on a socket Horae bound, leaving the process's signal handlers alone."""

    def __init__(self, config: uvicorn.Config, sock: socket.socket) -> None:
        super().__init__(config)
        self.sock = sock

    @property
    def address(self) -> tuple[str, int]:
        """The host and port it listens on."""
        host, port = self.sock.getsockname()[:2]
        return str(host), int(port)

    async def run_until_stopped(self) -> None:
        """Serve until `request_stop` (closes the socket when it ends)."""
        try:
            await self.serve(sockets=[self.sock])
        finally:
            self.sock.close()

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def request_stop(self, *, force: bool = False) -> None:
        """Stop accepting connections and end (`force`: without waiting for requests)."""
        self.should_exit = True
        if force:
            self.force_exit = True


def bind(host: str, port: int) -> socket.socket:
    """A listening TCP socket on `host:port` (IPv6 when `host` holds a `:`), or `BindError`."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        sock = socket.create_server((host, port), family=family, backlog=64)
    except (OSError, OverflowError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        raise BindError(f"cannot listen on {host}:{port}: {reason}") from None
    sock.set_inheritable(False)
    return sock


def build_server(app: ASGIApp, sock: socket.socket) -> ApiServer:
    """The server for `app` on the bound socket `sock` (`run_until_stopped`)."""
    host, port = sock.getsockname()[:2]
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        lifespan="off",
        timeout_graceful_shutdown=int(GRACEFUL_SHUTDOWN_S),
    )
    return ApiServer(config, sock)


__all__ = ["GRACEFUL_SHUTDOWN_S", "ApiServer", "BindError", "bind", "build_server"]
