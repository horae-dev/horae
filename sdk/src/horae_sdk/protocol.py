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
- Messages are capped at `MAX_MESSAGE_BYTES` (8 MiB) by the host: a larger one costs the
  plugin its process (`output_too_large`).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

INPUT_META = "dev.horae/input"
INTENTS_META = "dev.horae/intents"
PATHS_META = "dev.horae/paths"
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MESSAGE_MARGIN = 64 * 1024
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


def meta_of(meta: Any) -> Mapping[str, Any]:
    """A request's `_meta` as a mapping (`{}` when absent)."""
    return meta if isinstance(meta, Mapping) else {}


__all__ = [
    "INPUT_META",
    "INTENTS_META",
    "MAX_MESSAGE_BYTES",
    "MAX_NOTE_BYTES",
    "MESSAGE_MARGIN",
    "PATHS_META",
    "Paths",
    "meta_of",
]
