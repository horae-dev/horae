# SPDX-License-Identifier: Apache-2.0
"""The run record: what one execution of a pipeline read, proposed, applied and cost.

PLAN.md §3.1 (inputs snapshot, step outputs, intents, applied changes, cost and status) and
§2.1.6 (inputs, plugin versions and the LLM request and response, for exact replays).
"""

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field

from horae.contracts._base import (
    CommitSha,
    Contract,
    NonEmptyStr,
    PipelineName,
    PluginName,
    PluginRef,
    SemVer,
    SkillRef,
)
from horae.contracts.intents import Intent

RunStatus = Literal["ok", "noop", "degraded", "failed"]
StepKind = Literal["collect", "transform", "llm", "act", "notify"]
StepIndex = Annotated[int, Field(ge=0)]
Count = Annotated[int, Field(ge=0)]


class PluginVersion(Contract):
    """The version of a plugin used by the run."""

    version: SemVer
    commit: CommitSha | None = None
    """Pinned commit, for plugins installed from git."""


class InputSnapshot(Contract):
    """What one source returned, recorded so that the run can be replayed."""

    step: StepIndex
    """Index of the `collect` step in the pipeline."""
    source: PluginRef
    params: dict[str, Any]
    data: Any
    """The source's output, as returned."""


class StepRecord(Contract):
    """The outcome of one step."""

    kind: StepKind
    status: Literal["ok", "skipped", "failed"]
    output: Any = None
    """The step's output, when it has one worth recording."""


class Usage(Contract):
    """Tokens and money spent."""

    tokens_in: Count
    tokens_out: Count
    usd: Annotated[float, Field(ge=0)]


class LlmCall(Contract):
    """One LLM request and its response."""

    step: StepIndex
    """Index of the `llm` step in the pipeline."""
    skill: SkillRef
    skill_version: NonEmptyStr
    """Version of the skill (part of the cache key)."""
    backend: Literal["claude-cli", "anthropic", "openai-compatible", "ollama"]
    model: NonEmptyStr
    """The concrete model the alias resolved to."""
    cached: bool
    """True when the response came from the cache (no call was made)."""
    request: Any
    response: Any
    usage: Usage


class ProposedIntent(Contract):
    """An intent proposed during the run, with where it came from."""

    step: StepIndex
    """Index of the step that proposed it."""
    proposed_by: PluginRef
    intent: Intent


class AppliedChange(Contract):
    """A change that was applied. Details for undo are kept in the journal."""

    intent: StepIndex
    """Index into `intents`."""
    target: NonEmptyStr
    """What was changed, e.g. a note path or a URL."""


class RunRecord(Contract):
    """One execution of a pipeline."""

    schema_version: Literal[1]
    """Version of this record format."""
    id: NonEmptyStr
    """Run id, as used by `horae replay` and `horae undo`."""
    pipeline: PipelineName
    dry_run: bool
    """A dry run applies nothing: `changes` is empty."""
    started_at: AwareDatetime
    finished_at: AwareDatetime
    status: RunStatus
    plugins: dict[PluginName, PluginVersion]
    """Plugin versions used by the run."""
    inputs: list[InputSnapshot]
    steps: list[StepRecord]
    """One record per pipeline step, in order."""
    llm: list[LlmCall]
    intents: list[ProposedIntent]
    changes: list[AppliedChange]
    cost: Usage
    """Total for the run."""
    error: NonEmptyStr | None = None
    """Why the run is `degraded` or `failed`."""
