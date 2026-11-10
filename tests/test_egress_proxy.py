# SPDX-License-Identifier: Apache-2.0
"""The egress allow-list proxy's policy and server, in process (#50, schemas decision 76).

The proxy (`horae.plugins.host.egress_proxy`) runs here on loopback with a fake resolver and a
connector that records the address it is asked to dial and connects to a local test server
instead: nothing leaves the machine. `PUBLIC` is a globally routable address that is never
dialled. `tests/test_plugin_egress_live.py` runs the same code in its container.
"""

import ast
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from horae.plugins.host import egress_proxy as ep

PUBLIC = "1.2.3.4"  # globally routable; the test connector never dials it
PUBLIC_2 = "1.2.3.5"
POLICY = ep.Policy.from_json(
    {
        "network": ["api.example.org", "files.example.org"],
        "network_local": [
            "printer.lan:8080",
            "192.168.1.10:8123",
            "[fd00::10]:8123",
            "127.0.0.1:1143",
        ],
    }
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


def answers(table: dict[str, list[str]]) -> Resolver:
    async def resolver(host: str, _port: int) -> list[str]:
        if host not in table:
            raise OSError("no such name")
        return list(table[host])

    return resolver


# --- the policy, without a socket -------------------------------------------------------------


@pytest.mark.parametrize(
    ("authority", "parsed"),
    [
        ("api.example.org:443", ("api.example.org", 443)),
        ("API.Example.ORG:443", ("api.example.org", 443)),
        ("192.168.1.10:8123", ("192.168.1.10", 8123)),
        ("[fd00::10]:8123", ("fd00::10", 8123)),
        ("api.example.org", None),  # the port is explicit
        ("api.example.org:0", None),
        ("api.example.org:65536", None),
        ("api.example.org:0443", None),
        ("api.example.org.:443", None),  # a trailing dot is another spelling
        ("127.1:443", None),  # what only a resolver reads as an address
        ("0x7f000001:443", None),
        ("2130706433:443", None),
        ("[fd00:0::10]:8123", None),  # not canonical
        ("fd00::10:8123", None),  # IPv6 without brackets
        ("user@api.example.org:443", None),
        ("api_example.org:443", None),
        ("a" * 300 + ":443", None),
    ],
)
def test_authorities_are_parsed_strictly(authority: str, parsed: tuple[str, int] | None) -> None:
    assert ep.split_authority(authority) == parsed


@pytest.mark.parametrize(
    ("host", "port", "plain", "outcome"),
    [
        ("api.example.org", 443, False, "public"),
        ("api.example.org", 8443, False, "port_not_allowed"),
        ("api.example.org", 80, True, "plain_http_not_allowed"),
        ("api.example.org", 443, True, "plain_http_not_allowed"),
        ("evil.example.net", 443, False, "host_not_allowed"),
        ("example.org", 443, False, "host_not_allowed"),  # no wildcard, no parent
        ("x.api.example.org", 443, False, "host_not_allowed"),
        (PUBLIC, 443, False, "ip_literal"),
        ("192.168.1.11", 8123, False, "ip_literal"),
        ("fd00::10", 8123, False, "local"),
        ("192.168.1.10", 8123, False, "local"),
        ("192.168.1.10", 8123, True, "local"),  # plain http on a local grant (decision 66 C)
        ("192.168.1.10", 22, False, "ip_literal"),  # another port of a granted address
        ("printer.lan", 8080, True, "local"),
        ("printer.lan", 8081, False, "host_not_allowed"),
    ],
)
def test_decide(host: str, port: int, plain: bool, outcome: str) -> None:
    def decided() -> str:
        target = ep.decide(POLICY, host, port, plain_http=plain)
        return "local" if target.local else "public"

    assert _outcome(decided, host, port) == outcome


def _outcome(function: Callable[[], str], host: str | None = None, port: int = 0) -> str:
    """`function()`, or the reason it was refused (checking what the refusal names)."""
    try:
        return function()
    except ep.Refused as refused:
        reason, named = refused.reason, (refused.host, refused.port)
    if host is not None and named != (host, port):
        return f"{reason} naming {named}"
    return reason


PUBLIC_TARGET = ep.Target("api.example.org", 443, local=False)
LOCAL_TARGET = ep.Target("printer.lan", 8080, local=True)
LITERAL_LINK_LOCAL = ep.Target("169.254.10.10", 80, local=True)


@pytest.mark.parametrize(
    ("target", "resolved", "outcome"),
    [
        (PUBLIC_TARGET, [PUBLIC], PUBLIC),
        (PUBLIC_TARGET, ["2606:4700::1", PUBLIC], PUBLIC),  # the first IPv4 answer
        (PUBLIC_TARGET, ["10.0.0.1"], "private_address"),
        (PUBLIC_TARGET, ["192.168.1.10"], "private_address"),
        (PUBLIC_TARGET, ["172.16.0.1"], "private_address"),
        (PUBLIC_TARGET, ["100.64.0.1"], "private_address"),  # CGNAT
        (PUBLIC_TARGET, ["198.51.100.7"], "private_address"),  # documentation range
        (PUBLIC_TARGET, ["0.0.0.0"], "private_address"),  # noqa: S104
        (PUBLIC_TARGET, ["224.0.0.1"], "private_address"),
        (PUBLIC_TARGET, ["127.0.0.1"], "loopback_address"),
        (PUBLIC_TARGET, ["::1"], "loopback_address"),
        (PUBLIC_TARGET, ["169.254.169.254"], "metadata_address"),
        (PUBLIC_TARGET, ["fd00:ec2::254"], "metadata_address"),
        (PUBLIC_TARGET, ["169.254.1.1"], "private_address"),
        (PUBLIC_TARGET, ["fe80::1%eth0"], "private_address"),
        (PUBLIC_TARGET, ["::ffff:10.0.0.1"], "embedded_address"),  # IPv4-mapped
        (PUBLIC_TARGET, ["::ffff:1.2.3.4"], "embedded_address"),  # even of a public address
        (PUBLIC_TARGET, ["::1.2.3.4"], "embedded_address"),  # IPv4-compatible
        (PUBLIC_TARGET, ["fec0::1"], "private_address"),  # site-local
        (PUBLIC_TARGET, ["168.63.129.16"], "metadata_address"),  # Azure wireserver, public
        (PUBLIC_TARGET, ["::ffff:169.254.169.254"], "metadata_address"),
        (PUBLIC_TARGET, ["64:ff9b::a00:1"], "embedded_address"),  # NAT64 of 10.0.0.1
        (PUBLIC_TARGET, ["2002:a00:1::1"], "embedded_address"),  # 6to4 of 10.0.0.1
        (PUBLIC_TARGET, [PUBLIC, "10.0.0.1"], "private_address"),  # one bad answer: all refused
        (PUBLIC_TARGET, ["10.0.0.1", PUBLIC], "private_address"),
        (PUBLIC_TARGET, ["2606:4700::1"], "no_ipv4_address"),
        (PUBLIC_TARGET, [], "dns_failed"),
        (PUBLIC_TARGET, ["not an address"], "dns_failed"),
        (LOCAL_TARGET, ["192.168.1.20"], "192.168.1.20"),
        (LOCAL_TARGET, ["10.1.2.3", "fd00::20"], "10.1.2.3"),
        (LOCAL_TARGET, [PUBLIC], "not_local_address"),  # a local name resolving publicly
        (LOCAL_TARGET, ["100.64.0.1"], "not_local_address"),
        (LOCAL_TARGET, ["169.254.169.254"], "metadata_address"),
        (LOCAL_TARGET, ["169.254.170.2"], "metadata_address"),
        (LOCAL_TARGET, ["127.0.0.1"], "loopback_address"),  # the proxy's own container
        (LOCAL_TARGET, ["169.254.10.10"], "link_local_address"),  # never as a name's answer
        (LOCAL_TARGET, ["fe80::10"], "link_local_address"),
        (LOCAL_TARGET, ["168.63.129.16"], "metadata_address"),
        (LITERAL_LINK_LOCAL, ["169.254.10.10"], "169.254.10.10"),  # an explicit literal grant
        (LOCAL_TARGET, ["192.168.1.20", PUBLIC], "not_local_address"),
    ],
)
def test_resolved_addresses_are_checked_and_pinned(
    target: ep.Target, resolved: list[str], outcome: str
) -> None:
    assert _outcome(lambda: ep.pin(target, resolved), target.host, target.port) == outcome


def test_the_policy_is_only_the_locked_lists() -> None:
    assert ep.Policy.from_json({}) == ep.Policy()
    assert POLICY.hosts == {"api.example.org", "files.example.org"}
    assert ("fd00::10", 8123) in POLICY.local
    bad: object
    for bad in (
        [],
        {"network": "api.example.org"},
        {"network": ["https://api.example.org"]},
        {"network": [1]},
        {"network_local": ["printer.lan"]},
        {"network_local": [None]},
    ):
        with pytest.raises(ValueError, match=r"network|policy"):
            ep.Policy.from_json(bad)


def test_requests_are_parsed_strictly() -> None:
    head = ep.parse_head(b"CONNECT api.example.org:443 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert (head.method, head.target) == ("CONNECT", "api.example.org:443")
    for raw in (
        b"CONNECT api.example.org:443\r\n\r\n",
        b"CONNECT api.example.org:443 HTTP/2\r\n\r\n",
        b"CONNECT  api.example.org:443 HTTP/1.1\r\n\r\n",
        b"GET http://x/ HTTP/1.1\r\nBad Header\r\n\r\n",
        b"G\xc3\xa9T http://x/ HTTP/1.1\r\n\r\n",
    ):
        with pytest.raises(ep.Refused):
            ep.parse_head(raw)
    assert ep.http_target("http://printer.lan:8080/a?b=c") == ("printer.lan", 8080, "/a?b=c")
    assert ep.http_target("http://printer.lan?b") == ("printer.lan", 80, "/?b")
    assert ep.http_target("http://[fd00::10]:8123/") == ("fd00::10", 8123, "/")
    for target in (
        "https://api.example.org/",
        "http://user:pw@printer.lan:8080/",
        "http://printer.lan:8080/#x",
        "http://printer.lan:8080\\@evil.example.net/",
        "/relative",
        "http://:80/",
    ):
        with pytest.raises(ep.Refused):
            ep.http_target(target)


def test_reports_are_capped_per_window_and_never_silenced() -> None:
    """Junk requests cannot hide later blocks: the cap is per window, and what it hides is
    counted and reported (`flush`, every `FLUSH_S` in the container)."""
    events: list[dict[str, object]] = []
    now = [1000.0]
    proxy = ep.EgressProxy(POLICY, emit=events.append, clock=lambda: now[0])
    for index in range(ep.MAX_EVENTS + 5):
        proxy.blocked(ep.Refused("host_not_allowed", f"h{index}.example.net", 443))
    assert len(events) == ep.MAX_EVENTS
    assert events[0] == blocked("h0.example.net", 443, "host_not_allowed")
    proxy.flush()
    assert events[-1] == {"event": "suppressed", "count": 5}
    proxy.flush()
    assert len(events) == ep.MAX_EVENTS + 1  # nothing more to report
    proxy.blocked(ep.Refused("host_not_allowed", "late.example.net", 443))
    assert len(events) == ep.MAX_EVENTS + 1  # still in the window: counted
    now[0] += ep.EVENT_WINDOW_S
    proxy.blocked(ep.Refused("host_not_allowed", "later.example.net", 443))
    assert events[-2:] == [
        {"event": "suppressed", "count": 1},
        blocked("later.example.net", 443, "host_not_allowed"),  # a new window lists again
    ]


def test_the_source_runs_alone_with_the_standard_library_only() -> None:
    source = Path(ep.__file__).read_text(encoding="utf-8")
    assert len(source.encode()) < 100 * 1024  # one argument of `docker run`
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    assert modules <= set(sys.stdlib_module_names)


# --- the server, on loopback ------------------------------------------------------------------


class Lab:
    """A proxy on loopback whose connector dials a local upstream instead of `address`."""

    def __init__(self, resolver: Resolver, policy: ep.Policy = POLICY) -> None:
        self.events: list[dict[str, object]] = []
        self.dialled: list[tuple[str, int]] = []
        self.received: list[bytes] = []
        self.lookups: list[str] = []
        self._resolver = resolver
        self.proxy = ep.EgressProxy(
            policy, emit=self.events.append, resolver=self._resolve, connector=self._connect
        )
        self.port = 0
        self._upstream_port = 0
        self._servers: list[asyncio.Server] = []

    async def _resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        return await self._resolver(host, port)

    async def _connect(
        self, address: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.dialled.append((address, port))
        return await asyncio.open_connection("127.0.0.1", self._upstream_port)

    async def _upstream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        self.received.append(data)
        if data.startswith(b"ping"):
            writer.write(b"pong:" + data)
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async def __aenter__(self) -> "Lab":
        upstream = await asyncio.start_server(self._upstream, "127.0.0.1", 0)
        server = await self.proxy.serve("127.0.0.1", 0)
        self._servers = [upstream, server]
        self._upstream_port = upstream.sockets[0].getsockname()[1]
        self.port = server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        for server in self._servers:
            server.close()

    async def connect(self, authority: str, payload: bytes = b"ping") -> tuple[bytes, bytes]:
        """`CONNECT authority`, then `payload` through the tunnel: (status line, answer)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        status = head.split(b"\r\n", 1)[0]
        answer = b""
        if b" 200 " in status:
            writer.write(payload)
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(65536), 10)
        else:
            answer = await asyncio.wait_for(reader.read(65536), 10)
        writer.close()
        return status, answer

    async def send(self, raw: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), 10)
        writer.close()
        return data


def run(coroutine: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    return asyncio.run(coroutine())


def blocked(host: str, port: int, reason: str) -> dict[str, object]:
    return {"event": "blocked", "host": host, "port": port, "reason": reason}


def test_an_undeclared_host_is_blocked_and_recorded() -> None:
    async def go() -> tuple[Lab, bytes, bytes]:
        async with Lab(answers({"evil.example.net": [PUBLIC]})) as lab:
            status, body = await lab.connect("evil.example.net:443")
            return lab, status, body

    lab, status, body = run(go)
    assert status == b"HTTP/1.1 403 Forbidden"
    assert b"host_not_allowed" in body
    assert lab.events == [blocked("evil.example.net", 443, "host_not_allowed")]
    assert lab.lookups == []  # refused before any resolution
    assert lab.dialled == []


def test_a_declared_host_works_over_https_connect() -> None:
    async def go() -> tuple[Lab, bytes, bytes]:
        async with Lab(answers({"api.example.org": [PUBLIC]})) as lab:
            status, answer = await lab.connect("api.example.org:443", b"ping\x16\x03\x01")
            return lab, status, answer

    lab, status, answer = run(go)
    assert status == b"HTTP/1.1 200 Connection established"
    assert answer == b"pong:ping\x16\x03\x01"  # bytes pass untouched: no TLS interception
    assert lab.dialled == [(PUBLIC, 443)]  # the checked address, never the name
    assert lab.events == []


def test_a_declared_host_is_https_on_443_only() -> None:
    async def go() -> Lab:
        async with Lab(answers({"api.example.org": [PUBLIC]})) as lab:
            assert (await lab.connect("api.example.org:8443"))[0].endswith(b"403 Forbidden")
            plain = b"GET http://api.example.org/x HTTP/1.1\r\nHost: api.example.org\r\n\r\n"
            assert (await lab.send(plain)).startswith(b"HTTP/1.1 403")
            return lab

    lab = run(go)
    assert lab.events == [
        blocked("api.example.org", 8443, "port_not_allowed"),
        blocked("api.example.org", 80, "plain_http_not_allowed"),
    ]
    assert lab.dialled == []


def test_a_private_address_is_refused_without_a_grant_and_allowed_with_one() -> None:
    table = {"api.example.org": ["192.168.1.10"], "printer.lan": ["192.168.1.10"]}

    async def go() -> tuple[Lab, list[bytes]]:
        async with Lab(answers(table)) as lab:
            statuses = [
                (await lab.connect("api.example.org:443"))[0],  # a public name, private answer
                (await lab.connect("192.168.1.10:9999"))[0],  # an ungranted port
                (await lab.connect("192.168.1.10:8123"))[0],  # the exact grant
                (await lab.connect("printer.lan:8080"))[0],  # a granted local name
            ]
            return lab, statuses

    lab, statuses = run(go)
    assert [status.split(b" ", 1)[1] for status in statuses] == [
        b"403 Forbidden",
        b"403 Forbidden",
        b"200 Connection established",
        b"200 Connection established",
    ]
    assert lab.events == [
        blocked("api.example.org", 443, "private_address"),
        blocked("192.168.1.10", 9999, "ip_literal"),
    ]
    assert lab.dialled == [("192.168.1.10", 8123), ("192.168.1.10", 8080)]


def test_dns_rebinding_cannot_swap_the_checked_address() -> None:
    """A resolver that answers publicly first and privately next: each connection resolves once
    and dials exactly the address it checked, so the private answer is refused, never used."""
    seen: list[int] = []

    async def rebinding(_host: str, _port: int) -> list[str]:
        seen.append(1)
        return [PUBLIC] if len(seen) == 1 else ["10.0.0.7"]

    async def go() -> tuple[Lab, bytes, bytes]:
        async with Lab(rebinding) as lab:
            first = (await lab.connect("api.example.org:443"))[0]
            second = (await lab.connect("api.example.org:443"))[0]
            return lab, first, second

    lab, first, second = run(go)
    assert first.endswith(b"200 Connection established")
    assert second.endswith(b"403 Forbidden")
    assert lab.lookups == ["api.example.org", "api.example.org"]  # once per connection
    assert lab.dialled == [(PUBLIC, 443)]
    assert lab.events == [blocked("api.example.org", 443, "private_address")]


def test_a_rebinding_answer_mixing_public_and_private_is_refused() -> None:
    async def go() -> Lab:
        async with Lab(answers({"api.example.org": [PUBLIC, "169.254.169.254"]})) as lab:
            assert (await lab.connect("api.example.org:443"))[0].endswith(b"403 Forbidden")
            return lab

    lab = run(go)
    assert lab.events == [blocked("api.example.org", 443, "metadata_address")]
    assert lab.dialled == []


def test_ip_literals_are_refused_unless_granted() -> None:
    async def go() -> Lab:
        async with Lab(answers({})) as lab:
            for authority in (f"{PUBLIC}:443", "[2606:4700::1]:443", "10.0.0.1:443"):
                assert (await lab.connect(authority))[0].endswith(b"403 Forbidden")
            for authority in ("127.1:443", "0x7f000001:443", "0177.0.0.1:443", "x.org.:443"):
                assert (await lab.connect(authority))[0].endswith(b"400 Bad Request")
            plain = b"GET http://user@printer.lan:8080/ HTTP/1.1\r\n\r\n"
            assert (await lab.send(plain)).startswith(b"HTTP/1.1 400")
            return lab

    lab = run(go)
    malformed = blocked("", 0, "malformed_target")
    assert lab.events == [
        blocked(PUBLIC, 443, "ip_literal"),
        blocked("2606:4700::1", 443, "ip_literal"),
        blocked("10.0.0.1", 443, "ip_literal"),
        *[malformed] * 5,  # reported too, without what the plugin wrote
    ]
    assert lab.dialled == []
    assert lab.lookups == []


def test_a_loopback_grant_never_reaches_the_proxy_itself() -> None:
    async def go() -> Lab:
        async with Lab(answers({})) as lab:
            assert (await lab.connect("127.0.0.1:1143"))[0].endswith(b"403 Forbidden")
            return lab

    lab = run(go)
    assert lab.events == [blocked("127.0.0.1", 1143, "loopback_address")]


def test_a_local_name_resolving_elsewhere_is_refused() -> None:
    async def go() -> Lab:
        async with Lab(answers({"printer.lan": [PUBLIC]})) as lab:
            assert (await lab.connect("printer.lan:8080"))[0].endswith(b"403 Forbidden")
            return lab

    assert run(go).events == [blocked("printer.lan", 8080, "not_local_address")]


def test_plain_http_is_forwarded_to_a_local_grant_only() -> None:
    request = (
        b"GET http://printer.lan:8080/status?job=1 HTTP/1.1\r\n"
        b"Host: printer.lan:8080\r\n"
        b"Proxy-Authorization: Basic c2VjcmV0\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Connection: keep-alive\r\n"
        b"Accept: */*\r\n\r\n"
    )

    async def go() -> tuple[Lab, bytes]:
        async with Lab(answers({"printer.lan": ["192.168.1.20"]})) as lab:
            return lab, await lab.send(request)

    lab, answer = run(go)
    assert answer.endswith(b"\r\n\r\nok")
    assert lab.dialled == [("192.168.1.20", 8080)]
    forwarded = lab.received[0].decode()
    assert forwarded.startswith("GET /status?job=1 HTTP/1.1\r\n")
    assert "Host: printer.lan:8080" in forwarded
    assert "Accept: */*" in forwarded
    assert "Proxy-" not in forwarded
    assert "keep-alive" not in forwarded
    assert forwarded.endswith("Connection: close\r\n\r\n")


def test_a_report_never_holds_the_url() -> None:
    request = (
        b"GET http://evil.example.net/leak/path?token=abc123 HTTP/1.1\r\n"
        b"Host: evil.example.net\r\nAuthorization: Bearer xyz789\r\n\r\n"
    )

    async def go() -> Lab:
        async with Lab(answers({})) as lab:
            assert (await lab.send(request)).startswith(b"HTTP/1.1 403")
            return lab

    lab = run(go)
    assert lab.events == [blocked("evil.example.net", 80, "host_not_allowed")]
    text = json.dumps(lab.events)
    for fragment in ("leak", "token", "abc123", "xyz789", "http"):
        assert fragment not in text


def test_malformed_and_oversized_requests_are_refused() -> None:
    async def go() -> tuple[Lab, list[bytes]]:
        async with Lab(answers({})) as lab:
            answers_ = [
                await lab.send(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"),  # not a proxy request
                await lab.send(b"GET https://api.example.org/ HTTP/1.1\r\n\r\n"),
                await lab.send(b"CONNECT api.example.org HTTP/1.1\r\n\r\n"),
                await lab.send(
                    b"CONNECT api.example.org:443 HTTP/1.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n"
                ),
            ]
            return lab, answers_

    lab, replies = run(go)
    assert all(reply.startswith(b"HTTP/1.1 400") for reply in replies)
    assert lab.dialled == []


def test_a_name_that_does_not_resolve_is_a_bad_gateway() -> None:
    async def go() -> Lab:
        async with Lab(answers({})) as lab:
            assert (await lab.connect("api.example.org:443"))[0].endswith(b"502 Bad Gateway")
            return lab

    lab = run(go)
    assert lab.dialled == []
    assert lab.events == [blocked("api.example.org", 443, "dns_failed")]


def test_a_name_without_an_ipv4_answer_is_reported() -> None:
    async def go() -> Lab:
        async with Lab(answers({"api.example.org": ["2606:4700::1"]})) as lab:
            assert (await lab.connect("api.example.org:443"))[0].endswith(b"502 Bad Gateway")
            return lab

    assert run(go).events == [blocked("api.example.org", 443, "no_ipv4_address")]


def test_an_unreachable_upstream_is_a_bad_gateway() -> None:
    async def refuse(_address: str, _port: int) -> Any:
        raise ConnectionRefusedError

    async def go() -> bytes:
        proxy = ep.EgressProxy(
            POLICY, emit=lambda _e: None, resolver=answers({"api.example.org": [PUBLIC]})
        )
        proxy._connector = refuse
        server = await proxy.serve("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT api.example.org:443 HTTP/1.1\r\n\r\n")
        data = await asyncio.wait_for(reader.read(65536), 10)
        writer.close()
        server.close()
        return data

    assert run(go).startswith(b"HTTP/1.1 502")


def test_the_script_needs_its_policy() -> None:
    import subprocess

    done = subprocess.run(  # noqa: S603 (this interpreter and the module's own source)
        [sys.executable, "-I", "-S", "-c", Path(ep.__file__).read_text(encoding="utf-8")],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 64
    assert b"usage" in done.stderr


def test_only_the_plugin_network_may_use_the_proxy() -> None:
    elsewhere = ep.Policy.from_json({"network": ["api.example.org"], "clients": ["10.0.0.0/8"]})
    assert elsewhere.clients is not None

    async def go() -> tuple[Lab, bytes]:
        async with Lab(answers({"api.example.org": [PUBLIC]}), elsewhere) as lab:
            reader, writer = await asyncio.open_connection("127.0.0.1", lab.port)
            writer.write(b"CONNECT api.example.org:443 HTTP/1.1\r\n\r\n")
            try:
                data = await asyncio.wait_for(reader.read(65536), 10)
            except ConnectionResetError:
                data = b""
            writer.close()
            return lab, data

    lab, data = run(go)
    assert data == b""  # dropped without an answer
    assert lab.dialled == []
    assert lab.lookups == []
    with pytest.raises(ValueError, match="clients"):
        ep.Policy.from_json({"clients": "10.0.0.0/8"})


def test_only_the_admitted_plugin_container_may_use_the_proxy() -> None:
    """A sibling run's container on the same network is dropped: only the address the host
    admitted is served, and nobody before that (decision 76 C)."""
    loopback = ep.Policy.from_json({"network": ["api.example.org"], "clients": ["127.0.0.0/8"]})

    async def go() -> tuple[Lab, list[bytes]]:
        async with Lab(answers({"api.example.org": [PUBLIC]}), loopback) as lab:
            before = await _raw_connect(lab.port)
            assert not lab.proxy.admit("10.0.0.2")  # outside the plugin network
            assert lab.proxy.admit("127.0.0.2")  # another container (a sibling's view)
            sibling = await _raw_connect(lab.port)  # we come from 127.0.0.1: not admitted
            assert not lab.proxy.admit("127.0.0.1")  # only the first admission counts
            return lab, [before, sibling]

    lab, replies = run(go)
    assert replies == [b"", b""]
    assert lab.dialled == []

    async def admitted() -> bytes:
        async with Lab(answers({"api.example.org": [PUBLIC]}), loopback) as lab:
            assert lab.proxy.admit("127.0.0.1")
            return (await lab.connect("api.example.org:443"))[0]

    assert run(admitted) == b"HTTP/1.1 200 Connection established"


async def _raw_connect(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT api.example.org:443 HTTP/1.1\r\n\r\n")
    try:
        data = await asyncio.wait_for(reader.read(65536), 10)
    except ConnectionResetError:
        data = b""
    writer.close()
    return data


def test_the_proxy_listens_on_its_plugin_network_address_only() -> None:
    policy = ep.Policy.from_json({"clients": ["127.0.0.0/8"]})
    assert ep.listen_address(policy).startswith("127.")
    with pytest.raises(ValueError, match="no plugin network"):
        ep.listen_address(ep.Policy())
    with pytest.raises(ValueError, match="no address"):
        ep.listen_address(ep.Policy.from_json({"clients": ["192.0.2.0/24"]}))
