# SPDX-License-Identifier: Apache-2.0
"""Pipeline definition (PLAN.md §3.1, §3.2).

A pipeline is a trigger followed by a declared sequence of steps. Control flow is never
decided by an LLM: the step list is the program.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints

from horae.contracts._base import (
    Contract,
    NonEmptyStr,
    PipelineName,
    PluginRef,
    SkillRef,
)

CallWithParams = Annotated[
    dict[PluginRef, dict[str, Any]],
    Field(min_length=1, max_length=1, json_schema_extra={"additionalProperties": False}),
]
"""`{<plugin>.<tool>: {params}}`: one plugin tool with its parameters."""

PluginCall = PluginRef | CallWithParams
"""A plugin tool, either bare (`tasks.normalize`) or with parameters."""

CallList = Annotated[list[PluginCall], Field(min_length=1)]


class Trigger(Contract):
    """When the built-in scheduler runs the pipeline."""

    cron: Annotated[str, StringConstraints(pattern=r"^\s*\S+(\s+\S+){4}\s*$")]
    """A five-field cron expression, e.g. `30 5 * * *`."""


class CollectStep(Contract):
    """Read from sources."""

    collect: CallList


class TransformStep(Contract):
    """Deterministic transforms over the collected data."""

    transform: CallList


class Budget(Contract):
    """Cost and token ceiling for one LLM step."""

    usd: Annotated[float, Field(gt=0)]
    """Maximum spend for the step, in US dollars."""
    tokens_out: Annotated[int, Field(ge=1)]
    """Maximum output tokens for the step."""


class LlmSpec(Contract):
    """The bounded LLM step: a typed function with no tools (PLAN.md §2.1.2, §3.4)."""

    skill: SkillRef
    """The skill (prompt and output schema), `<plugin>/<skill>`."""
    model: NonEmptyStr
    """A model alias, resolved in `horae.yaml`."""
    budget: Budget
    on_unchanged_input: Literal["skip"] = "skip"
    """What to do when the input hash is unchanged since the last run: `skip` makes no LLM
    call (the $0 path)."""
    timeout_s: Annotated[float, Field(gt=0)] | None = None
    """Timeout in seconds. When omitted, the core default applies."""


class LlmStep(Contract):
    """One bounded LLM call."""

    llm: LlmSpec


class ActStep(Contract):
    """Sinks that turn the results into intents, which the core validates and applies."""

    act: CallList


class NotifyStep(Contract):
    """Notifiers."""

    notify: CallList


Step = CollectStep | TransformStep | LlmStep | ActStep | NotifyStep
"""A step: an object with exactly one key, the step kind."""


class Pipeline(Contract):
    """A pipeline definition, e.g. `pipelines/daily-brief.yaml`."""

    name: PipelineName
    """Pipeline name, used by `horae run <name>`."""
    trigger: Trigger
    steps: Annotated[list[Step], Field(min_length=1)]
    """Steps, run in order."""
