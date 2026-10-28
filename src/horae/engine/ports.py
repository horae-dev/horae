# SPDX-License-Identifier: Apache-2.0
"""What the engine needs from the outside: plugins, tool calls and the LLM.

The engine depends only on these protocols. The plugin host (#24) implements `PluginRegistry`
and `ToolCaller`; the M2 LLM layer implements `LlmBackend`. `horae.engine.testing` has
in-memory implementations for tests.

**Async.** `ToolCaller.call` and `LlmBackend.complete` are coroutines: the MCP SDK's
`ClientSession.call_tool` is async (anyio), and so are the HTTP clients the LLM backends will
use, so an async engine calls them directly, without a thread per call or a nested event loop.
The calls of one `collect` step run concurrently (at most `MAX_PARALLEL_CALLS` in flight,
started in declaration order); transforms run one at a time, each fed the previous output. A
`ToolCaller` must therefore accept concurrent calls. Plugin resolution (`PluginRegistry`) is
plain data lookup, hence synchronous.

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

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from horae.contracts.intents import IntentType
from horae.contracts.pipeline import Budget
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import ErrorKind, Usage, Violation

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


_USAGE = "horae_usage"


def attach_usage(error: BaseException, usage: Usage) -> None:
    """Record on `error` (a cancellation, typically) what the attempt cost before it: an
    attempt cancelled while its output was being validated was already billed (#31, schemas
    decision 44 E). The usage ledger settles with it and the engine adds it to the run's
    cost (`attached_usage`)."""
    setattr(error, _USAGE, usage)


def attached_usage(error: BaseException) -> Usage | None:
    """The usage `attach_usage` put on `error`, its `__cause__` or its `__context__` (a
    `TimeoutError` that `asyncio.timeout` raised from the cancellation), or `None`."""
    seen: BaseException | None = error
    for _ in range(3):
        if seen is None:
            return None
        usage = getattr(seen, _USAGE, None)
        if isinstance(usage, Usage):
            return usage
        seen = seen.__cause__ or seen.__context__
    return None


class UnknownPlugin(LookupError):
    """No installed plugin provides this tool or skill."""


@dataclass(frozen=True)
class ResolvedPlugin:
    """The installed plugin a tool or skill namespace resolves to."""

    manifest: PluginManifest
    commit: str | None = None
    """Pinned commit (from `horae.lock`), for plugins installed from git."""


class PluginRegistry(Protocol):
    """Resolves `<namespace>.<tool>` and `<namespace>/<skill>` to installed plugins.

    Resolution is plain data lookup: an implementation may read local files (manifests,
    `horae.lock`) but performs no other I/O, starts no process and opens no connection. Only
    `ToolCaller` reaches a plugin. `horae.replay` relies on this: it resolves every ref of the
    pipeline once, up front, into a frozen snapshot, and `horae replay` resolves with
    `horae.replay.LockRegistry` (`horae.lock` and the run record), never with a live host."""

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
    violations: tuple[Violation, ...] = ()
    """What the host flagged in this call (e.g. `secret_in_output`), added to
    `RunRecord.violations` in declaration order."""


class ToolCaller(Protocol):
    """Calls one plugin tool."""

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        """Call tool `ref` with trusted `params` and untrusted `input`; raises
        `ToolCallError`. `timeout_s` is the call's own timeout from the pipeline (`ToolCall`);
        `None` means the host's per-plugin default. The host enforces it (kind `timeout`)."""
        ...


class ReleasingToolCaller(ToolCaller, Protocol):
    """A `ToolCaller` that can stop a plugin the run will not call again (optional).

    When `tools` has a `release` method, the engine awaits `release(plugin)` (the plugin's
    manifest name) right after the last step of the plan that calls one of its tools, or when
    the run stops early, so a plugin's process is not alive during later steps that do not use
    it (schemas decision 35 A). `release` must not raise; if it does, the error is ignored."""

    async def release(self, plugin: str) -> None: ...


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
    prompt: str | None = None
    """The skill's prompt (trusted, sent verbatim). `None` only when the engine was given no
    `SkillSource` (tests with fakes); a real backend (#28) must refuse such a request."""
    output_schema: Mapping[str, Any] | None = None
    """The skill's output JSON Schema (draft 2020-12, bounded; a fresh copy per request)."""
    skill_version: str | None = None
    """The loaded skill's version."""
    skill_digest: str | None = None
    """SHA-256 of the skill's content (`LoadedSkill.digest`)."""
    classified: bool = False
    """The skill's input view declares field classes (#34): a cloud backend must then be
    given a de-identified request (`deidentified`), and refuses one that is not
    (`deid_required`)."""
    deidentified: Mapping[str, Any] | None = None
    """When `input` is the pseudonymized viewed input (#34): the `Pseudonymized.summary`
    (version, digest of what is sent, counts; never a value). `None`: not de-identified."""


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
    """The request as the backend sent it (its own shape), recorded as `LlmCall.request` for
    information. Replays do not rely on it: the engine records its own `model_alias`,
    `input_hash` and `repair` for each attempt."""
    cached: bool = False
    """The response came from the backend's cache: its usage counts towards neither the cost
    nor the budget."""
    intents: Any = field(default_factory=list)
    """Raw intents in the output, validated by the engine against the skill plugin's
    permissions (source `skill:<name>`) and routed to an `act` / `notify` entry."""


@dataclass(frozen=True)
class PreviousLlm:
    """An accepted LLM output, for the unchanged-input path: a cache entry.

    Given by the caller as `previous` (replay: the output the recorded run reused) or
    returned by an `LlmCache` (`horae.runner.StoreLlmCache`, the store's `llm_cache` table,
    #32). When its `input_hash` equals this run's cache key, the engine makes no LLM call and
    reuses `output` and `intents` (or, if those intents no longer validate or route, calls the
    LLM as on a cache miss).

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
    source_run: str | None = None
    """The run that produced the output (a cache entry's producer), recorded in the reuse
    marker of the run that reuses it (`request.source_run`); `None` when unknown."""
    deid_input_sha256: str | None = None
    """When the output was produced from a de-identified request (#34): the SHA-256 of what
    was sent (`llm[].request.deid.input_sha256`). The engine reuses the output only when this
    run's de-identified input has the same digest (both `None` without de-identification):
    otherwise the pseudonyms it holds could name other values (another vocabulary, another
    transform), so it is a miss, and a replay reports the mismatch."""


def deid_digest(request: Any) -> str | None:
    """`request.deid.input_sha256` of a recorded LLM request (#34), else `None`."""
    deid = request.get("deid") if isinstance(request, dict) else None
    digest = deid.get("input_sha256") if isinstance(deid, dict) else None
    return digest if isinstance(digest, str) else None


class LlmCache(Protocol):
    """Looks up accepted LLM outputs by cache key (the unchanged-input path, #32).

    The engine only reads: it awaits `lookup` once per `llm` step, with the step's cache key
    (`RunRecord.input_hash` once accepted), the step's model alias and, with a loaded skill,
    its full output schema (the reserved `intents` property included). Writing accepted
    outputs, expiry and purging are the caller's (`horae.runner`). `lookup` may read and
    clean local storage (the SQLite store) and validate the entry against `output_schema`
    (an invalid entry is a miss), but must not call a model or the network. An exception is
    a cache miss. `discard` is part of the port (#35, schemas decision 46), so mypy checks
    that every cache has it: the engine awaits it when the action guard refuses a hit's
    intents, and its exceptions are ignored. A cache that cannot delete entries subclasses
    `NoDiscardCache` (a documented no-op) or defines its own no-op. A refused `previous`
    (replay, decision 33) is not a cache entry and is never discarded."""

    async def lookup(
        self, key: str, *, model_alias: str, output_schema: Mapping[str, Any] | None
    ) -> PreviousLlm | None:
        """The live, valid entry for `key` (its `input_hash` is `key`), or `None`."""
        ...

    async def discard(self, key: str, *, model_alias: str) -> None:
        """Delete the entry for `key` (the action guard refused its intents on reuse), so a
        poisoned entry is not looked up again."""
        ...


class NoDiscardCache(ABC):
    """A base for an `LlmCache` that cannot delete entries: `discard` is a no-op, so a refused
    hit stays a miss each time it is looked up (the guard refuses it again; nothing of it is
    ever routed). Subclasses implement `lookup`."""

    @abstractmethod
    async def lookup(
        self, key: str, *, model_alias: str, output_schema: Mapping[str, Any] | None
    ) -> PreviousLlm | None:
        """The live, valid entry for `key`, or `None` (`LlmCache.lookup`)."""

    async def discard(self, key: str, *, model_alias: str) -> None:
        """A no-op: this cache cannot delete entries."""
        del key, model_alias


class SkillUnavailable(Exception):
    """A skill could not be loaded: missing or invalid files (`horae.skills.SkillError`).

    The message names the skill and the problem (file names, never file content); the engine
    records it as a `skill_invalid` plan error."""


class LoadedSkill(Protocol):
    """A validated skill (`horae.skills.Skill`): what the engine needs from it."""

    @property
    def name(self) -> str:
        """`<namespace>/<skill>`."""
        ...

    @property
    def version(self) -> str:
        """The skill's semver version (`LlmCall.skill_version`)."""
        ...

    @property
    def digest(self) -> str:
        """SHA-256 of the skill's prompt, output schema, input view and intents."""
        ...

    @property
    def prompt(self) -> str: ...

    @property
    def output_schema(self) -> Mapping[str, Any]:
        """The output JSON Schema (a fresh copy on each access)."""
        ...

    @property
    def intents(self) -> tuple[IntentType, ...]:
        """The intents the skill may propose (within the plugin's permissions)."""
        ...

    @property
    def intent_limits(self) -> Mapping[str, int]:
        """The most intents of a type the skill may propose per step (types not listed: no
        limit beyond the run's)."""
        ...

    @property
    def output_bound(self) -> int:
        """The output schema's size bound, in UTF-8 bytes of compact JSON."""
        ...

    @property
    def view_paths(self) -> tuple[str, ...]:
        """The input view paths (`SkillManifest.input_view`), sorted."""
        ...

    @property
    def view_options(self) -> Mapping[str, Mapping[str, Any]]:
        """The non-empty options of the input view paths, by path (field classes, #34)."""
        ...

    def view(self, data: Any) -> Any:
        """The step input restricted to the skill's input view (plain JSON, deterministic)."""
        ...


class SkillSource(Protocol):
    """Loads the skill a plugin ships (`horae.skills.DirectorySkills`).

    Like `PluginRegistry`, it reads local files only. The engine loads the skill of the `llm`
    step while planning, before any plugin is called."""

    def load_skill(self, skill: str, plugin: ResolvedPlugin) -> LoadedSkill:
        """The validated skill `skill` of `plugin`; raises `SkillUnavailable`."""
        ...


class DeidUnavailable(Exception):
    """De-identification cannot be done (`horae.llm.deid.DeidError`): the step degrades
    (`deid_unavailable`) and nothing is sent. The message holds no personal data."""


class Pseudonyms(Protocol):
    """One viewed input, pseudonymized (`horae.llm.deid.Pseudonymized`), with its mapping in
    memory only."""

    @property
    def data(self) -> Any:
        """What the model is sent instead of the viewed input."""
        ...

    def summary(self) -> dict[str, Any]:
        """What the run record keeps (`llm[].request.deid`): never a value."""
        ...

    def reidentify(self, value: Any) -> tuple[Any, list[str]]:
        """`value` with real values put back, and the pseudonyms left unresolved."""
        ...

    def growth(self, value: Any) -> int:
        """How many bytes re-identifying `value` would add (computed without substituting)."""
        ...


class Deidentifier(Protocol):
    """The instance's de-identification policy (`horae.llm.deid.Deidentifier`, #34)."""

    def pseudonymize(
        self, model_alias: str, view_options: Mapping[str, Mapping[str, Any]], data: Any
    ) -> Pseudonyms | None:
        """`data` pseudonymized for `model_alias`, or `None` (a local alias, no field class).
        Raises `DeidUnavailable`."""
        ...


class LlmBackend(Protocol):
    """Runs one attempt of an LLM step (no tools)."""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """One attempt; raises `LlmError`. Must honour `request.budget` and `timeout_s`."""
        ...
