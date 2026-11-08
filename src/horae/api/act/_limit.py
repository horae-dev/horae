# SPDX-License-Identifier: Apache-2.0
"""The `/act` rate limit (#38, schemas decision 54 H; maintainer decision 9).

In process, before any decoding or store access, for `GET` and `POST`: a token bucket **per
client IP** (`PER_IP_PER_MIN` requests a minute, a burst of `PER_IP_BURST`) and a **global**
one (`GLOBAL_PER_MIN` a minute, the same burst), both answered `429` with `Retry-After`. Each
request costs 1 up front; a request whose token turns out invalid costs `INVALID_COST` in all
(`charge` takes the rest, and a bucket may go into debt down to minus its burst, so the next
requests wait). The per-IP table is an LRU of at most `MAX_CLIENTS` entries. Constants, not
configuration (maintainer decision 9).

**The client IP** (`client_ip`) is the peer address, unless the peer is one of
`horae.yaml` `http.trusted_proxies`: then it is the right-most address of `X-Forwarded-For`
that is not itself a trusted proxy (every address right of it was added by a trusted proxy,
so it is the one that connected to the first of them; anything left of it is the client's
own claim and never read). A malformed hop stops the walk at the last trusted address. Without
trusted proxies the header is ignored.

The buckets live in one process: several workers would each have their own (the reference
deployment runs one, `horae serve`).
"""

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

PER_IP_PER_MIN = 10
PER_IP_BURST = 5
GLOBAL_PER_MIN = 60
GLOBAL_BURST = 60
INVALID_COST = 3
MAX_CLIENTS = 4096
MAX_FORWARDED_HOPS = 32

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Two token buckets per request: the client's and the global one (thread-safe)."""

    def __init__(
        self,
        *,
        per_ip_per_min: float = PER_IP_PER_MIN,
        per_ip_burst: float = PER_IP_BURST,
        global_per_min: float = GLOBAL_PER_MIN,
        global_burst: float = GLOBAL_BURST,
        max_clients: int = MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ip_rate = per_ip_per_min / 60.0
        self._ip_burst = float(per_ip_burst)
        self._global_rate = global_per_min / 60.0
        self._global_burst = float(global_burst)
        self._max = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: OrderedDict[str, _Bucket] = OrderedDict()
        self._global = _Bucket(self._global_burst, clock())

    @staticmethod
    def _refill(bucket: _Bucket, rate: float, burst: float, now: float) -> None:
        bucket.tokens = min(burst, bucket.tokens + max(0.0, now - bucket.updated) * rate)
        bucket.updated = now

    def _client(self, client: str, now: float) -> _Bucket:
        bucket = self._clients.get(client)
        if bucket is None:
            bucket = _Bucket(self._ip_burst, now)
            self._clients[client] = bucket
            while len(self._clients) > self._max:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(client)
        self._refill(bucket, self._ip_rate, self._ip_burst, now)
        return bucket

    def acquire(self, client: str) -> int | None:
        """Take one request for `client`; `None` when allowed, else the seconds to wait
        (`Retry-After`). A refused request takes nothing."""
        with self._lock:
            now = self._clock()
            mine = self._client(client, now)
            self._refill(self._global, self._global_rate, self._global_burst, now)
            if mine.tokens >= 1 and self._global.tokens >= 1:
                mine.tokens -= 1
                self._global.tokens -= 1
                return None
            waits = [
                (1 - bucket.tokens) / rate
                for bucket, rate in ((mine, self._ip_rate), (self._global, self._global_rate))
                if bucket.tokens < 1
            ]
            return max(1, math.ceil(max(waits)))

    def charge(self, client: str, cost: float) -> None:
        """Take `cost` more from `client` and the global bucket (an invalid token), down to
        minus their burst at most."""
        with self._lock:
            now = self._clock()
            mine = self._client(client, now)
            self._refill(self._global, self._global_rate, self._global_burst, now)
            mine.tokens = max(-self._ip_burst, mine.tokens - cost)
            self._global.tokens = max(-self._global_burst, self._global.tokens - cost)


def parse_networks(values: Iterable[str]) -> tuple[IpNetwork, ...]:
    """`horae.yaml` `http.trusted_proxies` as networks (validated by the contract already)."""
    return tuple(ipaddress.ip_network(value, strict=False) for value in values)


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip()
    if text.startswith("[") and "]" in text:  # `[v6]:port`
        text = text[1 : text.index("]")]
    elif text.count(":") == 1:  # `v4:port`
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None, nets: Sequence[IpNetwork]
) -> bool:
    return address is not None and any(address in net for net in nets)


def client_ip(peer: str | None, forwarded: Sequence[str], trusted: Sequence[IpNetwork]) -> str:
    """The client's address for the rate limit (see the module docstring). `peer` is the
    connection's address (`None`: unknown), `forwarded` the `X-Forwarded-For` header values in
    order (several headers are joined)."""
    peer_address = None if peer is None else _address(peer)
    current = str(peer_address) if peer_address is not None else (peer or "unknown")
    if not trusted or not _trusted(peer_address, trusted):
        return current
    hops = [hop for value in forwarded for hop in value.split(",")][-MAX_FORWARDED_HOPS:]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            return current
        current = str(address)
        if not _trusted(address, trusted):
            return current
    return current


__all__ = [
    "GLOBAL_BURST",
    "GLOBAL_PER_MIN",
    "INVALID_COST",
    "MAX_CLIENTS",
    "PER_IP_BURST",
    "PER_IP_PER_MIN",
    "RateLimiter",
    "client_ip",
    "parse_networks",
]
