# SPDX-License-Identifier: Apache-2.0
"""Push monitors: one ping, `up` or `down`, after each run (#39, schemas decision 49 F).

`horae.yaml` `scheduler.monitors.<pipeline>` names the **secret** that holds the push URL
(an Uptime Kuma push URL carries the monitor's token, a Healthchecks URL is the check's
UUID): it is resolved like any secret and never written in configuration, logs or output.

The ping follows the network posture of the LLM backends (schemas decision 40):

- one `GET`, no retry, no redirect followed, no ambient proxy, `.netrc` or `SSL_CERT_*`
  (`trust_env=False` on the client and its transport), TLS verified on the system store;
- the URL must be a `BackendUrl` (decision 24): `https://` to any host, `http://` only to a
  loopback, private or single-label host, and for `http://` every resolved address is checked
  when connecting (`LanOnlyNetwork`: loopback, RFC 1918 or IPv6 ULA, no rebinding);
- bounded: `PING_TIMEOUT_S` for the whole exchange, the body is never read beyond
  `PING_RESPONSE_CAP` bytes (`BoundedTransport`) and only the status is used;
- quiet: a failure is logged as a warning in Horae's own words (the pipeline and a closed
  reason, never the URL, the response or the exception's message) and **never fails the
  run**: `ping` does not raise (except a cancellation).

`up` is a run that finished `ok`, `noop` or `degraded`; anything else (`failed`, a timeout, a
lost or crashed run, an interrupted one, a store or lock problem) is `down`. The message is
the outcome (a closed set of words, no data).
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import httpx2

from horae import __version__
from horae.config import Instance
from horae.contracts.instance import PushMonitor, is_push_url
from horae.llm.backends._shared import RESPONSE_CAP, Refused, Unreadable, transport
from horae.secrets import SecretError, logs

logger = logging.getLogger("horae.scheduler.monitor")

PING_TIMEOUT_S = 10.0
"""Seconds one ping may take, connection included."""
PING_RESPONSE_CAP = 64 * 1024
"""Largest response body accepted (it is not used: only the status is)."""
UP_STATUSES = frozenset({"ok", "noop", "degraded"})
"""Run outcomes pinged `up`; every other outcome is `down`."""

PingResult = Literal["none", "sent", "failed"]
"""`none`: the pipeline has no monitor; `sent`: the monitor answered 2xx; `failed`: see the
warning logged."""


@dataclass(frozen=True)
class RunOutcome:
    """How one run ended, for its push monitor and the scheduler's state."""

    status: str
    """The run's status (`ok`, `noop`, `degraded`, `failed`) or what stopped it (`timeout`,
    `cancelled`, `run_lost`, `internal`, an error kind)."""
    run_id: str | None = None

    @property
    def up(self) -> bool:
        return self.status in UP_STATUSES


def ping_url(url: str, monitor: PushMonitor, outcome: RunOutcome, duration_s: float) -> str:
    """The URL pinged for `outcome` (see `horae.contracts.instance.MonitorKind`)."""
    parsed = httpx2.URL(url)
    if monitor.kind == "healthchecks":
        if outcome.up:
            return str(parsed)
        return str(parsed.copy_with(path=parsed.path.rstrip("/") + "/fail"))
    milliseconds = max(0, round(duration_s * 1000))
    return str(
        parsed.copy_set_param("status", "up" if outcome.up else "down")
        .copy_set_param("msg", outcome.status)
        .copy_set_param("ping", str(milliseconds))
    )


Reason = Literal[
    "timeout",
    "refused",
    "unreadable",
    "connection",
    "unresolved",
    "not_a_push_url",
    "status",
    "error",
]
"""Why a ping failed: a closed set, logged through `REASONS` (never the URL, the response or
an exception's message)."""
REASONS: Mapping[Reason, str] = {
    "timeout": "timed out",
    "refused": "refused: the http:// host does not resolve to a LAN address",
    "unreadable": "an unreadable or oversized response",
    "connection": "connection failed",
    "unresolved": "its secret could not be resolved",
    "not_a_push_url": "its secret is not an https:// URL or an http:// URL on the LAN",
    "status": "the monitor answered an error status",
    "error": "an unexpected error",
}


@dataclass(frozen=True)
class _Failure:
    reason: Reason
    status: int = 0
    """The HTTP status, for `status`."""


def _reason(error: Exception) -> Reason:
    """The closed reason of a failed exchange (never the exception's message)."""
    if isinstance(error, httpx2.TimeoutException | TimeoutError):
        return "timeout"
    if isinstance(error, Refused):
        return "refused"
    if isinstance(error, Unreadable):
        return "unreadable"
    if isinstance(error, httpx2.TransportError):
        return "connection"
    return "error"


def _describe(failure: _Failure) -> str:
    """`failure` in Horae's own words: a fixed text, and the status code for `status`."""
    text = REASONS[failure.reason]
    return f"{text} {failure.status:d}" if failure.reason == "status" else text


async def ping(
    instance: Instance,
    pipeline: str,
    outcome: RunOutcome,
    duration_s: float,
    *,
    inner: httpx2.AsyncBaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
) -> PingResult:
    """Ping `pipeline`'s push monitor, if it has one, with `outcome`. Never raises (but a
    cancellation): a failure is logged and returned as `failed`. `inner` replaces the HTTP
    transport (tests); `environ` is read only for an `{env: VAR}` secret reference."""
    monitor = instance.config.scheduler.monitors.get(pipeline)
    if monitor is None:
        return "none"
    logs.install()
    try:
        failure = await _send(instance, monitor, outcome, duration_s, inner, environ)
    except Exception as exc:  # pragma: no cover - `_send` handles its own errors
        failure = _Failure(_reason(exc))
        del exc
    if failure is None:
        return "sent"
    logger.warning(
        "push monitor of pipeline %s: the %s ping failed (%s); the run is not affected",
        pipeline,
        "up" if outcome.up else "down",
        _describe(failure),
    )
    return "failed"


async def _send(
    instance: Instance,
    monitor: PushMonitor,
    outcome: RunOutcome,
    duration_s: float,
    inner: httpx2.AsyncBaseTransport | None,
    environ: Mapping[str, str] | None,
) -> _Failure | None:
    """`None` when the monitor answered 2xx, else why not."""
    try:
        secret = instance.secret_resolver(environ).resolve(monitor.secret)
    except SecretError:
        return _Failure("unresolved")
    url = secret.reveal()
    if not is_push_url(url):  # the check `horae doctor` makes; the value is never shown
        return _Failure("not_a_push_url")
    target = ping_url(url, monitor, outcome, duration_s)
    error: Exception | None = None
    status = 0
    cap = RESPONSE_CAP.set(PING_RESPONSE_CAP)
    try:
        async with (
            httpx2.AsyncClient(
                transport=transport(url, inner),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(PING_TIMEOUT_S),
            ) as client,
            asyncio.timeout(PING_TIMEOUT_S),
        ):
            response = await client.get(
                target,
                headers={"user-agent": f"horae/{__version__}", "accept-encoding": "identity"},
            )
            status = response.status_code
    except Exception as exc:
        error = exc
        del exc
    finally:
        RESPONSE_CAP.reset(cap)
    if error is not None:
        reason = _reason(error)
        del error
        return _Failure(reason)
    if not 200 <= status < 300:
        return _Failure("status", status)
    return None


__all__ = [
    "PING_RESPONSE_CAP",
    "PING_TIMEOUT_S",
    "REASONS",
    "UP_STATUSES",
    "PingResult",
    "Reason",
    "RunOutcome",
    "ping",
    "ping_url",
]
