# SPDX-License-Identifier: Apache-2.0
"""The `/act/{token}` routes (#38, schemas decision 54 G to K).

**Exactly `/act/{token}`**, `GET` and `POST` (`HEAD` and the other methods are 405): the
token is one path segment (Starlette's default converter, which stops at `/`); no path
converter, no mount, no prefix router under `/act`, so `%2F`, `..` or a trailing segment can
never reach another route through the prefix the reverse proxy exempts from SSO (PLAN §7;
`horae.api._app` turns every other 404 under `/act` into the same page). The routes read no
SSO header and set or read no cookie: the token is the only credential. The `Host` check
(#37) still applies.

In order, for both methods: the rate limit (`horae.api.act._limit`, before any decoding or
store access: 429 with `Retry-After`), `/act` disabled (no key, no `http.public_url` or no
route: 503), then for `POST` the origin check (an `Origin` header, when present, must be
`http.public_url`'s origin; a `Sec-Fetch-Site` header, when present, must be `same-origin` or
`none`; the ntfy app's native client sends neither), then the service. Every refusal of a token
is the same 404 page and costs `INVALID_COST` in the rate limit.

**Logs.** One line per request (`horae.api.act`): `act <method> <kind> <ref> <outcome>`, where
`ref` is the first 8 hex digits of SHA-256(nonce), only for a token whose tag verified; an
unverified string is `act <method> invalid len=<n>`. Never the token, the URL or the task.
"""

import logging
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, Response

from horae.api.act import _page as page
from horae.api.act._limit import INVALID_COST, IpNetwork, RateLimiter, client_ip
from horae.api.act._service import ActService, Result

logger = logging.getLogger("horae.api.act")

_SAME_SITE = frozenset({"same-origin", "none"})


def act_response(shown: page.Page) -> Response:
    """`shown` as an HTML response with the `/act` headers."""
    headers = dict(page.PAGE_HEADERS)
    if shown.retry_after is not None:
        headers["retry-after"] = str(shown.retry_after)
    return HTMLResponse(shown.body, shown.status, headers=headers)


def is_act_path(path: str) -> bool:
    """Whether a request path is under `/act` (its 404s are the `/act` page)."""
    return path == "/act" or path.startswith("/act/")


def _origin(url: str) -> str:
    return url.rstrip("/").lower()


def origin_allowed(origin: list[str], fetch_site: list[str], public_url: str) -> bool:
    """Whether a `POST`'s `Origin` and `Sec-Fetch-Site` headers (all values) are acceptable."""
    if len(origin) > 1 or len(fetch_site) > 1:
        return False
    if origin and _origin(origin[0]) != _origin(public_url):
        return False
    return not (fetch_site and fetch_site[0].strip().lower() not in _SAME_SITE)


@dataclass(frozen=True)
class ActSetup:
    """What the `/act` routes need: the service (`None`: `/act` is disabled, every request
    503), the public URL (`http.public_url`), the rate limiter and the trusted proxies."""

    service: ActService | None = None
    public_url: str | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    trusted_proxies: tuple[IpNetwork, ...] = ()


def add_act_routes(app: FastAPI, setup: ActSetup) -> None:
    """Add `GET` and `POST /act/{token}` to `app` (see the module docstring)."""
    service, public_url = setup.service, setup.public_url
    limiter, trusted_proxies = setup.limiter, setup.trusted_proxies

    def client(request: Request) -> str:
        peer = request.client.host if request.client is not None else None
        return client_ip(peer, request.headers.getlist("x-forwarded-for"), trusted_proxies)

    def log(method: str, token: str, result: Result) -> None:
        if result.token is None:
            logger.info("act %s invalid len=%d", method, len(token))
        else:
            parsed = result.token
            logger.info("act %s %s %s %s", method, parsed.kind, parsed.ref, result.outcome)

    def limited(who: str) -> Response | None:
        wait = limiter.acquire(who)
        if wait is None:
            return None
        return act_response(page.Page(429, page.TOO_MANY.body, wait))

    def finish(method: str, token: str, who: str, result: Result) -> Response:
        if result.page.status == page.NOT_VALID.status:
            limiter.charge(who, INVALID_COST - 1)
        log(method, token, result)
        return act_response(result.page)

    @app.get("/act/{token}", include_in_schema=False)
    async def act_get(request: Request, token: str) -> Response:
        who = client(request)
        if (refused := limited(who)) is not None:
            return refused
        if service is None:
            return act_response(page.UNAVAILABLE)
        try:
            result = await run_in_threadpool(service.preview, token)
        except Exception as exc:
            logger.warning("act GET failed (%s)", type(exc).__name__)
            return act_response(page.UNAVAILABLE)
        return finish("GET", token, who, result)

    @app.post("/act/{token}", include_in_schema=False)
    async def act_post(request: Request, token: str) -> Response:
        who = client(request)
        if (refused := limited(who)) is not None:
            return refused
        if service is None or public_url is None:
            return act_response(page.UNAVAILABLE)
        headers = request.headers
        if not origin_allowed(
            headers.getlist("origin"), headers.getlist("sec-fetch-site"), public_url
        ):
            limiter.charge(who, INVALID_COST - 1)
            logger.info("act POST refused: cross-origin")
            return act_response(page.NOT_VALID)
        try:
            result = await service.act(token)
        except Exception as exc:
            logger.warning("act POST failed (%s)", type(exc).__name__)
            return act_response(page.UNAVAILABLE)
        return finish("POST", token, who, result)


__all__ = ["ActSetup", "act_response", "add_act_routes", "is_act_path", "origin_allowed"]
