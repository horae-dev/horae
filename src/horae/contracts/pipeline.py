# SPDX-License-Identifier: Apache-2.0
"""Pipeline definition (PLAN.md §3.1, §3.2).

A pipeline is an optional trigger followed by a declared sequence of steps. Control flow is
never decided by an LLM: the step list is the program.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, Strict, StringConstraints, model_validator

from horae.contracts.types import (
    MAX_ITEMS,
    Contract,
    IanaTimezone,
    PipelineName,
    PluginRef,
    SkillRef,
    StrictFloat,
    StrictInt,
    Token,
)

CallWithParams = Annotated[
    dict[PluginRef, dict[str, Any]],
    Field(min_length=1, max_length=1, json_schema_extra={"additionalProperties": False}),
]
"""`{<namespace>.<tool>: {params}}`: one plugin tool with its parameters."""

PluginCall = PluginRef | CallWithParams
"""A plugin tool, either bare (`tasks.normalize`) or with parameters."""

CallList = Annotated[list[PluginCall], Field(min_length=1, max_length=MAX_ITEMS)]

_CRON_FIELD = r"[0-9A-Za-z*/,-]+"

CronExpression = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=rf"^{_CRON_FIELD}( {_CRON_FIELD}){{4}}$", max_length=128),
]
"""Five cron fields separated by single spaces, e.g. `30 5 * * *`. No `@daily` macros."""


class Trigger(Contract):
    """When the built-in scheduler runs the pipeline."""

    cron: CronExpression
    """A five-field cron expression, e.g. `30 5 * * *`."""
    timezone: IanaTimezone | None = None
    """IANA time zone the expression is evaluated in. When omitted, the instance's."""


class CollectStep(Contract):
    """Read from sources."""

    collect: CallList


class TransformStep(Contract):
    """Deterministic transforms over the collected data."""

    transform: CallList


class Budget(Contract):
    """Cost and token ceiling for one LLM step."""

    usd: Annotated[StrictFloat, Field(gt=0)]
    """Maximum spend for the step, in US dollars."""
    tokens_out: Annotated[StrictInt, Field(ge=1)]
    """Maximum output tokens for the step."""


class LlmSpec(Contract):
    """The bounded LLM step: a typed function with no tools (PLAN.md §2.1.2, §3.4)."""

    skill: SkillRef
    """The skill (prompt and output schema), `<namespace>/<skill>`."""
    model: Token
    """A model alias, resolved in `horae.yaml`."""
    budget: Budget
    on_unchanged_input: Literal["skip"] = "skip"
    """What to do when the input hash is unchanged since the last run: `skip` makes no LLM
    call (the $0 path)."""
    timeout_s: Annotated[StrictFloat, Field(gt=0)] | None = None
    """Timeout in seconds. When omitted, the core default applies."""


class LlmStep(Contract):
    """One bounded LLM call."""

    llm: LlmSpec


class ActStep(Contract):
    """Sinks, which apply the intents routed to them after the run's steps. The core first
    validates every intent against the proposing plugin's `permissions.intents`, routes it to
    the one entry whose sink may apply it and whose trusted parameters (a required `mode`, and
    `path` or `url`) bind its target, shows it in a dry run and journals it (rules in
    `horae.engine.executor`)."""

    act: CallList


class NotifyStep(Contract):
    """Notifiers, which apply the `notify.push` intents routed to them. Their parameters
    (`topic`, `when`…) are trusted metadata for the notifier; a proposed `link` is routed only
    to an entry whose trusted `link_prefix` it starts with."""

    notify: CallList


Step = CollectStep | TransformStep | LlmStep | ActStep | NotifyStep
"""A step: an object with exactly one key, the step kind."""


class Pipeline(Contract):
    """A pipeline definition, e.g. `pipelines/daily-brief.yaml`."""

    name: PipelineName
    """Pipeline name, used by `horae run <name>`."""
    trigger: Trigger | None = None
    """When omitted, the pipeline runs only from external triggers (`horae run <name>`)."""
    steps: Annotated[
        list[Step],
        Field(
            min_length=1,
            max_length=MAX_ITEMS,
            json_schema_extra={
                "contains": {"required": ["llm"]},
                "minContains": 0,
                "maxContains": 1,
            },
        ),
    ]
    """Steps, run in order. At most one `llm` step."""

    @model_validator(mode="after")
    def _one_llm_step(self) -> Self:
        if sum(isinstance(step, LlmStep) for step in self.steps) > 1:
            raise ValueError("a pipeline has at most one `llm` step")
        return self
