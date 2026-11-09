# SPDX-License-Identifier: Apache-2.0
"""The wire contract of the core's plugin host (`horae.plugins.host`, schemas decisions 35, 36).

- Tools are called by their full ref (`hello.greet`), the name listed in `provides` and in
  `tools/list`. The tool's **arguments** are the trusted parameters of the pipeline call.
- The request's `_meta["dev.horae/input"]` carries a transform's untrusted input (the previous
  step's data; absent for a source), so it can never shadow a parameter.
- The request's `_meta["dev.horae/paths"]` carries the plugin's path permissions **as approved
  in `horae.lock`** (`{"read": [...], "write": [...]}`): trusted configuration. A plugin must
  never take a path to read or write from its untrusted data.
- The result's data is its `structuredContent`, else a single text block that parses as
  JSON; `isError: true` is a tool error. Proposed intents travel in the result's
  `_meta["dev.horae/intents"]` (a list).
- The request's `_meta["horae/network"]` (horae-sdk 0.5) carries the plugin's network
  allow-list **as approved in `horae.lock`** (a list of host names; empty when the lock grants
  no host). A core older than that sends nothing: the SDK then reports the list as unknown
  (`Network.known` is false) and the plugin decides, e.g. by falling back to its own config.
- The request's `_meta["horae/network_local"]` (horae-sdk 0.6, schemas decision 66) carries
  the plugin's approved local-network endpoints (`permissions.network_local`): `host:port`
  strings such as `127.0.0.1:1143`, `[::1]:1143` or `homeassistant.local:8123`, matched by
  exact host and port, plain `http` allowed. A core that sends `horae/network` without it
  (decision 63 only) granted no local endpoint: the SDK reads it as an empty list.
- Messages are capped at `MAX_MESSAGE_BYTES` (8 MiB) by the host: a larger one costs the
  plugin its process (`output_too_large`). The SDK refuses such a result before sending it
  (a tool error, or a sink operation's `output_too_large`), keeping `MESSAGE_MARGIN` for the
  JSON-RPC envelope.
- One step proposes at most `MAX_INTENTS` (256) intents (`horae.intents.MAX_INTENTS`): the
  core refuses the whole step otherwise (`batch_too_large`), so the SDK refuses it first.
"""

import ipaddress
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

INPUT_META = "dev.horae/input"
INTENTS_META = "dev.horae/intents"
PATHS_META = "dev.horae/paths"
NETWORK_META = "horae/network"
NETWORK_LOCAL_META = "horae/network_local"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MESSAGE_MARGIN = 64 * 1024
MAX_INTENTS = 256
MAX_NOTE_BYTES = (MAX_MESSAGE_BYTES - MESSAGE_MARGIN) // 4 * 3
"""The largest note sent over MCP either way, decoded (`horae.plugins.host.sink`)."""


def _absolute(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str) and os.path.isabs(value))


@dataclass(frozen=True)
class Paths:
    """The plugin's path permissions as approved in `horae.lock` (trusted, from the core)."""

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, value: object) -> "Paths":
        """From `_meta["dev.horae/paths"]` or a sink operation's `paths` (malformed: none)."""
        if not isinstance(value, Mapping):
            return cls()
        return cls(_absolute(value.get("read")), _absolute(value.get("write")))

    def allowed(self, path: str, *, write: bool = False) -> str | None:
        """The real path of `path` (absolute, symbolic links resolved) when it is inside an
        approved entry (`write` entries only, or `read` and `write` entries for reading);
        `None` otherwise. A check at the time of the call: open what it returns with
        `O_NOFOLLOW` if links may change meanwhile."""
        if not os.path.isabs(path):
            return None
        roots = self.write if write else (*self.read, *self.write)
        real = os.path.realpath(path)
        for root in roots:
            top = os.path.realpath(root)
            if os.path.commonpath([top, real]) == top:
                return real
        return None

    def to_wire(self) -> dict[str, list[str]]:
        return {"read": list(self.read), "write": list(self.write)}


_URL_HOST = re.compile(
    # `https://` + host + optional port + optional path/query (no fragment): the grammar of the
    # core's `HttpsUrl`, so what it refuses (user info, backslash, space, control characters,
    # a bracketed IP literal, an empty or leading-zero port) matches nothing here.
    r"https://(?P<host>[^/?#:@\\\[\]\x00-\x20\x7f]+)"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
    r"(?:[/?][^\x00-\x20\x7f\\#]*)?\Z"
)
_HOSTNAME = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
"""The lock's `Hostname` grammar (`horae.contracts.types.HOST`): a lowercase DNS name whose last
label starts with a letter (no IPv4 literal), without port or wildcard."""


def url_host(url: object) -> str | None:
    """The host of a well-formed `https` URL, exactly as written; `None` for anything else
    (another scheme, user info, a backslash, a bad port, an IP literal, control characters)."""
    if not isinstance(url, str):
        return None
    match = _URL_HOST.match(url)
    if match is None or (match["port"] is not None and int(match["port"]) > 65535):
        return None
    return match["host"]


# --- local endpoints (decision 66): the core's `LocalEndpoint`, copied (no core dependency) ---

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_LOCAL_V4 = (
    rf"(?:127(?:\.{_OCTET}){{3}}|10(?:\.{_OCTET}){{3}}|172\.(?:1[6-9]|2[0-9]|3[01])(?:\.{_OCTET}){{2}}"
    rf"|192\.168(?:\.{_OCTET}){{2}}|169\.254(?:\.{_OCTET}){{2}})"
)
_LOCAL_HOST = re.compile(
    rf"(?:{_LABEL}\.)+(?:local|lan|internal|home\.arpa)|{_LOCAL_V4}"
    r"|\[(?:::1|f[cd][0-9a-f]{2}:[0-9a-f:]{1,35}|fe[89ab][0-9a-f]:[0-9a-f:]{1,35})\]"
)
_PORT = re.compile(r"[1-9][0-9]{0,4}")
_U_LABEL_CATEGORIES = frozenset({"Ll", "Lo", "Lm", "Mn", "Mc", "Nd"})
_METADATA = frozenset(
    {"169.254.169.254", "169.254.170.2", "169.254.170.23", "[fd00:ec2::254]", "[fd00:ec2::23]"}
)
_METADATA_LABELS = frozenset({"metadata", "instance-data"})
"""A name whose first label is one of these (`metadata.google.internal`,
`instance-data.ec2.internal`) is a cloud metadata service, never a local endpoint."""
_LOCAL_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def local_host(host: object) -> bool:
    """Whether `host` (as written in a URL or a grant) may be the host of a local endpoint: a
    `.local` / `.lan` / `.internal` / `.home.arpa` name, or a canonical loopback, private or
    link-local IP literal (IPv6 in brackets); never a cloud metadata address or name."""
    if not isinstance(host, str) or len(host) > 253 or not _LOCAL_HOST.fullmatch(host):
        return False
    if host in _METADATA or host.split(".", 1)[0] in _METADATA_LABELS:
        return False
    if not host.startswith("[") and not re.fullmatch(_LOCAL_V4, host):
        return all(_valid_label(label) for label in host.split("."))
    literal = host[1:-1] if host.startswith("[") else host
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        return False
    return str(address) == literal and any(address in net for net in _LOCAL_NETS)


def _valid_label(label: str) -> bool:
    """The core's `_valid_a_label` (`horae.contracts.types`): an `xn--` label must round-trip
    through the IDNA codec and decode to letters, marks, digits and `-` only."""
    if not label.startswith("xn--"):
        return True
    try:
        decoded = label.encode("ascii").decode("idna")
        if decoded.encode("idna").decode("ascii") != label:
            return False
    except UnicodeError:
        return False
    return decoded != label and all(
        char == "-" or unicodedata.category(char) in _U_LABEL_CATEGORIES for char in decoded
    )


def local_endpoint(entry: object) -> str | None:
    """`entry` if it is a valid local endpoint (`host:port`, explicit port), else `None`."""
    if not isinstance(entry, str):
        return None
    host, sep, port = entry.rpartition(":")
    if not sep or not _PORT.fullmatch(port) or int(port) > 65535 or not local_host(host):
        return None
    return entry


_ANY_URL = re.compile(
    # `http` or `https` + host (a name or IPv4 literal, or a bracketed IPv6 literal) + optional
    # port + optional path/query (no fragment). No user info: `@` cannot be in the host, and
    # after the host only `:port`, `/` or `?` may follow.
    r"(?P<scheme>https?)://(?P<host>\[[0-9a-f:]+\]|[^/?#:@\\\[\]\x00-\x20\x7f]+)"
    r"(?::(?P<port>[1-9][0-9]{0,4}))?"
    r"(?:[/?][^\x00-\x20\x7f\\#]*)?\Z"
)
_DEFAULT_PORT = {"http": 80, "https": 443}


def url_endpoint(url: object) -> tuple[str, str, int] | None:
    """`(scheme, host, port)` of a well-formed `http(s)` URL, the host exactly as written and
    the port the URL's or the scheme's default (80, 443); `None` for anything else."""
    if not isinstance(url, str):
        return None
    match = _ANY_URL.match(url)
    if match is None:
        return None
    port = int(match["port"]) if match["port"] is not None else _DEFAULT_PORT[match["scheme"]]
    if port > 65535:
        return None
    return match["scheme"], match["host"], port


def _local_entries(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if local_endpoint(entry) is not None)


@dataclass(frozen=True)
class Network:
    """The plugin's network allow-list as approved in `horae.lock` (trusted, from the core).

    `hosts` is `None` when the core did not send one (a core older than horae-sdk 0.5): the
    list is then **unknown**, which is not the same as empty (no host approved). `local` holds
    the approved local-network endpoints (`host:port`, horae-sdk 0.6, decision 66)."""

    hosts: tuple[str, ...] | None = None
    local: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, value: object, local: object = None) -> "Network":
        """From `_meta["horae/network"]` and `_meta["horae/network_local"]`. Both absent:
        unknown. Present but not a list (a core bug): nothing approved, never "unknown", so a
        malformed value fails closed. `horae/network` without `horae/network_local` (a core
        with decision 63 only) grants no local endpoint."""
        if value is None and local is None:
            return cls()
        if not isinstance(value, list):
            return cls((), _local_entries(local))
        # An entry that is not a valid `Hostname` cannot be in a lock: it matches nothing.
        valid = (
            host
            for host in value
            if isinstance(host, str) and len(host) <= 253 and _HOSTNAME.fullmatch(host)
        )
        return cls(tuple(valid), _local_entries(local))

    @property
    def known(self) -> bool:
        return self.hosts is not None

    def allows(self, url: object) -> bool | None:
        """Whether `url` may be contacted: `None` when the core sent no list (unknown: the
        plugin decides), else `True` only for

        - an `https` URL whose host is exactly one of the approved hosts. The lock's semantics
          (`horae.contracts.Hostname`, `HttpsUrl`, schemas decision 63): hosts are lowercase DNS
          names compared as written, with no wildcard; any port is accepted on an approved host
          (decision 10);
        - an `http` or `https` URL whose host **and** port are exactly an approved local
          endpoint (decision 66); a URL without a port means 80 (`http`) or 443 (`https`).

        There is no normalisation or resolution, so a different case, a trailing dot, a
        non-ASCII (non-`xn--`) name, user info, another spelling of an IP literal, another port
        or another scheme is refused, and a public name never matches a local endpoint."""
        if self.hosts is None:
            return None
        if url_host(url) in self.hosts:
            return True
        endpoint = url_endpoint(url)
        if endpoint is None:
            return False
        _scheme, host, port = endpoint
        return f"{host}:{port}" in self.local

    def address_allowed(self, host: object, port: object) -> bool | None:
        """For a protocol that is not HTTP (IMAP, SMTP, MQTT...): whether `host` and `port` may
        be contacted. `None` when unknown (as `allows`); else `True` when `host` is exactly an
        approved host (any port) or `host:port` exactly an approved local endpoint. An IPv6
        literal may be given with or without brackets; nothing else is normalised."""
        if self.hosts is None:
            return None
        if type(port) is not int or not 0 < port <= 65535 or not isinstance(host, str):
            return False
        if host in self.hosts:
            return True
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{port}" in self.local


def meta_of(meta: Any) -> Mapping[str, Any]:
    """A request's `_meta` as a mapping (`{}` when absent)."""
    return meta if isinstance(meta, Mapping) else {}


__all__ = [
    "INPUT_META",
    "INTENTS_META",
    "MAX_INTENTS",
    "MAX_MESSAGE_BYTES",
    "MAX_NOTE_BYTES",
    "MESSAGE_MARGIN",
    "NETWORK_LOCAL_META",
    "NETWORK_META",
    "PATHS_META",
    "Network",
    "Paths",
    "local_endpoint",
    "local_host",
    "meta_of",
    "url_endpoint",
    "url_host",
]
