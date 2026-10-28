# SPDX-License-Identifier: Apache-2.0
"""Recorded ports: a `ToolCaller` and an `LlmBackend` that answer only from a `RunRecord`.

Neither holds a host, a process, a socket or a backend: their only state is the record (and
the pipeline it is replayed against). A call the record cannot answer is a **mismatch**: it is
noted (`mismatches`) and fails the call with kind `replay_mismatch`; it is never forwarded
anywhere, because there is nowhere to forward it to.
"""

import copy
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from horae.contracts.pipeline import Pipeline
from horae.contracts.run import LlmCall, RunContext, RunRecord, Usage
from horae.engine.canonical import canonical_json, sha256_hex
from horae.engine.executor import PlannedCall, planned_calls
from horae.engine.ports import (
    LlmError,
    LlmRequest,
    LlmResponse,
    PreviousLlm,
    ToolCallError,
    ToolResult,
)
from horae.engine.template import TemplateError

MISMATCH = "replay_mismatch"
"""Error kind of a call the record cannot answer."""


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


def missing_per_call(record: RunRecord) -> str | None:
    """Why `record` cannot be replayed call by call, or `None`.

    Every `collect` step that ran must give each source's position (`InputSnapshot.call`, and
    `call` in its failed sources), and every `transform` step that ran its calls
    (`StepRecord.calls`): records written before these fields existed lack them."""
    for index, step in enumerate(record.steps):
        if step.started_at is None or step.status == "skipped":
            continue  # never ran, or failed while planning: no call to answer
        if step.kind == "collect":
            snapshots = [s for s in record.inputs if s.step == index]
            failures = step.output if isinstance(step.output, list) else []
            if any(s.call is None for s in snapshots) or any(
                not isinstance(f, dict) or type(f.get("call")) is not int for f in failures
            ):
                return f"step {index} (collect) does not record the position of its sources"
        elif step.kind == "transform" and step.calls is None:
            return f"step {index} (transform) does not record its calls"
    return None


@dataclass(frozen=True)
class _Answer:
    ref: str
    params: Any
    data: Any = None
    intents: Any = None
    """The raw intents, as recorded."""
    error: tuple[str, str] | None = None
    """`(kind, message)` of a recorded failure."""
    input_hash: str | None = None
    """The recorded input's SHA-256, checked when set."""


def _answers(record: RunRecord) -> dict[tuple[int, int], _Answer]:
    """What the record says each `(step, call)` returned."""
    answers: dict[tuple[int, int], _Answer] = {}
    for snap in record.inputs:
        if snap.call is not None:
            answers[(snap.step, snap.call)] = _Answer(
                snap.source, snap.params, data=snap.data, intents=snap.intents
            )
    for index, step in enumerate(record.steps):
        error = step.error
        if step.kind == "collect" and isinstance(step.output, list):
            failures = [f for f in step.output if isinstance(f, dict)]
            # `source_failed` joins one line per failed source, in order. Any other error
            # (rejected intents) replaced those lines: the messages are then not recorded.
            lines = error.message.split("\n") if error and error.kind == "source_failed" else []
            if len(lines) != len(failures):
                lines = [""] * len(failures)
            for failure, line in zip(failures, lines, strict=True):
                position = failure.get("call")
                if type(position) is not int:
                    continue
                ref, kind = str(failure.get("source")), str(failure.get("error_kind"))
                message = _tail(line, ref, kind) or "recorded failure"
                answers[(index, position)] = _Answer(
                    ref, failure.get("params"), error=(kind, message)
                )
        elif step.kind == "transform" and step.calls:
            for number, call in enumerate(step.calls):
                # Only the first call's input is the record's own data: later calls are given
                # the previous call's output, which is not recorded (only its hash is).
                checked = call.input_hash if number == 0 else None
                if call.error_kind is not None:
                    tail = _tail(error.message, call.ref, call.error_kind) if error else None
                    answers[(index, call.call)] = _Answer(
                        call.ref,
                        call.params,
                        error=(call.error_kind, tail or "recorded failure"),
                        input_hash=checked,
                    )
                    continue
                last = number == len(step.calls) - 1 and step.status == "ok"
                answers[(index, call.call)] = _Answer(
                    call.ref,
                    call.params,
                    data=step.output if last else None,
                    intents=call.intents,
                    input_hash=checked,
                )
    return answers


def _tool_calls(pipeline: Pipeline, context: RunContext) -> list[PlannedCall]:
    """The `collect` / `transform` calls the engine will make, in order (`planned_calls`);
    none when planning fails on a template (the engine then calls nothing)."""
    try:
        planned = planned_calls(pipeline, context)
    except TemplateError:
        return []
    if any(call.params is None for call in planned):
        return []
    return [call for call in planned if call.kind in ("collect", "transform")]


class RecordedToolCaller:
    """A `ToolCaller` that serves `collect` / `transform` results from a run record.

    Each call is matched, in order, against the pipeline's planned calls
    (`horae.engine.planned_calls`), and answered by its `(step, call)` position: a source's
    `InputSnapshot` (data and raw intents) or failed-source entry, or a transform's
    `CallRecord` (raw intents, failure; the step's final output for its last call). The tool
    `ref` and the canonical JSON of the rendered `params` must equal the recorded ones, and the
    first transform call of a step must be given the input the record hashed. Anything else is
    a mismatch."""

    def __init__(self, record: RunRecord, pipeline: Pipeline, context: RunContext) -> None:
        self._expected: Iterator[PlannedCall] = iter(_tool_calls(pipeline, context))
        self._answers = _answers(record)
        self.mismatches: list[str] = []

    def _mismatch(self, message: str) -> ToolCallError:
        self.mismatches.append(message)
        return ToolCallError(MISMATCH, message)

    async def call(self, ref: str, params: dict[str, Any], input: Any) -> ToolResult:
        expected = next(self._expected, None)
        if expected is None or expected.ref != ref or _canon(expected.params) != _canon(params):
            raise self._mismatch(f"unexpected call to {ref}: the pipeline does not plan it here")
        where = f"step {expected.step} call {expected.call} ({ref})"
        answer = self._answers.get((expected.step, expected.call))
        if answer is None or answer.ref != ref or _canon(answer.params) != _canon(params):
            raise self._mismatch(f"{where}: no recorded result for this tool and these parameters")
        if answer.input_hash is not None and sha256_hex(input) != answer.input_hash:
            raise self._mismatch(f"{where}: the input differs from the recorded one")
        if answer.error is not None:
            raise ToolCallError(*answer.error)
        return ToolResult(data=copy.deepcopy(answer.data), intents=copy.deepcopy(answer.intents))


def _is_reuse(call: LlmCall) -> bool:
    """The engine's record of a reused output (it builds this request itself)."""
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

    Each request is checked against the engine-owned fields of the recorded attempt, whatever
    the backend recorded as `request`: the skill, the model alias (`model_alias`), the SHA-256
    of the input (`input_hash`) and the repair errors (`repair`) must all match. The response
    is served with its recorded schema errors (`schema_errors`). An attempt the record does not
    hold is answered with the step's recorded failure if the step degraded or failed there,
    else it is a mismatch."""

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
        attempt = f"LLM attempt {request.attempt}"
        if call.skill != request.skill:
            raise self._mismatch(f"{attempt}: skill {request.skill} differs")
        if call.model_alias is None or call.input_hash is None or call.repair is None:
            raise self._mismatch(f"{attempt}: the record has no model alias, input hash or repair")
        if call.model_alias != request.model:
            raise self._mismatch(f"{attempt}: the model alias differs")
        if call.input_hash != sha256_hex(request.input):
            raise self._mismatch(f"{attempt}: the input differs")
        if list(request.repair) != call.repair:
            raise self._mismatch(f"{attempt}: the repair errors differ")

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
            if not call.schema_errors:
                message = f"LLM attempt {request.attempt}: the record has no schema errors"
                raise self._mismatch(message)
            errors = tuple(call.schema_errors)
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
