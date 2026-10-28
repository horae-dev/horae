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

**Which tools are called.** `ToolCaller` is used for `collect` sources and `transform` tools
only. `act` sinks and `notify` notifiers are never called while a pipeline executes: they apply
the routed intents afterwards (#21/#22, ADR 0002), so a dry run calls nothing that writes.

**Trusted vs untrusted.** A call gets `params` (rendered from the pipeline definition and the run
context only: trusted) and `input` (the data from earlier steps: untrusted, to be treated as
data). Results are data too: nothing a tool or the LLM returns can add, skip or reorder steps.

**Redaction is the host's job.** `ToolCallError.message`, `ToolCallError.stderr`,
`ToolResult.stderr` and `LlmError.message` are recorded in the run record as given (only
truncated and stripped of control characters), so the host must redact secret values from them
before raising or returning.

**Misbehaving ports never crash a run.** An error kind that is not a valid `ErrorKind` is
recorded as `host_error`; a result of the wrong type, or output that is not plain, bounded JSON
data, fails the call (`output_invalid` / `output_too_large`); any other exception is recorded as
`host_error` without its message.
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
    """The LLM call failed (backend error, timeout…).

    `usage` is what the failed attempt still cost (e.g. tokens billed before the error); the
    engine adds it to the run's cost and to the step's budget."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        stderr: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(kind, message, stderr=stderr)
        self.usage = usage


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
    """Raw intents the tool proposes (a list; `None` or an empty tuple mean none), validated
    against the tool's plugin permissions and routed to an `act` / `notify` entry by the
    engine (sources and transforms propose; ADR 0002)."""
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
    """What is left of the step's single deadline (`timeout_s` covers both attempts)."""
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
    """The response came from the backend's cache: its usage counts towards neither the cost
    nor the budget."""
    intents: Any = field(default_factory=list)
    """Raw intents in the output, validated by the engine against the skill plugin's
    permissions (source `skill:<name>`) and routed to an `act` / `notify` entry."""


@dataclass(frozen=True)
class PreviousLlm:
    """The last accepted LLM output of a pipeline, for the unchanged-input path.

    Built by the caller from the last finished, non-dry-run run when it is `ok` or `noop`
    (`horae.store.Store.last_accepted_llm`). When its `input_hash` equals this run's cache key,
    the engine makes no LLM call and reuses `output` and `intents` (or, if those intents no
    longer validate or route, calls the LLM as on a cache miss).

    Reuse re-proposes identical intents, `notify.push` included: appliers (#21/#22) must
    deduplicate notifications, e.g. by `input_hash` and intent, so an unchanged input does not
    notify twice."""

    input_hash: str
    output: Any
    intents: Any
    """The raw intents proposed with `output` (validated and routed again)."""
    skill_version: str
    model: str
    backend: LlmBackendName


class LlmBackend(Protocol):
    """Runs one attempt of an LLM step (no tools)."""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """One attempt; raises `LlmError`. Must honour `request.budget` and `timeout_s`."""
        ...
