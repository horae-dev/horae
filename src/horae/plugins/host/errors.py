# SPDX-License-Identifier: Apache-2.0
"""The error kinds the plugin host reports (`ToolCallError.kind`).

Every failure of a plugin call is one of these, recorded as the step's `StepError.kind`; the
engine's step policy decides what it means for the run (the host never sets a run status).
Nothing else escapes `PluginHost.call`: an unexpected exception becomes `host_error`, without
its message.

| Kind | Meaning |
|---|---|
| `runtime_unsupported` | no runtime, or `mcp-http` (not supported in v1) |
| `secret_missing` | a declared secret could not be resolved (nothing started) |
| `env_unsafe` | a declared secret name is on the deny list (`horae.plugins.host.env`) |
| `spawn_failed` | the command could not be found or started |
| `startup_timeout` | handshake and `tools/list` took longer than the startup timeout |
| `handshake_failed` | the process exited or answered wrongly during the handshake |
| `tool_missing` | the tool is not in the manifest's `provides` or not in `tools/list` |
| `timeout` | the call took longer than its timeout; the process was killed |
| `plugin_crashed` | the process exited (or closed stdout) while a call was in flight |
| `plugin_unavailable` | the plugin failed earlier in the run and has used its one restart |
| `protocol_error` | a JSON-RPC error response, or a message that is not JSON-RPC |
| `output_too_large` | one stdout message was over `MAX_MESSAGE_BYTES`; the process was killed |
| `output_invalid` | no structured content nor single JSON text block, or malformed intents |
| `tool_error` | the tool reported an error (`isError: true`) |
| `host_error` | a bug in the host |
"""

from typing import Literal

from horae.engine.ports import ToolCallError

HostErrorKind = Literal[
    "runtime_unsupported",
    "secret_missing",
    "env_unsafe",
    "spawn_failed",
    "startup_timeout",
    "handshake_failed",
    "tool_missing",
    "timeout",
    "plugin_crashed",
    "plugin_unavailable",
    "protocol_error",
    "output_too_large",
    "output_invalid",
    "tool_error",
    "host_error",
]


class PluginCallError(ToolCallError):
    """A failed plugin call. `message` and `stderr` are already redacted."""

    def __init__(self, kind: HostErrorKind, message: str, *, stderr: str | None = None) -> None:
        super().__init__(kind, message, stderr=stderr)
        self.host_kind: HostErrorKind = kind


__all__ = ["HostErrorKind", "PluginCallError"]
