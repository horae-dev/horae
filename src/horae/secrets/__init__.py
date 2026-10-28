# SPDX-License-Identifier: Apache-2.0
"""Secret values and where they come from (PLAN.md §7, §9).

A secret is read from the file of the same name in the secrets directory
(`/run/secrets/<NAME>`, as Docker and Compose mount them), or from the source an explicit
reference names: `{file: name}` in that directory, or `{env: VAR}`. Environment variables are
read **only** through an explicit `{env: VAR}` reference (schemas README, decision 25), so a
plugin that declares a secret name can never pull an arbitrary variable out of the host
environment.

The value lives only inside a `Secret`, whose `repr`, `str`, `format` and pydantic
serialization are redacted, and which refuses to be pickled, copied into a subclass or
re-initialised. Code that needs the value calls `Secret.reveal()` at the last moment.

Error messages name the secret and where it was looked for, never its value, and no exception
raised here keeps a reference to the value: not in its message, not in its `__context__` (no
decoding error is chained) and not in the local variables of the frames it went through.
Resolution logs nothing.

Templating must never call `str.format` (or `format_map`, `string.Formatter`) with secrets or
with context objects that can reach one: format fields can read attributes
(`"{s._value}"`), which bypasses every redaction above. Use a renderer that only substitutes
plain values.
"""

import hmac
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn, final

from pydantic import GetCoreSchemaHandler, TypeAdapter, ValidationError
from pydantic_core import CoreSchema, core_schema

from horae.contracts.instance import EnvSecretRef, SecretRef
from horae.contracts.types import SecretName
from horae.secrets import redact

REDACTED = "[REDACTED]"
"""What a secret looks like wherever it is printed, logged or serialized."""

MAX_SECRET_BYTES = 64 * 1024
"""Largest secret file read, in bytes."""

MIN_SECRET_BYTES = redact.MIN_SECRET_BYTES
"""Shortest secret value accepted, in UTF-8 bytes (#53, schemas decision 43 H): a shorter value
cannot be redacted reliably (its encodings would match unrelated text, and its raw value
matches inside ordinary words), so it is refused when it is resolved."""

_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-./")


def safe_excerpt(text: str, limit: int = 8) -> str:
    """At most `limit` characters of `text`, safe to show in an error message.

    For untrusted names that may in fact be a value pasted in the wrong place: anything but
    `A-Z a-z 0-9 _ - . /` becomes `?`, and a longer text is cut and ends with `…`."""
    shown = "".join(char if char in _SAFE_CHARS else "?" for char in text[:limit])
    return shown + ("…" if len(text) > limit else "")


@final
class Secret:
    """A secret value that does not print itself."""

    __slots__ = ("_name", "_value")

    _name: str
    _value: str

    def __init__(self, name: str, value: str) -> None:
        try:
            object.__getattribute__(self, "_value")
        except AttributeError:
            pass
        else:
            raise TypeError("a Secret cannot be re-initialised")
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_value", value)

    def __init_subclass__(cls, **kwargs: Any) -> NoReturn:
        raise TypeError("Secret cannot be subclassed")

    @property
    def name(self) -> str:
        """The secret's name, e.g. `GITHUB_TOKEN` (not sensitive)."""
        return self._name

    def reveal(self) -> str:
        """The value. Pass it straight to its consumer; never log or store it."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self._name!r}, {REDACTED!r})"

    def __str__(self) -> str:
        return REDACTED

    def __format__(self, format_spec: str) -> str:
        return REDACTED

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._name == other._name and hmac.compare_digest(
            self._value.encode(), other._value.encode()
        )

    def __hash__(self) -> int:
        return hash((Secret, self._name))

    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "Secret":
        return self

    def __getstate__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        raise TypeError("a Secret cannot be pickled")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: type[Any], handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda _value: REDACTED, when_used="always"
            ),
        )


class SecretError(Exception):
    """A secret could not be resolved. The message never contains a value."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"secret {name}: {reason}")
        self.name = name
        self.reason = reason


_SECRET_NAME: TypeAdapter[str] = TypeAdapter(SecretName)


def _valid_name(name: object) -> bool:
    try:
        _SECRET_NAME.validate_python(name)
    except ValidationError:
        return False
    return True


def _problem(value: str) -> str | None:
    """Why `value` (one trailing newline already stripped) is not a usable secret, if so."""
    if not value:
        return "is empty"
    if "\x00" in value:
        return "contains a NUL character"
    if "\r" in value or "\n" in value:
        return "contains a line break (only one trailing newline is allowed)"
    if len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        return f"is too short for reliable redaction (min {MIN_SECRET_BYTES} bytes)"
    return None


def _strip_newline(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None  # the error holds the bytes: never let it escape or chain


_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


class SecretResolver:
    """Resolves secret names to `Secret` values.

    A name with an explicit reference (`secrets.refs` in `horae.yaml`) is read from that
    environment variable or file only. Any other name is read from the file of the same name
    in `directory` only, never from the environment.

    A value shorter than `MIN_SECRET_BYTES` (8 bytes of UTF-8) is refused (`SecretError`
    "... is too short for reliable redaction (min 8 bytes)", never the value): it could not be
    redacted reliably (schemas decision 43 H).

    The resolver remembers every secret it handed out (`resolved()`), so that the plugin
    host can redact all of them from captured output, and adds each one to the process
    registry (`horae.secrets.redact.remember`), so that the store, the logs, the LLM layer
    and the CLI redact it too (#53).
    """

    def __init__(
        self,
        refs: Mapping[str, SecretRef],
        directory: Path,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._refs = dict(refs)
        self._directory = directory
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._resolved: dict[str, Secret] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    def referenced(self) -> tuple[str, ...]:
        """The names with an explicit reference (`secrets.refs`), sorted. The plugin host
        resolves them all when it opens, so that it can redact them (schemas decision 35 B)."""
        return tuple(sorted(self._refs))

    def resolved(self) -> tuple[Secret, ...]:
        """Every secret this resolver returned so far, in resolution order (for redaction)."""
        return tuple(self._resolved.values())

    def resolve(self, name: str) -> Secret:
        """The secret `name`, or `SecretError` saying where it was looked for."""
        if not _valid_name(name):
            shown = safe_excerpt(str(name))
            del name  # it may be a value pasted in the wrong place: keep it out of tracebacks
            raise SecretError(shown, "invalid secret name")
        ref = self._refs.get(name)
        if isinstance(ref, EnvSecretRef):
            secret = self._from_env(name, ref.env)
        elif ref is not None:
            secret = self._from_file(name, self._directory / ref.file)
        else:
            secret = self._from_file(name, self._directory / name)
        self._resolved[name] = secret
        redact.remember(secret)
        return secret

    def _from_env(self, name: str, variable: str) -> Secret:
        if variable not in self._environ:
            raise SecretError(name, f"environment variable {variable} is not set")
        value = _strip_newline(self._environ[variable])
        problem = _problem(value)
        if problem is not None:
            del value
            raise SecretError(name, f"environment variable {variable} {problem}")
        return Secret(name, value)

    def _from_file(self, name: str, path: Path) -> Secret:
        data = self._read(name, path)
        text = _decode(data)
        del data
        if text is None:
            raise SecretError(name, f"file {path} is not valid UTF-8")
        text = _strip_newline(text)
        problem = _problem(text)
        if problem is not None:
            del text
            raise SecretError(name, f"file {path} {problem}")
        return Secret(name, text)

    @staticmethod
    def _read(name: str, path: Path) -> bytes:
        """The bytes of `path`, a regular file of at most `MAX_SECRET_BYTES`.

        Opened once, non-blocking (a FIFO never blocks), and checked on the open descriptor
        (`fstat`), so the file cannot be swapped between the check and the read."""
        reason: str | None = None
        try:
            fd = os.open(path, _OPEN_FLAGS)
        except FileNotFoundError:
            reason = "does not exist"
        except OSError as exc:
            reason = f"cannot be read ({exc.strerror})"
        if reason is not None:
            raise SecretError(name, f"file {path} {reason}")
        chunks: list[bytes] = []
        chunk = b""
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                reason = "is not a regular file"
            elif info.st_size > MAX_SECRET_BYTES:
                reason = f"is larger than {MAX_SECRET_BYTES} bytes"
            else:
                size = 0
                while size <= MAX_SECRET_BYTES:
                    chunk = os.read(fd, MAX_SECRET_BYTES + 1 - size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                if size > MAX_SECRET_BYTES:
                    reason = f"is larger than {MAX_SECRET_BYTES} bytes"
        except OSError as exc:
            reason = f"cannot be read ({exc.strerror})"
        finally:
            os.close(fd)
        if reason is not None:
            chunks.clear()
            chunk = b""  # drop the last chunk from this frame's locals
            raise SecretError(name, f"file {path} {reason}")
        return b"".join(chunks)


__all__ = [
    "MAX_SECRET_BYTES",
    "MIN_SECRET_BYTES",
    "REDACTED",
    "Secret",
    "SecretError",
    "SecretResolver",
    "safe_excerpt",
]
