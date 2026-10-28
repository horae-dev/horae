# SPDX-License-Identifier: Apache-2.0
"""Skill evaluation cases and recorded responses (#36, schemas decision 48).

Golden cases live next to the skill they test, in its folder:

```
skills/<namespace>/<skill>/
  evals/
    <case>.yaml                    # EvalCase
    recorded/<backend>/<case>.json # RecordedResponse, one per backend
```

**A case** (`EvalCase`) holds the step `input` **before** the skill's input view (the eval
applies the view, as the engine does), optional `targets` for the intents' routed fields (the
pipeline's `act` entries bind them in a run), an optional `budget` for a live call, and what
is expected (`EvalExpect`): whether the output passes the skill's schema, the exact output or
JSON-pointer assertions on it, the exact intents or their count per type, and whether the
action guard accepts them.

**A recorded response** (`RecordedResponse`) is provider-agnostic: the parsed JSON output as
the provider backend returned it (the reserved `intents` included), its `usage` and stop
reason, and the digests of what was sent (`request`: the skill's prompt, the viewed input and
the output schema), so an eval tells when a recording is stale (the skill or the input
changed since it was recorded). It never holds the provider's raw payload, headers or any
secret: `horae eval --live --record` writes it redacted (schemas decision 43).
"""

from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, Strict, StringConstraints, model_validator

from horae.contracts.instance import LlmBackend
from horae.contracts.intents import IntentType
from horae.contracts.pipeline import Budget
from horae.contracts.run import Usage
from horae.contracts.types import (
    BoundedList,
    Contract,
    Count,
    Line,
    SemVer,
    Sha256Hex,
    SkillRef,
    StrictBool,
    Token,
)

CaseName = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$", max_length=64)
]
"""A case's name: its file name without `.yaml` (lower case, digits, `_` and `-`)."""

JsonPointer = Annotated[
    str,
    Strict(),
    StringConstraints(pattern=r"^(/[^/\x00-\x1f\x7f]*)*$", max_length=512),
]
"""An RFC 6901 JSON pointer (`""` is the whole document; `~0` is `~`, `~1` is `/`)."""

TargetType = Literal["note.create", "note.append_section", "note.replace_owned_block", "http.post"]
"""The intent types whose target (`path`, or `url` for `http.post`) a pipeline binds."""

CHECKS = ("equals", "length", "contains", "exists")
"""The checks of an assertion: exactly one per assertion."""


class EvalAssertion(Contract):
    """One check on the evaluated document `{"output": ..., "intents": [...]}` (the output
    without its reserved `intents`, and the raw intents), at `pointer`."""

    model_config = ConfigDict(
        extra="forbid",
        use_attribute_docstrings=True,
        json_schema_extra={"oneOf": [{"required": [check]} for check in CHECKS]},
    )

    pointer: JsonPointer
    """Where, e.g. `/output/ranked/0/why` or `/intents`."""
    equals: Any = None
    """The value there, exactly (canonical JSON equality)."""
    length: Count | None = None
    """The length of the string, array or object there."""
    contains: Any = None
    """A substring of the string there, or an element of the array there."""
    exists: StrictBool | None = None
    """Whether anything is there."""

    @model_validator(mode="after")
    def _one_check(self) -> Self:
        given = [check for check in CHECKS if check in self.model_fields_set]
        if len(given) != 1:
            raise ValueError(f"an assertion has exactly one of {', '.join(CHECKS)}")
        return self

    @property
    def check(self) -> str:
        return next(check for check in CHECKS if check in self.model_fields_set)


class EvalExpect(Contract):
    """What a case expects. Everything is optional: an empty `expect` only asks for a
    schema-valid output whose intents the guard accepts."""

    schema_valid: StrictBool = True
    """Whether the output passes the skill's full output schema (`validate_output`)."""
    output: Any = None
    """The exact output, without its reserved `intents` (canonical JSON equality). Absent:
    not compared."""
    assertions: BoundedList[EvalAssertion] = Field(default_factory=list)
    intents: BoundedList[Any] | None = None
    """The exact raw intents, as the model proposed them (before any target is filled)."""
    intent_counts: dict[IntentType, Count] | None = None
    """How many intents of each type (a type not listed: none)."""
    guard: Literal["pass", "violation"] = "pass"
    """Whether the action guard accepts the intents (decision 46); `violation` for a case that
    proves a hostile output is refused."""

    @property
    def has_output(self) -> bool:
        return "output" in self.model_fields_set


class EvalCase(Contract):
    """One golden case of a skill, `evals/<case>.yaml`."""

    schema_version: Literal[1]
    description: Line | None = None
    input: Any
    """The step input before the skill's input view (synthetic data only)."""
    targets: Annotated[dict[TargetType, Line], Field(max_length=4)] = Field(default_factory=dict)
    """The routed target of each intent type the case's intents need (`note.create:
    Daily/2026-10-28.md`); a note type without one gets `Evals/<case>.md`."""
    budget: Budget | None = None
    """The budget of a live call (default: `horae.evals.DEFAULT_BUDGET`)."""
    expect: EvalExpect = Field(default_factory=EvalExpect)


class RecordedRequest(Contract):
    """The digests of what was sent (SHA-256 of canonical JSON, or of the prompt's UTF-8)."""

    system_sha256: Sha256Hex
    """The skill's prompt."""
    input_sha256: Sha256Hex
    """The viewed input."""
    schema_sha256: Sha256Hex
    """The skill's output schema."""


class RecordedResponse(Contract):
    """A provider's answer to one case, normalised (`evals/recorded/<backend>/<case>.json`)."""

    schema_version: Literal[1]
    backend: LlmBackend
    model: Token
    """The concrete model that answered."""
    skill: SkillRef
    skill_version: SemVer
    skill_digest: Sha256Hex
    """The skill's digest when it was recorded (a different one: the recording is stale)."""
    request: RecordedRequest
    output: Any
    """The parsed JSON output, its reserved `intents` included, redacted."""
    usage: Usage
    stop_reason: Token | None = None


__all__ = [
    "CHECKS",
    "CaseName",
    "EvalAssertion",
    "EvalCase",
    "EvalExpect",
    "JsonPointer",
    "RecordedRequest",
    "RecordedResponse",
    "TargetType",
]
