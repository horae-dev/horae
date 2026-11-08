# SPDX-License-Identifier: Apache-2.0
"""The `/act` HMAC key (#38, schemas decision 53 C).

A dedicated secret, `HORAE_ACT_KEY`, never shared with another use, read by
`SecretResolver.resolve_core` (the file `<secrets.dir>/HORAE_ACT_KEY`, else the environment
variable of that name), so it is remembered for redaction like any secret. At least
`MIN_KEY_BYTES` (32) bytes of UTF-8: `openssl rand -base64 48` gives 64 characters.

The **key id** is `HMAC-SHA256(key, "horae-act/kid")[:4]`: derived, so it needs no
configuration, changes with the key and says nothing about it. One key is loaded at a time:
rotating it (a new value, then a restart) refuses every token minted under the old one, with
no grace window (maintainer decision 8).
"""

import hashlib
import hmac
from typing import NoReturn, final

from horae.secrets import Secret, SecretError, SecretMissing, SecretResolver

ACT_KEY_NAME = "HORAE_ACT_KEY"
"""The secret holding the key."""
MIN_KEY_BYTES = 32
"""The shortest key accepted (256 bits)."""
KID_LABEL = b"horae-act/kid"
TAG_LABEL = b"horae-act/v1\0"
"""Domain separation: the MAC of a token is over `TAG_LABEL || payload`."""


class ActKeyError(Exception):
    """The `/act` key is set but cannot be used (too short, unreadable). The message names the
    secret and the reason, never the value."""


@final
class ActKey:
    """The loaded key: its id (`kid`) and the MAC of a token payload (`tag`). Its value stays
    inside the `Secret` (redacted `repr`), revealed only to compute a MAC."""

    __slots__ = ("_secret", "kid")
    _secret: Secret
    kid: bytes
    """The key id: `HMAC-SHA256(key, "horae-act/kid")[:4]`."""

    def __init__(self, secret: Secret) -> None:
        if len(secret.reveal().encode("utf-8")) < MIN_KEY_BYTES:
            raise ActKeyError(
                f"secret {ACT_KEY_NAME} is too short: at least {MIN_KEY_BYTES} bytes "
                "(e.g. `openssl rand -base64 48`)"
            )
        object.__setattr__(self, "_secret", secret)
        object.__setattr__(self, "kid", self._mac(KID_LABEL)[:4])

    def _mac(self, data: bytes) -> bytes:
        return hmac.new(self._secret.reveal().encode("utf-8"), data, hashlib.sha256).digest()

    def tag(self, payload: bytes) -> bytes:
        """`HMAC-SHA256(key, TAG_LABEL || payload)`, 32 bytes (never truncated)."""
        return self._mac(TAG_LABEL + payload)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("ActKey is immutable")

    def __repr__(self) -> str:
        return f"ActKey(kid={self.kid.hex()})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("ActKey cannot be pickled")


def load_act_key(resolver: SecretResolver) -> ActKey | None:
    """The `/act` key, or `None` when `HORAE_ACT_KEY` is set nowhere (then `/act` answers 503
    and notifications are sent without buttons). Raises `ActKeyError` when it is set but
    unusable: `horae serve` refuses to start and `horae doctor` reports an error."""
    try:
        secret = resolver.resolve_core(ACT_KEY_NAME)
    except SecretMissing:
        return None
    except SecretError as exc:
        raise ActKeyError(str(exc)) from None
    return ActKey(secret)


__all__ = ["ACT_KEY_NAME", "MIN_KEY_BYTES", "ActKey", "ActKeyError", "load_act_key"]
