# SPDX-License-Identifier: Apache-2.0
"""Redaction of secret values from what a plugin writes or returns (#24, PR #85 answer 4).

The patterns are **every secret the instance resolved** (`SecretResolver.resolved()`; the host
resolves every secret it knows of when it opens, schemas decision 35 B), not only the plugin's
own: a plugin can echo data that came from another step or another plugin.

**Long secrets** (at least `MIN_PARTIAL_BYTES`, 8 bytes, of UTF-8) are matched anywhere, as
UTF-8, in these forms:

- the value itself;
- its base64 (standard and URL-safe alphabets, with and without `=` padding), and the
  **misaligned** base64 of the value embedded in a longer stream (`user:<secret>` in an HTTP
  Basic header) at byte offsets 1 and 2 modulo 3: for offset `k` and `n` bytes, the characters
  `ceil(8k/6)` to `floor((8k + 8n)/6)` (exclusive) of the encoding, the only ones that depend
  on the value alone (the one or two characters around them also depend on the neighbouring
  bytes, so up to 4 bits of the value can stay visible on each side);
- its lower- and upper-case hex;
- its percent-encoding (`urllib.parse.quote(value, safe="")`, upper- and lower-case hex);
- its JSON string escaping (`json.dumps`, with and without `ensure_ascii`, quotes removed).

**Short secrets** (under 8 bytes) are only matched as a **whole** value: a JSON string or
number that is exactly the secret, or a message that is exactly it. Matching them inside text
would rewrite unrelated output (and confirm the value by its absence); they are not looked for
in stderr.

Each match becomes `[REDACTED:<NAME>]` (the name is not sensitive). Patterns are tried longest
first, so a value that contains another secret is replaced whole. **Overlaps:** when the
matches of two secrets overlap (one value's end is another's start), only the leftmost one is
replaced and the rest of the other stays visible. Other encodings (gzip, encryption, a value
split across messages…) are not recognised: the redactor is a safety net for honest plugins,
not a defence against a hostile one (M5).

`redact_data` walks a JSON value iteratively (string and number leaves), so any nesting is
safe. It never rewrites an object key: a key holding a secret raises `SecretInKey` (the host
turns it into `output_invalid`, and nothing is stored).
"""

import base64
import binascii
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from horae.secrets import Secret

MAX_EXPANSION = 6
"""How much longer than its UTF-8 value an encoded form can be (a JSON `\\u00XX` escape)."""
MIN_PARTIAL_BYTES = 8
"""Secrets shorter than this (UTF-8 bytes) are only matched as whole values."""


def token(name: str) -> str:
    """What a match of secret `name` is replaced with."""
    return f"[REDACTED:{name}]"


class SecretInKey(ValueError):
    """An object key of a value holds secret `name` (keys are never rewritten)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"an object key holds secret {name}")
        self.name = name


def _base64_forms(raw: bytes) -> set[str]:
    forms: set[str] = set()
    n = len(raw)
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        aligned = encode(raw).decode("ascii")
        forms |= {aligned, aligned.rstrip("=")}
        for offset in (0, 1, 2):
            encoded = encode(b"\0" * offset + raw + b"\0\0").decode("ascii")
            start = -(-8 * offset // 6)
            end = (8 * offset + 8 * n) // 6
            forms.add(encoded[start:end])
    return forms


def _forms(value: str) -> set[str]:
    raw = value.encode("utf-8")
    forms = {value} | _base64_forms(raw)
    hexed = binascii.hexlify(raw).decode("ascii")
    forms |= {hexed, hexed.upper()}
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
        self._whole: dict[str, str] = {}
        """Every value (long or short) to its name, for whole-value matches."""
        for secret in secrets:
            value = secret.reveal()
            self._whole.setdefault(value, secret.name)
            if len(value.encode("utf-8")) >= MIN_PARTIAL_BYTES:
                for form in _forms(value):
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
        return bool(self._whole)

    def spans(self, data: bytes) -> list[tuple[int, int, str]]:
        """`(start, end, name)` of every match in `data`, left to right, not overlapping."""
        if self._bytes is None:
            return []
        return [
            (m.start(), m.end(), self._bytes_names[m.group(0)]) for m in self._bytes.finditer(data)
        ]

    def redact_bytes(self, data: bytes) -> bytes:
        """`data` with every match of a long secret replaced."""
        if self._bytes is None:
            return data
        return self._bytes.sub(lambda m: token(self._bytes_names[m.group(0)]).encode(), data)

    def redact_text(self, text: str) -> tuple[str, set[str]]:
        """`text` with every match replaced (a short secret only when it is all of `text`),
        and the names of the secrets found."""
        whole = self._whole.get(text)
        if whole is not None:
            return token(whole), {whole}
        if self._text is None:
            return text, set()
        found: set[str] = set()

        def replace(match: re.Match[str]) -> str:
            name = self._names[match.group(0)]
            found.add(name)
            return token(name)

        return self._text.sub(replace, text), found

    def _in_key(self, key: str) -> str | None:
        whole = self._whole.get(key)
        if whole is not None:
            return whole
        match = self._text.search(key) if self._text is not None else None
        return None if match is None else self._names[match.group(0)]

    def redact_data(self, value: Any) -> tuple[Any, set[str]]:
        """A copy of the JSON `value` with every string and number leaf redacted, and the
        names of the secrets found. Raises `SecretInKey` if an object key holds a secret."""
        found: set[str] = set()
        if not self._whole:
            return value, found

        root: list[Any] = [None]
        stack: list[tuple[Any, Any, Any]] = [(value, root, 0)]
        while stack:
            item, parent, key = stack.pop()
            if isinstance(item, str):
                new, names = self.redact_text(item)
                found.update(names)
                parent[key] = new
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                name = self._whole.get(json.dumps(item))
                if name is not None:
                    found.add(name)
                    parent[key] = token(name)
                else:
                    parent[key] = item
            elif isinstance(item, dict):
                mapping: dict[Any, Any] = {}
                parent[key] = mapping
                for child_key, child in item.items():
                    if isinstance(child_key, str):
                        secret = self._in_key(child_key)
                        if secret is not None:
                            raise SecretInKey(secret)
                    mapping[child_key] = None
                    stack.append((child, mapping, child_key))
            elif isinstance(item, list):
                sequence: list[Any] = [None] * len(item)
                parent[key] = sequence
                stack.extend((child, sequence, index) for index, child in enumerate(item))
            else:
                parent[key] = item
        return root[0], found


__all__ = ["MAX_EXPANSION", "MIN_PARTIAL_BYTES", "Redactor", "SecretInKey", "token"]
