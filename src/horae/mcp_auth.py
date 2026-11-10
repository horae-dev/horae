# SPDX-License-Identifier: Apache-2.0
"""Bearer-token authentication of the MCP server's HTTP transport (#56, schemas decision 75).

**Tokens.** `hmcp_` followed by 32 random bytes (`secrets.token_urlsafe`, 256 bits), 48
characters in all. The prefix makes a leaked token recognisable (secret scanners, the log
scrubber of `horae.secrets.logs`). A token is printed once, by `horae mcp token create`, and
never stored: the store keeps its **digest**, `SHA-256("horae-mcp-token/v1\\0" || token)`
(`token_digest`), with a name, the creation time, an optional expiry, the last use, a
revocation time and its scopes (`horae.store.McpTokenRow`, migration 9).

**Why a plain SHA-256.** A password hash (argon2, scrypt, bcrypt) slows down guessing a
low-entropy secret from its digest. A token is 256 uniformly random bits: inverting its
SHA-256 is a 2^256 search however fast one guess is, so a slow hash adds nothing but a
per-request cost that anyone able to send requests could use to load the server. The label
separates this digest from any other SHA-256 of the same bytes.

**Verification** (`match`). Every request reads every token row (revoked and expired ones
included), computes the digest of what it was given (of a fixed dummy when that is not a
well-formed token, so the work is the same) and compares it with **every** row's digest with
`hmac.compare_digest`, without stopping at a match. Only then are the result's revocation and
expiry looked at. A missing, malformed, unknown, wrong, revoked or expired token therefore
costs the same work and gets the **same answer** (refusals are logged as a count, at most
one line a minute: `RefusalLog`): `401`, `{"error": "invalid_token"}`, the
same `WWW-Authenticate` header (`TokenAuth`). Revocation takes effect on the next request:
nothing is cached.

**Scopes** (`SCOPES`). `read`: the read-only tools (`tasks.today`, `brief.latest`,
`runs.recent`). `pipeline.run`: the tool `pipeline.run` (#55, decision 77), which lets a client
*ask* for a pipeline's dry run and an approval; it never approves anything (the approval is
the user's, at the instance's terminal: `horae approval approve`). A token gets
`read` unless told otherwise. A tool checks its scope with `require_scope`.

**stdio needs no token.** `horae mcp serve` over stdio is a child process of the user's own
MCP client: whoever can start it can already read the store and run `horae run`, so the
operating system has authenticated the caller. It is granted `STDIO_SCOPES` (every scope);
the approval of #55 still gates anything that changes state. A token is required on every
network transport, and the HTTP listener refuses to start while no usable token exists.

**Nothing logs a token.** The middleware never logs the header, uvicorn's access log is off,
and the log record factory replaces anything shaped like a token (`hmcp_…`) as a last resort.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio.to_thread
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from starlette.authentication import AuthCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from horae.store import HEARTBEAT_BUSY_TIMEOUT_MS, McpTokenRow, Store, StoreError

logger = logging.getLogger("horae.mcp")

TOKEN_PREFIX = "hmcp_"  # noqa: S105 - a prefix, not a secret
TOKEN_BYTES = 32
"""Random bytes in a token (256 bits)."""
TOKEN_CHARS = len(TOKEN_PREFIX) + 43
"""A token's length: the prefix and 32 bytes of unpadded base64url."""
_TOKEN = re.compile(r"hmcp_[A-Za-z0-9_-]{43}")
DIGEST_LABEL = b"horae-mcp-token/v1\0"
_DUMMY = TOKEN_PREFIX + "A" * 43
"""Digested in place of a malformed token, so a malformed one costs the same work."""

SCOPE_READ = "read"
SCOPE_PIPELINE_RUN = "pipeline.run"
SCOPES: frozenset[str] = frozenset({SCOPE_READ, SCOPE_PIPELINE_RUN})
DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_READ,)
STDIO_SCOPES: frozenset[str] = SCOPES
"""What a stdio session may do: everything its user may (decision 75 B)."""

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
"""A token's name: 1 to 64 characters, letters, digits, `.`, `_`, `-`; not a secret."""
MAX_EXPIRES_DAYS = 3650
TOUCH_INTERVAL = timedelta(minutes=1)
"""`last_used_at` is written at most this often per token (a write per request is waste)."""

INVALID_TOKEN_BODY = (
    b'{"error":"invalid_token","error_description":"a valid bearer token is required"}'
)
UNAVAILABLE_BODY = b'{"error":"temporarily_unavailable"}'
_401_HEADERS = [
    (b"content-type", b"application/json"),
    (b"cache-control", b"no-store"),
    (b"www-authenticate", b'Bearer realm="horae", error="invalid_token"'),
]


def new_token() -> str:
    """A fresh token: `hmcp_` and 32 random bytes as unpadded base64url."""
    raw = secrets.token_bytes(TOKEN_BYTES)
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def well_formed(token: str) -> bool:
    """Whether `token` has a token's shape (prefix, length, alphabet)."""
    return _TOKEN.fullmatch(token) is not None


def token_digest(token: str) -> bytes:
    """What the store keeps of a token: `SHA-256(DIGEST_LABEL || token)`, 32 bytes."""
    return hashlib.sha256(DIGEST_LABEL + token.encode("utf-8")).digest()


def match(rows: Iterable[McpTokenRow], presented: str, now: datetime) -> McpTokenRow | None:
    """The usable row whose digest is `presented`'s, else `None`; the same work whether the
    token is malformed, unknown, revoked or expired (see the module's docstring)."""
    good = well_formed(presented)
    digest = token_digest(presented if good else _DUMMY)
    found: McpTokenRow | None = None
    for row in rows:
        if hmac.compare_digest(row.token_sha256, digest):
            found = row
    if found is None or not good:
        return None
    if found.revoked_at is not None:
        return None
    if found.expires_at is not None and found.expires_at <= now:
        return None
    return found


def usable(row: McpTokenRow, now: datetime) -> bool:
    """Whether `row` is neither revoked nor expired at `now`."""
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


def status(row: McpTokenRow, now: datetime) -> str:
    """`active`, `expired` or `revoked`."""
    if row.revoked_at is not None:
        return "revoked"
    return "active" if usable(row, now) else "expired"


@dataclass(frozen=True)
class Principal:
    """Who a request authenticated as: a token's id, name and scopes (never the token)."""

    token_id: int
    name: str
    scopes: frozenset[str]
    expires_at: datetime | None


class TokenVerifier:
    """Checks a presented token against the store at `store_path`, read on every call."""

    def __init__(
        self, store_path: Path, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.store_path, self.now = store_path, now

    def _rows(self) -> list[McpTokenRow]:
        if not self.store_path.exists():
            return []
        # `immutable=False`: a WAL reader, so a revocation committed a moment ago is seen.
        with Store.open(self.store_path, read_only=True, immutable=False) as store:
            return store.mcp_tokens()

    def usable_count(self) -> int:
        """How many tokens could authenticate now (raises `StoreError`)."""
        now = self.now()
        return sum(usable(row, now) for row in self._rows())

    def verify(self, presented: str) -> Principal | None:
        """The principal of `presented`, or `None`. Raises `StoreError` when the store cannot
        be read (the caller fails closed)."""
        now = self.now()
        row = match(self._rows(), presented, now)
        if row is None:
            return None
        if row.last_used_at is None or now - row.last_used_at >= TOUCH_INTERVAL:
            try:
                with Store.open(
                    self.store_path, busy_timeout_ms=HEARTBEAT_BUSY_TIMEOUT_MS
                ) as store:
                    store.mcp_token_touch(row.id, at=now)
            except (StoreError, OSError):  # a busy store only skips a last-used update
                logger.warning("the last use of an MCP token could not be recorded")
        return Principal(row.id, row.name, frozenset(row.scopes), row.expires_at)


def _bearer(scope: Scope) -> str:
    """The token of the request's one `Authorization: Bearer` header, else `""`."""
    values = [v for k, v in scope.get("headers", ()) if k.lower() == b"authorization"]
    if len(values) != 1:
        return ""
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError:
        return ""
    scheme, _, token = text.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _user(principal: Principal) -> AuthenticatedUser:
    expires = None if principal.expires_at is None else int(principal.expires_at.timestamp())
    return AuthenticatedUser(
        AccessToken(
            token="[REDACTED:mcp_token]",  # noqa: S106 - a placeholder: the token stays out
            client_id=f"mcp-token-{principal.token_id}",
            scopes=sorted(principal.scopes),
            expires_at=expires,
            subject=principal.name,
        )
    )


async def _reply(send: Send, status_code: int, body: bytes, headers: list[Any]) -> None:
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


REFUSAL_LOG_INTERVAL_S = 60.0
"""Refused requests are logged as one summary line at most this often."""


class RefusalLog:
    """Logs refused requests as a count, at most one INFO line per `interval` seconds, so a
    client flooding bad tokens cannot flood the log (one line for the first refusal, then
    one per interval with how many were refused since the last line). Called on the event
    loop only."""

    def __init__(
        self,
        interval: float = REFUSAL_LOG_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval, self.clock = interval, clock
        self.pending = 0
        self.last: float | None = None

    def refused(self) -> None:
        self.pending += 1
        now = self.clock()
        if self.last is None or now - self.last >= self.interval:
            logger.info(
                "%d MCP request(s) without a valid token refused since the last report",
                self.pending,
            )
            self.pending, self.last = 0, now


class TokenAuth:
    """ASGI middleware: every HTTP request needs a valid token, else `401 invalid_token`
    (one answer for every reason) before the app sees it; a store that cannot be read is
    `503` (fail closed). The authenticated request carries `scope["user"]` (the SDK's
    `AuthenticatedUser`, which binds an MCP session to its token) and `scope["auth"]` (its
    scopes). Lifespan events pass; a WebSocket is closed."""

    def __init__(
        self, app: ASGIApp, verifier: TokenVerifier, refusals: RefusalLog | None = None
    ) -> None:
        self.app, self.verifier = app, verifier
        self.refusals = refusals or RefusalLog()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        presented = _bearer(scope)
        try:
            principal = await anyio.to_thread.run_sync(self.verifier.verify, presented)
        except (StoreError, OSError):
            logger.warning("the MCP token store could not be read; request refused")
            headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
            await _reply(send, 503, UNAVAILABLE_BODY, headers)
            return
        del presented
        if principal is None:
            self.refusals.refused()
            await _reply(send, 401, INVALID_TOKEN_BODY, _401_HEADERS)
            return
        inner = dict(scope)
        inner["user"] = _user(principal)
        inner["auth"] = AuthCredentials(sorted(principal.scopes))
        await self.app(inner, receive, send)


def granted_scopes(request: object | None, *, stdio: bool) -> frozenset[str]:
    """The scopes of the request a tool serves. Over stdio, `STDIO_SCOPES`. Over HTTP, its
    token's (the `AuthenticatedUser` `TokenAuth` put in the request's scope); none when there
    is no such user (fail closed: the transport, not the request's presence, decides)."""
    if stdio:
        return STDIO_SCOPES
    user = getattr(request, "scope", {}).get("user") if request is not None else None
    if isinstance(user, AuthenticatedUser):
        return frozenset(user.scopes)
    return frozenset()


_CLIENT_ID = re.compile(r"mcp-token-([0-9]{1,18})")


def requester(request: object | None, *, stdio: bool) -> tuple[int | None, str]:
    """Who a tool call comes from, for `pipeline.run`'s approvals: `(None, "stdio")` over
    stdio, else the token's row id and name (from the `AuthenticatedUser` `TokenAuth` set).
    Raises the tool error `insufficient_scope` when an HTTP call has no such user."""
    if stdio:
        return None, "stdio"
    user = getattr(request, "scope", {}).get("user") if request is not None else None
    if isinstance(user, AuthenticatedUser):
        found = _CLIENT_ID.fullmatch(user.access_token.client_id)
        if found is not None:
            return int(found.group(1)), user.access_token.subject or found.group(0)
    raise ToolError("insufficient_scope: an authenticated token is required")


def require_scope(request: object | None, needed: str, *, stdio: bool) -> None:
    """Raise the tool error `insufficient_scope` unless the request was granted `needed`."""
    if needed not in granted_scopes(request, stdio=stdio):
        raise ToolError(f"insufficient_scope: the scope {needed} is required")


__all__ = [
    "DEFAULT_SCOPES",
    "DIGEST_LABEL",
    "MAX_EXPIRES_DAYS",
    "NAME",
    "REFUSAL_LOG_INTERVAL_S",
    "SCOPES",
    "SCOPE_PIPELINE_RUN",
    "SCOPE_READ",
    "STDIO_SCOPES",
    "TOKEN_BYTES",
    "TOKEN_CHARS",
    "TOKEN_PREFIX",
    "Principal",
    "RefusalLog",
    "TokenAuth",
    "TokenVerifier",
    "granted_scopes",
    "match",
    "new_token",
    "requester",
    "require_scope",
    "status",
    "token_digest",
    "usable",
    "well_formed",
]
