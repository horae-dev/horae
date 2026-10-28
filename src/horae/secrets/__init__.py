# SPDX-License-Identifier: Apache-2.0
"""Secret values and where they come from (PLAN.md §7, §9).

A secret is resolved from an environment variable or from a file in the secrets directory
(`/run/secrets/<name>`, as Docker and Compose mount them). Its value lives only inside a
`Secret`, whose `repr`, `str`, `format` and pydantic serialization are redacted, and which
refuses to be pickled. Code that needs the value calls `Secret.reveal()` at the last moment.

Error messages name the secret and where it was looked for, never its value. Resolution
logs nothing.
"""

import hmac
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from horae.contracts.instance import EnvSecretRef, SecretRef

REDACTED = "[REDACTED]"
"""What a secret looks like wherever it is printed, logged or serialized."""

MAX_SECRET_BYTES = 64 * 1024
"""Largest secret file read, in bytes."""


class Secret:
    """A secret value that does not print itself."""

    __slots__ = ("_name", "_value")

    _name: str
    _value: str

    def __init__(self, name: str, value: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_value", value)

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


def _clean(name: str, raw: str, origin: str) -> Secret:
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    if not raw:
        raise SecretError(name, f"{origin} is empty")
    if "\x00" in raw:
        raise SecretError(name, f"{origin} contains a NUL character")
    return Secret(name, raw)


class SecretResolver:
    """Resolves secret names to `Secret` values.

    A name with an explicit reference (`secrets.refs` in `horae.yaml`) is read from that
    environment variable or file only. Any other name is read from the environment variable
    of the same name, else from the file of the same name in `directory`.
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

    @property
    def directory(self) -> Path:
        return self._directory

    def resolve(self, name: str) -> Secret:
        """The secret `name`, or `SecretError` saying where it was looked for."""
        ref = self._refs.get(name)
        if isinstance(ref, EnvSecretRef):
            secret = self._from_env(name, ref.env)
            if secret is None:
                raise SecretError(name, f"environment variable {ref.env} is not set")
        elif ref is not None:
            secret = self._from_file(name, self._directory / ref.file)
            if secret is None:
                raise SecretError(name, f"file {self._directory / ref.file} does not exist")
        else:
            secret = self._from_env(name, name) or self._from_file(name, self._directory / name)
            if secret is None:
                raise SecretError(
                    name,
                    f"not set: no environment variable {name} and no file {self._directory / name}",
                )
        return secret

    def _from_env(self, name: str, variable: str) -> Secret | None:
        if variable not in self._environ:
            return None
        secret = _clean(name, self._environ[variable], f"environment variable {variable}")
        return secret

    def _from_file(self, name: str, path: Path) -> Secret | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecretError(name, f"file {path} cannot be read ({exc.strerror})") from None
        if not stat.S_ISREG(info.st_mode):
            raise SecretError(name, f"{path} is not a regular file")
        if info.st_size > MAX_SECRET_BYTES:
            raise SecretError(name, f"file {path} is larger than {MAX_SECRET_BYTES} bytes")
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_SECRET_BYTES + 1)
        except OSError as exc:
            raise SecretError(name, f"file {path} cannot be read ({exc.strerror})") from None
        if len(data) > MAX_SECRET_BYTES:
            raise SecretError(name, f"file {path} is larger than {MAX_SECRET_BYTES} bytes")
        try:
            secret = _clean(name, data.decode("utf-8"), f"file {path}")
        except UnicodeDecodeError:
            raise SecretError(name, f"file {path} is not valid UTF-8") from None
        return secret


__all__ = [
    "MAX_SECRET_BYTES",
    "REDACTED",
    "Secret",
    "SecretError",
    "SecretResolver",
]
