# SPDX-License-Identifier: Apache-2.0
"""Replay from recorded inputs (#23): identical intents, diffs, and no external call."""

import asyncio
import datetime
import itertools
import json
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.contracts import Pipeline, RunRecord
from horae.contracts.lockfile import Lockfile
from horae.contracts.pipeline import Budget
from horae.contracts.run import MAX_PIPELINE_BYTES, Violation
from horae.engine import (
    LlmError,
    LlmRequest,
    LlmResponse,
    PreviousLlm,
    ToolCallError,
    ToolResult,
    canonical_pipeline,
    execute,
    make_context,
    planned_calls,
)
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.replay import (
    MISMATCH,
    LockRegistry,
    RecordedLlm,
    RecordedToolCaller,
    ReplayResult,
    definition_for,
    intents_json,
    recorded_previous,
    replay,
    status_before_undo,
)

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "schemas" / "examples" / "pipeline" / "valid"
START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
CONTEXT = make_context(START, "Europe/Paris")
BRIEF = {"items": ["Review PR 42", "Dentist at 10:00"]}
NOTE = {"type": "note.replace_owned_block", "block": "horae-brief", "content": "- [ ] PR 42\n"}
PUSH = {"type": "notify.push", "body": "2 items today"}
SKILL = "daily-brief/rank-and-word"
MODELS = {"default": "fixture-model"}


def _manifest(name: str, intents: tuple[str, ...] = (), **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0.1,<0.2",
        "provides": provides,
        "permissions": {"intents": list(intents)},
    }


def _plugins() -> FixturePlugins:
    """Synthetic plugins for the PLAN.md §3.2 daily brief (as in `test_engine`)."""
    plugins = FixturePlugins()
    plugins.add(
        _manifest("github", ("task.create",), sources=["github.review_requests"]),
        {
            "github.review_requests": lambda p, i: ToolResult(
                data=[{"title": "Fix the parser", "number": 42}],
                intents=[{"type": "task.create", "title": "Review 42", "fingerprint": "gh:42"}],
            )
        },
    )
    plugins.add(
        _manifest("calendar", sources=["calendar.events"], transforms=["calendar.prep_tasks"]),
        {
            "calendar.events": lambda p, i: [{"title": "Dentist", "days": p["days"]}],
            "calendar.prep_tasks": lambda p, i: {**i, "prep": True},
        },
    )
    plugins.add(
        _manifest("mcp-brief", sources=["coach.brief"]),
        {"coach.brief": lambda p, i: {e: "fine" for e in p["endpoints"]}},
    )
    plugins.add(
        _manifest(
            "obsidian",
            ("note.replace_owned_block", "task.create", "task.update_status"),
            sources=["obsidian.open_tasks"],
            sinks=["obsidian.write_note"],
        ),
        {"obsidian.open_tasks": lambda p, i: []},
    )
    plugins.add(
        _manifest("tasks", transforms=["tasks.normalize", "tasks.dedupe_fingerprint"]),
        {
            "tasks.normalize": lambda p, i: {"sources": [s["source"] for s in i]},
            "tasks.dedupe_fingerprint": lambda p, i: {**i, "deduped": True},
        },
    )
    plugins.add(
        _manifest("pack-daily-brief", ("note.replace_owned_block", "notify.push"), skills=[SKILL]),
        commit="a" * 40,
    )
    plugins.add(_manifest("ntfy", ("notify.push",), notifiers=["ntfy.digest"]))
    return plugins


def _brief() -> Pipeline:
    data = yaml.safe_load((EXAMPLES / "daily-brief.yaml").read_text())
    # A task sink, so the github source's proposal routes.
    data["steps"][3]["act"].append({"obsidian.tasks": {"mode": "tasks"}})
    pipeline = Pipeline.model_validate(data)
    return pipeline


def _brief_plugins() -> FixturePlugins:
    plugins = _plugins()
    obsidian = plugins.plugins["obsidian"].manifest.model_dump(mode="json")
    obsidian["provides"]["sinks"].append("obsidian.tasks")
    plugins.add(obsidian, {"obsidian.open_tasks": lambda p, i: []})
    return plugins


def _clock() -> Callable[[], datetime.datetime]:
    ticks = itertools.count()
    return lambda: START + datetime.timedelta(milliseconds=250 * next(ticks))


def _record(
    pipeline: Pipeline,
    plugins: FixturePlugins,
    llm: Any = None,
    **kwargs: Any,
) -> RunRecord:
    kwargs.setdefault("context", CONTEXT)
    kwargs.setdefault("models", MODELS)
    return asyncio.run(
        execute(
            pipeline,
            run_id="20261027T053000Z-daily-brief-01",
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            clock=_clock(),
            **kwargs,
        )
    )


class _Sentinel(FixturePlugins):
    """A registry whose tools fail the test if anything calls them."""

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        raise AssertionError(f"replay called the live tool {ref}")


def _sentinel(plugins: FixturePlugins) -> _Sentinel:
    sentinel = _Sentinel()
    sentinel.plugins = dict(plugins.plugins)
    return sentinel


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here fails if anything opens a socket or starts a process."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("replay touched the network or started a process")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)


def _replay(
    record: RunRecord, pipeline: Pipeline | None, plugins: FixturePlugins, **kw: Any
) -> Any:
    kw.setdefault("models", MODELS)
    result = replay(record, pipeline, plugins=_sentinel(plugins), **kw)
    assert isinstance(result, ReplayResult)
    json.dumps(result.to_dict())  # always JSON-friendly
    return result


def _kind(record: RunRecord, index: int) -> str:
    error = record.steps[index].error
    assert error is not None
    return error.kind


def _assert_identical(result: ReplayResult) -> None:
    assert result.outcome == "identical", result.render()
    assert result.identical
    assert result.intents_diff == ""
    assert result.summary == ()
    assert result.mismatches == ()


# --- identical intents --------------------------------------------------------------------


def test_daily_brief_replays_identically() -> None:
    plugins = _brief_plugins()
    pipeline = _brief()
    record = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    assert record.status == "ok", record.error
    assert [i.intent.type for i in record.intents] == [
        "task.create",
        "note.replace_owned_block",
        "notify.push",
    ]
    calls = len(plugins.calls)
    result = _replay(record, pipeline, plugins)
    _assert_identical(result)
    assert len(plugins.calls) == calls  # the live plugins were not called
    replayed = result.replayed
    assert replayed is not None
    assert intents_json(replayed) == intents_json(record)
    assert replayed.id == record.id
    assert replayed.context == record.context
    assert replayed.started_at == record.started_at
    assert replayed.inputs == record.inputs
    assert replayed.llm == record.llm
    assert replayed.cost == record.cost
    assert [s.output for s in replayed.steps] == [s.output for s in record.steps]
    assert "identical" in result.render()


def test_replay_is_deterministic() -> None:
    plugins = _brief_plugins()
    pipeline = _brief()
    record = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    first = _replay(record, pipeline, plugins).replayed
    second = _replay(record, pipeline, plugins).replayed
    assert first.model_dump_json() == second.model_dump_json()
    assert first.finished_at == record.started_at  # frozen clock, not the wall clock
    later = START + datetime.timedelta(hours=1)
    third = _replay(record, pipeline, plugins, clock=lambda: later).replayed
    assert third.finished_at == later


def test_degraded_collect_replays_identically() -> None:
    plugins = _brief_plugins()

    def crash(params: dict[str, Any], data: Any) -> Any:
        raise ToolCallError("plugin_crashed", "exit 3")

    plugins.handlers["calendar.events"] = crash
    pipeline = _brief()
    record = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    assert record.status == "degraded"
    assert [s.status for s in record.steps] == ["degraded", "ok", "ok", "skipped", "ok"]
    result = _replay(record, pipeline, plugins)
    _assert_identical(result)
    assert result.replayed.steps[0].error == record.steps[0].error  # message reproduced
    assert result.replayed.steps[0].output == record.steps[0].output


def test_failed_collect_replays_identically() -> None:
    plugins = _brief_plugins()

    def down(params: dict[str, Any], data: Any) -> Any:
        raise ToolCallError("timeout", "slow")

    for ref in ("github.review_requests", "calendar.events", "coach.brief", "obsidian.open_tasks"):
        plugins.handlers[ref] = down
    record = _record(_brief(), plugins, FakeLlm())
    assert record.status == "failed"
    _assert_identical(_replay(record, _brief(), plugins))


@pytest.mark.parametrize(
    "script",
    [
        # schema failure, then a valid repair
        [
            llm_response({"oops": 1}, schema_errors=["items: missing"]),
            llm_response(BRIEF, intents=[NOTE, PUSH]),
        ],
        # schema failure twice: degraded
        [
            llm_response({"oops": 1}, schema_errors=["items: missing"]),
            llm_response({"oops": 2}, schema_errors=["items: missing"]),
        ],
        # schema failure, then the backend fails (and bills)
        [
            llm_response({"oops": 1}, schema_errors=["x"]),
            LlmError("backend_error", "cut off", usage=llm_response(None, usd=0.01).usage),
        ],
        # the backend fails at once
        [LlmError("backend_unavailable", "503")],
        # a backend-cached response
        [llm_response(BRIEF, intents=[NOTE], cached=True)],
        # over budget
        [llm_response(BRIEF, tokens_out=5000)],
    ],
    ids=["repair", "schema-x2", "retry-error", "error", "cached", "budget"],
)
def test_llm_retry_paths_replay_identically(script: list[LlmResponse | LlmError]) -> None:
    plugins = _brief_plugins()
    pipeline = _brief()
    record = _record(pipeline, plugins, FakeLlm(list(script)))
    result = _replay(record, pipeline, plugins)
    _assert_identical(result)
    assert result.replayed.llm == record.llm
    assert result.replayed.cost.tokens_out == record.cost.tokens_out
    assert result.replayed.cost.usd == pytest.approx(record.cost.usd)


def test_repair_errors_are_served_from_the_retry_request() -> None:
    plugins = _brief_plugins()
    script: list[LlmResponse | LlmError] = [
        llm_response({"oops": 1}, schema_errors=["items: missing"]),
        llm_response(BRIEF, intents=[NOTE, PUSH]),
    ]
    record = _record(_brief(), plugins, FakeLlm(script))
    llm = RecordedLlm(record)
    tools = RecordedToolCaller(record, _brief(), record.context)
    asyncio.run(
        execute(
            _brief(),
            run_id=record.id,
            context=record.context,
            started_at=START,
            plugins=plugins,
            tools=tools,
            llm=llm,
            models=MODELS,
        )
    )
    assert [r.attempt for r in llm.requests] == [1, 2]
    assert llm.requests[1].repair == ("items: missing",)


def _previous(record: RunRecord) -> PreviousLlm:
    assert record.input_hash is not None
    call = record.llm[-1]
    return PreviousLlm(
        input_hash=record.input_hash,
        output=call.response,
        intents=call.intents,
        skill_version=call.skill_version,
        model=call.model,
        backend=call.backend,
    )


def test_reuse_path_replays_without_an_llm_call() -> None:
    plugins = _brief_plugins()
    pipeline = _brief()
    first = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    second = _record(pipeline, plugins, FakeLlm(), previous=_previous(first))
    assert second.llm[0].cached is True
    assert recorded_previous(second) is not None
    assert recorded_previous(first) is None
    assert second.models == MODELS
    _assert_identical(_replay(second, pipeline, plugins))
    # The recorded model targets win over the caller's: the cache key is the run's.
    _assert_identical(_replay(second, pipeline, plugins, models={"default": "other-model"}))
    # A record without them uses the caller's; other targets give another cache key, so the
    # replay would need an LLM call that the record does not have: a mismatch, never a call.
    bare = second.model_copy(update={"models": None})
    _assert_identical(_replay(bare, pipeline, plugins))
    result = _replay(bare, pipeline, plugins, models={"default": "other-model"})
    assert result.outcome == "error"
    assert result.mismatches == ("LLM attempt 1 was not recorded",)
    assert result.replayed.steps[2].error.kind == MISMATCH
    assert "the record keeps no model targets" in result.notes[0]


def test_noop_reuse_replays_identically() -> None:
    plugins = _brief_plugins()
    first = _record(_brief(), plugins, FakeLlm([llm_response(BRIEF, intents=[])]))
    plugins.handlers["github.review_requests"] = lambda p, i: [{"title": "x"}]
    second = _record(_brief(), plugins, FakeLlm([llm_response(BRIEF)]))
    assert second.status == "ok"
    third_plugins = _brief_plugins()
    third_plugins.handlers["github.review_requests"] = lambda p, i: []
    one = _record(_brief(), third_plugins, FakeLlm([llm_response(BRIEF, intents=[])]))
    two = _record(_brief(), third_plugins, FakeLlm(), previous=_previous(one))
    assert two.status == "noop"
    _assert_identical(_replay(two, _brief(), third_plugins))
    _assert_identical(_replay(first, _brief(), plugins))
    undone = two.model_copy(update={"status": "undone"})
    assert status_before_undo(undone) == "noop"
    _assert_identical_but_noted(_replay(undone, _brief(), third_plugins))


def _assert_identical_but_noted(result: ReplayResult) -> None:
    assert result.outcome == "identical", result.render()
    assert result.notes


def test_registry_is_resolved_once_up_front() -> None:
    record, plugins = _ok_record()
    asked: list[str] = []

    class Counting(FixturePlugins):
        def plugin_for_tool(self, ref: str) -> Any:
            asked.append(ref)
            return super().plugin_for_tool(ref)

    counting = Counting()
    counting.plugins = dict(plugins.plugins)
    result = replay(record, _brief(), plugins=counting, models=MODELS)
    _assert_identical(result)
    assert sorted(asked) == sorted(set(asked))  # each ref once, before the run
    # A registry that no longer knows a plugin fails the replayed plan, as a live run would.
    missing = FixturePlugins()
    missing.plugins = {k: v for k, v in plugins.plugins.items() if k != "daily-brief"}
    result = _replay(record, _brief(), missing)
    assert result.outcome == "different"
    assert result.replayed.steps[2].error.kind == "plugin_unknown"
    result = _replay(record, _brief(), FixturePlugins())
    assert result.replayed.steps[0].error.kind == "plugin_unknown"


def test_definition_for() -> None:
    record, _ = _ok_record()
    bare = record.model_copy(update={"pipeline_definition": None})
    forged = record.model_copy(update={"pipeline_definition": canonical_pipeline(_moved_note())})
    assert definition_for(record, None, False)[1] == "recorded"
    assert definition_for(bare, _brief(), False)[1] == "current"
    assert definition_for(bare, _moved_note(), False) == (None, None)
    assert definition_for(bare, None, True) == (None, None)
    assert definition_for(forged, _brief(), False) == (None, None)
    assert definition_for(record, _moved_note(), True)[1] == "current"


def test_engine_refuses_invalid_digests_and_models() -> None:
    plugins = _brief_plugins()
    for kwargs in ({"lock_digest": "nope"}, {"config_digest": "A" * 64}, {"models": {"": "x"}}):
        with pytest.raises(ValueError, match="invalid"):
            _record(_brief(), plugins, FakeLlm(), **kwargs)
    assert _record(_brief(), plugins, FakeLlm(), models=None).models is None


def test_pipeline_definition_is_bounded_and_checked() -> None:
    record, _ = _ok_record()
    data = record.model_dump(mode="json")
    definition = data["pipeline_definition"]
    assert Pipeline.model_validate(definition).name == "daily-brief"
    with pytest.raises(ValueError, match="another pipeline"):
        RunRecord.model_validate({**data, "pipeline_definition": {**definition, "name": "x"}})
    with pytest.raises(ValueError, match="not a valid pipeline"):
        RunRecord.model_validate({**data, "pipeline_definition": {"name": "daily-brief"}})
    huge = {**definition, "steps": [*definition["steps"]]}
    huge["steps"][0] = {"collect": [{"github.review_requests": {"x": "y" * MAX_PIPELINE_BYTES}}]}
    with pytest.raises(ValueError, match="larger than"):
        RunRecord.model_validate({**data, "pipeline_definition": huge})
    with pytest.raises(ValueError, match="plain JSON"):
        RunRecord.model_validate({**data, "pipeline_definition": {**definition, "x": float("nan")}})
    # The engine leaves out a definition too large to record.
    plugins = _brief_plugins()
    big = _brief().model_dump(mode="json", exclude_none=True)
    big["steps"][0]["collect"][2] = {"coach.brief": {"endpoints": ["e" * MAX_PIPELINE_BYTES]}}
    plugins.handlers["coach.brief"] = lambda p, i: {}
    assert _record(Pipeline.model_validate(big), plugins, FakeLlm()).pipeline_definition is None


def test_transform_failure_replays_identically() -> None:
    plugins = _brief_plugins()

    def broken(params: dict[str, Any], data: Any) -> Any:
        raise ToolCallError("tool_error", "bad input")

    plugins.handlers["tasks.dedupe_fingerprint"] = broken
    record = _record(_brief(), plugins, FakeLlm())
    assert record.steps[1].status == "failed"
    result = _replay(record, _brief(), plugins)
    _assert_identical(result)
    assert result.replayed.steps[1].error == record.steps[1].error
    assert result.notes == ()


def test_transform_intent_failure_is_revalidated() -> None:
    plugins = _brief_plugins()
    plugins.handlers["tasks.normalize"] = lambda p, i: ToolResult(data={}, intents=[PUSH])
    record = _record(_brief(), plugins, FakeLlm())
    assert _kind(record, 1) == "intent_rejected"
    assert record.steps[1].calls is not None
    assert record.steps[1].calls[0].intents == [PUSH]
    result = _replay(record, _brief(), plugins)
    _assert_identical(result)
    assert result.notes == ()
    assert result.replayed.steps[1].error == record.steps[1].error


def test_collect_intent_failure_is_revalidated() -> None:
    plugins = _brief_plugins()
    plugins.handlers["coach.brief"] = lambda p, i: ToolResult(data={}, intents=[PUSH])
    record = _record(_brief(), plugins, FakeLlm())
    assert _kind(record, 0) == "intent_rejected"
    # The rejected source and its raw proposals are recorded, so the replay re-validates them;
    # so is every later source of the step (they all ran, concurrently).
    recorded = [(s.source, s.call, s.intents) for s in record.inputs]
    assert ("coach.brief", 2, [PUSH]) in recorded
    calls = [call or 0 for _, call, _ in recorded]
    assert calls == sorted(calls)
    result = _replay(record, _brief(), plugins)
    _assert_identical(result)
    assert result.replayed.steps[0].error == record.steps[0].error


def test_same_tool_twice_answers_each_call_with_its_own_proposals() -> None:
    """Repro B: two calls of one tool in a step; only the second proposes, and its proposal
    routes only because of the second call's data (the fingerprint check)."""
    plugins = _brief_plugins()

    def tasks(params: dict[str, Any], data: Any) -> Any:
        if params.get("folder") == "Inbox":
            return ToolResult(data=[])
        update = {"type": "task.update_status", "task": "ob:7", "status": "done"}
        return ToolResult(data=[{"fingerprint": "ob:7"}], intents=[update])

    plugins.handlers["obsidian.open_tasks"] = tasks
    data = _brief().model_dump(mode="json", exclude_none=True)
    data["steps"][0]["collect"] = [
        {"obsidian.open_tasks": {"folder": "Inbox"}},
        {"obsidian.open_tasks": {"folder": "Projects"}},
    ]
    data["steps"][1]["transform"] = ["tasks.normalize"]
    pipeline = Pipeline.model_validate(data)
    record = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    assert record.status == "ok", record.error
    assert next(i.intent.type for i in record.intents) == "task.update_status"
    assert [(s.call, s.intents is None) for s in record.inputs] == [(0, True), (1, False)]
    _assert_identical(_replay(record, pipeline, plugins))


def test_multi_call_transform_with_intents_on_a_non_final_call() -> None:
    plugins = _brief_plugins()
    task = {"type": "task.create", "title": "Prep", "fingerprint": "cal:1"}
    calendar = plugins.plugins["calendar"].manifest.model_dump(mode="json")
    calendar["permissions"]["intents"] = ["task.create"]
    plugins.add(calendar, dict(plugins.handlers))
    plugins.handlers["calendar.prep_tasks"] = lambda p, i: ToolResult(data=i, intents=[task])
    data = _brief().model_dump(mode="json", exclude_none=True)
    data["steps"][1]["transform"] = ["calendar.prep_tasks", "tasks.normalize"]
    pipeline = Pipeline.model_validate(data)
    plugins.handlers["tasks.normalize"] = lambda p, i: {"sources": len(i)}
    record = _record(pipeline, plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    assert record.status == "ok", record.error
    calls = record.steps[1].calls
    assert calls is not None
    assert [(c.ref, c.intents) for c in calls] == [
        ("calendar.prep_tasks", [task]),
        ("tasks.normalize", None),
    ]
    assert any(i.proposed_by == "calendar.prep_tasks" for i in record.intents)
    _assert_identical(_replay(record, pipeline, plugins))


def test_omitted_target_is_filled_again_from_the_current_entry() -> None:
    """Repro C: a source proposes `note.create` without a `path`; replayed against a pipeline
    whose act entry moved, the raw proposal routes to the new path."""
    plugins = FixturePlugins()
    plugins.add(_manifest("src", ("note.create",), sources=["src.read"]))
    plugins.add(_manifest("vault", ("note.create",), sinks=["vault.write"]))
    plugins.handlers["src.read"] = lambda p, i: ToolResult(
        data=[], intents=[{"type": "note.create", "content": "hi"}]
    )

    def flow(path: str) -> Pipeline:
        return Pipeline.model_validate(
            {
                "name": "notes",
                "steps": [
                    {"collect": ["src.read"]},
                    {"act": [{"vault.write": {"path": path, "mode": "create"}}]},
                ],
            }
        )

    record = _record(flow("Notes/{{date}}.md"), plugins)
    assert record.status == "ok", record.error
    assert record.inputs[0].intents == [{"type": "note.create", "content": "hi"}]
    _assert_identical(_replay(record, flow("Notes/{{date}}.md"), plugins))
    result = _replay(record, flow("Inbox/{{date}}.md"), plugins, against_current=True)
    assert result.outcome == "different", result.render()
    assert '+      "path": "Inbox/2026-10-27.md",' in result.intents_diff
    assert result.summary == ()
    assert result.replayed.status == "ok"


def test_pipeline_without_llm_replays() -> None:
    plugins = FixturePlugins()
    plugins.add(_manifest("obsidian", sources=["obsidian.open_tasks"]), {})
    plugins.add(_manifest("ntfy", ("notify.push",), notifiers=["ntfy.reminders"]))
    plugins.handlers["obsidian.open_tasks"] = lambda p, i: ToolResult(data=[], intents=[])
    pipeline = Pipeline.model_validate(yaml.safe_load((EXAMPLES / "remind.yaml").read_text()))
    record = _record(pipeline, plugins)
    _assert_identical(_replay(record, pipeline, plugins))


# --- differences --------------------------------------------------------------------------


def _ok_record() -> tuple[RunRecord, FixturePlugins]:
    plugins = _brief_plugins()
    record = _record(_brief(), plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    return record, plugins


def test_changed_recorded_response_is_reported_as_a_diff() -> None:
    record, plugins = _ok_record()
    llm = [record.llm[0].model_copy(update={"intents": [NOTE, {**PUSH, "body": "3 items"}]})]
    tampered = record.model_copy(update={"llm": llm})
    result = _replay(tampered, _brief(), plugins)
    assert result.outcome == "different"
    assert '-      "body": "2 items today",' in result.intents_diff
    assert '+      "body": "3 items",' in result.intents_diff
    assert result.intents_diff.startswith("--- recorded/")
    assert result.summary == ()
    assert "different" in result.render()


def test_status_and_step_differences_are_summarised() -> None:
    record, plugins = _ok_record()
    steps = list(record.steps)
    steps[4] = steps[4].model_copy(update={"status": "skipped", "output": None})
    tampered = record.model_copy(update={"steps": steps, "status": "degraded"})
    result = _replay(tampered, _brief(), plugins)
    assert result.outcome == "different"
    fields = {d.field: (d.recorded, d.replayed) for d in result.summary}
    assert fields == {"status": ("degraded", "ok"), "steps.4.status": ("skipped", "ok")}
    assert result.to_dict()["summary"][0] == {
        "field": "status",
        "recorded": "degraded",
        "replayed": "ok",
    }
    assert 'status: "degraded" -> "ok"' in result.render()


def test_recorded_params_must_match() -> None:
    record, plugins = _ok_record()
    inputs = list(record.inputs)
    inputs[1] = inputs[1].model_copy(update={"params": {"days": 3}})
    result = _replay(record.model_copy(update={"inputs": inputs}), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == (
        "step 0 call 1 (calendar.events): no recorded result for this tool and these parameters",
        "step 1 call 0 (tasks.normalize): the input differs from the recorded one",
    )
    assert result.replayed.steps[0].status == "degraded"
    assert "mismatch:" in result.render()


def test_tampered_collected_data_fails_the_transform_input_check() -> None:
    record, plugins = _ok_record()
    inputs = list(record.inputs)
    inputs[0] = inputs[0].model_copy(update={"data": [{"title": "Other"}]})
    result = _replay(record.model_copy(update={"inputs": inputs}), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == (
        "step 1 call 0 (tasks.normalize): the input differs from the recorded one",
    )


def _tampered_llm(record: RunRecord, index: int = 0, **update: Any) -> RunRecord:
    llm = list(record.llm)
    llm[index] = llm[index].model_copy(update=update)
    return record.model_copy(update={"llm": llm})


def test_changed_llm_input_hash_is_a_mismatch() -> None:
    record, plugins = _ok_record()
    result = _replay(_tampered_llm(record, input_hash="0" * 64), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == ("LLM attempt 1: the input differs",)


def test_backend_shaped_request_still_checks_the_input() -> None:
    """Repro A: the backend records its own request shape; a tampered LLM input (here, the
    transform output it is computed from) is still a mismatch."""
    plugins = _brief_plugins()
    response = llm_response(BRIEF, intents=[NOTE, PUSH])
    shaped = LlmResponse(**{**response.__dict__, "request": {"messages": ["opaque"]}})
    record = _record(_brief(), plugins, FakeLlm([shaped]))
    assert record.llm[0].request == {"messages": ["opaque"]}
    assert record.llm[0].model_alias == "default"
    _assert_identical(_replay(record, _brief(), plugins))
    steps = list(record.steps)
    steps[1] = steps[1].model_copy(update={"output": {"tampered": True}})
    result = _replay(record.model_copy(update={"steps": steps}), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == ("LLM attempt 1: the input differs",)


def test_model_alias_mismatch() -> None:
    record, plugins = _ok_record()
    result = _replay(_tampered_llm(record, model_alias="cheap"), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == ("LLM attempt 1: the model alias differs",)
    legacy = _tampered_llm(record, model_alias=None)
    result = _replay(legacy, _brief(), plugins)
    assert result.mismatches == (
        "LLM attempt 1: the record has no model alias, input hash or repair",
    )


def _repaired() -> tuple[RunRecord, FixturePlugins]:
    plugins = _brief_plugins()
    script: list[LlmResponse | LlmError] = [
        llm_response({"oops": 1}, schema_errors=["items: missing"]),
        llm_response(BRIEF, intents=[NOTE, PUSH]),
    ]
    return _record(_brief(), plugins, FakeLlm(script)), plugins


def test_tampered_repair_is_a_mismatch() -> None:
    """Repro D: the repair errors sent with the retry are checked."""
    record, plugins = _repaired()
    assert record.llm[0].schema_errors == ["items: missing"]
    assert record.llm[1].repair == ["items: missing"]
    _assert_identical(_replay(record, _brief(), plugins))
    result = _replay(_tampered_llm(record, 1, repair=["other"]), _brief(), plugins)
    assert result.outcome == "error"
    assert result.mismatches == ("LLM attempt 2: the repair errors differ",)
    result = _replay(_tampered_llm(record, 0, schema_errors=["other"]), _brief(), plugins)
    assert result.mismatches == ("LLM attempt 2: the repair errors differ",)
    result = _replay(_tampered_llm(record, 0, schema_errors=None), _brief(), plugins)
    assert result.mismatches == ("LLM attempt 1: the record has no schema errors",)


def test_other_pipeline_is_an_error() -> None:
    record, plugins = _ok_record()
    other = Pipeline.model_validate({**_brief().model_dump(mode="json"), "name": "other"})
    result = _replay(record, other, plugins)
    assert result.outcome == "error"
    assert "is a run of daily-brief" in (result.error or "")


def _moved_note() -> Pipeline:
    data = _brief().model_dump(mode="json", exclude_none=True)
    data["steps"][3]["act"][0] = {
        "obsidian.write_note": {"path": "Journal/{{date}}.md", "mode": "replace-owned"}
    }
    return Pipeline.model_validate(data)


def test_changed_pipeline_replays_the_recorded_definition() -> None:
    record, plugins = _ok_record()
    assert record.pipeline_definition is not None
    result = _replay(record, _moved_note(), plugins)
    _assert_identical(result)
    assert result.definition == "recorded"
    assert result.digest_changed
    assert "replayed the recorded definition" in result.notes[0]
    assert result.to_dict()["pipeline_digest"]["changed"] is True
    # Without a current definition at all, the recorded one is enough.
    _assert_identical(_replay(record, None, plugins))


def test_changed_pipeline_without_a_recorded_definition_is_reported() -> None:
    record, plugins = _ok_record()
    bare = record.model_copy(update={"pipeline_definition": None})
    _assert_identical(_replay(bare, _brief(), plugins))
    result = _replay(bare, _moved_note(), plugins)
    assert result.outcome == "error"
    assert "changed since the run" in (result.error or "")
    assert "--against-current" in (result.error or "")
    result = _replay(bare, None, plugins)
    assert "keeps no pipeline definition" in (result.error or "")
    result = _replay(bare, None, plugins, against_current=True)
    assert "is not in the instance" in (result.error or "")


def test_recorded_definition_must_match_its_digest() -> None:
    record, plugins = _ok_record()
    forged = record.model_copy(update={"pipeline_definition": canonical_pipeline(_moved_note())})
    result = _replay(forged, None, plugins)
    assert result.outcome == "error"
    assert "does not match its digest" in (result.error or "")


def test_lock_and_config_drift() -> None:
    record, plugins = _ok_record()
    stamped = record.model_copy(update={"lock_digest": "1" * 64, "config_digest": "2" * 64})
    same = {"lock_digest": "1" * 64, "config_digest": "2" * 64}
    _assert_identical(_replay(stamped, _brief(), plugins, **same))
    result = _replay(stamped, _brief(), plugins, lock_digest="3" * 64, config_digest="2" * 64)
    assert result.outcome == "error"
    assert result.error.startswith("horae.lock changed since the run (recorded digest 111111")
    assert result.to_dict()["lock_digest"] == {
        "recorded": "1" * 64,
        "current": "3" * 64,
        "changed": True,
    }
    result = _replay(stamped, _brief(), plugins, lock_digest="1" * 64, config_digest="4" * 64)
    assert result.error.startswith("horae.yaml (models, plugins) changed since the run")
    result = _replay(
        stamped,
        _brief(),
        plugins,
        lock_digest="3" * 64,
        config_digest="4" * 64,
        against_current=True,
    )
    assert result.outcome == "identical"
    assert [n.split(" changed")[0] for n in result.notes] == [
        "horae.lock",
        "horae.yaml (models, plugins)",
    ]
    # Unknown on either side: not compared.
    _assert_identical(_replay(record, _brief(), plugins, lock_digest="3" * 64))


def test_undone_run_compares_its_status_before_the_undo() -> None:
    record, plugins = _ok_record()
    undone = record.model_copy(update={"status": "undone"})
    result = _replay(undone, _brief(), plugins)
    _assert_identical(result)
    assert result.notes == ("the run was undone: its status is compared as ok, as before the undo",)
    assert status_before_undo(undone) == "ok"
    degraded = _record(_brief(), plugins, FakeLlm([LlmError("backend_error", "x")]))
    assert status_before_undo(degraded.model_copy(update={"status": "undone"})) == "degraded"


def test_violation_kinds_are_compared() -> None:
    record, plugins = _ok_record()
    blocked = Violation(kind="egress_blocked", detail="x")
    result = _replay(record.model_copy(update={"violations": [blocked]}), _brief(), plugins)
    assert result.outcome == "different"
    assert [d.to_dict() for d in result.summary] == [
        {"field": "violations", "recorded": ["egress_blocked"], "replayed": []}
    ]


def test_mismatched_failure_lines_are_served_by_position() -> None:
    plugins = _brief_plugins()

    def crash(params: dict[str, Any], data: Any) -> Any:
        raise ToolCallError("plugin_crashed", "exit 3")

    plugins.handlers["calendar.events"] = crash
    plugins.handlers["coach.brief"] = crash
    record = _record(_brief(), plugins, FakeLlm([llm_response(BRIEF, intents=[NOTE, PUSH])]))
    steps = list(record.steps)
    error = steps[0].error
    assert error is not None
    assert len(error.message.split("\n")) == 2
    one_line = error.model_copy(update={"message": "a single, unrelated line"})
    steps[0] = steps[0].model_copy(update={"error": one_line})
    result = _replay(record.model_copy(update={"steps": steps}), _brief(), plugins)
    _assert_identical(result)  # messages are not compared; kinds and positions are
    replayed_error = result.replayed.steps[0].error
    assert replayed_error.message.count("recorded failure") == 2


def test_record_without_per_call_data_is_not_replayable() -> None:
    record, plugins = _ok_record()
    inputs = [s.model_copy(update={"call": None}) for s in record.inputs]
    result = _replay(record.model_copy(update={"inputs": inputs}), _brief(), plugins)
    assert result.outcome == "error"
    assert result.error == (
        "not replayable: step 0 (collect) does not record the position of its sources "
        "(recorded before #23)"
    )
    steps = list(record.steps)
    steps[1] = steps[1].model_copy(update={"calls": None})
    result = _replay(record.model_copy(update={"steps": steps}), _brief(), plugins)
    assert "step 1 (transform) does not record its calls" in (result.error or "")


def test_against_current_shows_what_the_edit_changes() -> None:
    record, plugins = _ok_record()
    result = _replay(record, _moved_note(), plugins, against_current=True)
    assert result.outcome == "different"
    assert '-      "path": "Daily/2026-10-27.assistant.md",' in result.intents_diff
    assert '+      "path": "Journal/2026-10-27.md",' in result.intents_diff
    assert "replayed its current definition" in result.render()
    assert result.definition == "current"
    # The same definition with --against-current is simply identical.
    _assert_identical(_replay(record, _brief(), plugins, against_current=True))


def test_against_current_with_new_sources_is_a_mismatch() -> None:
    record, plugins = _ok_record()
    data = _brief().model_dump(mode="json", exclude_none=True)
    data["steps"][0]["collect"][1] = {"calendar.events": {"days": 7}}
    result = _replay(record, Pipeline.model_validate(data), plugins, against_current=True)
    assert result.outcome == "error"
    assert "calendar.events" in result.mismatches[0]


# --- recorded ports -----------------------------------------------------------------------


def test_recorded_tool_caller_rejects_unplanned_calls() -> None:
    record, _ = _ok_record()
    tools = RecordedToolCaller(record, _brief(), record.context)
    with pytest.raises(ToolCallError) as caught:
        asyncio.run(tools.call("evil.read", {}, None))
    assert caught.value.kind == MISMATCH
    assert tools.mismatches == ["unexpected call to evil.read: the pipeline does not plan it here"]


def test_planned_calls_are_the_engines_and_template_errors_plan_nothing() -> None:
    calls = planned_calls(_brief(), CONTEXT)
    assert [(c.step, c.call, c.kind, c.ref) for c in calls][:2] == [
        (0, 0, "collect", "github.review_requests"),
        (0, 1, "collect", "calendar.events"),
    ]
    [note] = [c for c in calls if c.ref == "obsidian.write_note"]
    assert note.kind == "act"
    assert note.params == {"path": "Daily/2026-10-27.assistant.md", "mode": "replace-owned"}
    broken = Pipeline.model_validate(
        {"name": "x", "steps": [{"collect": [{"a.b": {"day": "{{nope}}"}}]}]}
    )
    [call] = planned_calls(broken, CONTEXT)
    assert call.params is None
    assert call.error is not None
    tools = RecordedToolCaller(_ok_record()[0], broken, CONTEXT)
    with pytest.raises(ToolCallError):
        asyncio.run(tools.call("a.b", {}, None))


def test_unrecorded_llm_attempt_is_a_mismatch() -> None:
    record, _ = _ok_record()
    llm = RecordedLlm(record.model_copy(update={"llm": []}))
    request = LlmRequest
    with pytest.raises(LlmError):
        asyncio.run(llm.complete(request(SKILL, "default", {}, Budget(usd=1, tokens_out=1), 1, 1)))
    assert llm.mismatches == ["LLM attempt 1 was not recorded"]
    other = RecordedLlm(record)
    with pytest.raises(LlmError):
        asyncio.run(
            other.complete(request("x/y", "default", {}, Budget(usd=1, tokens_out=1), 1, 1))
        )
    assert other.mismatches == ["LLM attempt 1: skill x/y differs"]


# --- the lockfile registry ----------------------------------------------------------------


LOCK = {
    "schema_version": 1,
    "plugins": {
        name: {
            "version": "0.1.0",
            "repo": f"https://github.com/horae-dev/plugin-{name}",
            "commit": "c" * 40,
            "permissions": {"intents": intents},
        }
        for name, intents in {
            "src": ["notify.push"],
            "sink": ["note.create", "notify.push"],
            "skills": ["note.create", "notify.push"],
        }.items()
    },
}
BUDGET = {"usd": 1, "tokens_out": 99}
FLOW = {
    "name": "flow",
    "steps": [
        {"collect": ["src.read"]},
        {"transform": ["src.shape"]},
        {
            "llm": {
                "skill": "skills/rank",
                "model": "default",
                "budget": {"usd": 1, "tokens_out": 99},
            }
        },
        {"act": [{"sink.write": {"path": "Notes/{{date}}.md", "mode": "create"}}]},
        {"notify": ["sink.notify"]},
    ],
}


def test_lock_registry_resolves_by_plugin_name() -> None:
    registry = LockRegistry(Lockfile.model_validate(LOCK), Pipeline.model_validate(FLOW))
    assert registry.unresolved == ()
    src = registry.plugin_for_tool("src.read")
    assert src.commit == "c" * 40
    assert src.manifest.provides.sources == ["src.read"]
    assert src.manifest.provides.transforms == ["src.shape"]
    assert registry.plugin_for_skill("skills/rank").manifest.provides.skills == ["skills/rank"]
    assert registry.plugin_for_tool("sink.notify").manifest.permissions.intents == [
        "note.create",
        "notify.push",
    ]
    with pytest.raises(LookupError):
        registry.plugin_for_tool("coach.brief")
    brief = LockRegistry(Lockfile.model_validate(LOCK), _brief())
    assert "coach.brief" in brief.unresolved
    assert SKILL in brief.unresolved
    with pytest.raises(LookupError):
        registry.plugin_for_skill("other/skill")


def test_lock_registry_resolves_as_the_run_did() -> None:
    """The PLAN.md §3.2 brief: `coach.brief` from plugin `mcp-brief`, the skill from
    `pack-daily-brief`, neither named after its namespace."""
    record, plugins = _ok_record()
    assert record.plugins["mcp-brief"].provides == ["coach.brief"]
    assert record.plugins["pack-daily-brief"].provides == [SKILL]
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                name: {
                    "version": "0.1.0",
                    "repo": f"https://github.com/horae-dev/plugin-{name}",
                    "commit": "a" * 40,
                    "permissions": plugin.manifest.permissions.model_dump(mode="json"),
                }
                for plugin in plugins.plugins.values()
                for name in [plugin.manifest.name]
            },
        }
    )
    assert LockRegistry(lock, _brief()).unresolved == ("coach.brief", SKILL)
    registry = LockRegistry(lock, _brief(), record)
    assert registry.unresolved == ()
    assert registry.plugin_for_tool("coach.brief").manifest.name == "mcp-brief"
    assert registry.plugin_for_skill(SKILL).manifest.provides.skills == [SKILL]
    result = replay(record, _brief(), plugins=registry, models=MODELS)
    # The lock pins other commits than the fixtures: only the plugin versions differ.
    assert result.outcome == "different"
    assert {d.field for d in result.summary} == {"plugins"}
    assert result.intents_diff == ""
