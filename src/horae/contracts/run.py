# SPDX-License-Identifier: Apache-2.0
"""The run record: what one execution of a pipeline read, proposed, applied and cost.

PLAN.md §3.1 (inputs snapshot, step outputs, intents, applied changes, cost and status) and
§2.1.6 (inputs, plugin versions and the LLM request and response, for exact replays).
"""

from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, Strict, StringConstraints, model_validator

from horae.contracts.intents import Intent
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


def _ordered(started: Any, finished: Any) -> None:
    if started is not None and finished is not None and finished < started:
        raise ValueError("`finished_at` is before `started_at`")


class PluginVersion(Contract):
    """The version of a plugin used by the run."""

    version: SemVer
    commit: CommitSha | None = None
    """Pinned commit, for plugins installed from git."""


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


class StepError(Contract):
    """Why a step failed or degraded."""

    kind: ErrorKind
    message: Message


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

    @model_validator(mode="after")
    def _times_ordered(self) -> Self:
        _ordered(self.started_at, self.finished_at)
        return self


class Usage(Contract):
    """Tokens and money spent."""

    tokens_in: Count
    tokens_out: Count
    usd: Annotated[StrictFloat, Field(ge=0)]


class LlmCall(Contract):
    """One LLM request and its response."""

    step: StepIndex
    """Index of the `llm` step in the pipeline."""
    skill: SkillRef
    skill_version: Token
    """Version of the skill (part of the cache key)."""
    backend: Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]
    model: Token
    """The concrete model the alias resolved to."""
    cached: StrictBool
    """True when the response came from the cache (no call was made)."""
    attempt: Annotated[StrictInt, Field(ge=1)] | None = None
    """1 for the first call, 2 for the repair retry."""
    schema_valid: StrictBool | None = None
    """Whether the response validated against the skill's output schema."""
    request: Any
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

    kind: Literal["egress_blocked", "guard", "undeclared_secret", "undeclared_path"]
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
    """SHA-256 of `horae.lock` at run time."""
    config_digest: Sha256Hex | None = None
    """SHA-256 of the effective `horae.yaml` at run time."""
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
