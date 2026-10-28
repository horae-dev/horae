# SPDX-License-Identifier: Apache-2.0
"""Redaction of secret values, shared by every channel (#24, PR #85 answer 4; #53).

Moved here from `horae.plugins.host.redact` (#53, which re-exports it) so that the store, the
engine, the LLM layer, the log scrubber (`horae.secrets.logs`) and the CLI use the same
redactor without importing the plugin host.

**The process registry** (#53, schemas decision 43). Every `SecretResolver` remembers each
secret it resolves here (`remember`), so `current()` is a `Redactor` of every secret this
process has resolved, by any resolver (the plugin host's, an LLM backend's, the CLI's).
The store's last line of defence, the plan-time refusal of trusted inputs, the LLM layer, the
log record factory and the CLI output all redact with it. It only grows (a rotated value
stays known, so its old value is still redacted); `forget_all()` is for tests.

In the plugin host, the patterns are **every secret the instance resolved**
(`SecretResolver.resolved()`; the host resolves every secret it knows of when it opens,
schemas decision 35 B), not only the plugin's own: a plugin can echo data that came from
another step or another plugin.

**Every secret is at least `MIN_SECRET_BYTES` (8 bytes) of UTF-8** (#53, maintainer decision
recorded in schemas decision 43 H, superseding decision 35 D): `SecretResolver` refuses a
shorter value, and `Redactor` raises `ValueError` if it is handed one. A secret is matched
anywhere, as UTF-8, in these forms:

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

A JSON number whose text (`json.dumps`) is exactly a secret's value (`pin: 12345678`) is a
**numeric match**: `redact_data` replaces it, `find` reports it, and `scrub` redacts, flags or
ignores it as asked (the store flags it without changing the number's type).

Each match becomes `[REDACTED:<NAME>]` (the name is not sensitive). Patterns are tried longest
first, so a value that contains another secret is replaced whole. **Overlaps:** when the
matches of two secrets overlap (one value's end is another's start), only the leftmost one is
replaced and the rest of the other stays visible. Other encodings (gzip, encryption, a value
split across messages…) are not recognised: the redactor is a safety net for honest plugins,
not a defence against a hostile one (M5).

`redact_data` walks a JSON value iteratively (string and number leaves), so any nesting is
safe. It never rewrites an object key: a key holding a secret raises `SecretInKey` (the host
turns it into `output_invalid`, and nothing is stored). `scrub` (the store's and the CLI's
variant) rewrites such a key instead; when two keys of one object redact to the same text,
the later one is made unique (`[REDACTED:NAME]#2`, `#3`…) and the collision is reported
(`Scrubbed.collisions`), so nothing is lost silently (schemas decision 43 G).
"""

import base64
import binascii
import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

if TYPE_CHECKING:  # `horae.secrets` imports this module: no import cycle at run time
    from horae.secrets import Secret, SecretResolver

MAX_EXPANSION = 6
"""How much longer than its UTF-8 value an encoded form can be (a JSON `\\u00XX` escape)."""
MIN_SECRET_BYTES = 8
"""Shortest secret value accepted, in UTF-8 bytes (`SecretResolver` refuses a shorter one)."""
MIN_PARTIAL_BYTES = MIN_SECRET_BYTES
"""Former name of `MIN_SECRET_BYTES` (decision 35 D, superseded by decision 43 H)."""


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


def encodings(value: str) -> frozenset[str]:
    """The forms of `value` the redactor looks for (tests assert none of them leaks)."""
    return frozenset(_forms(value))


def too_short(value: str) -> bool:
    """Whether `value` is under `MIN_SECRET_BYTES` (refused: it cannot be redacted reliably)."""
    return len(value.encode("utf-8")) < MIN_SECRET_BYTES


NumberMode = Literal["redact", "flag", "ignore"]
"""What `Redactor.scrub` does with a number whose text is a secret's value."""


@dataclass(frozen=True)
class Scrubbed:
    """What `Redactor.scrub` returns."""

    value: Any
    """The copy, redacted."""
    redacted: frozenset[str]
    """The names of the secrets replaced."""
    flagged: frozenset[str]
    """The names of the secrets found in numbers kept as they were (`numbers="flag"`)."""
    collisions: frozenset[str]
    """The names of the secrets whose redaction made two keys of one object equal (the later
    key was made unique: `[REDACTED:NAME]#2`)."""


class Redactor:
    """Replaces secret values (and their encodings) with `[REDACTED:<NAME>]`."""

    def __init__(self, secrets: "Iterable[Secret]") -> None:
        owner: dict[str, str] = {}
        self._whole: dict[str, str] = {}
        """Every value to its name, for numeric (whole-value) matches."""
        for secret in secrets:
            value = secret.reveal()
            if too_short(value):
                raise ValueError(
                    f"secret {secret.name} is shorter than {MIN_SECRET_BYTES} bytes: "
                    "it cannot be redacted reliably"
                )
            self._whole.setdefault(value, secret.name)
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

    def counts(self, data: bytes) -> dict[str, int]:
        """How many times each secret is matched in `data`, by name."""
        found: dict[str, int] = {}
        for _start, _end, name in self.spans(data):
            found[name] = found.get(name, 0) + 1
        return found

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

    def text_spans(self, text: str) -> list[tuple[int, int, str]]:
        """`(start, end, name)` of every match in `text` (character offsets), left to right,
        not overlapping."""
        if self._text is None:
            return []
        return [(m.start(), m.end(), self._names[m.group(0)]) for m in self._text.finditer(text)]

    def names_in(self, text: str) -> set[str]:
        """The names of the secrets matched in `text`."""
        if self._text is None:
            return set()
        return {self._names[m.group(0)] for m in self._text.finditer(text)}

    def number(self, value: Any) -> str | None:
        """The name of the secret whose value is the text of the JSON number `value`."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return self._whole.get(json.dumps(value))

    def find(self, value: Any) -> set[str]:
        """The names of the secrets anywhere in the JSON `value`: string leaves, object keys
        and numbers (whole value). For the checks that refuse (plan time, the LLM cache)."""
        found: set[str] = set()
        if not self._whole:
            return found
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                found |= self.names_in(item)
            elif isinstance(item, dict):
                for key, child in item.items():
                    stack.extend((key, child))
            elif isinstance(item, (list, tuple)):
                stack.extend(item)
            else:
                name = self.number(item)
                if name is not None:
                    found.add(name)
        return found

    def redact_data(self, value: Any) -> tuple[Any, set[str]]:
        """A copy of the JSON `value` with every string and number leaf redacted, and the
        names of the secrets found. Raises `SecretInKey` if an object key holds a secret."""
        result = self._walk(value, rewrite_keys=False, numbers="redact")
        return result.value, set(result.redacted)

    def scrub(self, value: Any, *, numbers: NumberMode = "flag") -> Scrubbed:
        """The store's and the CLI's variant of `redact_data` (#53): string leaves **and
        object keys** are redacted (a key holding a secret is rewritten, never raised; a key
        that becomes equal to another key of its object is made unique, see `Scrubbed`), and
        numbers as `numbers` says."""
        return self._walk(value, rewrite_keys=True, numbers=numbers)

    def scrub_data(self, value: Any) -> tuple[Any, set[str]]:
        """`scrub(value, numbers="redact")`: the copy and the names of the secrets found."""
        result = self.scrub(value, numbers="redact")
        return result.value, set(result.redacted)

    def _key(
        self, key: Any, original: dict[Any, Any], mapping: dict[Any, Any], rewrite: bool
    ) -> tuple[Any, set[str], bool]:
        """The key `key` becomes, the names found in it, and whether it collided."""
        if not isinstance(key, str):
            return key, set(), False
        new, names = self.redact_text(key)
        if not names:
            return key, names, False
        if not rewrite:
            raise SecretInKey(min(names))
        if new not in mapping and new not in original:
            return new, names, False
        count = 2
        while f"{new}#{count}" in mapping or f"{new}#{count}" in original:
            count += 1
        return f"{new}#{count}", names, True

    def _walk(self, value: Any, *, rewrite_keys: bool, numbers: NumberMode) -> Scrubbed:
        found: set[str] = set()
        flagged: set[str] = set()
        collisions: set[str] = set()
        if not self._whole:
            return Scrubbed(value, frozenset(), frozenset(), frozenset())

        root: list[Any] = [None]
        stack: list[tuple[Any, Any, Any]] = [(value, root, 0)]
        while stack:
            item, parent, key = stack.pop()
            if isinstance(item, str):
                new, names = self.redact_text(item)
                found.update(names)
                parent[key] = new
            elif isinstance(item, dict):
                mapping: dict[Any, Any] = {}
                parent[key] = mapping
                for child_key, child in item.items():
                    new_key, names, collided = self._key(child_key, item, mapping, rewrite_keys)
                    found.update(names)
                    if collided:
                        collisions.update(names)
                    mapping[new_key] = None
                    stack.append((child, mapping, new_key))
            elif isinstance(item, list):
                sequence: list[Any] = [None] * len(item)
                parent[key] = sequence
                stack.extend((child, sequence, index) for index, child in enumerate(item))
            else:
                name = None if numbers == "ignore" else self.number(item)
                if name is not None and numbers == "redact":
                    found.add(name)
                    parent[key] = token(name)
                else:
                    if name is not None:
                        flagged.add(name)
                    parent[key] = item
        return Scrubbed(root[0], frozenset(found), frozenset(flagged), frozenset(collisions))


# --- the process registry (#53) -----------------------------------------------------------

_log = logging.getLogger("horae.secrets")
_lock = threading.Lock()
_known: "dict[tuple[str, str], Secret]" = {}
"""Every secret resolved in this process, by `(name, value)`, in resolution order."""
_cache: "tuple[int, Redactor] | None" = None


def remember(secret: "Secret") -> None:
    """Add `secret` to the process registry (`SecretResolver.resolve` calls it)."""
    global _cache
    key = (secret.name, secret.reveal())
    with _lock:
        if key not in _known:
            _known[key] = secret
            _cache = None


def known() -> "tuple[Secret, ...]":
    """Every secret resolved in this process so far, in resolution order."""
    with _lock:
        return tuple(_known.values())


def current() -> Redactor:
    """A `Redactor` of every secret resolved in this process (rebuilt when one is added)."""
    global _cache
    with _lock:
        cache = _cache
        if cache is None or cache[0] != len(_known):
            cache = _cache = (len(_known), Redactor(tuple(_known.values())))
        return cache[1]


def redact(text: str) -> str:
    """`text` with every secret of the process registry redacted."""
    return current().redact_text(text)[0]


def prime(resolver: "SecretResolver", names: Iterable[str]) -> None:
    """Resolve `names` with `resolver`, for redaction only: a secret that cannot be resolved
    is skipped (what needs it fails later, with its own error); one refused as too short
    (`MIN_SECRET_BYTES`) is skipped with a warning naming it (`horae.secrets`, never its
    value or its length)."""
    from horae.secrets import SecretError

    for name in sorted(set(names)):
        try:
            resolver.resolve(name)
        except SecretError as error:
            too_short_value = "too short" in error.reason
            del error
            if too_short_value:  # the name only (a validated `SecretName`), never a value
                _log.warning(
                    "secret %s is too short for reliable redaction (min 8 bytes): "
                    "refused, so it is not primed and what needs it fails",
                    name,
                )


def forget_all() -> None:
    """Empty the process registry (tests only: a process never forgets a secret)."""
    global _cache
    with _lock:
        _known.clear()
        _cache = None


__all__ = [
    "MAX_EXPANSION",
    "MIN_PARTIAL_BYTES",
    "MIN_SECRET_BYTES",
    "NumberMode",
    "Redactor",
    "Scrubbed",
    "SecretInKey",
    "current",
    "encodings",
    "forget_all",
    "known",
    "prime",
    "redact",
    "remember",
    "token",
    "too_short",
]
