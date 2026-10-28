# SPDX-License-Identifier: Apache-2.0
"""Secrets resolve from the environment or files and never show their value."""

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
from horae.secrets import MAX_SECRET_BYTES, REDACTED, Secret, SecretError, SecretResolver

VALUE = "s3cr3t-sentinel-0b5d2e7a"  # synthetic


class Holder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    token: Secret


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
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(secret)


def test_secret_is_an_immutable_value() -> None:
    secret = Secret("API_KEY", VALUE)
    with pytest.raises(AttributeError):
        secret._value = "other"
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


def test_default_lookup_env_then_file(tmp_path: Path) -> None:
    (tmp_path / "API_KEY").write_text("from-file\n", encoding="utf-8")
    assert _resolver(tmp_path, {"API_KEY": VALUE}).resolve("API_KEY").reveal() == VALUE
    assert _resolver(tmp_path, {}).resolve("API_KEY").reveal() == "from-file"
    with pytest.raises(SecretError) as excinfo:
        _resolver(tmp_path, {}).resolve("OTHER")
    assert excinfo.value.name == "OTHER"
    assert str(excinfo.value) == (
        f"secret OTHER: not set: no environment variable OTHER and no file {tmp_path / 'OTHER'}"
    )


def test_explicit_references(tmp_path: Path) -> None:
    (tmp_path / "api_key").write_text(VALUE + "\r\n", encoding="utf-8")
    resolver = _resolver(
        tmp_path,
        {"API_KEY": "ignored", "VAR": "from-env"},
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
    monkeypatch.setenv("HORAE_TEST_SECRET", VALUE)
    assert SecretResolver({}, tmp_path).resolve("HORAE_TEST_SECRET").reveal() == VALUE


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "is empty"),
        (b"\n", "is empty"),
        (b"a\x00" + VALUE.encode(), "contains a NUL character"),
        (b"\xff" + VALUE.encode(), "is not valid UTF-8"),
        (VALUE.encode() * (MAX_SECRET_BYTES // len(VALUE) + 1), "larger than"),
    ],
)
def test_bad_secret_files_fail_without_showing_the_value(
    tmp_path: Path, content: bytes, message: str
) -> None:
    (tmp_path / "API_KEY").write_bytes(content)
    with pytest.raises(SecretError, match=message) as excinfo:
        _resolver(tmp_path, {}).resolve("API_KEY")
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert VALUE not in rendered
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


def test_bad_env_values(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="environment variable API_KEY is empty"):
        _resolver(tmp_path, {"API_KEY": ""}).resolve("API_KEY")
    with pytest.raises(SecretError, match="contains a NUL") as excinfo:
        _resolver(tmp_path, {"API_KEY": VALUE + "\x00"}).resolve("API_KEY")
    assert VALUE not in str(excinfo.value)


def test_secret_path_must_be_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "API_KEY").mkdir()
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
    finally:
        path.chmod(0o600)


def test_unstattable_secret_path(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x", encoding="utf-8")
    resolver = SecretResolver({}, tmp_path / "file", {})
    with pytest.raises(SecretError, match="cannot be read"):
        resolver.resolve("API_KEY")


def test_file_growing_after_stat_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "API_KEY"
    path.write_bytes(b"x" * (MAX_SECRET_BYTES + 10))
    real_stat = Path.stat

    class Small:
        def __init__(self, info: os.stat_result) -> None:
            self.st_mode = info.st_mode
            self.st_size = 1

    monkeypatch.setattr(Path, "stat", lambda self, **kw: Small(real_stat(self, **kw)))
    with pytest.raises(SecretError, match="larger than"):
        _resolver(tmp_path, {}).resolve("API_KEY")


def test_open_failure_after_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "API_KEY").write_text(VALUE, encoding="utf-8")

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refuse)
    with pytest.raises(SecretError, match=r"cannot be read \(Permission denied\)"):
        _resolver(tmp_path, {}).resolve("API_KEY")


def test_resolution_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "FILE_KEY").write_text(VALUE + "-file", encoding="utf-8")
    resolver = _resolver(tmp_path, {"ENV_KEY": VALUE + "-env"})
    with caplog.at_level(logging.DEBUG):
        resolver.resolve("ENV_KEY")
        resolver.resolve("FILE_KEY")
        with pytest.raises(SecretError):
            resolver.resolve("MISSING")
    assert VALUE not in caplog.text
    assert caplog.records == []
