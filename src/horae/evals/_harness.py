# SPDX-License-Identifier: Apache-2.0
"""Running a skill's golden cases (#36, schemas decision 48).

Each case goes through the **same path** as the `llm` step of a run, but for routing:

1. the skill's input view on the case's `input` (`Skill.view`);
2. the request, rendered by the real provider backend of the alias (`ModelBackends.render`:
   the skill's prompt, the viewed input, the output schema, `max_tokens` from the budget);
3. the response: **offline**, the recorded one is handed back by a provider backend whose
   `render` is the real one and whose `send` returns the recording (`_Replay`); **live**, the
   real `send` (one request, no repair retry);
4. `ModelBackends.send`'s own checks: the output validated against the skill's full schema
   (`horae.llm.validate.validate_output`, the killable worker), its reserved `intents` split
   off (an invalid output proposes nothing), the response redacted;
5. the intents: their omitted targets filled from the case's `targets` (the pipeline's `act`
   entries do that in a run), then `horae.intents.validate_intents` against the plugin's
   permissions narrowed to the skill's `intents`, then the action guard
   (`horae.engine.guard.check_intents`, with the viewed input as what the model was shown and
   the skill's per-type limits);
6. the comparison with `expect`, each check with a unified diff of canonical JSON.

A case's `status` is `pass`, `fail` (a check failed) or `error` (it could not be evaluated: no
recording, a backend or validator error, an input that does not fit the view).
"""

import difflib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from horae.contracts.evals import (
    EvalAssertion,
    EvalExpect,
    RecordedRequest,
    RecordedResponse,
    TargetType,
)
from horae.contracts.instance import ModelAlias
from horae.contracts.intents import IntentType
from horae.contracts.pipeline import Budget
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import Usage
from horae.engine import TARGET_FIELDS
from horae.engine.guard import check_intents
from horae.engine.ports import LlmError, LlmRequest, LlmResponse
from horae.evals._cases import LoadedCase
from horae.intents import IntentRejected, validate_intents
from horae.llm.backends import FACTORIES
from horae.llm.base import (
    BackendFactory,
    CompletionResult,
    ModelBackends,
    ProviderBackend,
    RenderedRequest,
    Validator,
    production_validator,
)
from horae.llm.validate import validate_output
from horae.secrets import SecretResolver
from horae.secrets.redact import current as current_redactor
from horae.skills import Skill

DEFAULT_BUDGET = Budget(usd=0.05, tokens_out=1024)
"""The budget of a case without one (it sizes `max_tokens` and the worst-case cost)."""
DEFAULT_TIMEOUT_S = 60.0
"""Each case's request deadline (the validation included)."""
DEFAULT_NOTE_DIR = "Evals"
"""A note intent without a target in the case gets `Evals/<case>.md`."""
OFFLINE_SECRET = "EVAL_UNUSED_SECRET"  # noqa: S105 (a name, not a value)
"""The `secret` of an offline alias of a recorded backend: never resolved (the offline
resolver knows no secret, and nothing is sent)."""
OFFLINE_BASE_URL = "https://llm.invalid"
"""The `base_url` of an offline alias that needs one (`.invalid` never resolves, RFC 6761)."""
MAX_DIFF_LINES = 400

Status = Literal["pass", "fail", "error"]
GuardResult = Literal["pass", "violation", "not_run"]


@dataclass(frozen=True)
class Check:
    """One comparison with `expect`."""

    name: str
    ok: bool
    message: str | None = None
    diff: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "message": self.message, "diff": self.diff}


@dataclass
class CaseResult:
    """What one case gave, for one backend."""

    case: str
    backend: str | None = None
    model: str | None = None
    status: Status = "pass"
    schema_valid: bool | None = None
    schema_errors: list[str] = field(default_factory=list)
    intents: list[Any] = field(default_factory=list)
    intents_error: str | None = None
    guard: GuardResult = "not_run"
    guard_detail: str | None = None
    stale: list[str] = field(default_factory=list)
    """What changed since the response was recorded: `skill`, `prompt`, `input`, `schema`."""
    checks: list[Check] = field(default_factory=list)
    usage: Usage | None = None
    worst_case: Usage | None = None
    recorded_diff: str | None = None
    """Live: the unified diff of the output against the recorded one (`None`: identical or
    nothing recorded)."""
    recorded_path: str | None = None
    """Live with `--record`: the recording written."""
    error: dict[str, str] | None = None
    output: Any = None

    def fail(self, kind: str, message: str) -> "CaseResult":
        self.status, self.error = "error", {"kind": kind, "message": message}
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "backend": self.backend,
            "model": self.model,
            "status": self.status,
            "schema_valid": self.schema_valid,
            "schema_errors": list(self.schema_errors),
            "intents_error": self.intents_error,
            "guard": self.guard,
            "guard_detail": self.guard_detail,
            "stale": list(self.stale),
            "checks": [check.to_dict() for check in self.checks],
            "usage": None if self.usage is None else self.usage.model_dump(mode="json"),
            "worst_case": (
                None if self.worst_case is None else self.worst_case.model_dump(mode="json")
            ),
            "recorded_diff": self.recorded_diff,
            "recorded_path": self.recorded_path,
            "error": self.error,
        }


@dataclass(frozen=True)
class SkillUnderEval:
    """The skill, the manifest of the plugin that ships it, and the plugin's name."""

    skill: Skill
    plugin: PluginManifest

    @property
    def source(self) -> str:
        return f"skill:{self.skill.name}"

    def proposer(self) -> PluginManifest:
        """The plugin's manifest with its `permissions.intents` narrowed to the skill's, as
        the engine checks the LLM's intents (`_proposer`)."""
        allowed = [t for t in self.plugin.permissions.intents if t in self.skill.intents]
        permissions = self.plugin.permissions.model_copy(update={"intents": allowed})
        return self.plugin.model_copy(update={"permissions": permissions})


# --- comparisons ------------------------------------------------------------------------------


def pretty(value: Any) -> str:
    """`value` as indented canonical JSON (sorted keys, ASCII)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)


def unified(expected: Any, actual: Any, a: str = "expected", b: str = "actual") -> str | None:
    """The unified diff of two JSON values' `pretty` forms, `None` when they are equal."""
    left, right = pretty(expected), pretty(actual)
    if left == right:
        return None
    lines = list(
        difflib.unified_diff(
            left.splitlines(), right.splitlines(), fromfile=a, tofile=b, lineterm=""
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], f"... ({len(lines) - MAX_DIFF_LINES} more lines)"]
    return "\n".join(lines)


_ABSENT = object()


def resolve(document: Any, pointer: str) -> Any:
    """The value at the JSON `pointer` (RFC 6901) in `document`, or `_ABSENT`."""
    if pointer == "":
        return document
    value = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                return _ABSENT
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return _ABSENT
            index = int(token)
            if index >= len(value):
                return _ABSENT
            value = value[index]
        else:
            return _ABSENT
    return value


def _same(one: Any, other: Any) -> bool:
    return pretty(one) == pretty(other)


def assertion_check(assertion: EvalAssertion, document: Any) -> Check:
    """One assertion on `{"output": ..., "intents": [...]}`."""
    value = resolve(document, assertion.pointer)
    found = value is not _ABSENT
    name = f"{assertion.check} {assertion.pointer or '/'}"
    if assertion.check == "exists":
        ok = found == assertion.exists
        return Check(name, ok, None if ok else f"found: {str(found).lower()}")
    if not found:
        return Check(name, False, "nothing there")
    if assertion.check == "equals":
        diff = unified(assertion.equals, value)
        return Check(name, diff is None, None if diff is None else "different", diff)
    if assertion.check == "length":
        if not isinstance(value, (str, list, dict)):
            return Check(name, False, f"not a string, array or object ({type(value).__name__})")
        ok = len(value) == assertion.length
        return Check(name, ok, None if ok else f"length {len(value)}, not {assertion.length}")
    wanted = assertion.contains
    if isinstance(value, str):
        ok = isinstance(wanted, str) and wanted in value
    elif isinstance(value, list):
        ok = any(_same(item, wanted) for item in value)
    else:
        return Check(name, False, f"not a string or an array ({type(value).__name__})")
    return Check(name, ok, None if ok else "not contained")


def _counts(intents: Sequence[Any]) -> dict[str, int]:
    types = Counter(
        str(intent.get("type")) if isinstance(intent, dict) else "?" for intent in intents
    )
    return dict(sorted(types.items()))


def compare(expect: EvalExpect, result: CaseResult) -> list[Check]:
    """Every check of `expect` against `result` (its output, intents, validity and guard)."""
    checks: list[Check] = []
    ok = result.schema_valid is expect.schema_valid
    message = None
    if not ok:
        errors = "; ".join(result.schema_errors[:5]) or "invalid"
        message = "the output is valid" if expect.schema_valid is False else errors
    checks.append(Check("schema_valid", ok, message))
    if expect.has_output:
        diff = unified(expect.output, result.output)
        checks.append(Check("output", diff is None, None if diff is None else "different", diff))
    document = {"output": result.output, "intents": result.intents}
    checks.extend(assertion_check(assertion, document) for assertion in expect.assertions)
    if expect.intents is not None:
        diff = unified(expect.intents, result.intents)
        checks.append(Check("intents", diff is None, None if diff is None else "different", diff))
    if expect.intent_counts is not None:
        wanted = {k: v for k, v in sorted(expect.intent_counts.items()) if v}
        diff = unified(wanted, _counts(result.intents))
        checks.append(
            Check("intent_counts", diff is None, None if diff is None else "different", diff)
        )
    if result.intents_error is not None:
        checks.append(Check("intents_valid", False, result.intents_error))
    if expect.guard == "pass":
        ok = result.guard != "violation"
        checks.append(Check("guard", ok, None if ok else result.guard_detail))
    else:
        ok = result.guard == "violation"
        checks.append(Check("guard", ok, None if ok else f"the guard result is {result.guard}"))
    return checks


# --- intents ----------------------------------------------------------------------------------


def _filled(raw: list[Any], loaded: LoadedCase) -> list[Any]:
    """`raw` with each omitted target filled from the case's `targets` (a note type without
    one: `Evals/<case>.md`), as a pipeline's `act` entries fill them in a run."""
    out = []
    for payload in raw:
        if isinstance(payload, dict):
            kind = payload.get("type")
            name = TARGET_FIELDS.get(cast(IntentType, kind)) if isinstance(kind, str) else None
            if name is not None and payload.get(name) is None:
                target = loaded.case.targets.get(cast(TargetType, kind))
                if target is None and name == "path":
                    target = f"{DEFAULT_NOTE_DIR}/{loaded.name}.md"
                if target is not None:
                    payload = {**payload, name: target}
        out.append(payload)
    return out


def judge_intents(sue: SkillUnderEval, loaded: LoadedCase, viewed: Any, result: CaseResult) -> None:
    """Validate the result's intents, then run the action guard on them (a valid output
    only: an invalid one proposes nothing)."""
    if not result.schema_valid:
        return
    try:
        typed = validate_intents(
            _filled(result.intents, loaded),
            permissions=sue.proposer().permissions,
            source=sue.source,
        )
    except IntentRejected as rejected:
        result.intents_error = "; ".join(r.describe() for r in rejected.rejections)[:1000]
        return
    detail = check_intents(typed, shown=viewed, source=sue.source, limits=sue.skill.intent_limits)
    result.guard = "pass" if detail is None else "violation"
    result.guard_detail = detail


# --- requests ---------------------------------------------------------------------------------


def request_for(
    sue: SkillUnderEval, alias: str, loaded: LoadedCase, viewed: Any, timeout_s: float
) -> LlmRequest:
    """The one attempt of a case, as the engine builds it for the `llm` step."""
    skill = sue.skill
    return LlmRequest(
        skill.name,
        alias,
        viewed,
        loaded.case.budget or DEFAULT_BUDGET,
        timeout_s,
        1,
        prompt=skill.prompt,
        output_schema=skill.output_schema,
        skill_version=skill.version,
        skill_digest=skill.digest,
    )


def viewed_input(sue: SkillUnderEval, loaded: LoadedCase, result: CaseResult) -> Any:
    """The case's input through the skill's input view; `None` (and `result` an error) when
    nothing of it fits the view's shape."""
    viewed = sue.skill.view(loaded.case.input)
    if viewed is None:
        result.fail("input_invalid", "the case input does not have the shape of the input view")
    return viewed


def recorded_request(rendered: RenderedRequest) -> RecordedRequest:
    summary = rendered.summary
    return RecordedRequest(
        system_sha256=summary["system_sha256"],
        input_sha256=summary["input_sha256"],
        schema_sha256=summary["schema_sha256"],
    )


def staleness(
    sue: SkillUnderEval, rendered: RenderedRequest, recorded: RecordedResponse
) -> list[str]:
    """What changed since `recorded` was made: the skill (its digest), and which part of the
    request (prompt, viewed input, schema)."""
    stale = []
    if recorded.skill_digest != sue.skill.digest or recorded.skill_version != sue.skill.version:
        stale.append("skill")
    now = recorded_request(rendered)
    for name, before, after in (
        ("prompt", recorded.request.system_sha256, now.system_sha256),
        ("input", recorded.request.input_sha256, now.input_sha256),
        ("schema", recorded.request.schema_sha256, now.schema_sha256),
    ):
        if before != after:
            stale.append(name)
    return stale


def absorb(response: LlmResponse, result: CaseResult) -> None:
    result.schema_valid = not response.schema_errors
    result.schema_errors = list(response.schema_errors)
    result.output = response.output
    result.intents = list(response.intents) if isinstance(response.intents, list) else []
    result.usage = response.usage
    result.model = response.model


def finish(sue: SkillUnderEval, loaded: LoadedCase, viewed: Any, result: CaseResult) -> CaseResult:
    """Judge the intents, compare with `expect` and set the status."""
    judge_intents(sue, loaded, viewed, result)
    result.checks = compare(loaded.case.expect, result)
    if result.status != "error":
        result.status = "pass" if all(check.ok for check in result.checks) else "fail"
    return result


# --- offline ----------------------------------------------------------------------------------


class _Replay:
    """A provider backend whose `render` is the real backend's and whose `send` returns a
    recorded result: nothing leaves the host."""

    def __init__(self, inner: ProviderBackend, result: CompletionResult) -> None:
        self._inner, self._result = inner, result

    @property
    def name(self) -> Any:
        return self._inner.name

    @property
    def may_write_cache(self) -> bool:
        return getattr(self._inner, "may_write_cache", False) is True

    def render(self, completion: Any) -> dict[str, Any]:
        return self._inner.render(completion)

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        return self._result

    async def aclose(self) -> None:
        await self._inner.aclose()


def offline_alias(backend: str, model: str) -> ModelAlias:
    """The alias of a recorded backend when no instance alias is chosen: the recorded model,
    and a placeholder secret and endpoint, which `render` never uses."""
    spec: dict[str, Any] = {"backend": backend, "model": model}
    if backend == "anthropic":
        spec["secret"] = OFFLINE_SECRET
    if backend in ("ollama", "openai-compatible"):
        spec["base_url"] = OFFLINE_BASE_URL
    return ModelAlias.model_validate(spec)


def _offline_backends(
    alias: str,
    spec: ModelAlias,
    recorded: RecordedResponse,
    factories: Mapping[str, BackendFactory],
    validate: Validator,
) -> ModelBackends:
    result = CompletionResult(
        output=recorded.output, usage=recorded.usage, stop_reason=recorded.stop_reason
    )

    def replay(name: str, alias_spec: ModelAlias, secrets: SecretResolver) -> ProviderBackend:
        factory = factories.get(alias_spec.backend)
        if factory is None:
            raise LlmError(
                "backend_unavailable", f"backend {alias_spec.backend!r} is not available"
            )
        return _Replay(factory(name, alias_spec, secrets), result)

    return ModelBackends(
        models={alias: spec},
        secrets=SecretResolver({}, Path("/nonexistent"), {}),
        factories={spec.backend: replay},
        validate=validate,
    )


async def run_offline(
    sue: SkillUnderEval,
    cases: Sequence[LoadedCase],
    *,
    alias: tuple[str, ModelAlias] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    factories: Mapping[str, BackendFactory] = FACTORIES,
    validate: Validator | None = None,
) -> list[CaseResult]:
    """Every case against its recorded responses: one result per recorded backend, or only
    for the backend of `alias` (`(name, spec)` from the instance's models)."""
    checker = validate or validate_output
    results: list[CaseResult] = []
    for loaded in cases:
        backends = [alias[1].backend] if alias is not None else sorted(loaded.recorded)
        if not backends:
            results.append(
                CaseResult(loaded.name).fail(
                    "no_recording", "no recorded response (record one with --live --record)"
                )
            )
            continue
        for backend in backends:
            results.append(
                await _offline_case(sue, loaded, backend, alias, timeout_s, factories, checker)
            )
    return results


async def _offline_case(
    sue: SkillUnderEval,
    loaded: LoadedCase,
    backend: str,
    alias: tuple[str, ModelAlias] | None,
    timeout_s: float,
    factories: Mapping[str, BackendFactory],
    validate: Validator,
) -> CaseResult:
    result = CaseResult(loaded.name, backend)
    recorded = loaded.recorded.get(backend)
    if recorded is None:
        return result.fail("no_recording", f"no recorded response for backend {backend}")
    viewed = viewed_input(sue, loaded, result)
    if viewed is None:
        return result
    name, spec = alias if alias is not None else ("eval", offline_alias(backend, recorded.model))
    backends = _offline_backends(name, spec, recorded, factories, validate)
    try:
        rendered = backends.render(request_for(sue, name, loaded, viewed, timeout_s))
        result.stale = staleness(sue, rendered, recorded)
        absorb(await backends.send(rendered), result)
    except LlmError as error:
        return result.fail(error.kind, error.message)
    finally:
        await backends.aclose()
    return finish(sue, loaded, viewed, result)


# --- live -------------------------------------------------------------------------------------


class _Capture:
    """The real provider backend, keeping the last raw result (for `--record`)."""

    def __init__(self, inner: ProviderBackend) -> None:
        self._inner = inner
        self.last: CompletionResult | None = None

    @property
    def name(self) -> Any:
        return self._inner.name

    @property
    def may_write_cache(self) -> bool:
        return getattr(self._inner, "may_write_cache", False) is True

    def render(self, completion: Any) -> dict[str, Any]:
        return self._inner.render(completion)

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.last = None
        self.last = await self._inner.send(payload, timeout_s)
        return self.last

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class LiveEval:
    """The live backends of one alias (`ModelBackends` over capturing provider backends)."""

    alias: str
    backends: ModelBackends
    captures: dict[str, _Capture] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        alias: str,
        models: Mapping[str, ModelAlias],
        secrets: SecretResolver,
        *,
        factories: Mapping[str, BackendFactory] = FACTORIES,
        validate: Validator | None = None,
    ) -> "LiveEval":
        live = cls(alias, ModelBackends(models={}, secrets=secrets, factories={}))

        def wrap(factory: BackendFactory) -> BackendFactory:
            def build(name: str, spec: ModelAlias, resolver: SecretResolver) -> ProviderBackend:
                capture = _Capture(factory(name, spec, resolver))
                live.captures[name] = capture
                return capture

            return build

        live.backends = ModelBackends(
            models=models,
            secrets=secrets,
            factories={name: wrap(factory) for name, factory in factories.items()},
            validate=validate or production_validator(),
        )
        return live


@dataclass
class Planned:
    """A live case, rendered (nothing sent yet)."""

    loaded: LoadedCase
    viewed: Any
    rendered: RenderedRequest
    worst_case: Usage


def plan_live(
    live: LiveEval, sue: SkillUnderEval, cases: Sequence[LoadedCase], timeout_s: float
) -> tuple[list[Planned], list[CaseResult]]:
    """Render every case (no I/O: `prepare` reads nothing but the request) and bound what it
    can cost (`ModelBackends.worst_case`); cases that cannot be rendered are errors."""
    planned: list[Planned] = []
    errors: list[CaseResult] = []
    for loaded in cases:
        result = CaseResult(loaded.name)
        viewed = viewed_input(sue, loaded, result)
        if viewed is None:
            errors.append(result)
            continue
        try:
            rendered = live.backends.render(request_for(sue, live.alias, loaded, viewed, timeout_s))
        except LlmError as error:
            errors.append(result.fail(error.kind, error.message))
            continue
        planned.append(Planned(loaded, viewed, rendered, live.backends.worst_case(rendered)))
    return planned, errors


def total(usages: Sequence[Usage]) -> Usage:
    return Usage(
        tokens_in=sum(u.tokens_in for u in usages),
        tokens_out=sum(u.tokens_out for u in usages),
        usd=sum((u.usd for u in usages), 0.0),
        priced=all(u.priced is not False for u in usages),
    )


def recording(sue: SkillUnderEval, planned: Planned, raw: CompletionResult) -> RecordedResponse:
    """The redacted recording of a live answer (schemas decision 43: every secret this
    process resolved becomes `[REDACTED:<NAME>]`)."""
    redactor = current_redactor()
    output, _ = redactor.scrub_data(raw.output)
    stop = raw.stop_reason
    if stop is not None:
        stop = redactor.redact_text(stop)[0]
    rendered = planned.rendered
    data = {
        "schema_version": 1,
        "backend": rendered.backend,
        "model": rendered.model,
        "skill": sue.skill.name,
        "skill_version": sue.skill.version,
        "skill_digest": sue.skill.digest,
        "request": recorded_request(rendered).model_dump(),
        "output": output,
        "usage": raw.usage.model_dump(),
        "stop_reason": stop,
    }
    try:
        return RecordedResponse.model_validate(data)
    except ValueError:
        return RecordedResponse.model_validate({**data, "stop_reason": None})


async def send_live(
    live: LiveEval,
    sue: SkillUnderEval,
    planned: Sequence[Planned],
    *,
    on_record: Callable[[str, RecordedResponse], str | None] | None = None,
) -> list[CaseResult]:
    """Send every planned case (one request each, in order) and judge the answers; with
    `on_record`, each answer that came back is handed over (redacted) to be written, and
    returns where."""
    results = []
    for item in planned:
        loaded = item.loaded
        result = CaseResult(loaded.name, item.rendered.backend, item.rendered.model)
        result.worst_case = item.worst_case
        try:
            response = await live.backends.send(item.rendered)
        except LlmError as error:
            result.usage = error.usage if isinstance(error.usage, Usage) else None
            results.append(result.fail(error.kind, error.message))
            continue
        absorb(response, result)
        capture = live.captures.get(live.alias)
        raw = None if capture is None else capture.last
        recorded = loaded.recorded.get(item.rendered.backend)
        if recorded is not None:
            result.stale = staleness(sue, item.rendered, recorded)
            if raw is not None:
                before, _ = current_redactor().scrub_data(recorded.output)
                after, _ = current_redactor().scrub_data(raw.output)
                result.recorded_diff = unified(before, after, "recorded", "live")
        if on_record is not None and raw is not None:
            result.recorded_path = on_record(loaded.name, recording(sue, item, raw))
        results.append(finish(sue, loaded, item.viewed, result))
    return results


def summary(results: Sequence[CaseResult]) -> dict[str, Any]:
    statuses = Counter(result.status for result in results)
    used = [result.usage for result in results if result.usage is not None]
    spent = total(used) if used else None
    return {
        "cases": len(results),
        "passed": statuses["pass"],
        "failed": statuses["fail"],
        "errors": statuses["error"],
        "stale": sum(1 for result in results if result.stale),
        "usage": None if spent is None else spent.model_dump(mode="json"),
    }


__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_TIMEOUT_S",
    "OFFLINE_SECRET",
    "CaseResult",
    "Check",
    "LiveEval",
    "Planned",
    "SkillUnderEval",
    "compare",
    "offline_alias",
    "plan_live",
    "pretty",
    "resolve",
    "run_offline",
    "send_live",
    "summary",
    "total",
    "unified",
]
