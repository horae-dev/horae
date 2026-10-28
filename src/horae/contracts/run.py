# SPDX-License-Identifier: Apache-2.0
"""The run record: what one execution of a pipeline read, proposed, applied and cost.

PLAN.md §3.1 (inputs snapshot, step outputs, intents, applied changes, cost and status) and
§2.1.6 (inputs, plugin versions and the LLM request and response, for exact replays).
"""

import json
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from horae.contracts.intents import Intent
from horae.contracts.pipeline import Pipeline
from horae.contracts.types import (
    MAX_ITEMS,
    BoundedList,
    CommitSha,
    Contract,
    Count,
    IanaTimezone,
    Message,
    PipelineName,
    PluginName,
    PluginRef,
    SchemaVersion1,
    SemVer,
    Sha256Hex,
    SkillRef,
    StrictBool,
    StrictFloat,
    StrictInt,
    Timestamp,
    Token,
)

RunStatus = Literal["ok", "noop", "degraded", "failed", "undone"]
StepStatus = Literal["ok", "skipped", "degraded", "failed"]
StepKind = Literal["collect", "transform", "llm", "act", "notify"]
StepIndex = Annotated[int, Strict(), Field(ge=0)]

MAX_RUN_INTENTS = MAX_ITEMS
"""Upper bound on the intents of one run (`RunRecord.intents`), summed over all its steps.

`horae.intents.MAX_INTENTS` bounds one step's proposals only; the engine must also keep the
run-wide total within this bound."""

RunId = Annotated[str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,128}$")]
"""A run id, e.g. `20261027T053000Z-daily-brief-01`."""

ErrorKind = Annotated[str, Strict(), StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
"""A machine-readable error class, e.g. `timeout` or `schema_invalid`."""

VarName = Annotated[str, Strict(), StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
VarValue = Annotated[str, Strict(), StringConstraints(max_length=4096)]

Stderr = Annotated[str, Strict(), StringConstraints(max_length=65536)]
"""A plugin's standard error, truncated by the runtime to 64 KiB."""

Target = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[^\x00-\x1f\x7f]+$", max_length=2048)
]


MAX_PIPELINE_BYTES = 65536
"""Largest `RunRecord.pipeline_definition` (the canonical pipeline definition), as compact
UTF-8 JSON."""


def _ordered(started: Any, finished: Any) -> None:
    if started is not None and finished is not None and finished < started:
        raise ValueError("`finished_at` is before `started_at`")


def _pipeline_definition(value: dict[str, Any]) -> dict[str, Any]:
    try:
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(text.encode())
    except (TypeError, ValueError):
        raise ValueError("`pipeline_definition` is not plain JSON data") from None
    if size > MAX_PIPELINE_BYTES:
        raise ValueError(f"`pipeline_definition` is larger than {MAX_PIPELINE_BYTES} bytes")
    try:
        Pipeline.model_validate(value)
    except ValidationError:
        raise ValueError("`pipeline_definition` is not a valid pipeline") from None
    return value


class PluginVersion(Contract):
    """The version of a plugin used by the run."""

    version: SemVer
    commit: CommitSha | None = None
    """Pinned commit, for plugins installed from git."""
    provides: BoundedList[PluginRef | SkillRef] | None = None
    """The tools (`<namespace>.<tool>`) and skills (`<namespace>/<skill>`) of the pipeline that
    the run resolved to this plugin, sorted. Recorded so that a replay resolves them the same
    way (a namespace need not be the plugin's name)."""


class RunContext(Contract):
    """The template context the pipeline was rendered with, so a replay renders the same."""

    now: Timestamp
    """The run's notion of "now"."""
    timezone: IanaTimezone
    """IANA time zone used for dates in templates, e.g. `Europe/Paris`."""
    vars: Annotated[dict[VarName, VarValue], Field(max_length=MAX_ITEMS)]
    """Rendered template variables, e.g. `{"date": "2026-10-27"}`."""


class InputSnapshot(Contract):
    """What one source returned, recorded so that the run can be replayed."""

    step: StepIndex
    """Index of the `collect` step in the pipeline."""
    source: PluginRef
    params: dict[str, Any]
    data: Any
    """The source's output, as returned."""
    call: StepIndex | None = None
    """Index of the source in its step's list of calls."""
    intents: Any = None
    """The raw intents the source returned with `data`, as proposed (before the core fills a
    target or validates them; `null` or absent: none). Plain JSON data of any shape: the
    engine validates it. Recorded for replays; the accepted intents are in
    `RunRecord.intents`."""


class StepError(Contract):
    """Why a step failed or degraded."""

    kind: ErrorKind
    message: Message


class CallRecord(Contract):
    """One tool call of a `transform` step, recorded for replays."""

    call: StepIndex
    """Index of the call in its step's list of calls."""
    ref: PluginRef
    params: dict[str, Any]
    """The rendered (trusted) parameters."""
    input_hash: Sha256Hex
    """SHA-256 of the canonical JSON of the input the call was given."""
    intents: Any = None
    """The raw intents the call returned, as proposed (`null` or absent: none)."""
    error_kind: ErrorKind | None = None
    """Why the call failed (the step's `error` has the message); absent when it returned."""


class StepRecord(Contract):
    """The outcome of one step."""

    kind: StepKind
    status: StepStatus
    output: Any = None
    """The step's output, when it has one worth recording."""
    error: StepError | None = None
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    duration_ms: Count | None = None
    stderr: Stderr | None = None
    """Standard error of the plugins the step called, capped at 64 KiB."""
    calls: BoundedList[CallRecord] | None = None
    """For a `transform` step that ran: each call made, in order (the last one may have
    failed). Intermediate outputs are not recorded, only the step's final `output`."""

    @model_validator(mode="after")
    def _times_ordered(self) -> Self:
        _ordered(self.started_at, self.finished_at)
        return self


class Usage(Contract):
    """Tokens and money spent."""

    tokens_in: Count
    """Input tokens billed at the full input price (for the Anthropic API, `input_tokens`:
    cache reads and writes are counted apart)."""
    tokens_out: Count
    usd: Annotated[StrictFloat, Field(ge=0)]
    """What the tokens cost, from the model alias' `price` in `horae.yaml`; `0` when it has
    none (then `priced` is `false`)."""
    cache_read_tokens: Count | None = None
    """Input tokens read from the provider's prompt cache, when it reports them."""
    cache_write_tokens: Count | None = None
    """Input tokens written to the provider's prompt cache, when it reports them."""
    priced: StrictBool | None = None
    """`false` when the model alias has no `price`, so `usd` is `0` and does not reflect the
    real cost; absent when unknown (e.g. records written before decision 38)."""


class LlmCall(Contract):
    """One LLM request and its response."""

    step: StepIndex
    """Index of the `llm` step in the pipeline."""
    skill: SkillRef
    skill_version: Token
    """Version of the skill (part of the cache key)."""
    skill_digest: Sha256Hex | None = None
    """SHA-256 of the loaded skill's content (prompt, output schema, input view, intents; part
    of the cache key), set by the engine when it loaded the skill (#27). Replays report a
    skill that changed since the run."""
    backend: Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]
    model: Token
    """The concrete model the alias resolved to."""
    cached: StrictBool
    """True when the response came from the cache (no call was made)."""
    attempt: Annotated[StrictInt, Field(ge=1)] | None = None
    """1 for the first call, 2 for the repair retry."""
    schema_valid: StrictBool | None = None
    """Whether the response validated against the skill's output schema."""
    model_alias: Token | None = None
    """The model alias the pipeline asked for (set by the engine)."""
    input_hash: Sha256Hex | None = None
    """SHA-256 of the canonical JSON of the input sent with this attempt (set by the engine,
    whatever `request` holds). Not the cache key: that is `RunRecord.input_hash`."""
    repair: BoundedList[Message] | None = None
    """The schema errors sent with this attempt for the repair retry (empty on the first
    attempt; set by the engine)."""
    schema_errors: BoundedList[Message] | None = None
    """Why `response` failed the skill's output schema (empty when it is valid; set by the
    engine, each error capped at 4096 characters, at most 256)."""
    request: Any
    """The request as the backend reports it (its own shape), or the engine's
    `{skill, model, input, attempt, repair}` when the backend reports none. Informational:
    replays check `model_alias`, `input_hash` and `repair`."""
    response: Any
    intents: BoundedList[Any] | None = None
    """The raw intents returned with `response`, as proposed (before the core fills a target
    from the act entry's parameters). Recorded for replays and for reuse on unchanged input;
    the validated, routed intents are in `RunRecord.intents`."""
    usage: Usage


class ProposedIntent(Contract):
    """An intent proposed during the run, with where it came from and which entry applies it."""

    model_config = ConfigDict(
        json_schema_extra={
            "dependentRequired": {
                "applied_by": ["applied_by_step", "applied_by_call"],
                "applied_by_step": ["applied_by", "applied_by_call"],
                "applied_by_call": ["applied_by", "applied_by_step"],
            }
        }
    )

    step: StepIndex
    """Index of the step that proposed it."""
    proposed_by: PluginRef | SkillRef
    """The tool (`<namespace>.<tool>`) or, for the `llm` step, the skill (`<namespace>/<skill>`)
    that proposed it."""
    intent: Intent
    applied_by: PluginRef | None = None
    """The sink or notifier that will apply the intent: the tool of the `act` or `notify`
    entry it was routed to. Set by the engine for every intent it records; an intent without
    it must not be applied."""
    applied_by_step: StepIndex | None = None
    """Index of the `act` or `notify` step of that entry."""
    applied_by_call: StepIndex | None = None
    """Index of that entry in its step's list of calls."""

    @model_validator(mode="after")
    def _route_complete(self) -> Self:
        route = (self.applied_by, self.applied_by_step, self.applied_by_call)
        if any(part is None for part in route) and any(part is not None for part in route):
            raise ValueError("`applied_by`, `applied_by_step` and `applied_by_call` go together")
        return self


class AppliedChange(Contract):
    """A change that was applied. Details for undo are kept in the journal."""

    intent: StepIndex
    """Index into `intents` (checked by the pydantic model only)."""
    target: Target
    """What was changed, e.g. a note path or a URL."""


class Violation(Contract):
    """Access that was blocked or flagged during the run."""

    kind: Literal[
        "egress_blocked", "guard", "undeclared_secret", "undeclared_path", "secret_in_output"
    ]
    """`secret_in_output`: a plugin returned the value (or an encoding) of an instance secret in
    its output or intents; the plugin host replaced it with `[REDACTED:<NAME>]` (#24)."""
    detail: Message


class RunRecord(Contract):
    """One execution of a pipeline."""

    model_config = ConfigDict(
        json_schema_extra={
            "if": {"required": ["dry_run"], "properties": {"dry_run": {"const": True}}},
            "then": {"properties": {"changes": {"maxItems": 0}}},
        }
    )

    schema_version: SchemaVersion1
    """Version of this record format."""
    id: RunId
    """Run id, as used by `horae replay` and `horae undo`."""
    pipeline: PipelineName
    pipeline_digest: Sha256Hex
    """SHA-256 of the canonical pipeline definition that ran."""
    context: RunContext
    lock_digest: Sha256Hex | None = None
    """SHA-256 of the canonical JSON of `horae.lock` at run time
    (`horae.config.Instance.lock_digest`)."""
    config_digest: Sha256Hex | None = None
    """SHA-256 of the canonical JSON of the `models` and `plugins` sections of `horae.yaml` at
    run time (`horae.config.Instance.config_digest`)."""
    models: Annotated[dict[Token, Token], Field(max_length=MAX_ITEMS)] | None = None
    """Model alias to concrete model, as given to the engine (part of the LLM cache key)."""
    pipeline_definition: Annotated[dict[str, Any], AfterValidator(_pipeline_definition)] | None = (
        None
    )
    """The canonical pipeline definition that ran (`horae.engine.canonical_pipeline`, whose
    SHA-256 is `pipeline_digest`), at most `MAX_PIPELINE_BYTES` as compact JSON; omitted when
    larger. A valid pipeline named `pipeline` (checked by the pydantic model only)."""
    input_hash: Sha256Hex | None = None
    """The LLM step's cache key (`horae.engine`), used for the unchanged-input skip. Set only
    when the LLM output was accepted, or reused unchanged from the previous run."""
    dry_run: StrictBool
    """A dry run applies nothing: `changes` is empty."""
    started_at: Timestamp
    finished_at: Timestamp
    """Not before `started_at` (checked by the pydantic model only)."""
    status: RunStatus
    plugins: Annotated[dict[PluginName, PluginVersion], Field(max_length=MAX_ITEMS)]
    """Plugin versions used by the run."""
    inputs: BoundedList[InputSnapshot]
    steps: BoundedList[StepRecord]
    """One record per pipeline step, in order."""
    llm: BoundedList[LlmCall]
    intents: Annotated[list[ProposedIntent], Field(max_length=MAX_RUN_INTENTS)]
    """Intents proposed and accepted during the run, at most `MAX_RUN_INTENTS` in all. A routed
    intent's entry must exist (checked by the pydantic model only): `step` and
    `applied_by_step` index `steps`, the entry's step comes after the proposer's and is a
    `notify` step for `notify.push`, an `act` step otherwise, and when that step recorded its
    entries (`output`), entry `applied_by_call` is `applied_by` and lists this intent."""
    changes: BoundedList[AppliedChange]
    violations: BoundedList[Violation] = Field(default_factory=list)
    cost: Usage
    """Total for the run."""
    error: Message | None = None
    """Why the run is `degraded` or `failed`."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.dry_run and self.changes:
            raise ValueError("a dry run applies nothing: `changes` must be empty")
        for change in self.changes:
            if change.intent >= len(self.intents):
                raise ValueError(f"change refers to intent {change.intent}, out of range")
        for index, intent in enumerate(self.intents):
            _check_route(index, intent, self.steps)
        definition = self.pipeline_definition
        if definition is not None and definition.get("name") != self.pipeline:
            raise ValueError("`pipeline_definition` is the definition of another pipeline")
        _ordered(self.started_at, self.finished_at)
        return self


def _check_route(index: int, intent: ProposedIntent, steps: list[StepRecord]) -> None:
    """`intent` (at `index` in `RunRecord.intents`) names an entry the run really has."""
    if intent.step >= len(steps):
        raise ValueError(f"intent {index}: step {intent.step} is out of range")
    target, call = intent.applied_by_step, intent.applied_by_call
    if target is None or call is None:
        return
    if not intent.step < target < len(steps):
        raise ValueError(f"intent {index}: applied_by_step {target} is not a later step")
    kind = "notify" if intent.intent.type == "notify.push" else "act"
    if steps[target].kind != kind:
        raise ValueError(
            f"intent {index}: {intent.intent.type} must be applied by a step of kind {kind}"
        )
    output = steps[target].output
    if output is None:
        return
    entry = output[call] if isinstance(output, list) and call < len(output) else None
    if (
        not isinstance(entry, dict)
        or entry.get("call") != intent.applied_by
        or not isinstance(entry.get("intents"), list)
        or index not in entry["intents"]
    ):
        raise ValueError(f"intent {index}: entry {call} of step {target} does not list it")
