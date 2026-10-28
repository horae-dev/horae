# SPDX-License-Identifier: Apache-2.0
"""Recorded ports: a `ToolCaller` and an `LlmBackend` that answer only from a `RunRecord`.

Neither holds a host, a process, a socket or a backend: their only state is the record (and
the pipeline it is replayed against). A call the record cannot answer is a **mismatch**: it is
noted (`mismatches`) and fails the call with kind `replay_mismatch`; it is never forwarded
anywhere, because there is nowhere to forward it to.
"""

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from horae.contracts.pipeline import CollectStep, Pipeline, TransformStep
from horae.contracts.run import LlmCall, ProposedIntent, RunContext, RunRecord, StepRecord, Usage
from horae.engine.canonical import canonical_json
from horae.engine.ports import (
    LlmError,
    LlmRequest,
    LlmResponse,
    PreviousLlm,
    ToolCallError,
    ToolResult,
)
from horae.engine.template import TemplateError, render, variables
from horae.intents import STEP_ERROR_KIND

MISMATCH = "replay_mismatch"
"""Error kind of a call the record cannot answer."""

INTENT_KINDS = frozenset(
    {STEP_ERROR_KIND, "intent_unrouted", "intent_ambiguous", "run_intents_exceeded"}
)
"""Step error kinds caused by a step's proposals (whose rejected intents are not recorded)."""


class NotReplayable(Exception):
    """The record lacks what an exact replay needs (see `horae.replay`, "Gaps")."""


def _canon(value: Any) -> bytes:
    return canonical_json(value)


def _unquote(text: str) -> str:
    """The text inside a `safe_excerpt` quote (`'…'`), as the engine wrote it."""
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    return text


def _tail(message: str, ref: str, kind: str) -> str | None:
    """The port's own message, from a recorded `"<ref>: <kind>: '<message>'"` line."""
    head = f"{ref}: {kind}: "
    return _unquote(message[len(head) :]) if message.startswith(head) else None


@dataclass(frozen=True)
class Expected:
    """One tool call the pipeline makes, in order: what the engine will ask for."""

    step: int
    call: int
    ref: str
    params: dict[str, Any]


def planned_calls(pipeline: Pipeline, context: RunContext) -> list[Expected]:
    """Every `collect` / `transform` call of `pipeline`, rendered like the engine renders it.

    The engine calls a prefix of this list, in this order (a step that stops the run ends it).
    A template error yields no call (the engine fails the plan before calling anything)."""
    try:
        names = variables(context)
        expected: list[Expected] = []
        for index, step in enumerate(pipeline.steps):
            if not isinstance(step, CollectStep | TransformStep):
                continue
            calls = step.collect if isinstance(step, CollectStep) else step.transform
            for position, call in enumerate(calls):
                ref, params = (call, {}) if isinstance(call, str) else next(iter(call.items()))
                rendered = render(copy.deepcopy(params), names)
                expected.append(Expected(index, position, ref, rendered))
    except TemplateError:
        return []
    return expected


@dataclass(frozen=True)
class _Answer:
    data: Any = None
    intents: tuple[Any, ...] = ()
    error: tuple[str, str] | None = None
    """`(kind, message)` of a recorded failure."""


def _intents_by(record: RunRecord) -> dict[tuple[int, str], list[Any]]:
    """Recorded intents by `(step, proposed_by)`, in record order, as plain intent data."""
    found: dict[tuple[int, str], list[Any]] = {}
    for proposed in record.intents:
        found.setdefault((proposed.step, proposed.proposed_by), []).append(_intent_data(proposed))
    return found


def _intent_data(proposed: ProposedIntent) -> Any:
    return proposed.intent.model_dump(mode="json", exclude_none=True)


@dataclass
class _Tape:
    """Answers per `(step, call)`."""

    answers: dict[tuple[int, int], _Answer] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _step(record: RunRecord, index: int) -> StepRecord | None:
    return record.steps[index] if index < len(record.steps) else None


def _collect_answers(
    tape: _Tape, record: RunRecord, index: int, calls: list[Expected], intents: Any
) -> None:
    step = _step(record, index)
    if step is None or step.kind != "collect" or step.status == "skipped":
        return  # not run: any call is a mismatch
    kind = step.error.kind if step.error is not None else None
    if step.status == "failed" and kind != "source_failed":
        raise NotReplayable(
            f"step {index} (collect) failed with {kind}: the record keeps neither its sources' "
            "data nor the intents they proposed"
        )
    successes = [snap for snap in record.inputs if snap.step == index]
    failures = [f for f in step.output or [] if isinstance(f, dict)]
    lines = step.error.message.split("\n") if step.error is not None else []
    if len(lines) != len(failures):
        lines = [""] * len(failures)
    lines_by_failure = list(zip(failures, lines, strict=True))
    claimed: set[str] = set()
    for expected in calls:
        key = _canon(expected.params)
        snap = next(
            (s for s in successes if s.source == expected.ref and _canon(s.params) == key), None
        )
        if snap is not None:
            successes.remove(snap)
            own: tuple[Any, ...] = ()
            if expected.ref not in claimed:  # intents are recorded per tool, not per call
                claimed.add(expected.ref)
                own = tuple(intents.get((index, expected.ref), ()))
            tape.answers[(index, expected.call)] = _Answer(data=snap.data, intents=own)
            continue
        failed = next(
            (
                (f, line)
                for f, line in lines_by_failure
                if f.get("source") == expected.ref and _canon(f.get("params")) == key
            ),
            None,
        )
        if failed is not None:
            lines_by_failure.remove(failed)
            error_kind = str(failed[0].get("error_kind"))
            message = _tail(failed[1], expected.ref, error_kind) or "recorded failure"
            tape.answers[(index, expected.call)] = _Answer(error=(error_kind, message))


def _transform_answers(
    tape: _Tape, record: RunRecord, index: int, calls: list[Expected], intents: Any
) -> None:
    step = _step(record, index)
    if step is None or step.kind != "transform" or step.status == "skipped" or not calls:
        return
    if step.status != "failed":
        claimed: set[str] = set()
        for position, expected in enumerate(calls):
            own: tuple[Any, ...] = ()
            if expected.ref not in claimed:
                claimed.add(expected.ref)
                own = tuple(intents.get((index, expected.ref), ()))
            # Only the step's final output is recorded; intermediate outputs only feed the
            # next (recorded) transform, which ignores its input.
            data = step.output if position == len(calls) - 1 else None
            tape.answers[(index, expected.call)] = _Answer(data=data, intents=own)
        return
    error = step.error
    kind = error.kind if error is not None else "host_error"
    message = error.message if error is not None else ""
    culprit = next((c for c in calls if message.startswith(f"{c.ref}: ")), calls[0])
    for expected in calls:
        if expected is culprit:
            tail = _tail(message, expected.ref, kind)
            tape.answers[(index, expected.call)] = _Answer(
                error=(kind, tail if tail is not None else "recorded failure")
            )
            break
        tape.answers[(index, expected.call)] = _Answer()
    if kind in INTENT_KINDS:
        tape.notes.append(
            f"step {index} (transform) failed with {kind}: the rejected intents are not "
            "recorded, so the failure is served from the record instead of re-validated"
        )


def _tape(record: RunRecord, expected: list[Expected]) -> _Tape:
    tape = _Tape()
    intents = _intents_by(record)
    by_step: dict[int, list[Expected]] = {}
    for call in expected:
        by_step.setdefault(call.step, []).append(call)
    for index, calls in by_step.items():
        step = _step(record, index)
        if step is not None and step.kind == "collect":
            _collect_answers(tape, record, index, calls, intents)
        elif step is not None and step.kind == "transform":
            _transform_answers(tape, record, index, calls, intents)
    return tape


class RecordedToolCaller:
    """A `ToolCaller` that serves `collect` / `transform` results from a run record.

    Each call is matched, in order, against the pipeline's planned calls: the `(step, call)`
    position, the tool `ref` and the canonical JSON of the rendered `params` must all agree,
    and the record must hold an answer for that position (a source's `InputSnapshot`, a failed
    source's entry, a transform step's output or error). Anything else is a mismatch."""

    def __init__(self, record: RunRecord, pipeline: Pipeline, context: RunContext) -> None:
        planned = planned_calls(pipeline, context)
        self._expected: Iterator[Expected] = iter(planned)
        tape = _tape(record, planned)
        self._answers = tape.answers
        self.notes: list[str] = tape.notes
        self.mismatches: list[str] = []

    def _mismatch(self, message: str) -> ToolCallError:
        self.mismatches.append(message)
        return ToolCallError(MISMATCH, message)

    async def call(self, ref: str, params: dict[str, Any], input: Any) -> ToolResult:
        expected = next(self._expected, None)
        if expected is None or expected.ref != ref or _canon(expected.params) != _canon(params):
            raise self._mismatch(f"unexpected call to {ref}: the pipeline does not plan it here")
        answer = self._answers.get((expected.step, expected.call))
        if answer is None:
            raise self._mismatch(
                f"step {expected.step} call {expected.call} ({ref}): no recorded result "
                "for this tool and these parameters"
            )
        if answer.error is not None:
            raise ToolCallError(*answer.error)
        return ToolResult(data=copy.deepcopy(answer.data), intents=copy.deepcopy(answer.intents))


def _is_reuse(call: LlmCall) -> bool:
    return (
        call.cached
        and call.attempt is None
        and isinstance(call.request, dict)
        and isinstance(call.request.get("reused_input_hash"), str)
    )


def recorded_previous(record: RunRecord) -> PreviousLlm | None:
    """The `PreviousLlm` the recorded run reused (unchanged input), if it reused one."""
    for call in record.llm:
        if _is_reuse(call):
            return PreviousLlm(
                input_hash=str(call.request["reused_input_hash"]),
                output=call.response,
                intents=list(call.intents or []),
                skill_version=call.skill_version,
                model=call.model,
                backend=call.backend,
            )
    return None


def _leftover(record: RunRecord) -> Usage | None:
    """What a failed LLM attempt cost: the run's cost minus its recorded LLM calls."""
    tokens_in = record.cost.tokens_in - sum(c.usage.tokens_in for c in record.llm)
    tokens_out = record.cost.tokens_out - sum(c.usage.tokens_out for c in record.llm)
    usd = record.cost.usd - sum(c.usage.usd for c in record.llm)
    if tokens_in <= 0 and tokens_out <= 0 and usd <= 1e-12:
        return None
    return Usage(tokens_in=max(0, tokens_in), tokens_out=max(0, tokens_out), usd=max(0.0, usd))


class RecordedLlm:
    """An `LlmBackend` that serves the recorded `LlmCall`s of a run, by attempt.

    The request must be for the recorded skill and, when the engine recorded its own request
    shape, for the same input and repair errors. An attempt the record does not hold is
    answered with the step's recorded failure if the step degraded or failed there, else it
    is a mismatch."""

    def __init__(self, record: RunRecord) -> None:
        self._calls = {c.attempt: c for c in record.llm if not _is_reuse(c) and c.attempt}
        llm_steps = [s for s in record.steps if s.kind == "llm"]
        self._step = llm_steps[0] if llm_steps else None
        self._leftover = _leftover(record)
        self.mismatches: list[str] = []
        self.requests: list[LlmRequest] = []

    def _mismatch(self, message: str) -> LlmError:
        self.mismatches.append(message)
        return LlmError(MISMATCH, message)

    def _check(self, call: LlmCall, request: LlmRequest) -> None:
        if call.skill != request.skill:
            raise self._mismatch(f"LLM attempt {request.attempt}: skill {request.skill} differs")
        shape = call.request
        if isinstance(shape, dict) and set(shape) == {
            "skill",
            "model",
            "input",
            "attempt",
            "repair",
        }:
            if _canon(shape["input"]) != _canon(request.input):
                raise self._mismatch(f"LLM attempt {request.attempt}: the input differs")
            if shape["model"] != request.model:
                raise self._mismatch(f"LLM attempt {request.attempt}: the model alias differs")

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        call = self._calls.get(request.attempt)
        if call is None:
            step = self._step
            if step is not None and step.status in ("degraded", "failed") and step.error:
                message = _unquote(step.error.message)
                raise LlmError(step.error.kind, message, usage=self._leftover)
            raise self._mismatch(f"LLM attempt {request.attempt} was not recorded")
        self._check(call, request)
        errors: tuple[str, ...] = ()
        if call.schema_valid is False:
            errors = self._repair_errors(request.attempt)
        return LlmResponse(
            output=copy.deepcopy(call.response),
            schema_errors=errors,
            usage=call.usage,
            backend=call.backend,
            model=call.model,
            skill_version=call.skill_version,
            request=copy.deepcopy(call.request),
            cached=call.cached,
            intents=copy.deepcopy(list(call.intents or [])),
        )

    def _repair_errors(self, attempt: int) -> tuple[str, ...]:
        """The schema errors of `attempt`, as the next attempt's request recorded them."""
        following = self._calls.get(attempt + 1)
        if following is not None and isinstance(following.request, dict):
            repair = following.request.get("repair")
            if isinstance(repair, list) and repair and all(isinstance(e, str) for e in repair):
                return tuple(repair)
        return ("the recorded response failed its schema",)
