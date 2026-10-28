# SPDX-License-Identifier: Apache-2.0
"""Execute a validated `Pipeline` and produce its `RunRecord` (PLAN.md §2.1, §3.1, §3.2).

**Control flow is the definition.** Before the first call the engine builds a fixed plan from
the pipeline: every step, every call, every plugin resolved and every parameter rendered
(`horae.engine.template`). It then walks that plan in order. Nothing a tool or the LLM
returns is ever read as an instruction: a result that looks like steps, a "next step" or a
template is plain data. The only deviation from "every step, in order" is the fixed policy
below, which can stop the run early (the remaining steps are recorded as `skipped`); it can
never add, repeat or reorder a step. Calls run one at a time, in declaration order.

**Planning** fails the run before any plugin is called, with the offending step `failed`
and every other step `skipped`, when a tool or skill resolves to no plugin (`plugin_unknown`),
the plugin does not list it under the `provides` entry of the step kind (`tool_not_provided`:
`collect` uses `sources`, `transform` `transforms`, `act` `sinks`, `notify` `notifiers`, `llm`
`skills`), a parameter template is invalid (`template_error`), or the pipeline has an `llm`
step and no backend was given (`llm_unavailable`).

**Data between steps.** One JSON value flows through the run:

- `collect` calls each source with `input=None`; its output is the list
  `[{"source": <ref>, "params": {...}, "data": <output>}, ...]` of the sources that succeeded,
  in declaration order, each also recorded as an `InputSnapshot` (a later `collect` step starts
  a new list);
- each `transform` receives the current value and its output replaces it (a fold);
- `llm` receives the current value and its schema-valid output replaces it;
- `act` and `notify` receive the current value and do not change it; they return intents.

Every value is copied through JSON at each boundary, so a tool cannot change data it handed
over or was handed, and a result that is not plain JSON fails the call (`output_invalid`).

**Intents.** Only `act` and `notify` tools and the `llm` step may propose intents. Each call's
proposals are checked with `horae.intents.validate_intents` against the proposing plugin's
`permissions.intents` (`source` is the plugin name, or `skill:<skill>` for the LLM step, whose
permissions are those of the plugin shipping the skill). A step's intents are kept only if the
whole step is accepted, and the run-wide total is capped at `MAX_RUN_INTENTS`
(`run_intents_exceeded`). A rejected `http.post` host is also recorded as an `egress_blocked`
violation. Nothing is applied here: that is #21/#22, which must not apply a `failed` run.

**Step policy** (step status, then the effect on the run; the run status is the worst of
`failed` > `degraded` > `noop` > `ok`):

| Step | Failure | Step | Run | Later steps |
|---|---|---|---|---|
| any | intents rejected or over the run cap | failed | failed | skipped |
| collect, transform | a tool proposes intents (`intent_not_allowed`) | failed | failed | skipped |
| collect | some sources fail, at least one succeeds | degraded | degraded | run on partial data |
| collect | every source fails | failed | failed | skipped |
| transform | a transform fails | failed | failed | skipped |
| llm | backend error, timeout, over budget, schema invalid twice | degraded | degraded | skipped |
| llm | input hash unchanged (`on_unchanged_input: skip`) | skipped | noop | skipped (no LLM call) |
| act | a sink call fails | failed | failed | skipped |
| notify | a notifier fails | degraded | degraded | run (its intents dropped) |

Rationale: a write is never made from partial or unvalidated data (`transform`, `llm`, `act`
stop the run), reading is best effort (`collect` degrades, PLAN.md §2.2 "degraded"), and a
notification is best effort once the writes are decided. Rejected intents are a permission or
contract breach and always fail the run (ADR 0002).
"""

import asyncio
import copy
import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from horae.contracts.intents import Intent
from horae.contracts.pipeline import (
    ActStep,
    Budget,
    CollectStep,
    LlmSpec,
    LlmStep,
    NotifyStep,
    Pipeline,
    PluginCall,
    Step,
    TransformStep,
)
from horae.contracts.run import (
    MAX_RUN_INTENTS,
    InputSnapshot,
    LlmCall,
    PluginVersion,
    ProposedIntent,
    RunContext,
    RunRecord,
    RunStatus,
    StepError,
    StepKind,
    StepRecord,
    StepStatus,
    Usage,
    Violation,
)
from horae.engine.canonical import pipeline_digest, sha256_hex
from horae.engine.ports import (
    EngineCallError,
    LlmBackend,
    LlmError,
    LlmRequest,
    PluginRegistry,
    ResolvedPlugin,
    ToolCaller,
    ToolCallError,
    ToolResult,
    UnknownPlugin,
)
from horae.engine.template import TemplateError, render, variables
from horae.intents import IntentRejected, safe_excerpt, validate_intents

DEFAULT_LLM_TIMEOUT_S = 120.0
"""Timeout of one LLM attempt when the pipeline sets no `timeout_s`."""

MAX_LLM_ATTEMPTS = 2
"""The first call and one repair retry (PLAN.md §3.4)."""

_MAX_MESSAGE = 4096
_MAX_STDERR = 65536
_PROVIDES = {"collect": "sources", "transform": "transforms", "act": "sinks", "notify": "notifiers"}
_EFFECT: dict[StepStatus, RunStatus] = {
    "ok": "ok",
    "skipped": "ok",
    "degraded": "degraded",
    "failed": "failed",
}
_RANK: dict[RunStatus, int] = {"ok": 0, "noop": 1, "degraded": 2, "failed": 3}


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _cap(text: str, limit: int = _MAX_MESSAGE) -> str:
    text = text or "-"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_copy(value: Any) -> Any:
    """A deep copy of `value` through JSON; `ValueError` if it is not plain JSON data."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("not plain JSON data") from error


def _no_intents(raw: Any) -> bool:
    return raw is None or (isinstance(raw, list | tuple) and len(raw) == 0)


# --- plan ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    ref: str
    params: dict[str, Any]
    plugin: ResolvedPlugin


@dataclass(frozen=True)
class _Planned:
    index: int
    kind: StepKind
    calls: tuple[_Call, ...] = ()
    llm: LlmSpec | None = None
    skill_plugin: ResolvedPlugin | None = None


class _PlanError(Exception):
    def __init__(self, index: int, kind: StepKind, error: StepError) -> None:
        super().__init__(error.message)
        self.index = index
        self.kind = kind
        self.error = error


def _kind_of(step: Step) -> StepKind:
    if isinstance(step, CollectStep):
        return "collect"
    if isinstance(step, LlmStep):
        return "llm"
    if isinstance(step, ActStep):
        return "act"
    if isinstance(step, NotifyStep):
        return "notify"
    return "transform"


def _calls_of(step: Step) -> list[PluginCall]:
    if isinstance(step, CollectStep):
        return step.collect
    if isinstance(step, ActStep):
        return step.act
    if isinstance(step, NotifyStep):
        return step.notify
    if isinstance(step, TransformStep):
        return step.transform
    return []  # pragma: no cover (the llm step calls no plugin tool)


def _plan(
    pipeline: Pipeline,
    context: RunContext,
    plugins: PluginRegistry,
    has_llm: bool,
) -> tuple[_Planned, ...]:
    names = variables(context)
    planned: list[_Planned] = []
    for index, step in enumerate(pipeline.steps):
        kind = _kind_of(step)

        def fail(
            error_kind: str, message: str, index: int = index, kind: StepKind = kind
        ) -> NoReturn:
            raise _PlanError(index, kind, StepError(kind=error_kind, message=_cap(message)))

        if isinstance(step, LlmStep):
            if not has_llm:
                fail("llm_unavailable", "the pipeline has an `llm` step and no LLM backend")
            try:
                skill_plugin = plugins.plugin_for_skill(step.llm.skill)
            except UnknownPlugin:
                fail("plugin_unknown", f"no installed plugin ships skill {step.llm.skill}")
            if step.llm.skill not in skill_plugin.manifest.provides.skills:
                fail("tool_not_provided", f"{step.llm.skill} is not in the plugin's skills")
            planned.append(_Planned(index, kind, llm=step.llm, skill_plugin=skill_plugin))
            continue
        calls: list[_Call] = []
        for call in _calls_of(step):
            ref, params = (call, {}) if isinstance(call, str) else next(iter(call.items()))
            try:
                plugin = plugins.plugin_for_tool(ref)
            except UnknownPlugin:
                fail("plugin_unknown", f"no installed plugin provides {ref}")
            if ref not in getattr(plugin.manifest.provides, _PROVIDES[kind]):
                fail("tool_not_provided", f"{ref} is not in the plugin's {_PROVIDES[kind]}")
            try:
                rendered = render(copy.deepcopy(params), names)
            except TemplateError as error:
                fail("template_error", f"{ref}: {error}")
            calls.append(_Call(ref, rendered, plugin))
        planned.append(_Planned(index, kind, calls=tuple(calls)))
    return tuple(planned)


# --- execution -------------------------------------------------------------------------------


@dataclass
class _Outcome:
    status: StepStatus
    error: StepError | None = None
    output: Any = None
    intents: list[ProposedIntent] = field(default_factory=list)
    stop: bool = False
    noop: bool = False


@dataclass
class _Run:
    tools: ToolCaller
    llm: LlmBackend | None
    previous_input_hash: str | None
    data: Any = None
    inputs: list[InputSnapshot] = field(default_factory=list)
    llm_calls: list[LlmCall] = field(default_factory=list)
    intents: list[ProposedIntent] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    input_hash: str | None = None
    cost: Usage = field(default_factory=lambda: Usage(tokens_in=0, tokens_out=0, usd=0.0))

    async def call(self, call: _Call, data: Any) -> ToolResult | EngineCallError:
        """One tool call; errors are returned, never raised."""
        try:
            result = await self.tools.call(call.ref, copy.deepcopy(call.params), _json_copy(data))
        except ToolCallError as error:
            self._stderr(call.ref, error.stderr)
            return error
        except Exception as error:  # a host bug must not crash the run
            return ToolCallError("host_error", f"unexpected {type(error).__name__}")
        self._stderr(call.ref, result.stderr)
        try:
            return ToolResult(_json_copy(result.data), result.intents, result.stderr)
        except ValueError:
            return ToolCallError("output_invalid", "the tool output is not plain JSON data")

    def _stderr(self, ref: str, stderr: str | None) -> None:
        if stderr:
            self.stderr.append(f"--- {ref} ---\n{stderr}")

    def accept(
        self, raw: Any, plugin: ResolvedPlugin, source: str, index: int, by: str, pending: int
    ) -> tuple[list[ProposedIntent], StepError | None]:
        """Validate one call's proposals; `pending` intents of this step are already kept."""
        try:
            typed: tuple[Intent, ...] = validate_intents(
                raw, permissions=plugin.manifest.permissions, source=source
            )
        except IntentRejected as rejected:
            for rejection in rejected.rejections:
                if rejection.code == "host_undeclared":
                    detail = _cap(f"{source}: {rejection.describe()}")
                    self.violations.append(Violation(kind="egress_blocked", detail=detail))
            return [], rejected.step_error()
        if len(self.intents) + pending + len(typed) > MAX_RUN_INTENTS:
            message = f"more than {MAX_RUN_INTENTS} intents in the run"
            return [], StepError(kind="run_intents_exceeded", message=message)
        return [ProposedIntent(step=index, proposed_by=by, intent=i) for i in typed], None


def _describe(ref: str, error: EngineCallError) -> str:
    return f"{ref}: {error.kind}: {safe_excerpt(error.message, 200)}"


async def _collect(run: _Run, step: _Planned) -> _Outcome:
    collected: list[dict[str, Any]] = []
    failures: list[str] = []
    for call in step.calls:
        result = await run.call(call, None)
        if isinstance(result, EngineCallError):
            failures.append(_describe(call.ref, result))
            continue
        if not _no_intents(result.intents):
            message = f"{call.ref}: a collect tool may not propose intents"
            return _Outcome("failed", StepError(kind="intent_not_allowed", message=message))
        collected.append({"source": call.ref, "params": call.params, "data": result.data})
        run.inputs.append(
            InputSnapshot(step=step.index, source=call.ref, params=call.params, data=result.data)
        )
    run.data = collected
    if not failures:
        return _Outcome("ok")
    error = StepError(kind="source_failed", message=_cap("\n".join(failures)))
    if not collected:
        return _Outcome("failed", error)
    return _Outcome("degraded", error)


async def _transform(run: _Run, step: _Planned) -> _Outcome:
    for call in step.calls:
        result = await run.call(call, run.data)
        if isinstance(result, EngineCallError):
            return _Outcome(
                "failed", StepError(kind=result.kind, message=_describe(call.ref, result))
            )
        if not _no_intents(result.intents):
            message = f"{call.ref}: a transform may not propose intents"
            return _Outcome("failed", StepError(kind="intent_not_allowed", message=message))
        run.data = result.data
    return _Outcome("ok", output=run.data)


async def _sinks(run: _Run, step: _Planned) -> _Outcome:
    """`act` (any failure fails the step) and `notify` (a failed notifier degrades it)."""
    kept: list[ProposedIntent] = []
    outputs: list[dict[str, Any]] = []
    failures: list[str] = []
    for call in step.calls:
        result = await run.call(call, run.data)
        if isinstance(result, EngineCallError):
            if step.kind == "act":
                failed = StepError(kind=result.kind, message=_describe(call.ref, result))
                return _Outcome("failed", failed)
            failures.append(_describe(call.ref, result))
            continue
        name = call.plugin.manifest.name
        accepted, rejected = run.accept(
            result.intents, call.plugin, name, step.index, call.ref, len(kept)
        )
        if rejected is not None:
            return _Outcome("failed", rejected)
        kept.extend(accepted)
        outputs.append({"call": call.ref, "data": result.data})
    if failures:
        error = StepError(kind="notifier_failed", message=_cap("\n".join(failures)))
        return _Outcome("degraded", error, outputs, kept)
    return _Outcome("ok", output=outputs, intents=kept)


async def _llm_step(run: _Run, step: _Planned) -> _Outcome:
    spec, plugin, backend = step.llm, step.skill_plugin, run.llm
    if spec is None or plugin is None or backend is None:  # pragma: no cover (planned)
        raise RuntimeError("an llm step is planned with its spec, plugin and backend")
    run.input_hash = sha256_hex({"skill": spec.skill, "model": spec.model, "input": run.data})
    if run.previous_input_hash is not None and run.previous_input_hash == run.input_hash:
        return _Outcome("skipped", stop=True, noop=True)

    def degraded(kind: str, message: str) -> _Outcome:
        return _Outcome("degraded", StepError(kind=kind, message=_cap(message)), stop=True)

    timeout = spec.timeout_s or DEFAULT_LLM_TIMEOUT_S
    spent_usd, spent_tokens = 0.0, 0
    repair: tuple[str, ...] = ()
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        if spent_usd >= spec.budget.usd or spent_tokens >= spec.budget.tokens_out:
            return degraded("budget_exceeded", "no budget left for the repair retry")
        budget = Budget(
            usd=spec.budget.usd - spent_usd, tokens_out=spec.budget.tokens_out - spent_tokens
        )
        request = LlmRequest(
            spec.skill, spec.model, _json_copy(run.data), budget, timeout, attempt, repair
        )
        try:
            async with asyncio.timeout(timeout):
                response = await backend.complete(request)
        except TimeoutError:
            return degraded("timeout", f"the LLM did not answer within {timeout} s")
        except LlmError as error:
            return degraded(error.kind, safe_excerpt(error.message, 200))
        except Exception as error:  # a backend bug must not crash the run
            return degraded("host_error", f"unexpected {type(error).__name__}")
        try:
            output = _json_copy(response.output)
            recorded = (
                _json_copy(response.request)
                if response.request is not None
                else {
                    "skill": spec.skill,
                    "model": spec.model,
                    "input": request.input,
                    "attempt": attempt,
                    "repair": list(repair),
                }
            )
        except ValueError:
            return degraded("output_invalid", "the LLM output is not plain JSON data")
        usage = response.usage
        run.cost = Usage(
            tokens_in=run.cost.tokens_in + usage.tokens_in,
            tokens_out=run.cost.tokens_out + usage.tokens_out,
            usd=run.cost.usd + usage.usd,
        )
        spent_usd += usage.usd
        spent_tokens += usage.tokens_out
        run.llm_calls.append(
            LlmCall(
                step=step.index,
                skill=spec.skill,
                skill_version=response.skill_version,
                backend=response.backend,
                model=response.model,
                cached=response.cached,
                attempt=attempt,
                schema_valid=not response.schema_errors,
                request=recorded,
                response=output,
                usage=usage,
            )
        )
        if spent_usd > spec.budget.usd or spent_tokens > spec.budget.tokens_out:
            return degraded("budget_exceeded", "the LLM step spent more than its budget")
        if not response.schema_errors:
            source = f"skill:{spec.skill}"
            accepted, rejected = run.accept(
                response.intents, plugin, source, step.index, spec.skill, 0
            )
            if rejected is not None:
                return _Outcome("failed", rejected, stop=True)
            run.data = output
            return _Outcome("ok", output=output, intents=accepted)
        repair = tuple(response.schema_errors)
    return degraded(
        "schema_invalid", f"the output failed its schema after {MAX_LLM_ATTEMPTS} attempts"
    )


_STEPS = {
    "collect": _collect,
    "transform": _transform,
    "llm": _llm_step,
    "act": _sinks,
    "notify": _sinks,
}


async def execute(
    pipeline: Pipeline,
    *,
    run_id: str,
    context: RunContext,
    plugins: PluginRegistry,
    tools: ToolCaller,
    llm: LlmBackend | None = None,
    dry_run: bool = False,
    previous_input_hash: str | None = None,
    clock: Callable[[], datetime.datetime] = _utc_now,
) -> RunRecord:
    """Run `pipeline` once and return its record; never raises for a step failure.

    `context` is the template context (`horae.engine.make_context`). `previous_input_hash` is
    the last run's `input_hash` (from the store, #19), for the unchanged-input skip. `dry_run`
    is recorded only: the engine applies nothing either way. Persistence, locking and the run
    timeout are #19's."""
    started_at = clock()
    digest = pipeline_digest(pipeline)
    run = _Run(tools=tools, llm=llm, previous_input_hash=previous_input_hash)
    records: list[StepRecord] = []
    status: RunStatus = "ok"
    run_error: str | None = None
    try:
        plan = _plan(pipeline, context, plugins, llm is not None)
    except _PlanError as error:
        plan = ()
        status, run_error = "failed", f"step {error.index}: {error.error.message}"
        for index, declared in enumerate(pipeline.steps):
            if index == error.index:
                records.append(StepRecord(kind=error.kind, status="failed", error=error.error))
            else:
                records.append(StepRecord(kind=_kind_of(declared), status="skipped"))
    used = {s.plugin.manifest.name: s.plugin for p in plan for s in p.calls}
    used.update({p.skill_plugin.manifest.name: p.skill_plugin for p in plan if p.skill_plugin})

    stopped = False
    for step in plan:
        if stopped:
            records.append(StepRecord(kind=step.kind, status="skipped"))
            continue
        step_start = clock()
        run.stderr = []
        outcome = await _STEPS[step.kind](run, step)
        step_end = clock()
        stderr = _cap("\n".join(run.stderr), _MAX_STDERR) if run.stderr else None
        records.append(
            StepRecord(
                kind=step.kind,
                status=outcome.status,
                output=outcome.output,
                error=outcome.error,
                started_at=step_start,
                finished_at=step_end,
                stderr=stderr,
                duration_ms=max(0, int((step_end - step_start).total_seconds() * 1000)),
            )
        )
        run.intents.extend(outcome.intents)
        step_status = "noop" if outcome.noop else _EFFECT[outcome.status]
        if _RANK[step_status] > _RANK[status]:
            status = step_status
        if outcome.error is not None and run_error is None and outcome.status != "ok":
            run_error = f"step {step.index} ({step.kind}): {outcome.error.message}"
        stopped = outcome.stop or outcome.status == "failed"

    return RunRecord(
        schema_version=1,
        id=run_id,
        pipeline=pipeline.name,
        pipeline_digest=digest,
        context=context,
        input_hash=run.input_hash,
        dry_run=dry_run,
        started_at=started_at,
        finished_at=max(clock(), started_at),
        status=status,
        plugins={
            name: PluginVersion(version=p.manifest.version, commit=p.commit)
            for name, p in used.items()
        },
        inputs=run.inputs,
        steps=records,
        llm=run.llm_calls,
        intents=run.intents,
        changes=[],
        violations=run.violations,
        cost=run.cost,
        error=_cap(run_error) if run_error else None,
    )


__all__ = ["DEFAULT_LLM_TIMEOUT_S", "MAX_LLM_ATTEMPTS", "execute"]
