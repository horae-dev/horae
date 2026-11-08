# SPDX-License-Identifier: Apache-2.0
"""The `/act` token: a fixed binary payload and its HMAC, one base64url segment (#38, schemas
decision 53 B).

```
token   = b64url(payload || tag)                       (no padding)
payload = ver(1) kid(4) kind(1) exp(8) nonce(16) state(8) dur(1) fp_len(1) fingerprint
tag     = HMAC-SHA256(key, "horae-act/v1\\0" || payload)  (32 bytes)
```

- `ver` is 1; `kid` the key id (`horae.api.act._key`); `kind` 1 `task.done`, 2 `task.snooze`;
  `exp` the expiry, Unix seconds, big-endian; `nonce` 16 random bytes (single use: the store
  records it when the token is used); `state` the first 8 bytes of SHA-256 over the task's
  `(status, due, snoozed_until)` when the link was minted (`state_digest`); `dur` 0 for
  `task.done`, 1 `1h`, 2 `3h`, 3 `tomorrow` for `task.snooze`; the fingerprint is 1 to 128
  characters of `A-Z a-z 0-9 . _ : -`.
- So a token is 73 to 200 bytes, 98 to 267 characters (`TOKEN_PATTERN`). It carries **no
  secret, URL or title**: a kind, a fingerprint, numbers and random bytes. It is signed, not
  encrypted: whoever sees the link can read the fingerprint (maintainer decision 4).
- `verify` is strict and total: anything but the exact canonical spelling of a token this key
  minted, unexpired and expiring at most `MAX_AHEAD_S` ahead, gives `None`, never an
  exception; every string pays for one MAC over a buffer of the same size (`_MAC_INPUT`), so
  the work does not depend on which check failed.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import struct
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from horae.api.act._key import ActKey

VERSION = 1
TOKEN_TTL_S = 24 * 3600
"""A token expires 24 hours after it is minted."""
MAX_AHEAD_S = TOKEN_TTL_S + 300
"""A token whose expiry is further ahead than this is refused (a clock that jumped forward at
mint time, or a bug): 24 h 05 min."""
NONCE_BYTES = 16
STATE_BYTES = 8
MAX_FINGERPRINT = 128

ActKindName = Literal["task.done", "task.snooze"]
DurationName = Literal["1h", "3h", "tomorrow"]
KINDS: dict[str, int] = {"task.done": 1, "task.snooze": 2}
DURATIONS: dict[str, int] = {"1h": 1, "3h": 2, "tomorrow": 3}
_KIND_NAMES: dict[int, ActKindName] = {1: "task.done", 2: "task.snooze"}
_DURATION_NAMES: dict[int, DurationName] = {1: "1h", 2: "3h", 3: "tomorrow"}

_HEADER = struct.Struct(">B4sBQ16s8sBB")
"""ver, kid, kind, exp, nonce, state, dur, fp_len: 40 bytes."""
_TAG_BYTES = 32
MIN_RAW = _HEADER.size + 1 + _TAG_BYTES
MAX_RAW = _HEADER.size + MAX_FINGERPRINT + _TAG_BYTES
MIN_TOKEN_CHARS = -(-MIN_RAW * 4 // 3)
MAX_TOKEN_CHARS = -(-MAX_RAW * 4 // 3)
TOKEN_PATTERN = rf"^[A-Za-z0-9_-]{{{MIN_TOKEN_CHARS},{MAX_TOKEN_CHARS}}}$"
"""The only strings `/act/{token}` considers: `^[A-Za-z0-9_-]{98,267}$`."""
_TOKEN = re.compile(TOKEN_PATTERN)
_FINGERPRINT = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAC_INPUT = _HEADER.size + MAX_FINGERPRINT
"""The MAC of every candidate is computed over this many payload bytes (zero-padded for the
dummy MAC of a malformed string), so a malformed string costs the same MAC as a valid one."""


@dataclass(frozen=True)
class ActToken:
    """A verified token's content (never the token string)."""

    kind: ActKindName
    task: str
    expires_at: int
    nonce: bytes
    state: bytes
    duration: DurationName | None
    kid: bytes

    @property
    def ref(self) -> str:
        """The first 8 hex digits of SHA-256(nonce): how logs and pages name a token."""
        return hashlib.sha256(self.nonce).hexdigest()[:8]


def state_digest(status: str, due: date | None, snoozed_until: date | datetime | None) -> bytes:
    """The first 8 bytes of SHA-256 over a task's `(status, due, snoozed_until)` (canonical
    JSON, dates and times in ISO 8601 as the task index writes them): a link is stale once
    any of the three changed (maintainer decision 10)."""
    values = [
        status,
        None if due is None else due.isoformat(),
        None if snoozed_until is None else snoozed_until.isoformat(),
    ]
    text = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).digest()[:STATE_BYTES]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def mint(
    key: ActKey,
    *,
    kind: str,
    task: str,
    state: bytes,
    now: int,
    duration: str | None = None,
    nonce: bytes | None = None,
) -> str:
    """A token for `kind` on task `task` in state `state`, expiring `TOKEN_TTL_S` after `now`
    (Unix seconds). `ValueError` for an invalid kind, duration, fingerprint or state."""
    if kind not in KINDS:
        raise ValueError(f"unknown action kind {kind!r}")
    if (kind == "task.snooze") != (duration is not None):
        raise ValueError("a duration is required for task.snooze and only allowed there")
    if duration is not None and duration not in DURATIONS:
        raise ValueError(f"unknown snooze duration {duration!r}")
    if _FINGERPRINT.fullmatch(task) is None:
        raise ValueError("invalid task fingerprint")
    if type(state) is not bytes or len(state) != STATE_BYTES:
        raise ValueError("a state digest is 8 bytes")
    nonce = secrets.token_bytes(NONCE_BYTES) if nonce is None else nonce
    if type(nonce) is not bytes or len(nonce) != NONCE_BYTES:
        raise ValueError("a nonce is 16 bytes")
    fingerprint = task.encode("ascii")
    header = _HEADER.pack(
        VERSION,
        key.kid,
        KINDS[kind],
        now + TOKEN_TTL_S,
        nonce,
        state,
        0 if duration is None else DURATIONS[duration],
        len(fingerprint),
    )
    payload = header + fingerprint
    return _b64(payload + key.tag(payload))


def _decode(token: object) -> bytes | None:
    """The bytes of `token` if it is exactly a canonical unpadded base64url string of a
    plausible size, else `None`."""
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if not MIN_RAW <= len(raw) <= MAX_RAW or _b64(raw) != token:
        return None  # one token, one spelling (no stray bits in the last character)
    return raw


def verify(key: ActKey, token: object, now: int) -> ActToken | None:
    """The content of `token` if this key minted it and it is valid at `now` (Unix seconds),
    else `None` (whatever `token` is: never raises). Checked after the tag: the version, the
    key id, a known kind and a duration that fits it, the fingerprint's length and alphabet,
    `now < exp <= now + MAX_AHEAD_S`. Whether it was used is the store's to say."""
    raw = _decode(token)
    if raw is None:
        key.tag(bytes(_MAC_INPUT))  # the same MAC work as a candidate token
        return None
    payload, tag = raw[:-_TAG_BYTES], raw[-_TAG_BYTES:]
    expected = key.tag(payload)
    if not hmac.compare_digest(expected, tag):
        return None
    version, kid, kind, exp, nonce, state, dur, length = _HEADER.unpack_from(payload)
    fingerprint = payload[_HEADER.size :]
    if (
        version != VERSION
        or not hmac.compare_digest(kid, key.kid)
        or kind not in _KIND_NAMES
        or length != len(fingerprint)
        or not 1 <= length <= MAX_FINGERPRINT
    ):
        return None
    name = _KIND_NAMES[kind]
    duration = _DURATION_NAMES.get(dur)
    if (name == "task.done" and dur != 0) or (name == "task.snooze" and duration is None):
        return None
    try:
        task = fingerprint.decode("ascii")
    except UnicodeDecodeError:
        return None
    if _FINGERPRINT.fullmatch(task) is None:
        return None
    if not now < exp <= now + MAX_AHEAD_S:
        return None
    return ActToken(name, task, exp, nonce, state, duration, kid)


__all__ = [
    "DURATIONS",
    "KINDS",
    "MAX_AHEAD_S",
    "MAX_TOKEN_CHARS",
    "MIN_TOKEN_CHARS",
    "TOKEN_PATTERN",
    "TOKEN_TTL_S",
    "ActToken",
    "mint",
    "state_digest",
    "verify",
]
