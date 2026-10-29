# SPDX-License-Identifier: Apache-2.0
"""The HTTP API (#37, schemas decision 51): `GET /healthz`, `GET /runs`, `GET /runs/{id}`.

Served by `horae serve` in the scheduler's process and event loop (`horae.api.serve`). It is
**read-only** and has **no authentication of its own**: the reverse proxy's SSO middleware
guards it (PLAN §7, #41), so it must never be exposed without that proxy; `horae serve`
binds to loopback by default.

- **`/healthz`**: `200` when the store can be read at the current schema version and the
  scheduler is active (or its mode is `external`), else `503`. The body is the status, the
  Horae version, the store's state (`ok` / `error`, a closed `reason`, the schema version)
  and the scheduler's `SchedulerState.to_dict()`: no path, no configuration value beyond
  that state, no exception text.
- **`/runs`**: the runs, newest first, as `horae runs --json` shows them
  (`horae.run_view.run_json`), `limit` per page (1 to `MAX_PAGE`, default `DEFAULT_PAGE`),
  filtered by `pipeline` and `status`; live skill evals only with `all=true` or their
  `pipeline`. `next_cursor` (opaque) gives the next page, `null` on the last one.
  `/runs/{id}` is one run, the same object (`404` when unknown).
- **Store access**: each request opens the store read-only, plain `mode=ro`
  (`Store.open(read_only=True, immutable=False)`: no migration, no write; a WAL reader with
  SQLite's locks, so a run that writes and checkpoints in this process never tears a read,
  and the reader never blocks it) in a worker thread, never on the event loop. `/runs` uses
  the shared worker pool; `/healthz` is asynchronous and reads the store in its own small
  executor, so a pool busy with slow `/runs` requests does not fail the health check.
- **No secrets in responses**: every response body, errors included, is redacted as data
  then as text with the process registry (`horae.secrets.redact.current()`), as the CLI's
  JSON is. Error bodies are written by Horae (`{schema_version, error: {kind, message}}`)
  and never echo a request value, a path or an exception's message.
- **Headers**: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` on every response from the app
  (uvicorn's own replies to a malformed request, before the app runs, have none); no CORS;
  the OpenAPI document and the docs pages are disabled; no slash redirect (`/runs/` is
  `404`, never a redirect).
- **Host check (DNS rebinding)**: a request whose `Host` is not an allowed host is `400
  invalid_host` before any route runs (the host is never echoed in the response). Allowed:
  `localhost`, `127.0.0.1`, `::1` and the hosts `horae serve` is given (`--allowed-host`,
  the proxy's public hostname), any port. A web page that rebinds its own name to 127.0.0.1
  sends its own name as `Host`, so it cannot read the API from the user's browser. Each
  refused `Host` value is logged once (warning, `horae.api`; sanitised, truncated, at most
  `MAX_LOGGED_HOSTS` distinct values), so a misconfigured proxy is visible in the logs even
  though the loopback `HEALTHCHECK` stays green.
"""

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from horae import __version__
from horae.apply.render import dumps
from horae.run_view import RunStatusFilter, run_json, stale_flags
from horae.scheduler import SchedulerState
from horae.secrets.redact import current as current_redactor
from horae.store import (
    SchemaTooNewError,
    SchemaTooOldError,
    Store,
    StoreError,
)

logger = logging.getLogger("horae.api")

API_SCHEMA_VERSION = 1
"""`schema_version` of every API document."""
DEFAULT_PAGE = 20
MAX_PAGE = 100
"""The largest `limit` of `/runs`."""
MAX_CURSOR = 512
"""The longest cursor accepted (characters)."""
MAX_LOGGED_HOSTS = 32
"""How many distinct refused `Host` values are logged (then one last line says it stops)."""
MAX_LOGGED_HOST_CHARS = 64
"""How much of a refused `Host` value is logged."""
INVALID_HOST_MESSAGE = "host not allowed (see HORAE_ALLOWED_HOSTS)"

SECURITY_HEADERS: dict[str, str] = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}

StoreReason = Literal["missing", "schema_too_old", "schema_too_new", "unreadable"]
_PARAMETERS = frozenset({"limit", "cursor", "pipeline", "status", "all", "run_id"})
"""The parameter names a validation error may name."""
_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    400: ("invalid_request", "bad request"),
    404: ("not_found", "not found"),
    405: ("method_not_allowed", "method not allowed"),
}


class RedactedJSON(Response):
    """A JSON response, redacted (data, then text) and with the security headers."""

    media_type = "application/json"

    def __init__(
        self, content: Any, status_code: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(content, status_code, {**SECURITY_HEADERS, **(headers or {})})

    def render(self, content: Any) -> bytes:
        redactor = current_redactor()
        scrubbed, _ = redactor.scrub_data(content)
        text = dumps(scrubbed, ensure_ascii=True, separators=(",", ":"))
        return redactor.redact_text(text)[0].encode("ascii")


def _error(status: int, kind: str, message: str) -> RedactedJSON:
    body = {"schema_version": API_SCHEMA_VERSION, "error": {"kind": kind, "message": message}}
    return RedactedJSON(body, status)


class SecurityHeaders:
    """Adds `SECURITY_HEADERS` to every HTTP response from the app (also those Horae did not
    build, such as Starlette's own errors; not uvicorn's replies to a request it could not
    parse, which never reach the app)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in SECURITY_HEADERS.items()
                    if name.encode("latin-1") not in names
                ]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, with_headers)


def encode_cursor(started_at: datetime, run_id: str) -> str:
    """The opaque cursor of the page after the run (`started_at`, `run_id`)."""
    raw = json.dumps([started_at.isoformat(), run_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


_CURSOR_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")
"""A start time as `encode_cursor` writes it (the store's times are UTC)."""


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """`(started_at, run_id)`, or `None` for a cursor this API did not make.

    Total: whatever `cursor` holds, it returns or gives `None`, never raises. Only the exact
    shape `encode_cursor` writes is accepted (canonical base64url, a two-string JSON array,
    a UTC time in `isoformat`'s form, a run id of 1 to 128 characters, re-encoding to the
    same cursor), so no time zone arithmetic can overflow later."""
    try:
        if not cursor.isascii():
            return None
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"))
        if not (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(part, str) for part in value)
            and 1 <= len(value[1]) <= 128
            and _CURSOR_TIME.match(value[0])
        ):
            return None
        started = datetime.fromisoformat(value[0])
        if encode_cursor(started, value[1]) != cursor:
            return None
    except (binascii.Error, ValueError, OverflowError, TypeError, RecursionError):
        return None
    return started, value[1]


DEFAULT_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""The `Host` values always accepted (loopback: the container's HEALTHCHECK, a local client)."""
_HOSTNAME = re.compile(
    r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$"
)
"""DNS labels, `_` allowed too (Docker service and container names: `horae_app`)."""


def allowed_host(value: str) -> str:
    """`value` as an allowed host (lower case, no trailing dot, an IPv6 address without its
    brackets), or `ValueError`: a DNS name (`_` allowed, as in Docker service names) or an
    IP address, no port, wildcard or path."""
    host = value.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.removesuffix(".")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if len(host) > 253 or not _HOSTNAME.match(host):
        raise ValueError("an allowed host is a DNS name or an IP address (no port, no wildcard)")
    return host


def host_of(header: str | None) -> str | None:
    """The host of a `Host` header (lower case, port and trailing dot removed, IPv6 without
    brackets), or `None` when it is missing or malformed."""
    if not header:
        return None
    value = header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[1:end], value[end + 1 :]
    else:
        host, colon, port = value.partition(":")
        rest = colon + port
    if rest and not (rest.startswith(":") and rest[1:].isdigit() and len(rest) <= 6):
        return None
    try:
        return allowed_host(host)
    except ValueError:
        return None


def _loggable(values: list[bytes]) -> str:
    """The `Host` value(s) of a refused request, safe for one log line: printable ASCII only
    (`ascii()` escapes control, bidi and other non-ASCII characters), truncated."""
    if not values:
        return "(none)"
    text = ", ".join(value.decode("latin-1") for value in values)
    cut = text[:MAX_LOGGED_HOST_CHARS]
    return ascii(cut) + ("..." if len(text) > len(cut) else "")


class HostCheck:
    """Refuses (`400 invalid_host`) an HTTP request whose `Host` is not in `allowed`, and
    logs each refused value once (at most `MAX_LOGGED_HOSTS` of them)."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed
        self._logged: set[str] = set()
        self._full = False

    def _log_refused(self, values: list[bytes]) -> None:
        # Called on the event loop only: no lock needed for the set.
        shown = _loggable(values)
        if shown in self._logged or self._full:
            return
        if len(self._logged) >= MAX_LOGGED_HOSTS:
            self._full = True
            logger.warning("api: more refused Host values; not logging them any more")
            return
        self._logged.add(shown)
        # Through the process's redacting log record factory, like every record.
        logger.warning(
            "api: refused a request for Host %s (not an allowed host: set HORAE_ALLOWED_HOSTS "
            "or --allowed-host to the proxy's public hostname)",
            shown,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            values = [v for k, v in scope.get("headers", []) if k.lower() == b"host"]
            host = host_of(values[0].decode("latin-1")) if len(values) == 1 else None
            if host is None or host not in self.allowed:
                self._log_refused(values)
                await _error(400, "invalid_host", INVALID_HOST_MESSAGE)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@dataclass(frozen=True)
class ApiContext:
    """What the API reads: the store (read-only), the state directory (the pipeline locks,
    for `stale`) and the scheduler's state; and the `Host` values it accepts besides
    `DEFAULT_ALLOWED_HOSTS` (normalised with `allowed_host`)."""

    store_path: Path
    state_dir: Path
    scheduler_state: Callable[[], SchedulerState]
    allowed_hosts: frozenset[str] = frozenset()


def _store_health(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return {"status": "error", "reason": "missing", "schema_version": None}, False
    reason: StoreReason
    try:
        with Store.open(path, read_only=True, immutable=False) as store:
            version = store.schema_version
    except SchemaTooOldError:
        reason = "schema_too_old"
    except SchemaTooNewError:
        reason = "schema_too_new"
    except (StoreError, OSError):
        reason = "unreadable"
    else:
        return {"status": "ok", "reason": None, "schema_version": version}, True
    return {"status": "error", "reason": reason, "schema_version": None}, False


_health_lock = threading.Lock()
_health_executor: ThreadPoolExecutor | None = None


def _health_pool() -> ThreadPoolExecutor:
    """The executor of `/healthz`'s store check: two threads of its own, shared by every app,
    so the check never waits for the worker pool `/runs` uses."""
    global _health_executor
    with _health_lock:
        if _health_executor is None:
            _health_executor = ThreadPoolExecutor(2, thread_name_prefix="horae-healthz")
        return _health_executor


def create_app(context: ApiContext) -> FastAPI:
    """The API's ASGI application (see the module docstring)."""
    app = FastAPI(
        title="Horae",
        version=__version__,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        default_response_class=RedactedJSON,
        redirect_slashes=False,  # `/runs/` is a 404, not a redirect to `/runs`
    )
    # The last added runs first: the headers wrap everything, the host check runs before
    # any route.
    allowed = DEFAULT_ALLOWED_HOSTS | {allowed_host(host) for host in context.allowed_hosts}
    app.add_middleware(HostCheck, allowed=allowed)
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        # Horae's words for the status, never the exception's detail.
        kind, message = _HTTP_ERRORS.get(exc.status_code, ("http_error", "request refused"))
        response = _error(exc.status_code, kind, message)
        for name, value in (exc.headers or {}).items():
            if name.lower() == "allow":  # the only header of Starlette's errors kept
                response.headers["allow"] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        # The parameters' names only, from a closed set: never a value received.
        names = sorted(
            {
                str(error["loc"][-1]) if str(error["loc"][-1]) in _PARAMETERS else "request"
                for error in exc.errors()
                if error.get("loc")
            }
            or {"request"}
        )
        return _error(400, "invalid_request", "invalid parameter: " + ", ".join(names))

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> Response:
        message = f"internal error ({type(exc).__name__})"
        del exc
        return _error(500, "internal", message)

    @app.get("/healthz")
    async def healthz() -> Response:
        # Asynchronous: the store check runs in `/healthz`'s own executor (never on the loop,
        # never queued behind `/runs` in the shared pool), and `Scheduler.state()` is read on
        # the event loop, the scheduler's own (`horae serve` runs both in one loop), so the
        # snapshot cannot interleave with the scheduler changing it.
        loop = asyncio.get_running_loop()
        store, store_ok = await loop.run_in_executor(
            _health_pool(), _store_health, context.store_path
        )
        scheduler = context.scheduler_state()
        alive = scheduler.active or scheduler.mode == "external"
        healthy = store_ok and alive
        body = {
            "schema_version": API_SCHEMA_VERSION,
            "status": "ok" if healthy else "unavailable",
            "version": __version__,
            "store": store,
            "scheduler": scheduler.to_dict(),
        }
        return RedactedJSON(body, 200 if healthy else 503)

    def _read(read: Callable[[Store], Any]) -> Any:
        if not context.store_path.is_file():
            return None
        with Store.open(context.store_path, read_only=True, immutable=False) as store:
            return read(store)

    @app.get("/runs")
    def runs(
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        cursor: Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR)] = None,
        pipeline: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        status: RunStatusFilter | None = None,
        all_runs: Annotated[bool, Query(alias="all")] = False,
    ) -> Response:
        before = None
        if cursor is not None and (before := decode_cursor(cursor)) is None:
            return _error(400, "invalid_request", "invalid parameter: cursor")
        try:
            found = _read(
                lambda store: store.list_runs(
                    pipeline=pipeline,
                    status=None if status is None else status.value,
                    limit=limit + 1,
                    evals=all_runs or pipeline is not None,
                    before=before,
                )
            )
        except (StoreError, OSError):
            return _error(503, "store_error", "the store cannot be read")
        page = (found or [])[:limit]
        more = found is not None and len(found) > limit
        next_cursor = encode_cursor(page[-1].started_at, page[-1].id) if more else None
        stale = stale_flags(context.state_dir, page)
        body = {
            "schema_version": API_SCHEMA_VERSION,
            "runs": [run_json(run, s) for run, s in zip(page, stale, strict=True)],
            "next_cursor": next_cursor,
        }
        return RedactedJSON(body)

    @app.get("/runs/{run_id}")
    def run(run_id: Annotated[str, PathParam(min_length=1, max_length=128)]) -> Response:
        try:
            found = _read(lambda store: store.run_summary(run_id))
        except (StoreError, OSError):
            return _error(503, "store_error", "the store cannot be read")
        if found is None:
            return _error(404, "not_found", "no such run")
        stale = stale_flags(context.state_dir, [found])[0]
        return RedactedJSON({"schema_version": API_SCHEMA_VERSION, "run": run_json(found, stale)})

    return app


__all__ = [
    "API_SCHEMA_VERSION",
    "DEFAULT_ALLOWED_HOSTS",
    "DEFAULT_PAGE",
    "INVALID_HOST_MESSAGE",
    "MAX_CURSOR",
    "MAX_LOGGED_HOSTS",
    "MAX_PAGE",
    "SECURITY_HEADERS",
    "ApiContext",
    "HostCheck",
    "RedactedJSON",
    "allowed_host",
    "create_app",
    "decode_cursor",
    "encode_cursor",
    "host_of",
]
