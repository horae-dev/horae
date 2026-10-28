# SPDX-License-Identifier: Apache-2.0
"""The MCP-backed sink: `Sink` and `RestorableSink` over the plugin host (#25, follow-up of #24).

An `act` entry (`file-sink.write_note: {...}`) names a tool that a plugin lists under
`provides.sinks` (or `provides.notifiers`). The core never calls that tool: it applies the
intents routed to the entry through the plugin's **sink operations**, tools named by
convention after the entry tool's namespace `<ns>` and listed in the plugin's `tools/list`,
**not** in `provides` (the manifest contract reserves `<ns>.<operation>` for every
`SINK_OPERATIONS` entry and sink or notifier namespace, and `HostRegistry` refuses any plugin
providing one; so no pipeline step can call them: the engine only calls provided tools, and
only the core calls these, through `PluginHost.call_operation`, which accepts nothing else):

- `<ns>.read` (`path`): `{"content": <base64> | null}`;
- `<ns>.apply` (`tool`, `params`, `intent`, `run_id`, `index`, `input_hash`, `dedupe_key`,
  `expected_before_sha256`, `content`): `{"target": str, "after_sha256": <hex> | null}`;
- `<ns>.restore` (`path`, `content`: `<base64>` or `null` to remove, `expected_sha256`): `{}`;
- `<ns>.parent_exists` (`path`): `{"exists": bool}`;
- `<ns>.identity` (no argument): `{"identity": str}`.

Every operation also gets `paths` (see "Trusted configuration"); results are the tool's
`structuredContent`. Any operation may instead return
`{"error": {"kind": <^[a-z_]{1,32}$>, "message": str}}`, which becomes
`SinkError(kind, message)`: a kind outside the pattern is `host_error`, the message is
redacted by the host (like all plugin data), stripped of control characters and capped at
`MAX_SINK_MESSAGE` characters.

**The core computes the bytes** (schemas decision 32, D1). For a `note.*` intent, `apply`
reads the note through `<ns>.read`, checks it against `expected_before_sha256`
(`target_changed`), computes the new bytes with `horae.apply.notes.note_after` (ownership,
markers, line endings: the core's conventions, so the sink cannot write anything the dry-run
diff did not show) and sends them as `content`: the plugin's `<ns>.apply` only writes those
bytes, atomically, if the note still has SHA-256 `expected_before_sha256` (`null`: must not
exist), else `target_changed`. It reports the SHA-256 of what it wrote; another digest is
`apply_mismatch`: the change stays "maybe applied" in the journal, **without** the bytes the
sink wrote (`actual_after`: only their digest came back), so `horae undo` takes the note for
"changed since" and needs `--force` to restore it. For other intents (`task.*`,
`notify.push`, `http.post`) `content` and `expected_before_sha256` are `null` and the plugin
applies the intent itself (the routing, target and parameters are the core's; titles and
bodies are untrusted text, data only).

**Double read** (accepted). `horae.apply.apply_changes` reads the note (`Sink.read`) to check
it against the plan, and `McpSink.apply` reads it again to compute the bytes it sends: two
`<ns>.read` calls per note change. The second read is what the compare-and-swap is built on
(the adapter never trusts bytes it did not read itself), and notes are small; merging them
would couple the adapter to the apply phase.

**Trusted configuration.** v1 has no per-plugin configuration in `horae.yaml`, so every
operation also receives `paths`, the plugin's path permissions **as approved in
`horae.lock`** (`{"read": [...], "write": [...]}`; every tool call also carries them in
`_meta["dev.horae/paths"]`): `file-sink` writes to its single `paths.write` entry, the
vault. The plugin trusts the core, never a proposer, for it. (M4's `horae plugin add` must
write that entry into the lock; a per-plugin configuration in `horae.yaml` is a later
decision.)

**Bytes** travel base64-encoded (standard alphabet, validated), at most `MAX_MCP_NOTE_BYTES`
decoded each way (`note_too_large`), derived from the host's JSON-RPC message cap
(`MAX_MESSAGE_BYTES`, 8 MiB per message from the plugin): base64 is 4/3 of the bytes, and
`MESSAGE_MARGIN` is left for the envelope. A sink must send the note once per message (in
`structuredContent`, with a short text block, not a copy of the payload). Both sides check the
cap **before encoding**: `McpSink.apply` and `restore` refuse larger content before calling,
and `file-sink.read` returns `note_too_large` for a larger note instead of an oversized
message (which would cost the plugin its process, `output_too_large`). A malformed result is
`output_invalid`.

**Calls** go through `PluginHost.call_operation`: the plugin's process, its one restart, the
per-call timeout (`plugins.<name>.call_timeout_s`, else `plugin_defaults.call_timeout_s`,
`timeout`), redaction of secrets in everything the plugin returns. A host failure becomes
`SinkError(<host kind>)` with the host's (redacted) message. A `secret_in_output` violation
in a sink operation is logged (the secret's name only, never its value) and kept by
`McpSinks`; `horae.apply.apply_changes` adds the ones collected during a run to the run's
`violations` (`McpSinks.take_violations`), while those of other calls (`horae undo`, a dry
run's diff outside the runner) are only logged.

**Identity** (`horae.apply.sink_identity`) is `mcp:<plugin>:<what <ns>.identity returns>`
(hashed when longer than `MAX_IDENTITY`): where the sink says it writes (`file-sink`: the
vault's `local:<st_dev>:<st_ino>:<real path>`, like `LocalFileSink`), so `horae undo` refuses
a run applied to another vault (`sink_mismatch`). `sink_identity` is synchronous, so the core
**always** awaits the registry's `prepare(tools)` for the tools it is about to use before it
reads an identity (`horae.apply.prepare_sinks`: `apply_changes` for the run's `applied_by`
tools, `undo_run` for the journal's); each operation also prepares its sink first. Until
then reading it raises `SinkError("identity_unknown")`. A definitive failure
(`DEFINITIVE_IDENTITY_FAILURES`: the plugin's answer is wrong or its configuration is) is
cached; any other one (a timeout, a crash: host kinds) is retried once, on the next
`prepare`, then cached; reading the identity meanwhile raises the last failure's kind.
"""

import base64
import binascii
import hashlib
import logging
import unicodedata
from collections.abc import Iterable
from typing import Any, Protocol

from horae.apply.notes import NoteIntent, note_after
from horae.apply.sink import (
    MAX_IDENTITY,
    Applied,
    Sink,
    SinkCall,
    SinkError,
    error_kind,
)
from horae.contracts.plugin import SINK_OPERATIONS
from horae.contracts.run import Violation
from horae.contracts.types import MAX_ITEMS
from horae.engine.ports import ToolCallError, ToolResult, UnknownPlugin
from horae.plugins.host.host import HostRegistry, InstalledPlugin
from horae.plugins.host.transport import MAX_MESSAGE_BYTES

MAX_SINK_MESSAGE = 500
"""The longest error message kept from a sink operation."""
MESSAGE_MARGIN = 64 * 1024
"""Bytes of a message left for the JSON-RPC envelope around a note's base64."""
MAX_MCP_NOTE_BYTES = (MAX_MESSAGE_BYTES - MESSAGE_MARGIN) // 4 * 3
"""The largest note sent over MCP either way (6,242,304 bytes: about 5.95 MiB)."""
_MAX_B64 = 4 * -(-MAX_MCP_NOTE_BYTES // 3)
OPERATIONS = SINK_OPERATIONS
"""The sink operations, called `<ns>.<operation>`."""
DEFINITIVE_IDENTITY_FAILURES = frozenset({"identity_invalid", "vault_unset", "config_invalid"})
"""Identity failures cached at once (any other kind is retried once)."""
_IDENTITY_ATTEMPTS = 2
_log = logging.getLogger("horae.plugins.host.sink")


class OperationCaller(Protocol):
    """What the adapter needs from the host (`PluginHost.call_operation`)."""

    async def call_operation(
        self,
        plugin: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_s: float | None = None,
    ) -> ToolResult: ...


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _too_large() -> SinkError:
    return SinkError(
        "note_too_large", f"the note is larger than {MAX_MCP_NOTE_BYTES} bytes (the MCP cap)"
    )


def encode(data: bytes) -> str:
    """`data` as standard base64 (refusing more than `MAX_MCP_NOTE_BYTES`, before encoding)."""
    if len(data) > MAX_MCP_NOTE_BYTES:
        raise _too_large()
    return base64.b64encode(data).decode("ascii")


def decode(value: object, what: str = "content") -> bytes | None:
    """Base64 `value` (`None` stays `None`), at most `MAX_MCP_NOTE_BYTES` decoded."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SinkError("output_invalid", f"the sink's {what} is not a base64 string")
    if len(value) > _MAX_B64:
        raise _too_large()
    try:
        data = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise SinkError("output_invalid", f"the sink's {what} is not valid base64") from None
    if len(data) > MAX_MCP_NOTE_BYTES:  # pragma: no cover (bounded by _MAX_B64)
        raise _too_large()
    return data


def clean_message(message: object) -> str:
    """A sink's message on one line: control and format characters dropped, capped."""
    if not isinstance(message, str):
        return "the sink reported an error"
    text = "".join(
        " " if char in "\t\n\r" else char
        for char in message
        if char in "\t\n\r" or unicodedata.category(char) not in {"Cc", "Cf", "Zl", "Zp"}
    ).strip()
    if len(text) > MAX_SINK_MESSAGE:
        text = text[: MAX_SINK_MESSAGE - 1] + "…"
    return text or "the sink reported an error"


def _namespace(tool: str) -> str:
    return tool.split(".", 1)[0]


class _Violations:
    """`secret_in_output` violations of sink operations, logged and kept (deduplicated, at most
    `MAX_ITEMS`) until `take`."""

    def __init__(self) -> None:
        self._kept: list[Violation] = []

    def add(self, tool: str, violations: Iterable[Violation]) -> None:
        for violation in violations:
            # The detail names the secrets (the host redacted their values), never a value.
            _log.warning("sink operation %s: %s", tool, violation.detail)
            if violation not in self._kept and len(self._kept) < MAX_ITEMS:
                self._kept.append(violation)

    def take(self) -> tuple[Violation, ...]:
        kept, self._kept = tuple(self._kept), []
        return kept


class McpSink:
    """One sink namespace of one plugin, as a `RestorableSink` (see the module docstring)."""

    def __init__(
        self,
        host: OperationCaller,
        installed: InstalledPlugin,
        namespace: str,
        violations: _Violations | None = None,
    ) -> None:
        self._host = host
        self._installed = installed
        self.plugin = installed.manifest.name
        self.namespace = namespace
        self._violations = violations if violations is not None else _Violations()
        self._identity: str | None = None
        self._identity_error: SinkError | None = None
        self._identity_final = False
        self._identity_attempts = 0

    # --- calls -----------------------------------------------------------------------------

    def _paths(self) -> dict[str, list[str]]:
        paths = self._installed.manifest.permissions.paths
        return {"read": list(paths.read), "write": list(paths.write)}

    async def _op(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call `<ns>.<operation>`; its result object, or `SinkError`."""
        if operation != "identity":
            await self.prepare()
        return await self._raw(operation, arguments)

    async def _raw(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = f"{self.namespace}.{operation}"
        try:
            result = await self._host.call_operation(
                self.plugin, tool, {"paths": self._paths(), **arguments}
            )
        except ToolCallError as error:
            raise SinkError(error_kind(error.kind), clean_message(error.message)) from None
        self._violations.add(tool, result.violations)
        data = result.data
        if not isinstance(data, dict):
            raise SinkError("output_invalid", f"{tool} did not return an object")
        failure = data.get("error")
        if failure is not None:
            if not isinstance(failure, dict):
                raise SinkError("output_invalid", f"{tool} returned a malformed error")
            raise SinkError(error_kind(failure.get("kind")), clean_message(failure.get("message")))
        return data

    # --- identity --------------------------------------------------------------------------

    async def prepare(self) -> None:
        """Fetch and cache the identity (see the module docstring for what is retried).
        Never raises: a failure is raised when the identity is read."""
        if self._identity is not None or self._identity_final:
            return
        self._identity_attempts += 1
        try:
            data = await self._raw("identity", {})
            value = data.get("identity")
            if not isinstance(value, str) or not value or not value.isprintable():
                raise SinkError("identity_invalid", "the sink's identity is not a short string")
            identity = f"mcp:{self.plugin}:{value}"
            if len(identity) > MAX_IDENTITY:
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                identity = f"mcp:{self.plugin}:sha256:{digest}"
            self._identity, self._identity_error = identity, None
        except SinkError as error:
            self._identity_error = error
            self._identity_final = (
                error.kind in DEFINITIVE_IDENTITY_FAILURES
                or self._identity_attempts >= _IDENTITY_ATTEMPTS
            )

    @property
    def identity(self) -> str:
        if self._identity is not None:
            return self._identity
        if self._identity_error is not None:
            raise SinkError(self._identity_error.kind, self._identity_error.message)
        raise SinkError("identity_unknown", f"the identity of {self.plugin} was not fetched yet")

    # --- Sink ------------------------------------------------------------------------------

    async def read_target(self, target: str) -> bytes | None:
        data = await self._op("read", {"path": target})
        return decode(data.get("content"))

    async def read(self, call: SinkCall) -> bytes | None:
        intent = call.intent
        if not isinstance(intent, NoteIntent):
            raise SinkError("not_supported", f"{self.plugin} reads no {intent.type} target")
        return await self.read_target(intent.path)

    async def apply(self, call: SinkCall, *, expected_before_sha256: str | None) -> Applied:
        intent = call.intent
        arguments: dict[str, Any] = {
            "tool": call.tool,
            "params": call.params,
            "intent": intent.model_dump(mode="json"),
            "run_id": call.run_id,
            "index": call.index,
            "input_hash": call.input_hash,
            "dedupe_key": call.dedupe_key,
            "expected_before_sha256": None,
            "content": None,
        }
        if not isinstance(intent, NoteIntent):
            data = await self._op("apply", arguments)
            target = data.get("target")
            details = data.get("details")
            return Applied(
                target if isinstance(target, str) else call.tool,
                details=details if isinstance(details, dict) else {},
            )
        before = await self.read_target(intent.path)
        if _sha(before) != expected_before_sha256:
            raise SinkError("target_changed", f"{intent.path} changed since it was planned")
        after = note_after(intent, before)
        arguments["expected_before_sha256"] = expected_before_sha256
        arguments["content"] = encode(after)  # note_too_large before any call
        data = await self._op("apply", arguments)
        if data.get("after_sha256") != _sha(after):
            message = f"the sink reports other bytes in {intent.path} than the core planned"
            raise SinkError("apply_mismatch", message)
        return Applied(intent.path, before, after)

    async def parent_exists(self, target: str) -> bool:
        data = await self._op("parent_exists", {"path": target})
        exists = data.get("exists")
        if not isinstance(exists, bool):
            raise SinkError("output_invalid", f"{self.namespace}.parent_exists: not a boolean")
        return exists

    async def restore(
        self, target: str, content: bytes | None, *, expected_sha256: str | None
    ) -> None:
        payload = None if content is None else encode(content)  # note_too_large before any call
        arguments = {"path": target, "content": payload, "expected_sha256": expected_sha256}
        await self._op("restore", arguments)


class McpSinks:
    """A `SinkRegistry` over the host: resolves an `act` / `notify` entry's tool
    (`ProposedIntent.applied_by`) to the `McpSink` of its plugin and namespace (one object per
    pair, so the journal's per-sink bookkeeping sees one sink)."""

    def __init__(self, registry: HostRegistry, host: OperationCaller) -> None:
        self._registry = registry
        self._host = host
        self._sinks: dict[tuple[str, str], McpSink] = {}
        self._violations = _Violations()

    def sink_for(self, tool: str) -> Sink | None:
        return self.mcp_sink_for(tool)

    def mcp_sink_for(self, tool: str) -> McpSink | None:
        """The sink serving `tool` (a `provides.sinks` or `provides.notifiers` entry of an
        installed plugin), or `None`."""
        try:
            installed = self._registry.installed_for_tool(tool)
        except UnknownPlugin:
            return None
        provides = installed.manifest.provides
        if tool not in (*provides.sinks, *provides.notifiers):
            return None
        key = (installed.manifest.name, _namespace(tool))
        sink = self._sinks.get(key)
        if sink is None:
            sink = self._sinks[key] = McpSink(self._host, installed, key[1], self._violations)
        return sink

    def tools(self) -> tuple[str, ...]:
        """Every sink and notifier tool of the installed plugins, sorted."""
        return tuple(
            sorted(
                tool
                for plugin in self._registry.plugins()
                for tool in (*plugin.manifest.provides.sinks, *plugin.manifest.provides.notifiers)
            )
        )

    async def prepare(self, tools: Iterable[str]) -> None:
        """Fetch the identity of the sinks serving `tools` (the core passes the tools it is
        about to use: `horae.apply.prepare_sinks`), so `sink_identity` can be read. Never
        raises: a failure is raised when the identity is read."""
        for tool in sorted(set(tools)):
            sink = self.mcp_sink_for(tool)
            if sink is not None:
                await sink.prepare()

    def take_violations(self) -> tuple[Violation, ...]:
        """The `secret_in_output` violations of the sink operations since the last call
        (each was logged when it happened)."""
        return self._violations.take()


__all__ = [
    "DEFINITIVE_IDENTITY_FAILURES",
    "MAX_MCP_NOTE_BYTES",
    "MAX_SINK_MESSAGE",
    "MESSAGE_MARGIN",
    "OPERATIONS",
    "McpSink",
    "McpSinks",
    "OperationCaller",
    "clean_message",
    "decode",
    "encode",
]
