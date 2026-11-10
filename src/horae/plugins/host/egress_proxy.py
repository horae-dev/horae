# SPDX-License-Identifier: Apache-2.0
"""The egress allow-list proxy of a plugin container (#50, schemas decision 76).

**Standard library only, and self-contained**: the container runtime runs this file's source in
its own container with `python3 -I -S -c <source> <policy JSON>` (`horae.plugins.host.egress`),
so it imports nothing from Horae. The tests import it to run the same code in process.

The policy is the plugin's **locked** `permissions.network` (public host names) and
`permissions.network_local` (`host:port`, decision 66), nothing else:

- `CONNECT host:port` (HTTPS, or any TCP protocol tunnelled): to a `network` host on port 443
  only, or to an exact `network_local` endpoint. The tunnel is opaque: no TLS interception;
- a plain-HTTP request in absolute form (`GET http://host:port/path HTTP/1.1`): only to an
  exact `network_local` endpoint (plain `http` is never allowed to a public host, decision 66
  C); it is forwarded in origin form with `Connection: close`, the `Proxy-*` headers dropped;
- an IP literal only when it is exactly a `network_local` endpoint (canonical form);
- names are resolved **here**, once per connection: every answer of a public name must be a
  globally routable address, every answer of a local name a private or link-local one
  (decision 66 B); a metadata address (`METADATA`) and loopback (this container's own) are
  always refused. The connection goes to the **checked** address (the first IPv4 answer), never
  to a name resolved again, so DNS rebinding cannot swap it.

Each refusal writes one JSON line to stdout, `{"event": "blocked", "host", "port", "reason"}`
(a fixed `reason` code; never a URL, path, query, header or body), at most `MAX_EVENTS` lines;
`{"event": "ready", "port"}` once listening. The proxy exits when its stdin closes.
"""

import asyncio
import ipaddress
import json
import re
import socket
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

LISTEN_PORT = 3128
"""The port the proxy listens on (every address of its container)."""
HTTPS_PORT = 443
"""The only port a `network` host may be reached on."""
MAX_HEAD = 8 * 1024
"""The longest request head (request line and headers) read."""
HEAD_TIMEOUT_S = 10.0
DNS_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
IDLE_TIMEOUT_S = 300.0
"""A tunnel with no byte in either direction for this long is closed."""
MAX_CONNECTIONS = 64
MAX_EVENTS = 200
"""Blocked attempts reported per proxy; later ones are counted in one `suppressed` line."""
_CHUNK = 64 * 1024
_STREAM_LIMIT = 64 * 1024

METADATA = frozenset(
    ipaddress.ip_address(address)
    for address in (
        "169.254.169.254",
        "169.254.170.2",
        "169.254.170.23",
        "fd00:ec2::254",
        "fd00:ec2::23",
    )
)
"""Cloud metadata services: refused whatever the grant (decision 66 B)."""
LOCAL_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)
"""Where a `network_local` endpoint may resolve to (decision 66 B, loopback aside)."""
_EMBEDDING_NETS = tuple(
    ipaddress.ip_network(net) for net in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16")
)
"""IPv6 prefixes that carry an IPv4 address (NAT64, 6to4): never a public answer."""

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_NAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
    }
)

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class Refused(Exception):
    """A request the policy refuses: `reason` is a fixed code, `host` and `port` what it asked
    (already parsed; `host` is `""` when the request could not be read)."""

    def __init__(self, reason: str, host: str = "", port: int = 0, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host
        self.port = port
        self.status = status


@dataclass(frozen=True)
class Policy:
    """A plugin's locked network permissions."""

    hosts: frozenset[str] = frozenset()
    """`permissions.network`: public host names, `https` (`CONNECT` to port 443) only."""
    local: frozenset[tuple[str, int]] = frozenset()
    """`permissions.network_local` as `(host, port)`; an IPv6 host without its brackets."""
    clients: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] | None = None
    """Where a client may connect from: the plugin network's subnet (any when `None`, in
    tests only: the container runtime always sets it)."""

    @classmethod
    def from_json(cls, data: object) -> "Policy":
        """From `{"network": [...], "network_local": [...], "clients": [CIDR...]}`;
        `ValueError` if malformed."""
        if not isinstance(data, dict):
            raise ValueError("the policy is not an object")
        hosts = data.get("network", [])
        local = data.get("network_local", [])
        if not isinstance(hosts, list) or not isinstance(local, list):
            raise ValueError("network and network_local must be lists")
        names: set[str] = set()
        for host in hosts:
            if not isinstance(host, str) or not _is_name(host):
                raise ValueError("a network entry is not a host name")
            names.add(host)
        endpoints: set[tuple[str, int]] = set()
        for entry in local:
            parsed = split_authority(entry) if isinstance(entry, str) else None
            if parsed is None:
                raise ValueError("a network_local entry is not host:port")
            endpoints.add(parsed)
        clients = data.get("clients")
        if clients is None:
            return cls(frozenset(names), frozenset(endpoints))
        if not isinstance(clients, list) or not all(isinstance(net, str) for net in clients):
            raise ValueError("clients must be a list of networks")
        networks = tuple(ipaddress.ip_network(net) for net in clients)
        return cls(frozenset(names), frozenset(endpoints), networks)


def _is_name(host: str) -> bool:
    return len(host) <= 253 and _NAME.fullmatch(host) is not None


def _ip(host: str) -> Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def split_authority(authority: str) -> tuple[str, int] | None:
    """`(host, port)` of `host:port` or `[v6]:port`: the host lowercased, an IP literal in its
    canonical form only, the port explicit (1 to 65535, no leading zero); `None` otherwise."""
    if len(authority) > 262:
        return None
    host, separator, port_text = authority.rpartition(":")
    if not separator or _PORT.fullmatch(port_text) is None or int(port_text) > 65535:
        return None
    port = int(port_text)
    if host.startswith("[") and host.endswith("]"):
        literal = host[1:-1]
        address = _ip(literal)
        if not isinstance(address, ipaddress.IPv6Address) or str(address) != literal:
            return None
        return literal, port
    host = host.lower()
    address = _ip(host)
    if address is not None:
        return (host, port) if isinstance(address, ipaddress.IPv4Address) else None
    if not _is_name(host) or host.rsplit(".", 1)[-1].isdigit() or _numeric(host):
        return None  # `127.1`, `0x7f000001`...: forms only the resolver reads as an address
    return host, port


def _numeric(host: str) -> bool:
    """Whether the C library reads `host` as an IPv4 address (`inet_aton`'s legacy forms)."""
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


def _unwrap(address: Address) -> Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def public_problem(address: Address) -> str | None:
    """Why a public name may not connect to `address`, or `None`."""
    address = _unwrap(address)
    if address in METADATA:
        return "metadata_address"
    if address.is_loopback:
        return "loopback_address"
    if not address.is_global or address.is_multicast:
        return "private_address"
    if any(address in net for net in _EMBEDDING_NETS):
        return "private_address"
    return None


def local_problem(address: Address) -> str | None:
    """Why a `network_local` grant may not connect to `address`, or `None`."""
    address = _unwrap(address)
    if address in METADATA:
        return "metadata_address"
    if address.is_loopback:
        return "loopback_address"  # the proxy's own container, never the user's machine
    if not any(address in net for net in LOCAL_NETS):
        return "not_local_address"
    return None


@dataclass(frozen=True)
class Target:
    """What the policy allows a request to reach, before resolution."""

    host: str
    port: int
    local: bool
    """A `network_local` grant (else a `network` host)."""


def decide(policy: Policy, host: str, port: int, *, plain_http: bool) -> Target:
    """The `Target` of a request for `host:port` (already parsed by `split_authority`), or
    `Refused`. `plain_http`: an absolute-form HTTP request rather than a `CONNECT`."""
    if (host, port) in policy.local:
        return Target(host, port, local=True)
    if _ip(host) is not None:
        raise Refused("ip_literal", host, port)
    if host not in policy.hosts:
        raise Refused("host_not_allowed", host, port)
    if plain_http:
        raise Refused("plain_http_not_allowed", host, port)
    if port != HTTPS_PORT:
        raise Refused("port_not_allowed", host, port)
    return Target(host, port, local=False)


def pin(target: Target, answers: Iterable[str]) -> str:
    """The address to connect to: every answer must pass (`public_problem` or `local_problem`),
    so a resolver mixing a good and a bad answer is refused as a whole; then the first IPv4
    answer (the egress network has no IPv6). `Refused` otherwise."""
    check = local_problem if target.local else public_problem
    addresses: list[Address] = []
    for answer in answers:
        address = _ip(answer.split("%", 1)[0])
        if address is None:
            raise Refused("dns_failed", target.host, target.port, status=502)
        problem = check(address)
        if problem is not None:
            raise Refused(problem, target.host, target.port)
        addresses.append(_unwrap(address))
    for address in addresses:
        if isinstance(address, ipaddress.IPv4Address):
            return str(address)
    if addresses:
        raise Refused("no_ipv4_address", target.host, target.port, status=502)
    raise Refused("dns_failed", target.host, target.port, status=502)


Resolver = Callable[[str, int], Awaitable[list[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
Emit = Callable[[dict[str, object]], None]


async def resolve(host: str, port: int) -> list[str]:
    """Every address `host` resolves to (the system resolver; an IP literal is itself)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


async def connect(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A TCP connection to exactly `address` (an IP literal: nothing is resolved again)."""
    return await asyncio.open_connection(address, port, limit=_STREAM_LIMIT)


@dataclass
class _Head:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]] = field(default_factory=list)


def parse_head(raw: bytes) -> _Head:
    """The request line and headers of `raw` (ending with an empty line); `Refused` when it is
    not a well-formed HTTP/1.x request head."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("bad_request", status=400) from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or _TOKEN.fullmatch(parts[0]) is None:
        raise Refused("bad_request", status=400)
    method, target, version = parts
    if version not in ("HTTP/1.0", "HTTP/1.1") or not target:
        raise Refused("bad_request", status=400)
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or _TOKEN.fullmatch(name) is None or any(c in value for c in "\r\n\0"):
            raise Refused("bad_request", status=400)
        headers.append((name, value.strip()))
    return _Head(method, target, version, headers)


def http_target(target: str) -> tuple[str, int, str]:
    """`(host, port, origin-form path)` of an absolute `http://` target; `Refused` for user
    info, a missing or malformed host, or anything but `http`."""
    if not target.startswith("http://"):
        raise Refused("bad_request", status=400)
    rest = target[len("http://") :]
    end = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
    authority, path = rest[:end], rest[end:]
    if "@" in authority or "\\" in target or "#" in target:
        raise Refused("bad_request", status=400)
    if not path.startswith("/"):
        path = "/" + path
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in path):
        raise Refused("bad_request", status=400)
    has_port = "]:" in authority if authority.startswith("[") else ":" in authority
    parsed = split_authority(authority if has_port else f"{authority}:80")
    if parsed is None:
        raise Refused("bad_request", status=400)
    return parsed[0], parsed[1], path


_REASONS = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway", 503: "Service Unavailable"}


def _response(status: int, reason: str) -> bytes:
    body = f"horae egress proxy: {reason}\n".encode("ascii")
    return (
        f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
        f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body


class EgressProxy:
    """The proxy server (see the module docstring). `resolver` and `connector` are the system
    resolver and a plain TCP connection; tests replace them."""

    def __init__(
        self,
        policy: Policy,
        *,
        emit: Emit,
        resolver: Resolver = resolve,
        connector: Connector = connect,
    ) -> None:
        self.policy = policy
        self._emit = emit
        self._resolver = resolver
        self._connector = connector
        self._active = 0
        self._events = 0
        self._suppressed = 0

    def blocked(self, refused: Refused) -> None:
        """Report one refusal (at most `MAX_EVENTS`, then a count)."""
        if self._events >= MAX_EVENTS:
            self._suppressed += 1
            if self._suppressed == 1:
                self._emit({"event": "suppressed"})
            return
        self._events += 1
        event: dict[str, object] = {"event": "blocked", "host": refused.host}
        event.update(port=refused.port, reason=refused.reason)
        self._emit(event)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One client connection: one request, then a tunnel or a forwarded HTTP exchange."""
        if not self._client_allowed(writer):
            writer.transport.abort()  # not from the plugin's network: no answer at all
            return
        if self._active >= MAX_CONNECTIONS:
            await _close(writer, _response(503, "too many connections"))
            return
        self._active += 1
        try:
            await self._serve(reader, writer)
        except Exception:  # never let one connection stop the proxy
            await _close(writer)
        finally:
            self._active -= 1

    def _client_allowed(self, writer: asyncio.StreamWriter) -> bool:
        clients = self.policy.clients
        if clients is None:
            return True
        peer = writer.get_extra_info("peername")
        address = _ip(str(peer[0])) if isinstance(peer, tuple) and peer else None
        return address is not None and any(_unwrap(address) in net for net in clients)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(_read_head(reader), HEAD_TIMEOUT_S)
            request = parse_head(head)
            if request.method == "CONNECT":
                parsed = split_authority(request.target)
                if parsed is None:
                    raise Refused("bad_request", status=400)
                host, port = parsed
                forward = b""
                target = decide(self.policy, host, port, plain_http=False)
            else:
                host, port, path = http_target(request.target)
                target = decide(self.policy, host, port, plain_http=True)
                forward = _origin_head(request, path)
            address = await self._pin(target)
        except Refused as refused:
            if refused.status == 403:
                self.blocked(refused)
            await _close(writer, _response(refused.status, refused.reason))
            return
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            await _close(writer, _response(400, "bad_request"))
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self._connector(address, target.port), CONNECT_TIMEOUT_S
            )
        except (TimeoutError, OSError):
            await _close(writer, _response(502, "connect_failed"))
            return
        if forward:
            upstream_writer.write(forward)
        else:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await _relay(reader, writer, upstream_reader, upstream_writer)

    async def _pin(self, target: Target) -> str:
        address = _ip(target.host)
        if address is not None:
            return pin(target, [str(address)])
        try:
            answers = await asyncio.wait_for(
                self._resolver(target.host, target.port), DNS_TIMEOUT_S
            )
        except (TimeoutError, OSError, UnicodeError):
            raise Refused("dns_failed", target.host, target.port, status=502) from None
        return pin(target, answers)

    async def serve(self, host: str = "0.0.0.0", port: int = LISTEN_PORT) -> asyncio.Server:  # noqa: S104 (its own container)
        """Start listening."""
        return await asyncio.start_server(self.handle, host, port, limit=_STREAM_LIMIT)


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > MAX_HEAD:
        raise Refused("bad_request", status=400)
    return head


def _origin_head(request: _Head, path: str) -> bytes:
    lines = [f"{request.method} {path} {request.version}"]
    lines += [
        f"{name}: {value}"
        for name, value in request.headers
        if name.lower() not in _HOP_HEADERS and not name.lower().startswith("proxy-")
    ]
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


async def _close(writer: asyncio.StreamWriter, data: bytes = b"") -> None:
    try:
        if data:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 5)
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 5)
    except (TimeoutError, OSError):
        writer.transport.abort()


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    """Copy bytes both ways until both sides end, or nothing moved for `IDLE_TIMEOUT_S`."""
    loop = asyncio.get_running_loop()
    last = [loop.time()]

    async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    data = await asyncio.wait_for(source.read(_CHUNK), IDLE_TIMEOUT_S)
                except TimeoutError:
                    if loop.time() - last[0] >= IDLE_TIMEOUT_S:
                        break
                    continue
                if not data:
                    break
                last[0] = loop.time()
                sink.write(data)
                await sink.drain()
            if sink.can_write_eof():
                sink.write_eof()
        except OSError:
            pass

    try:
        await asyncio.gather(
            pipe(client_reader, upstream_writer), pipe(upstream_reader, client_writer)
        )
    finally:
        for writer in (upstream_writer, client_writer):
            writer.transport.abort()


def _print(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def _stdin_closed() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while await reader.read(_CHUNK):
        pass


async def main(policy_json: str) -> None:
    """Listen on `LISTEN_PORT` with the policy `policy_json`, until stdin closes."""
    proxy = EgressProxy(Policy.from_json(json.loads(policy_json)), emit=_print)
    server = await proxy.serve()
    _print({"event": "ready", "port": LISTEN_PORT})
    try:
        await _stdin_closed()
    finally:
        server.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write("usage: egress_proxy <policy JSON>\n")
        sys.exit(64)
    asyncio.run(main(sys.argv[1]))
