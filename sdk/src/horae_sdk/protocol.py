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
- Messages are capped at `MAX_MESSAGE_BYTES` (8 MiB) by the host: a larger one costs the
  plugin its process (`output_too_large`). The SDK refuses such a result before sending it
  (a tool error, or a sink operation's `output_too_large`), keeping `MESSAGE_MARGIN` for the
  JSON-RPC envelope.
- One step proposes at most `MAX_INTENTS` (256) intents (`horae.intents.MAX_INTENTS`): the
  core refuses the whole step otherwise (`batch_too_large`), so the SDK refuses it first.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

INPUT_META = "dev.horae/input"
INTENTS_META = "dev.horae/intents"
PATHS_META = "dev.horae/paths"
NETWORK_META = "horae/network"
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


@dataclass(frozen=True)
class Network:
    """The plugin's network allow-list as approved in `horae.lock` (trusted, from the core).

    `hosts` is `None` when the core did not send one (a core older than horae-sdk 0.5): the
    list is then **unknown**, which is not the same as empty (no host approved)."""

    hosts: tuple[str, ...] | None = None

    @classmethod
    def from_wire(cls, value: object) -> "Network":
        """From `_meta["horae/network"]`. Absent: unknown. Present but not a list (a core
        bug): no host approved, never "unknown", so a malformed value fails closed."""
        if value is None:
            return cls()
        if not isinstance(value, list):
            return cls(())
        # An entry that is not a valid `Hostname` cannot be in a lock: it matches nothing.
        valid = (
            host
            for host in value
            if isinstance(host, str) and len(host) <= 253 and _HOSTNAME.fullmatch(host)
        )
        return cls(tuple(valid))

    @property
    def known(self) -> bool:
        return self.hosts is not None

    def allows(self, url: object) -> bool | None:
        """Whether `url` may be contacted: `None` when the core sent no list (unknown: the
        plugin decides), else `True` only for an `https` URL whose host is exactly one of the
        approved hosts. The lock's semantics (`horae.contracts.Hostname`, `HttpsUrl`, schemas
        decision 63): hosts are lowercase DNS names compared as written, with no wildcard; any
        port is accepted on an approved host (decision 10); there is no normalisation, so a
        different case, a trailing dot, a non-ASCII (non-`xn--`) name, user info, an IP literal
        or another scheme is refused."""
        if self.hosts is None:
            return None
        host = url_host(url)
        return host is not None and host in self.hosts


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
    "NETWORK_META",
    "PATHS_META",
    "Network",
    "Paths",
    "meta_of",
    "url_host",
]
