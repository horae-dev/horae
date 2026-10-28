# SPDX-License-Identifier: Apache-2.0
"""Redaction of secret values from what a plugin writes or returns (#24, PR #85 answer 4).

The patterns are **every secret the instance resolved** (`SecretResolver.resolved()`), not only
the plugin's own: a plugin can echo data that came from another step. For each value the
redactor matches, as UTF-8:

- the value itself;
- its base64 (standard and URL-safe alphabets, with and without `=` padding);
- its percent-encoding (`urllib.parse.quote(value, safe="")`, upper- and lower-case hex);
- its JSON string escaping (`json.dumps`, with and without `ensure_ascii`, quotes removed).

Each match becomes `[REDACTED:<NAME>]` (the name is not sensitive). Patterns are tried longest
first, so a value that contains another secret is replaced whole. A value embedded in a longer
base64 stream at an offset that is not a multiple of 3 is **not** recognised (its base64 then
depends on the neighbouring bytes); nor are other encodings (hex, gzip, encryption…). The
redactor is a safety net for honest plugins, not a defence against a hostile one (M5).

`redact_data` walks a JSON value iteratively (keys and string values), so any nesting is safe.
"""

import base64
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from horae.secrets import Secret

MAX_EXPANSION = 6
"""How much longer than its UTF-8 value an encoded form can be (a JSON `\\u00XX` escape)."""


def token(name: str) -> str:
    """What a match of secret `name` is replaced with."""
    return f"[REDACTED:{name}]"


def _forms(value: str) -> set[str]:
    raw = value.encode("utf-8")
    forms = {value}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(raw).decode("ascii")
        forms |= {encoded, encoded.rstrip("=")}
    percent = quote(value, safe="")
    forms |= {percent, re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), percent)}
    for ascii_only in (True, False):
        forms.add(json.dumps(value, ensure_ascii=ascii_only)[1:-1])
    forms.discard("")
    return forms


class Redactor:
    """Replaces secret values (and their encodings) with `[REDACTED:<NAME>]`."""

    def __init__(self, secrets: Iterable[Secret]) -> None:
        owner: dict[str, str] = {}
        for secret in secrets:
            for form in _forms(secret.reveal()):
                owner.setdefault(form, secret.name)
        ordered = sorted(owner, key=lambda form: (-len(form.encode("utf-8")), form))
        self._names = dict(owner)
        self._bytes_names = {form.encode("utf-8"): name for form, name in owner.items()}
        self._text = re.compile("|".join(map(re.escape, ordered))) if ordered else None
        self._bytes = (
            re.compile(b"|".join(re.escape(form.encode("utf-8")) for form in ordered))
            if ordered
            else None
        )
        self.max_len = max((len(form.encode("utf-8")) for form in ordered), default=0)
        """The longest pattern, in bytes."""

    def __bool__(self) -> bool:
        return self._text is not None

    def spans(self, data: bytes) -> list[tuple[int, int, str]]:
        """`(start, end, name)` of every match in `data`, left to right, not overlapping."""
        if self._bytes is None:
            return []
        return [
            (m.start(), m.end(), self._bytes_names[m.group(0)]) for m in self._bytes.finditer(data)
        ]

    def redact_bytes(self, data: bytes) -> bytes:
        """`data` with every match replaced."""
        if self._bytes is None:
            return data
        return self._bytes.sub(lambda m: token(self._bytes_names[m.group(0)]).encode(), data)

    def redact_text(self, text: str) -> tuple[str, set[str]]:
        """`text` with every match replaced, and the names of the secrets found."""
        if self._text is None:
            return text, set()
        found: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            name = self._names[match.group(0)]
            found.add(name)
            return token(name)

        return self._text.sub(replace, text), found

    def redact_data(self, value: Any) -> tuple[Any, set[str]]:
        """A copy of the JSON `value` with every key and string redacted, and the names of the
        secrets found. Other values are kept as they are (the engine checks them)."""
        found: set[str] = set()
        if self._text is None:
            return value, found

        def fix(text: str) -> str:
            new, names = self.redact_text(text)
            found.update(names)
            return new

        root: list[Any] = [None]
        stack: list[tuple[Any, Any, Any]] = [(value, root, 0)]
        while stack:
            item, parent, key = stack.pop()
            if isinstance(item, str):
                parent[key] = fix(item)
            elif isinstance(item, dict):
                mapping: dict[Any, Any] = {}
                parent[key] = mapping
                for child_key, child in item.items():
                    new_key = fix(child_key) if isinstance(child_key, str) else child_key
                    mapping[new_key] = None
                    stack.append((child, mapping, new_key))
            elif isinstance(item, list):
                sequence: list[Any] = [None] * len(item)
                parent[key] = sequence
                stack.extend((child, sequence, index) for index, child in enumerate(item))
            else:
                parent[key] = item
        return root[0], found


__all__ = ["MAX_EXPANSION", "Redactor", "token"]
