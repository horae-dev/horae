# SPDX-License-Identifier: Apache-2.0
"""What the engine needs from the outside: plugins, tool calls and the LLM.

The engine depends only on these protocols. The plugin host (#24) implements `PluginRegistry`
and `ToolCaller`; the M2 LLM layer implements `LlmBackend`. `horae.engine.testing` has
in-memory implementations for tests.

**Async.** `ToolCaller.call` and `LlmBackend.complete` are coroutines: the MCP SDK's
`ClientSession.call_tool` is async (anyio), and so are the HTTP clients the LLM backends will
use, so an async engine calls them directly, without a thread per call or a nested event loop.
Calls are still awaited one at a time, in declaration order; parallel `collect` can come later
without changing these protocols. Plugin resolution (`PluginRegistry`) is plain data lookup,
hence synchronous.

**Trusted vs untrusted.** A call gets `params` (rendered from the pipeline definition and the run
context only: trusted) and `input` (the data from earlier steps: untrusted, to be treated as
data). Results are data too: nothing a tool or the LLM returns can add, skip or reorder steps.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from horae.contracts.pipeline import Budget
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import ErrorKind, Usage

LlmBackendName = Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]


class EngineCallError(Exception):
    """A failed tool or LLM call, with a machine-readable `kind` (an `ErrorKind`)."""

    def __init__(self, kind: ErrorKind, message: str, *, stderr: str | None = None) -> None:
        super().__init__(kind, message)
        self.kind = kind
        self.message = message
        self.stderr = stderr


class ToolCallError(EngineCallError):
    """A tool call failed (timeout, crash, tool error…). Kinds are the host's (#24)."""


class LlmError(EngineCallError):
    """The LLM call failed (backend error, timeout…)."""


class UnknownPlugin(LookupError):
    """No installed plugin provides this tool or skill."""


@dataclass(frozen=True)
class ResolvedPlugin:
    """The installed plugin a tool or skill namespace resolves to."""

    manifest: PluginManifest
    commit: str | None = None
    """Pinned commit (from `horae.lock`), for plugins installed from git."""


class PluginRegistry(Protocol):
    """Resolves `<namespace>.<tool>` and `<namespace>/<skill>` to installed plugins."""

    def plugin_for_tool(self, ref: str) -> ResolvedPlugin:
        """The plugin serving tool `ref`; raises `UnknownPlugin`."""
        ...

    def plugin_for_skill(self, skill: str) -> ResolvedPlugin:
        """The plugin shipping `skill`; raises `UnknownPlugin`."""
        ...


@dataclass(frozen=True)
class ToolResult:
    """What one tool call returned."""

    data: Any = None
    """The tool's output: plain JSON data."""
    intents: Any = field(default_factory=list)
    """Raw intents the tool proposes (a list), validated by the engine. Only `act` and `notify`
    tools may propose intents."""
    stderr: str | None = None
    """The plugin's (already redacted) standard error for this call."""


class ToolCaller(Protocol):
    """Calls one plugin tool."""

    async def call(self, ref: str, params: dict[str, Any], input: Any) -> ToolResult:
        """Call tool `ref` with trusted `params` and untrusted `input`; raises
        `ToolCallError`. The host applies its own per-call timeout."""
        ...


@dataclass(frozen=True)
class LlmRequest:
    """One attempt of the bounded LLM step."""

    skill: str
    model: str
    """The model alias from the pipeline."""
    input: Any
    """The step's input data (untrusted)."""
    budget: Budget
    """What this attempt may still spend (the step budget minus earlier attempts)."""
    timeout_s: float
    attempt: int
    """1, or 2 for the repair retry."""
    repair: tuple[str, ...] = ()
    """For the retry: the previous attempt's schema validation errors, and nothing else."""


@dataclass(frozen=True)
class LlmResponse:
    """The result of one LLM attempt, validated by the skill layer (M2)."""

    output: Any
    schema_errors: tuple[str, ...]
    """Why `output` fails the skill's output schema; empty when it is valid."""
    usage: Usage
    backend: LlmBackendName
    model: str
    """The concrete model the alias resolved to."""
    skill_version: str
    request: Any = None
    """The request as sent, recorded for replays."""
    cached: bool = False
    intents: Any = field(default_factory=list)
    """Raw intents in the output, validated by the engine against the skill plugin's
    permissions (source `skill:<name>`)."""


class LlmBackend(Protocol):
    """Runs one attempt of an LLM step (no tools)."""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """One attempt; raises `LlmError`. Must honour `request.budget` and `timeout_s`."""
        ...
