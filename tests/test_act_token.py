# SPDX-License-Identifier: Apache-2.0
"""The `/act` token and key (#38, schemas decision 54 B, C, E): format, MAC, strict decoding,
expiry, key id and rotation, and what a token does not carry."""

import base64
import hashlib
import hmac
import re
import struct
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from act_fixtures import KEY_VALUE, NOW, OTHER_KEY_VALUE, PUBLIC, TASK, key, token
from horae.api.act import (
    MAX_AHEAD_S,
    TOKEN_PATTERN,
    TOKEN_TTL_S,
    ActKey,
    ActKeyError,
    load_act_key,
    mint,
    state_digest,
    verify,
)
from horae.api.act._key import TAG_LABEL
from horae.secrets import Secret, SecretResolver
from horae.secrets.redact import current, forget_all

_HEADER = struct.Struct(">B4sBQ16s8sBB")
STATE = state_digest("todo", None, None)


def _raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _forge(payload: bytes, value: str = KEY_VALUE) -> str:
    """A token with a valid MAC over an arbitrary payload (a key holder's forgery)."""
    tag = hmac.new(value.encode(), TAG_LABEL + payload, hashlib.sha256).digest()
    return _b64(payload + tag)


def _payload(
    *,
    version: int = 1,
    kid: bytes | None = None,
    kind: int = 1,
    exp: int = NOW + TOKEN_TTL_S,
    dur: int = 0,
    fingerprint: bytes = TASK.encode(),
    length: int | None = None,
) -> bytes:
    header = _HEADER.pack(
        version,
        key().kid if kid is None else kid,
        kind,
        exp,
        bytes(16),
        STATE,
        dur,
        len(fingerprint) if length is None else length,
    )
    return header + fingerprint


# --- AC1: expiry ------------------------------------------------------------------------------


def test_a_token_is_valid_until_it_expires() -> None:
    value = token(at=NOW)
    assert verify(key(), value, NOW) is not None
    assert verify(key(), value, NOW + TOKEN_TTL_S - 1) is not None


def test_an_expired_token_is_refused() -> None:
    value = token(at=NOW)
    assert verify(key(), value, NOW + TOKEN_TTL_S) is None
    assert verify(key(), value, NOW + 10 * TOKEN_TTL_S) is None


def test_a_token_expiring_too_far_ahead_is_refused() -> None:
    # Minted by a clock that ran ahead: its expiry is more than 24 h 05 min from now.
    assert verify(key(), token(at=NOW + 301), NOW) is None
    assert verify(key(), token(at=NOW + 300), NOW) is not None
    assert MAX_AHEAD_S == TOKEN_TTL_S + 300


# --- AC2: no secrets, no URLs -------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    fingerprint=st.from_regex(r"\A[A-Za-z0-9._:-]{1,128}\Z"),
    kind=st.sampled_from(["task.done", "1h", "3h", "tomorrow"]),
    status=st.sampled_from(["todo", "done", "snoozed"]),
)
def test_a_token_decodes_to_kind_fingerprint_numbers_only(
    fingerprint: str, kind: str, status: str
) -> None:
    duration = None if kind == "task.done" else kind
    value = mint(
        key(),
        kind="task.done" if duration is None else "task.snooze",
        task=fingerprint,
        state=state_digest(status, None, None),
        now=NOW,
        duration=duration,
    )
    assert re.fullmatch(TOKEN_PATTERN, value)
    assert 98 <= len(value) <= 267
    raw = _raw(value)
    payload = raw[:-32]
    version, kid, kind_byte, exp, _nonce, _state, dur, length = _HEADER.unpack_from(payload)
    assert (version, kid, exp, length) == (1, key().kid, NOW + TOKEN_TTL_S, len(fingerprint))
    assert payload[_HEADER.size :] == fingerprint.encode()
    assert kind_byte in (1, 2)
    assert dur in (0, 1, 2, 3)
    # No key material, no URL, no title: only the fingerprint is readable.
    assert KEY_VALUE.encode() not in raw
    assert KEY_VALUE not in value
    assert b"://" not in raw
    assert PUBLIC.encode() not in raw
    parsed = verify(key(), value, NOW)
    assert parsed is not None
    assert parsed.task == fingerprint


def test_the_kid_is_derived_from_the_key_and_reveals_nothing() -> None:
    first, second = key(), key(OTHER_KEY_VALUE)
    assert first.kid == key().kid != second.kid
    assert len(first.kid) == 4
    assert first.kid not in KEY_VALUE.encode()
    assert "act-key" not in repr(first)


def test_a_key_cannot_be_pickled_or_changed() -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(key())
    with pytest.raises(AttributeError):
        key().kid = b"abcd"


# --- AC5: the key -----------------------------------------------------------------------------


def test_rotating_the_key_refuses_every_outstanding_token() -> None:
    outstanding = [token(), token("task.snooze", duration="1h"), token(task="other:1")]
    rotated = key(OTHER_KEY_VALUE)
    assert all(verify(rotated, value, NOW) is None for value in outstanding)
    assert all(verify(key(), value, NOW) is not None for value in outstanding)
    fresh = token(value=OTHER_KEY_VALUE)
    assert verify(rotated, fresh, NOW) is not None
    assert verify(key(), fresh, NOW) is None


def test_a_short_key_is_refused() -> None:
    with pytest.raises(ActKeyError, match="at least 32 bytes"):
        ActKey(Secret("HORAE_ACT_KEY", "x" * 31))
    ActKey(Secret("HORAE_ACT_KEY", "x" * 32))


def test_the_key_is_read_from_its_file_then_the_environment(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    assert load_act_key(SecretResolver({}, secrets_dir, {})) is None
    from_env = load_act_key(SecretResolver({}, secrets_dir, {"HORAE_ACT_KEY": KEY_VALUE}))
    assert from_env is not None
    assert from_env.kid == key().kid
    (secrets_dir / "HORAE_ACT_KEY").write_text(OTHER_KEY_VALUE + "\n")
    from_file = load_act_key(SecretResolver({}, secrets_dir, {"HORAE_ACT_KEY": KEY_VALUE}))
    assert from_file is not None
    assert from_file.kid == key(OTHER_KEY_VALUE).kid
    (secrets_dir / "HORAE_ACT_KEY").write_text("too-short-key\n")
    with pytest.raises(ActKeyError, match="too short") as raised:
        load_act_key(SecretResolver({}, secrets_dir, {}))
    assert "too-short-key" not in str(raised.value)


def test_the_resolved_key_is_redacted_everywhere(tmp_path: Path) -> None:
    forget_all()
    try:
        load_act_key(SecretResolver({}, tmp_path, {"HORAE_ACT_KEY": KEY_VALUE}))
        assert KEY_VALUE not in current().redact_text(f"leak {KEY_VALUE} here")[0]
    finally:
        forget_all()


def test_only_core_secrets_resolve_through_resolve_core(tmp_path: Path) -> None:
    from horae.secrets import SecretError

    resolver = SecretResolver({}, tmp_path, {"GITHUB_TOKEN": "x" * 40})
    with pytest.raises(SecretError, match="not a core secret"):
        resolver.resolve_core("GITHUB_TOKEN")


# --- forged, tampered and malformed tokens -------------------------------------------------------


def test_a_forged_or_tampered_token_is_refused() -> None:
    value = token()
    raw = bytearray(_raw(value))
    for index in range(len(raw)):  # every byte of the payload and of the MAC
        for bit in (0x01, 0x80):
            changed = bytearray(raw)
            changed[index] ^= bit
            assert verify(key(), _b64(bytes(changed)), NOW) is None, index


def test_truncated_oversized_and_non_base64_tokens_are_refused() -> None:
    value = token()
    candidates = [
        "",
        value[:-1],
        value[:97],
        value[: len(value) - 4],
        value + "A",
        value + "AAAA",
        "A" * 268,
        "A" * 98,
        value + "=",
        value.replace(value[10], "+" if value[10] != "+" else "/", 1),
        value[:20] + "." + value[21:],
        value[:20] + "%" + value[21:],
        value[:-1] + chr(ord(value[-1]) ^ 1),  # other trailing bits: a second spelling
        " " + value,
        value + "\n",
        "\u0430" + value[1:],
    ]
    for candidate in candidates:
        assert verify(key(), candidate, NOW) is None, candidate
    for junk in (None, 42, b"abc", value.encode()):
        assert verify(key(), junk, NOW) is None


def test_one_token_has_one_spelling() -> None:
    value = token()
    raw = _raw(value)
    # base64 of the same bytes with different (ignored) trailing bits decodes the same.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    spare = 6 - (len(raw) * 8) % 6 if (len(raw) * 8) % 6 else 0
    if spare:
        last = alphabet.index(value[-1])
        variant = value[:-1] + alphabet[last | 1]
        if variant != value:
            assert _raw(variant) == raw
            assert verify(key(), variant, NOW) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload(version=2), id="version"),
        pytest.param(_payload(kid=b"\0\0\0\0"), id="kid"),
        pytest.param(_payload(kind=0), id="kind-0"),
        pytest.param(_payload(kind=3), id="kind-3"),
        pytest.param(_payload(kind=255), id="kind-255"),
        pytest.param(_payload(kind=1, dur=1), id="done-with-duration"),
        pytest.param(_payload(kind=2, dur=0), id="snooze-without-duration"),
        pytest.param(_payload(kind=2, dur=4), id="snooze-unknown-duration"),
        pytest.param(_payload(length=5), id="length-short"),
        pytest.param(_payload(length=200), id="length-long"),
        pytest.param(_payload(fingerprint=b"a/b"), id="fingerprint-slash"),
        pytest.param(_payload(fingerprint=b"\xff\xfe"), id="fingerprint-not-ascii"),
        pytest.param(_payload(exp=NOW), id="expired"),
        pytest.param(_payload(exp=NOW + MAX_AHEAD_S + 1), id="too-far-ahead"),
    ],
)
def test_a_link_cannot_reach_another_intent_or_field(payload: bytes) -> None:
    # Even with a valid MAC (the key holder's own forgery), only the closed set decodes.
    assert verify(key(), _forge(payload), NOW) is None


def test_a_well_formed_forgery_with_the_right_key_is_the_only_way_in() -> None:
    assert verify(key(), _forge(_payload()), NOW) is not None
    assert verify(key(), _forge(_payload(), OTHER_KEY_VALUE), NOW) is None


def test_minting_refuses_what_a_token_cannot_carry() -> None:
    for kwargs in (
        {"kind": "http.post"},
        {"kind": "task.snooze"},
        {"kind": "task.done", "duration": "1h"},
        {"kind": "task.snooze", "duration": "2d"},
        {"kind": "task.done", "task": "a/b"},
        {"kind": "task.done", "task": "x" * 129},
        {"kind": "task.done", "state": b"short"},
        {"kind": "task.done", "nonce": b"short"},
    ):
        arguments = {"task": TASK, "state": STATE, "now": NOW, **kwargs}
        with pytest.raises(ValueError, match=r"kind|duration|fingerprint|state|nonce"):
            mint(key(), **arguments)  # type: ignore[arg-type]


def test_the_state_binds_status_due_and_snooze() -> None:
    import datetime as dt

    base = state_digest("todo", dt.date(2026, 1, 1), None)
    assert len(base) == 8
    assert base == state_digest("todo", dt.date(2026, 1, 1), None)
    assert base != state_digest("done", dt.date(2026, 1, 1), None)
    assert base != state_digest("todo", dt.date(2026, 1, 2), None)
    assert base != state_digest("todo", None, None)
    snoozed = state_digest("snoozed", None, dt.datetime(2026, 1, 1, 9, tzinfo=dt.UTC))
    assert snoozed != state_digest("snoozed", None, dt.datetime(2026, 1, 1, 10, tzinfo=dt.UTC))
