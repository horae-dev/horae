# SPDX-License-Identifier: Apache-2.0
"""Secrets resolve from files or explicit references and never show their value."""

import copy
import json
import logging
import os
import pickle
import traceback
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from horae.contracts import EnvSecretRef, FileSecretRef
from horae.secrets import (
    MAX_SECRET_BYTES,
    REDACTED,
    Secret,
    SecretError,
    SecretResolver,
    safe_excerpt,
)

VALUE = "s3cr3t-sentinel-0b5d2e7a"  # synthetic


class Holder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    token: Secret


def _leak_free(exc: BaseException) -> str:
    """The exception rendered with every frame's local variables, from the raising code on.

    The first frame is the test's own (whose locals hold the expected value), so it is
    skipped."""
    assert exc.__traceback__ is not None
    rendered = traceback.TracebackException(
        type(exc), exc, exc.__traceback__.tb_next, capture_locals=True
    )
    return "".join(rendered.format())


def test_secret_never_shows_its_value() -> None:
    secret = Secret("API_KEY", VALUE)
    assert secret.reveal() == VALUE
    assert secret.name == "API_KEY"
    rendered = [
        repr(secret),
        str(secret),
        f"{secret}",
        f"{secret!r}",
        f"{secret:>40}",
        "%s %r" % (secret, secret),  # noqa: UP031
        repr([secret]),
        repr({"k": secret}),
        Holder(token=secret).model_dump_json(),
        repr(Holder(token=secret)),
        str(Holder(token=secret).model_dump()),
    ]
    assert all(VALUE not in text for text in rendered)
    assert repr(secret) == "Secret('API_KEY', '[REDACTED]')"
    assert Holder(token=secret).model_dump() == {"token": REDACTED}
    with pytest.raises(TypeError) as excinfo:
        json.dumps({"k": secret})
    assert VALUE not in str(excinfo.value)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_secret_cannot_be_pickled(protocol: int) -> None:
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(Secret("API_KEY", VALUE), protocol=protocol)


def test_secret_state_and_reduce_refuse() -> None:
    secret = Secret("API_KEY", VALUE)
    with pytest.raises(TypeError, match="cannot be pickled"):
        secret.__getstate__()
    with pytest.raises(TypeError, match="cannot be pickled"):
        secret.__reduce__()
    with pytest.raises(TypeError, match="cannot be pickled"):
        secret.__reduce_ex__(2)


def test_secret_cannot_be_subclassed() -> None:
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Leaky(Secret):  # type: ignore[misc]
            def __str__(self) -> str:
                return self.reveal()


def test_secret_cannot_be_reinitialised() -> None:
    secret = Secret("API_KEY", VALUE)
    with pytest.raises(TypeError, match="re-initialised"):
        secret.__init__("API_KEY", "other")
    assert secret.reveal() == VALUE


def test_secret_is_an_immutable_value() -> None:
    secret = Secret("API_KEY", VALUE)
    with pytest.raises(AttributeError):
        secret._value = "other"
    with pytest.raises(AttributeError):
        del secret._value
    assert not hasattr(secret, "__dict__")
    assert copy.copy(secret) is secret
    assert copy.deepcopy(secret) is secret
    assert secret == Secret("API_KEY", VALUE)
    assert secret != Secret("API_KEY", "other")
    assert secret != Secret("OTHER", VALUE)
    assert secret != VALUE
    assert len({secret, Secret("API_KEY", VALUE)}) == 1


def _resolver(tmp_path: Path, environ: dict[str, str], **refs: object) -> SecretResolver:
    return SecretResolver(refs, tmp_path, environ)  # type: ignore[arg-type]


def test_default_lookup_is_the_secrets_directory_only(tmp_path: Path) -> None:
    (tmp_path / "API_KEY").write_text(VALUE + "\n", encoding="utf-8")
    resolver = _resolver(tmp_path, {"API_KEY": "from-env", "OTHER": "from-env"})
    assert resolver.resolve("API_KEY").reveal() == VALUE
    with pytest.raises(SecretError) as excinfo:
        resolver.resolve("OTHER")
    assert excinfo.value.name == "OTHER"
    # The environment is never read without an explicit `{env: VAR}` reference.
    assert str(excinfo.value) == f"secret OTHER: file {tmp_path / 'OTHER'} does not exist"


def test_explicit_references(tmp_path: Path) -> None:
    (tmp_path / "api_key").write_text(VALUE + "\r\n", encoding="utf-8")
    resolver = _resolver(
        tmp_path,
        {"API_KEY": "ignored", "VAR": "from-env\n"},
        API_KEY=FileSecretRef(file="api_key"),
        TOKEN=EnvSecretRef(env="VAR"),
        MISSING_ENV=EnvSecretRef(env="NOPE"),
        MISSING_FILE=FileSecretRef(file="nope"),
    )
    assert resolver.directory == tmp_path
    assert resolver.resolve("API_KEY").reveal() == VALUE
    assert resolver.resolve("TOKEN") == Secret("TOKEN", "from-env")
    with pytest.raises(SecretError, match="environment variable NOPE is not set"):
        resolver.resolve("MISSING_ENV")
    with pytest.raises(SecretError, match="nope does not exist"):
        resolver.resolve("MISSING_FILE")


def test_default_environ_is_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORAE_TEST_VARIABLE", VALUE)
    resolver = SecretResolver({"API_KEY": EnvSecretRef(env="HORAE_TEST_VARIABLE")}, tmp_path)
    assert resolver.resolve("API_KEY").reveal() == VALUE


def test_resolver_remembers_what_it_handed_out(tmp_path: Path) -> None:
    (tmp_path / "A_KEY").write_text("a-value", encoding="utf-8")
    resolver = _resolver(tmp_path, {"VAR": "b-value"}, B_KEY=EnvSecretRef(env="VAR"))
    assert resolver.resolved() == ()
    b = resolver.resolve("B_KEY")
    a = resolver.resolve("A_KEY")
    resolver.resolve("B_KEY")
    with pytest.raises(SecretError):
        resolver.resolve("MISSING")
    assert resolver.resolved() == (b, a)
    assert [s.reveal() for s in resolver.resolved()] == ["b-value", "a-value"]


@pytest.mark.parametrize("name", ["/etc/hosts", "../x", "api_key", "", "LD_PRELOAD", "HORAE_X"])
def test_invalid_names_are_refused_before_any_lookup(tmp_path: Path, name: str) -> None:
    (tmp_path / "x").write_text(VALUE, encoding="utf-8")
    (tmp_path / "api_key").write_text(VALUE, encoding="utf-8")
    resolver = _resolver(tmp_path, {name: VALUE})
    with pytest.raises(SecretError, match="invalid secret name") as excinfo:
        resolver.resolve(name)
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)
    assert resolver.resolved() == ()


def test_invalid_name_is_shown_as_a_safe_excerpt(tmp_path: Path) -> None:
    pasted = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"  # synthetic
    with pytest.raises(SecretError) as excinfo:
        _resolver(tmp_path, {}).resolve(pasted)
    assert excinfo.value.name == "sk-ant-a…"
    assert str(excinfo.value) == "secret sk-ant-a…: invalid secret name"
    assert pasted[9:] not in _leak_free(excinfo.value)


def test_safe_excerpt() -> None:
    assert safe_excerpt("/etc/hosts") == "/etc/hos…"
    assert safe_excerpt("../x") == "../x"
    assert safe_excerpt("a b\nc") == "a?b?c"
    assert safe_excerpt("abc", limit=2) == "ab…"


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "is empty"),
        (b"\n", "is empty"),
        (b"\r\n", "is empty"),
        (b"a\x00" + VALUE.encode(), "contains a NUL character"),
        (b"\xff" + VALUE.encode(), "is not valid UTF-8"),
        (VALUE.encode() + b"\nsecond-line", "contains a line break"),
        (VALUE.encode() + b"\n\n", "contains a line break"),
        (VALUE.encode() + b"\r", "contains a line break"),
        (VALUE.encode() * (MAX_SECRET_BYTES // len(VALUE) + 1), "larger than"),
    ],
)
def test_bad_secret_files_fail_without_keeping_the_value(
    tmp_path: Path, content: bytes, message: str
) -> None:
    _write(tmp_path / "API_KEY", content)
    del content
    with pytest.raises(SecretError, match=message) as excinfo:
        _resolver(tmp_path, {}).resolve("API_KEY")
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "environment variable VAR is empty"),
        ("\n", "environment variable VAR is empty"),
        (VALUE + "\x00", "environment variable VAR contains a NUL"),
        (VALUE + "\nmore", "environment variable VAR contains a line break"),
        (VALUE + "\r", "environment variable VAR contains a line break"),
    ],
)
def test_bad_env_values_fail_without_keeping_the_value(
    tmp_path: Path, value: str, message: str
) -> None:
    resolver = _resolver(tmp_path, {"VAR": value}, API_KEY=EnvSecretRef(env="VAR"))
    del value
    with pytest.raises(SecretError, match=message) as excinfo:
        resolver.resolve("API_KEY")
    assert excinfo.value.__context__ is None
    assert VALUE not in _leak_free(excinfo.value)


def test_secret_path_must_be_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "API_KEY").mkdir()
    with pytest.raises(SecretError, match="is not a regular file"):
        _resolver(tmp_path, {}).resolve("API_KEY")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_fifo_does_not_block(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "API_KEY")
    with pytest.raises(SecretError, match="is not a regular file"):
        _resolver(tmp_path, {}).resolve("API_KEY")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_unreadable_secret_file(tmp_path: Path) -> None:
    path = tmp_path / "API_KEY"
    path.write_text(VALUE, encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(SecretError, match="cannot be read") as excinfo:
            _resolver(tmp_path, {}).resolve("API_KEY")
        assert VALUE not in str(excinfo.value)
        assert excinfo.value.__context__ is None
    finally:
        path.chmod(0o600)


def test_unopenable_secret_path(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x", encoding="utf-8")
    resolver = SecretResolver({}, tmp_path / "file", {})
    with pytest.raises(SecretError, match="cannot be read") as excinfo:
        resolver.resolve("API_KEY")
    assert excinfo.value.__context__ is None


def test_file_growing_after_fstat_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "API_KEY").write_bytes(VALUE.encode() * (MAX_SECRET_BYTES // len(VALUE) + 1))
    real_fstat = os.fstat

    class Small:
        def __init__(self, info: os.stat_result) -> None:
            self.st_mode = info.st_mode
            self.st_size = 1

    monkeypatch.setattr(os, "fstat", lambda fd: Small(real_fstat(fd)))
    with pytest.raises(SecretError, match="larger than") as excinfo:
        _resolver(tmp_path, {}).resolve("API_KEY")
    assert VALUE not in _leak_free(excinfo.value)


def test_read_failure_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "API_KEY").write_text(VALUE, encoding="utf-8")

    def refuse(fd: int, size: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "read", refuse)
    with pytest.raises(SecretError, match=r"cannot be read \(Input/output error\)") as excinfo:
        _resolver(tmp_path, {}).resolve("API_KEY")
    assert excinfo.value.__context__ is None


def test_resolution_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "FILE_KEY").write_text(VALUE + "-file", encoding="utf-8")
    resolver = _resolver(tmp_path, {"VAR": VALUE + "-env"}, ENV_KEY=EnvSecretRef(env="VAR"))
    with caplog.at_level(logging.DEBUG):
        resolver.resolve("ENV_KEY")
        resolver.resolve("FILE_KEY")
        with pytest.raises(SecretError):
            resolver.resolve("MISSING")
    assert VALUE not in caplog.text
    assert caplog.records == []
