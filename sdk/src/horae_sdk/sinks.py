# SPDX-License-Identifier: Apache-2.0
"""Sinks and notifiers: the operations the core calls (`horae.plugins.host.sink`, decision 36).

A pipeline's `act` (or `notify`) entry names a tool the plugin lists under `provides.sinks`
(or `provides.notifiers`), e.g. `vault.write_task`. **The core never calls that tool**: it
applies the intents routed to the entry through five tools of the entry's namespace `<ns>`,
which the plugin lists in `tools/list` but **not** in `provides` (so no pipeline step can call
them):

- `<ns>.identity()` -> where the sink writes (`horae undo` refuses another target);
- `<ns>.apply(request)` -> `Applied`: apply one intent. For a `note.*` intent **the core
  computes the bytes** and sends them as `request.content`, with `expected_before_sha256`
  (`None`: the note must not exist): write exactly those bytes, atomically, only if the note
  still has that digest (else `SinkError("target_changed")`), and report what was written
  (`Applied.wrote`). For other intents (`task.*`, `notify.push`, `http.post`) `content` is
  `None` and the sink applies `request.intent` itself; titles and bodies are untrusted text,
  data only (a Tasks sink uses `horae_sdk.tasks.task_line`);
- `<ns>.read(paths, path)` -> the note's bytes or `None`;
- `<ns>.restore(paths, path, content, expected_sha256)`: compare-and-swap for `horae undo`
  (`content=None` removes the note);
- `<ns>.parent_exists(paths, path)` -> whether the note's directory exists.

A sink that only handles non-note intents (a notifier) leaves `read`, `restore` and
`parent_exists` to the defaults, which answer `not_supported`. Every operation receives
`paths`, the plugin's path permissions as approved in `horae.lock` (trusted configuration:
where the vault is). A failure is `SinkError(kind, message)`: `kind` matches `^[a-z_]{1,32}$`
(e.g. `target_changed`, `not_supported`, `vault_unset`, `config_invalid`), returned to the
core as `{"error": {"kind", "message"}}`. Note bytes travel as base64, at most
`MAX_NOTE_BYTES` decoded each way (`note_too_large`, checked before encoding); content that
is not base64 is `output_invalid`, the kind the core's own sinks use
(`horae.plugins.host.sink.decode`). Methods may be plain or `async`.
"""

import base64
import binascii
import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from horae_sdk.protocol import MAX_NOTE_BYTES, Paths

SinkRole = Literal["sinks", "notifiers"]
_KIND = re.compile(r"[a-z_]{1,32}")
_REF = re.compile(r"[a-z][a-z0-9-]*\.[a-z][a-z0-9_]*")


class SinkError(Exception):
    """A sink operation failed: `kind` (`^[a-z_]{1,32}$`) and a short message."""

    def __init__(self, kind: str, message: str) -> None:
        if not _KIND.fullmatch(kind):
            raise ValueError(f"sink error kind {kind!r} must match ^[a-z_]{{1,32}}$")
        super().__init__(kind, message)
        self.kind = kind
        self.message = message


def sha256(data: bytes | None) -> str | None:
    """The hex SHA-256 of `data` (`None` stays `None`)."""
    return None if data is None else hashlib.sha256(data).hexdigest()


def _too_large() -> SinkError:
    return SinkError("note_too_large", f"the note is larger than {MAX_NOTE_BYTES} bytes")


def encode(data: bytes | None) -> str | None:
    """`data` as standard base64, refusing more than `MAX_NOTE_BYTES` before encoding."""
    if data is None:
        return None
    if len(data) > MAX_NOTE_BYTES:
        raise _too_large()
    return base64.b64encode(data).decode("ascii")


def decode(value: object) -> bytes | None:
    """Base64 `value` from the core (`None` stays `None`), at most `MAX_NOTE_BYTES`."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SinkError("output_invalid", "content is not a base64 string")
    if len(value) > 4 * -(-MAX_NOTE_BYTES // 3):
        raise _too_large()
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise SinkError("output_invalid", "content is not valid base64") from None


@dataclass(frozen=True)
class ApplyRequest:
    """One routed intent, as the core sends it to `<ns>.apply`."""

    paths: Paths
    intent: dict[str, Any]
    """The intent (validated by the core); its text fields are untrusted data."""
    tool: str | None = None
    """The `act` / `notify` entry's tool, e.g. `vault.write_task`."""
    params: dict[str, Any] = field(default_factory=dict)
    """The entry's trusted parameters."""
    run_id: str | None = None
    index: int | None = None
    """Index of the intent in the run record."""
    input_hash: str | None = None
    dedupe_key: str | None = None
    """For `notify.push`: the notification's deduplication key."""
    content: bytes | None = None
    """For `note.*`: the bytes to write, computed by the core."""
    expected_before_sha256: str | None = None
    """For `note.*`: the digest the note must still have (`None`: it must not exist)."""


@dataclass(frozen=True)
class Applied:
    """What `apply` did: its target, the SHA-256 of the bytes written to a note, details."""

    target: str
    after_sha256: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def wrote(cls, target: str, data: bytes, **details: Any) -> "Applied":
        """A note write of `data` to `target` (the digest the core checks)."""
        return cls(target, sha256(data), dict(details))

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"target": self.target, "after_sha256": self.after_sha256}
        if self.details:
            data["details"] = self.details
        return data


class Sink(ABC):
    """A sink: the base class of a plugin's `provides.sinks` (see the module docstring).

    `tools` are the entry tools a pipeline names (`vault.write_task`), all in one namespace,
    whose operations this object serves."""

    role: ClassVar[SinkRole] = "sinks"

    def __init__(self, tools: Sequence[str]) -> None:
        listed = list(tools)
        if not listed or not all(_REF.fullmatch(str(tool)) for tool in listed):
            raise ValueError("a sink needs one or more tools, <namespace>.<tool>")
        namespaces = {tool.split(".", 1)[0] for tool in listed}
        if len(namespaces) != 1:
            raise ValueError("a sink's tools share one namespace (one object per namespace)")
        self.tools = tuple(listed)
        self.namespace = namespaces.pop()

    @abstractmethod
    def identity(self, paths: Paths) -> Any:
        """Where this sink writes: a short printable string (may be `async`)."""

    @abstractmethod
    def apply(self, request: ApplyRequest) -> Any:
        """Apply one intent; return `Applied` (may be `async`)."""

    def read(self, paths: Paths, path: str) -> Any:
        """The note's bytes, or `None` when it does not exist (may be `async`)."""
        raise SinkError("not_supported", f"{self.namespace} reads no note")

    def restore(
        self, paths: Paths, path: str, content: bytes | None, expected_sha256: str | None
    ) -> Any:
        """Make the note hold `content` (`None`: remove it) if it has `expected_sha256`."""
        raise SinkError("not_supported", f"{self.namespace} restores no note")

    def parent_exists(self, paths: Paths, path: str) -> Any:
        """Whether the note's directory exists (may be `async`)."""
        raise SinkError("not_supported", f"{self.namespace} writes no note")


class Notifier(Sink):
    """A notifier: a sink listed under `provides.notifiers` (`notify` steps, `notify.push`)."""

    role: ClassVar[SinkRole] = "notifiers"


__all__ = [
    "Applied",
    "ApplyRequest",
    "Notifier",
    "Sink",
    "SinkError",
    "decode",
    "encode",
    "sha256",
]
