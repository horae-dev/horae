# SPDX-License-Identifier: Apache-2.0
"""The `/act` rate limit and the client address (#38, schemas decision 54 H; maintainer
decision 9), including the reference deployment: Cloudflare tunnel, then Traefik."""

from pathlib import Path

from fastapi.testclient import TestClient

from act_fixtures import PUBLIC, make_env, token
from horae.api.act._limit import (
    GLOBAL_PER_MIN,
    INVALID_COST,
    PER_IP_BURST,
    PER_IP_PER_MIN,
    RateLimiter,
    client_ip,
    parse_networks,
)
from horae.api.act._routes import ActSetup

DOCKER = parse_networks(["172.18.0.0/16"])


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_limits_are_the_maintainers_constants() -> None:
    assert (PER_IP_PER_MIN, PER_IP_BURST, GLOBAL_PER_MIN, INVALID_COST) == (10, 5, 60, 3)


def test_per_ip_limit_returns_429() -> None:
    ticks = Ticks()
    limiter = RateLimiter(clock=ticks)
    assert [limiter.acquire("a") for _ in range(5)] == [None] * 5
    wait = limiter.acquire("a")
    assert wait is not None
    assert wait == 6
    assert limiter.acquire("b") is None  # another client has its own bucket
    ticks.now += 6
    assert limiter.acquire("a") is None
    assert limiter.acquire("a") is not None
    ticks.now += 60
    assert [limiter.acquire("a") for _ in range(6)].count(None) == 5  # burst, not 10


def test_global_limit_returns_429() -> None:
    ticks = Ticks()
    limiter = RateLimiter(clock=ticks)
    allowed = [limiter.acquire(f"10.0.{i // 250}.{i % 250}") for i in range(61)]
    assert allowed[:60] == [None] * 60
    assert allowed[60] == 1  # 60 a minute overall: the next one in a second
    ticks.now += 1
    assert limiter.acquire("10.9.9.9") is None


def test_invalid_tokens_cost_more() -> None:
    limiter = RateLimiter(clock=Ticks())
    assert limiter.acquire("a") is None
    limiter.charge("a", INVALID_COST - 1)  # 5 - 3 = 2
    assert limiter.acquire("a") is None
    limiter.charge("a", INVALID_COST - 1)  # 2 - 3 = -1
    assert limiter.acquire("a") is not None
    for _ in range(10):  # the debt is bounded by the burst
        limiter.charge("a", INVALID_COST)
    ticks = Ticks()
    deep = RateLimiter(clock=ticks)
    for _ in range(10):
        deep.charge("a", 100)
    # Both debts are bounded by their bursts: the client's at -5, the global one at -60.
    ticks.now += 60
    assert deep.acquire("a") is not None  # the global bucket is back to 0 only
    ticks.now += 1
    assert deep.acquire("a") is None


def test_the_client_table_is_bounded() -> None:
    limiter = RateLimiter(max_clients=3, clock=Ticks())
    for name in ("a", "b", "c", "d"):
        limiter.acquire(name)
    assert list(limiter._clients) == ["b", "c", "d"]


def test_forwarded_for_is_ignored_from_an_untrusted_peer() -> None:
    assert client_ip("198.51.100.7", ["203.0.113.1"], ()) == "198.51.100.7"
    assert client_ip("198.51.100.7", ["203.0.113.1"], DOCKER) == "198.51.100.7"
    assert client_ip(None, ["203.0.113.1"], DOCKER) == "unknown"
    assert client_ip("testclient", [], DOCKER) == "testclient"


def test_the_client_behind_the_cloudflare_tunnel_and_traefik() -> None:
    # Phone -> Cloudflare edge -> cloudflared (172.18.0.4) -> Traefik (172.18.0.3) -> Horae.
    # cloudflared sends `X-Forwarded-For: <phone>`; Traefik, trusting cloudflared
    # (forwardedHeaders.trustedIPs), appends cloudflared's address.
    assert client_ip("172.18.0.3", ["203.0.113.5, 172.18.0.4"], DOCKER) == "203.0.113.5"
    # Whatever the phone claimed itself is left of it and never read.
    spoofed = ["6.6.6.6, 203.0.113.5, 172.18.0.4"]
    assert client_ip("172.18.0.3", spoofed, DOCKER) == "203.0.113.5"
    assert client_ip("172.18.0.3", ["6.6.6.6", "203.0.113.5, 172.18.0.4"], DOCKER) == "203.0.113.5"
    # Traefik not trusting cloudflared: it rewrites the header to cloudflared's address, so
    # every phone shares one bucket (still limited, never unlimited).
    assert client_ip("172.18.0.3", ["172.18.0.4"], DOCKER) == "172.18.0.4"


def test_malformed_or_odd_hops() -> None:
    assert client_ip("172.18.0.3", ["not-an-ip, 172.18.0.4"], DOCKER) == "172.18.0.4"
    assert client_ip("172.18.0.3", ["203.0.113.5:4711"], DOCKER) == "203.0.113.5"
    assert client_ip("172.18.0.3", ["[2001:db8::1]:443"], DOCKER) == "2001:db8::1"
    assert client_ip("172.18.0.3", [" 2001:db8::2 "], DOCKER) == "2001:db8::2"
    assert client_ip("::ffff:172.18.0.3", ["203.0.113.5"], DOCKER) == "203.0.113.5"
    assert client_ip("172.18.0.3", [], DOCKER) == "172.18.0.3"
    assert client_ip("172.18.0.3", [""], DOCKER) == "172.18.0.3"
    long_chain = ", ".join(["203.0.113.9"] + ["172.18.0.9"] * 40)
    assert client_ip("172.18.0.3", [long_chain], DOCKER) == "172.18.0.9"


def test_forwarded_for_from_the_trusted_proxy_separates_clients(tmp_path: Path) -> None:
    env = make_env(tmp_path, limiter=RateLimiter(clock=Ticks()))
    setup = ActSetup(env.service, PUBLIC, env.limiter, DOCKER)
    from act_fixtures import HOST, IDLE
    from horae.api import ApiContext, create_app

    app = create_app(
        ApiContext(env.store_path, env.state_dir, lambda: IDLE, frozenset({HOST}), setup)
    )
    with TestClient(app, base_url=PUBLIC, client=("172.18.0.3", 80)) as proxy:
        one = {"X-Forwarded-For": "203.0.113.5"}
        two = {"X-Forwarded-For": "203.0.113.6"}
        assert [proxy.get(f"/act/{token()}", headers=one).status_code for _ in range(6)] == [
            200
        ] * 5 + [429]
        assert proxy.get(f"/act/{token()}", headers=two).status_code == 200
        spoof = {"X-Forwarded-For": "203.0.113.77, 203.0.113.5"}  # the right-most counts
        assert proxy.get(f"/act/{token()}", headers=spoof).status_code == 429
