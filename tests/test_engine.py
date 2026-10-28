# SPDX-License-Identifier: Apache-2.0
"""Pipeline execution (#17): the daily brief end to end, intent routing, step policy and fixed
control flow."""

import asyncio
import datetime
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from horae.contracts import MAX_RUN_INTENTS, Pipeline, RunRecord, StepRecord
from horae.contracts.run import ProposedIntent
from horae.engine import (
    MAX_DEPTH,
    MAX_OUTPUT_BYTES,
    LlmError,
    LlmRequest,
    LlmResponse,
    PreviousLlm,
    RunCancelled,
    ToolCallError,
    ToolResult,
    execute,
    make_context,
    pipeline_digest,
)
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.store import Store

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "schemas" / "examples" / "pipeline" / "valid"
RUN_RECORD_SCHEMA = json.loads((ROOT / "schemas" / "run-record" / "v1.json").read_text())
START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
CONTEXT = make_context(START, "Europe/Paris")
BRIEF = {"items": ["Review PR 42", "Dentist at 10:00"]}
NOTE_PATH = "Daily/2026-10-27.assistant.md"
NOTE = {
    "type": "note.replace_owned_block",
    "block": "horae-brief",
    "content": "- [ ] Review PR 42\n",
}
PUSH = {"type": "notify.push", "body": "2 items today"}
SKILL = "daily-brief/rank-and-word"


def _clock() -> Callable[[], datetime.datetime]:
    ticks = itertools.count()
    return lambda: START + datetime.timedelta(milliseconds=250 * next(ticks))


class _Monotonic:
    """A monotonic clock the tests move by hand (and by 0.25 s per read)."""

    def __init__(self, step: float = 0.25) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _manifest(name: str, intents: tuple[str, ...] = (), **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0.1,<0.2",
        "provides": provides,
        "permissions": {"intents": list(intents), "network": ["hooks.example.org"]},
    }


def _daily_brief_plugins() -> FixturePlugins:
    """Synthetic fixture plugins for every tool of the PLAN.md §3.2 daily brief. The sink and
    the notifier have no handler: the engine must never call them."""
    plugins = FixturePlugins()
    plugins.add(
        _manifest("github", sources=["github.review_requests"]),
        {"github.review_requests": lambda p, i: [{"title": "Fix the parser", "number": 42}]},
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
            ("note.replace_owned_block",),
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


def _daily_brief() -> Pipeline:
    return Pipeline.model_validate(yaml.safe_load((EXAMPLES / "daily-brief.yaml").read_text()))


def _brief_llm(*more: LlmResponse | LlmError, intents: Any = None) -> FakeLlm:
    proposed = [NOTE, PUSH] if intents is None else intents
    return FakeLlm([llm_response(BRIEF, intents=proposed), *more])


def _check(record: RunRecord) -> RunRecord:
    """Every produced record is valid against the JSON Schema and the model, on the wire."""
    data = json.loads(record.model_dump_json())
    validator = Draft202012Validator(RUN_RECORD_SCHEMA, format_checker=FormatChecker())
    errors = [error.message for error in validator.iter_errors(data)]
    assert errors == []
    RunRecord.model_validate_json(record.model_dump_json())
    return record


def _run(
    pipeline: Pipeline,
    plugins: FixturePlugins,
    llm: Any = None,
    **kwargs: Any,
) -> RunRecord:
    kwargs.setdefault("clock", _clock())
    kwargs.setdefault("monotonic", _Monotonic())
    kwargs.setdefault("context", CONTEXT)
    record = asyncio.run(
        execute(
            pipeline,
            run_id="20261027T053000Z-test-01",
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            **kwargs,
        )
    )
    return _check(record)


def _pipeline(*steps: dict[str, Any]) -> Pipeline:
    return Pipeline.model_validate({"name": "test", "steps": list(steps)})


def _statuses(record: RunRecord) -> list[str]:
    return [step.status for step in record.steps]


def _error_kind(record: RunRecord, index: int) -> str:
    error = record.steps[index].error
    assert error is not None
    return error.kind


# --- acceptance: the daily brief parses and executes with fixture plugins --------------------


def test_daily_brief_end_to_end() -> None:
    plugins = _daily_brief_plugins()
    llm = _brief_llm()
    record = _run(_daily_brief(), plugins, llm)

    assert record.status == "ok", record.error
    assert record.pipeline == "daily-brief"
    assert record.pipeline_digest == pipeline_digest(_daily_brief())
    assert record.started_at == START
    assert record.context.vars == {"date": "2026-10-27"}
    assert _statuses(record) == ["ok"] * 5
    assert [s.kind for s in record.steps] == ["collect", "transform", "llm", "act", "notify"]
    # Sources and transforms are called; the sink and the notifier are not.
    assert plugins.refs == [
        "github.review_requests",
        "calendar.events",
        "coach.brief",
        "obsidian.open_tasks",
        "tasks.normalize",
        "tasks.dedupe_fingerprint",
        "calendar.prep_tasks",
    ]
    assert all(call.input is None for call in plugins.calls[:4])
    assert [snap.source for snap in record.inputs] == plugins.refs[:4]
    assert record.inputs[1].params == {"days": 2}
    expected_input = {"sources": plugins.refs[:4], "deduped": True, "prep": True}
    assert record.steps[1].output == expected_input
    assert llm.requests[0].input == expected_input
    assert llm.requests[0].budget.usd == 0.05
    assert llm.requests[0].budget.tokens_out == 2000
    # The LLM proposed the note without a path: the core filled it from the act entry.
    routed = [
        (i.step, i.proposed_by, i.intent.type, i.applied_by, i.applied_by_step, i.applied_by_call)
        for i in record.intents
    ]
    assert routed == [
        (2, SKILL, "note.replace_owned_block", "obsidian.write_note", 3, 0),
        (2, SKILL, "notify.push", "ntfy.digest", 4, 0),
    ]
    assert record.intents[0].intent.model_dump()["path"] == NOTE_PATH
    assert record.steps[3].output == [
        {
            "call": "obsidian.write_note",
            "params": {"path": NOTE_PATH, "mode": "replace-owned"},
            "intents": [0],
        }
    ]
    assert record.steps[4].output == [
        {
            "call": "ntfy.digest",
            "params": {"when": "07:30", "only_if": "due_today_or_overdue"},
            "intents": [1],
        }
    ]
    assert record.llm[0].attempt == 1
    assert record.llm[0].schema_valid is True
    assert record.llm[0].intents == [NOTE, PUSH]  # raw, as proposed
    assert record.cost.usd == pytest.approx(0.001)
    assert record.input_hash is not None
    assert set(record.plugins) == {
        "github",
        "calendar",
        "mcp-brief",
        "obsidian",
        "tasks",
        "pack-daily-brief",
        "ntfy",
    }
    assert record.plugins["pack-daily-brief"].commit == "a" * 40
    assert record.changes == []  # applying intents is #21/#22
    assert record.steps[0].duration_ms == 250


@pytest.mark.parametrize(
    ("name", "notifier"),
    [
        ("external-trigger.yaml", "ntfy.digest"),
        ("remind.yaml", "ntfy.reminders"),
        ("with-timezone.yaml", "ntfy.reminders"),
    ],
)
def test_other_examples_run(name: str, notifier: str) -> None:
    plugins = FixturePlugins()
    plugins.add(
        _manifest("obsidian", ("notify.push",), sources=["obsidian.open_tasks"]),
        {"obsidian.open_tasks": lambda p, i: ToolResult(data=[], intents=[PUSH])},
    )
    plugins.add(
        _manifest("ntfy", ("notify.push",), notifiers=["ntfy.digest", "ntfy.reminders"]),
    )
    pipeline = Pipeline.model_validate(yaml.safe_load((EXAMPLES / name).read_text()))
    record = _run(pipeline, plugins)
    assert record.status == "ok", record.error
    assert [i.applied_by for i in record.intents] == [notifier]


@pytest.mark.parametrize("dry_run", [False, True])
def test_execute_calls_no_act_or_notify_tool(dry_run: bool) -> None:
    """Sinks and notifiers only apply intents later: `execute` never calls them, so a dry run
    calls nothing that writes."""
    plugins = _daily_brief_plugins()

    def forbidden(params: dict[str, Any], data: Any) -> Any:
        raise AssertionError("an act or notify tool was called")

    plugins.handlers["obsidian.write_note"] = forbidden
    plugins.handlers["ntfy.digest"] = forbidden
    record = _run(_daily_brief(), plugins, _brief_llm(), dry_run=dry_run)
    assert record.status == "ok"
    assert record.dry_run is dry_run
    assert not {"obsidian.write_note", "ntfy.digest"} & set(plugins.refs)


# --- acceptance: no step can change control flow at run time ---------------------------------

HOSTILE = {
    "steps": [{"act": ["evil.exfiltrate"]}],
    "next_step": "act",
    "skip": ["transform", "notify"],
    "goto": 0,
    "intents": [{"type": "http.post", "url": "https://evil.example.org/", "body": {}}],
    "path": "{{date}}",
}


def _flow_plugins(log: list[str]) -> FixturePlugins:
    plugins = FixturePlugins()

    def tool(ref: str, result: Any) -> Any:
        def handler(params: dict[str, Any], data: Any) -> Any:
            log.append(ref)
            return result

        return handler

    plugins.add(
        _manifest("src", ("notify.push",), sources=["src.read"], transforms=["src.shape"]),
        {"src.read": tool("src.read", HOSTILE), "src.shape": tool("src.shape", HOSTILE)},
    )
    plugins.add(
        _manifest(
            "sink",
            ("note.create", "notify.push"),
            sinks=["sink.write"],
            notifiers=["sink.notify"],
        ),
    )
    plugins.add(_manifest("evil", ("http.post",), sinks=["evil.exfiltrate"]))
    plugins.add(_manifest("skills", ("note.create", "notify.push"), skills=["skills/rank"]))
    return plugins


LLM = {"skill": "skills/rank", "model": "default", "budget": {"usd": 1, "tokens_out": 99}}
FLOW: tuple[dict[str, Any], ...] = (
    {"collect": ["src.read"]},
    {"transform": ["src.shape"]},
    {"llm": LLM},
    {"act": [{"sink.write": {"path": "Notes/{{date}}.md", "mode": "create", "day": "{{date}}"}}]},
    {"notify": ["sink.notify"]},
)
CREATE = {"type": "note.create", "path": "Notes/2026-10-27.md", "content": "hi"}


def test_step_like_results_are_plain_data() -> None:
    """Results shaped like steps, gotos, intents or templates never change what runs."""
    log: list[str] = []
    plugins = _flow_plugins(log)
    llm = FakeLlm([llm_response(HOSTILE, intents=[CREATE])])
    record = _run(_pipeline(*FLOW), plugins, llm)

    assert record.status == "ok", record.error
    assert log == ["src.read", "src.shape"]
    assert "evil.exfiltrate" not in plugins.refs
    # Data is passed on verbatim: the template in the data is not rendered.
    assert plugins.calls[1].input[0]["data"]["path"] == "{{date}}"
    assert llm.requests[0].input == HOSTILE
    # Params come from the definition and the context only.
    assert record.steps[3].output[0]["params"]["day"] == "2026-10-27"
    # `intents` inside data proposes nothing; only the declared intents count.
    assert [(i.intent.type, i.applied_by) for i in record.intents] == [
        ("note.create", "sink.write")
    ]


def test_mutating_inputs_or_the_pipeline_changes_nothing() -> None:
    """The plan is fixed before the first call and data is copied at each boundary."""
    pipeline = _pipeline(*FLOW)
    log: list[str] = []
    plugins = _flow_plugins(log)

    def tamper(params: dict[str, Any], data: Any) -> Any:
        log.append("src.shape")
        data[0]["data"] = "tampered"
        params["day"] = "tampered"
        pipeline.steps.clear()
        return {"ok": True}

    plugins.handlers["src.shape"] = tamper
    record = _run(pipeline, plugins, FakeLlm([llm_response(BRIEF)]))
    assert record.status == "ok"
    assert log == ["src.read", "src.shape"]
    assert len(record.steps) == 5
    assert record.inputs[0].data == HOSTILE


@pytest.mark.parametrize(("ref", "step"), [("src.read", 0), ("src.shape", 1)])
def test_sources_and_transforms_propose_intents(ref: str, step: int) -> None:
    plugins = _flow_plugins([])
    plugins.handlers[ref] = lambda p, i: ToolResult(data={}, intents=[PUSH])
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF)]))
    assert record.status == "ok", record.error
    assert [(i.step, i.proposed_by, i.applied_by) for i in record.intents] == [
        (step, ref, "sink.notify")
    ]

    # Validated against the proposer's own permissions.
    plugins.add(_manifest("src", sources=["src.read"], transforms=["src.shape"]))
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF)]))
    assert record.status == "failed"
    assert _error_kind(record, step) == "intent_rejected"
    assert "intent_undeclared" in (record.steps[step].error.message or "")  # type: ignore[union-attr]
    assert record.intents == []


@pytest.mark.parametrize("empty", [None, (), []])
def test_no_intents_in_any_spelling(empty: Any) -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data={}, intents=empty)
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF, intents=empty)]))
    assert record.status == "ok", record.error
    assert record.intents == []


# --- intent routing and binding --------------------------------------------------------------


def _route(intents: list[Any], *act: Any, notify: Any = None) -> RunRecord:
    plugins = _flow_plugins([])
    plugins.add(_manifest("other", ("note.create", "notify.push"), sinks=["other.write"]))
    steps: list[dict[str, Any]] = [{"collect": ["src.read"]}, {"llm": LLM}]
    if act:
        steps.append({"act": list(act)})
    steps.append({"notify": notify or ["sink.notify"]})
    return _run(_pipeline(*steps), plugins, FakeLlm([llm_response(BRIEF, intents=intents)]))


def test_explicit_target_must_match_the_binding() -> None:
    entry = {"sink.write": {"path": "Notes/{{date}}.md", "mode": "create"}}
    assert _route([CREATE], entry).status == "ok"

    other_path = {**CREATE, "path": "Secrets/stolen.md"}
    record = _route([other_path], entry)
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_unrouted"
    assert record.steps[1].error is not None
    assert "stolen" not in record.steps[1].error.message  # the target is not echoed
    assert record.intents == []


def test_omitted_target_is_filled_only_from_one_entry() -> None:
    first = {"sink.write": {"path": "Notes/{{date}}.md", "mode": "create"}}
    second = {"other.write": {"path": "Notes/other.md", "mode": "create"}}
    no_path = {"type": "note.create", "content": "hi"}
    record = _route([no_path], first)
    assert record.intents[0].intent.model_dump()["path"] == "Notes/2026-10-27.md"

    record = _route([no_path], first, second)
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_ambiguous"

    # Naming the target picks the entry.
    record = _route([{**no_path, "path": "Notes/other.md"}], first, second)
    assert record.status == "ok"
    assert [(i.applied_by, i.applied_by_call) for i in record.intents] == [("other.write", 1)]

    # Two entries bound to the same target are ambiguous.
    record = _route([CREATE], first, {"other.write": first["sink.write"]})
    assert _error_kind(record, 1) == "intent_ambiguous"


def test_sink_must_be_allowed_to_apply_the_type() -> None:
    plugins = _flow_plugins([])
    plugins.add(_manifest("weak", ("note.append_section",), sinks=["weak.write"]))
    steps: list[dict[str, Any]] = [
        {"collect": ["src.read"]},
        {"llm": LLM},
        {"act": [{"weak.write": {"path": "Notes/2026-10-27.md", "mode": "append-section"}}]},
    ]
    record = _run(_pipeline(*steps), plugins, FakeLlm([llm_response(BRIEF, intents=[CREATE])]))
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_unrouted"


@pytest.mark.parametrize("entry", ["sink.write", {"sink.write": {"path": "Notes/{{date}}.md"}}])
def test_act_entries_need_a_mode(entry: Any) -> None:
    plugins = _flow_plugins([])
    record = _run(_pipeline({"collect": ["src.read"]}, {"act": [entry]}), plugins)
    assert record.status == "failed"
    assert _statuses(record) == ["skipped", "failed"]
    assert _error_kind(record, 1) == "params_invalid"
    assert "missing `mode`" in (record.steps[1].error.message or "")  # type: ignore[union-attr]
    assert plugins.calls == []


@pytest.mark.parametrize(
    ("bound", "proposed"),
    [
        ("Notes/a.md", "Notes/A.md"),  # case
        ("Notes/a.md", "notes/a.md"),
        ("Notes/caf\u00e9.md", "Notes/cafe\u0301.md"),  # NFC entry, NFD intent
        ("Notes/cafe\u0301.md", "Notes/caf\u00e9.md"),  # NFD entry, NFC intent
        ("Notes/a.md", "Notes/a.md "),
    ],
)
def test_note_targets_match_as_exact_strings(bound: str, proposed: str) -> None:
    entry = {"sink.write": {"path": bound, "mode": "create"}}
    assert _route([{**CREATE, "path": bound}], entry).status == "ok"
    record = _route([{**CREATE, "path": proposed}], entry)
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_unrouted"


def test_only_later_entries_take_intents() -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data={}, intents=[PUSH])
    steps = [{"notify": ["sink.notify"]}, {"collect": ["src.read"]}]
    record = _run(_pipeline(*steps), plugins)
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_unrouted"


def test_http_post_binds_the_url() -> None:
    plugins = _flow_plugins([])
    plugins.add(_manifest("skills", ("http.post",), skills=["skills/rank"]))
    post = {"type": "http.post", "url": "https://hooks.example.org/x", "body": {"a": 1}}
    steps: list[dict[str, Any]] = [
        {"collect": ["src.read"]},
        {"llm": LLM},
        {"act": [{"evil.exfiltrate": {"mode": "post", "url": "https://hooks.example.org/x"}}]},
    ]
    record = _run(_pipeline(*steps), plugins, FakeLlm([llm_response(BRIEF, intents=[post])]))
    assert record.status == "ok", record.error
    for url in (
        "https://hooks.example.org/y",
        "https://hooks.example.org/x?a=1",  # query string
        "https://hooks.example.org/X",  # case
        "https://hooks.example.org/x/",
        "https://hooks.example.org:443/x",
    ):
        other = {**post, "url": url}
        record = _run(_pipeline(*steps), plugins, FakeLlm([llm_response(BRIEF, intents=[other])]))
        assert _error_kind(record, 1) == "intent_unrouted", url


def test_permissions_are_checked_before_routing() -> None:
    """A type the proposer may not propose is rejected, even when no entry could take it."""
    post = {"type": "http.post", "body": {}}  # no url, and no `post` entry to fill it from
    record = _route([post])
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_rejected"
    assert "intent_undeclared" in (record.steps[1].error.message or "")  # type: ignore[union-attr]

    # Mixed with an allowed, unroutable intent: still a rejection.
    record = _route([{"type": "note.create", "content": "hi"}, post])
    assert _error_kind(record, 1) == "intent_rejected"

    # Allowed but with nowhere to go: a routing error.
    record = _route([{"type": "note.create", "content": "hi"}])
    assert _error_kind(record, 1) == "intent_unrouted"


TASKS = ("task.create", "task.update_status")
FINGERPRINT = "calendar:3b1f0c"


def _task_run(intents: list[Any], data: Any = None, *, by_source: bool = False) -> RunRecord:
    plugins = _flow_plugins([])
    plugins.add(_manifest("todo", TASKS, sinks=["todo.write"]))
    plugins.add(_manifest("skills", TASKS, skills=["skills/rank"]))
    plugins.add(_manifest("src", TASKS, sources=["src.read", "src.more"]))
    read = data if data is not None else [{"id": FINGERPRINT, "title": "Dentist"}]
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data=read)
    plugins.handlers["src.more"] = lambda p, i: ToolResult(
        data={"more": True}, intents=intents if by_source else None
    )
    steps: list[dict[str, Any]] = [
        {"collect": ["src.read", "src.more"]},
        {"llm": LLM},
        {"act": [{"todo.write": {"mode": "tasks"}}]},
    ]
    llm = FakeLlm([llm_response(BRIEF, intents=None if by_source else intents)])
    return _run(_pipeline(*steps), plugins, llm)


def test_tasks_mode_routes_task_intents() -> None:
    create = {"type": "task.create", "title": "Call the dentist", "fingerprint": "x:1"}
    done = {"type": "task.update_status", "task": FINGERPRINT, "status": "done"}
    snooze = {**done, "status": "snoozed", "until": "2026-10-30"}
    record = _task_run([create, done, snooze])
    assert record.status == "ok", record.error
    assert [(i.intent.type, i.applied_by, i.applied_by_step) for i in record.intents] == [
        ("task.create", "todo.write", 2),
        ("task.update_status", "todo.write", 2),
        ("task.update_status", "todo.write", 2),
    ]
    assert record.steps[2].output == [
        {"call": "todo.write", "params": {"mode": "tasks"}, "intents": [0, 1, 2]}
    ]
    # A source may update a task an earlier source of the same step returned.
    assert _task_run([done], by_source=True).status == "ok"


@pytest.mark.parametrize(
    "data",
    [
        [{"id": "calendar:other"}],  # a different task
        [{FINGERPRINT: "Dentist"}],  # only as an object key
        [{"id": FINGERPRINT.upper()}],
        [{"id": f"{FINGERPRINT} "}],
    ],
)
def test_task_status_needs_a_collected_fingerprint(data: Any) -> None:
    done = {"type": "task.update_status", "task": FINGERPRINT, "status": "done"}
    record = _task_run([done], data)
    assert record.status == "failed"
    assert _error_kind(record, 1) == "intent_unrouted"
    assert FINGERPRINT not in (record.steps[1].error.message or "")  # type: ignore[union-attr]
    record = _task_run([done], data, by_source=True)
    assert _error_kind(record, 0) == "intent_unrouted"


LINK = "obsidian://open?vault=Main&file=Daily%2F2026-10-27"


@pytest.mark.parametrize(
    ("params", "link", "routed"),
    [
        ({}, None, True),
        ({}, LINK, False),  # no link_prefix: a proposed link is never routed
        ({"link_prefix": "obsidian://open?vault=Main&file="}, LINK, True),
        ({"link_prefix": "obsidian://open?vault=Main&file="}, None, True),
        ({"link_prefix": "obsidian://open?vault=Other&file="}, LINK, False),
        ({"link_prefix": "obsidian://open?vault=Main&file="}, "https://evil.example.org/", False),
        ({"link_prefix": "https://example.org/"}, "https://example.org.evil.example/x", False),
        ({"link_prefix": "https://example.org/"}, "https://example.org/notes/1", True),
    ],
)
def test_notification_links_need_a_trusted_prefix(
    params: dict[str, Any], link: str | None, routed: bool
) -> None:
    push = {**PUSH, "link": link} if link else PUSH
    record = _route([push], notify=[{"sink.notify": params}] if params else None)
    if routed:
        assert record.status == "ok", record.error
        assert record.intents[0].applied_by == "sink.notify"
    else:
        assert record.status == "failed"
        assert _error_kind(record, 1) == "intent_unrouted"
        assert "evil" not in (record.steps[1].error.message or "")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "prefix", [7, "", "ftp://example.org/", "https://example.org", "https://ex ample.org/"]
)
def test_invalid_link_prefix_is_a_plan_error(prefix: Any) -> None:
    plugins = _flow_plugins([])
    record = _run(_pipeline({"notify": [{"sink.notify": {"link_prefix": prefix}}]}), plugins)
    assert record.status == "failed"
    assert _error_kind(record, 0) == "params_invalid"


def test_two_notifiers_for_the_same_type_are_ambiguous() -> None:
    plugins = _flow_plugins([])
    plugins.add(_manifest("ntfy", ("notify.push",), notifiers=["ntfy.digest"]))
    plugins.add(_manifest("mail", notifiers=["mail.digest"]))  # may apply nothing
    plugins.handlers["src.read"] = lambda p, i: ToolResult(data={}, intents=[PUSH])
    both = _pipeline({"collect": ["src.read"]}, {"notify": ["sink.notify", "ntfy.digest"]})
    record = _run(both, plugins)
    assert record.status == "failed"
    assert _error_kind(record, 0) == "intent_ambiguous"

    one = _pipeline(
        {"collect": ["src.read"]},
        {"notify": [{"mail.digest": {"topic": "x"}}, {"ntfy.digest": {"topic": "brief"}}]},
    )
    record = _run(one, plugins)
    assert record.status == "ok", record.error
    assert [(i.applied_by, i.applied_by_call) for i in record.intents] == [("ntfy.digest", 1)]
    assert record.steps[1].output == [
        {"call": "mail.digest", "params": {"topic": "x"}, "intents": []},
        {"call": "ntfy.digest", "params": {"topic": "brief"}, "intents": [0]},
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "overwrite", "path": "Notes/a.md"},
        {"mode": 3, "path": "Notes/a.md"},
        {"mode": "replace-owned"},
        {"mode": "create"},  # no path
        {"mode": "append-section", "path": "Notes/a.md"},  # sink may not append
        {"mode": "create", "path": "../escape.md"},
        {"mode": "create", "path": 7},
        {"mode": "post", "url": "http://hooks.example.org/"},
    ],
)
def test_invalid_act_bindings_are_plan_errors(params: dict[str, Any]) -> None:
    plugins = _flow_plugins([])
    plugins.add(_manifest("sink", ("note.create", "http.post"), sinks=["sink.write"]))
    record = _run(_pipeline({"act": [{"sink.write": params}]}), plugins)
    assert record.status == "failed"
    assert _error_kind(record, 0) == "params_invalid"
    assert plugins.calls == []


# --- step policy -----------------------------------------------------------------------------


def _fail(kind: str = "timeout", message: str = "boom", stderr: str = "trace\n") -> Any:
    def handler(params: dict[str, Any], data: Any) -> Any:
        raise ToolCallError(kind, message, stderr=stderr)

    return handler


def test_collect_partial_failure_skips_act_but_notifies() -> None:
    plugins = _daily_brief_plugins()
    plugins.handlers["calendar.events"] = _fail("plugin_crashed", "exit 3\x1b[2J")
    record = _run(_daily_brief(), plugins, _brief_llm())
    assert record.status == "degraded"
    assert _statuses(record) == ["degraded", "ok", "ok", "skipped", "ok"]
    error = record.steps[0].error
    assert error is not None
    assert error.kind == "source_failed"
    assert "calendar.events: plugin_crashed" in error.message
    assert "\x1b" not in error.message
    assert record.steps[0].output == [
        {"source": "calendar.events", "params": {"days": 2}, "error_kind": "plugin_crashed"}
    ]
    assert record.steps[0].stderr == "--- calendar ---\ntrace\n"
    assert [s.source for s in record.inputs] == [
        "github.review_requests",
        "coach.brief",
        "obsidian.open_tasks",
    ]
    # The note is not written from partial data; the notification still goes out.
    assert _error_kind(record, 3) == "partial_input"
    assert [i.intent.type for i in record.intents] == ["notify.push"]
    assert record.steps[4].output[0]["intents"] == [0]
    assert record.error is not None
    assert record.error.startswith("step 0 (collect)")
    assert record.input_hash is not None  # the LLM output itself was accepted


def test_partial_input_drops_act_intents_and_records_notify_ones() -> None:
    plugins = _daily_brief_plugins()
    plugins.handlers["coach.brief"] = _fail()
    note = {**NOTE, "path": NOTE_PATH}
    record = _run(_daily_brief(), plugins, _brief_llm(intents=[note, PUSH, note]))
    assert record.status == "degraded"
    assert _statuses(record) == ["degraded", "ok", "ok", "skipped", "ok"]
    act = record.steps[3]
    assert act.output is None
    assert act.error is not None
    assert act.error.kind == "partial_input"
    assert "2 intent(s) dropped" in act.error.message
    # Only the notification is recorded, at index 0, and listed by its entry.
    assert [(i.intent.type, i.applied_by, i.applied_by_step) for i in record.intents] == [
        ("notify.push", "ntfy.digest", 4)
    ]
    assert record.steps[4].output[0]["intents"] == [0]


def test_collect_total_failure_fails_the_run() -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.read"] = _fail()
    record = _run(_pipeline(*FLOW), plugins, FakeLlm())
    assert record.status == "failed"
    assert _statuses(record) == ["failed", "skipped", "skipped", "skipped", "skipped"]
    assert record.steps[0].output == [{"source": "src.read", "params": {}, "error_kind": "timeout"}]
    assert plugins.refs == ["src.read"]


def test_transform_failure_fails_the_run() -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.shape"] = _fail("tool_error")
    record = _run(_pipeline(*FLOW), plugins, FakeLlm())
    assert record.status == "failed"
    assert _statuses(record) == ["ok", "failed", "skipped", "skipped", "skipped"]
    assert _error_kind(record, 1) == "tool_error"
    assert record.input_hash is None


class _NotAResult:
    data: Any = None


@pytest.mark.parametrize(
    ("handler", "kind"),
    [
        (lambda p, i: (_ for _ in ()).throw(RuntimeError("secret detail")), "host_error"),
        (_fail("NOT A KIND!", "x"), "host_error"),
        (lambda p, i: {"bad": {1, 2}}, "output_invalid"),
        (lambda p, i: {1: "int key"}, "output_invalid"),
        (lambda p, i: {"nan": float("nan")}, "output_invalid"),
        (lambda p, i: {"text": "lone \ud800 surrogate"}, "output_invalid"),
        (lambda p, i: ToolResult(data={}, intents=[{1, 2}]), "output_invalid"),
    ],
)
def test_misbehaving_tools_are_contained(handler: Any, kind: str) -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.shape"] = handler
    record = _run(_pipeline(*FLOW), plugins, FakeLlm())
    assert record.status == "failed"
    assert _error_kind(record, 1) == kind
    assert "secret detail" not in (record.steps[1].error.message or "")  # type: ignore[union-attr]


def test_a_host_returning_no_tool_result_is_contained() -> None:
    plugins = _flow_plugins([])

    class Host(FixturePlugins):
        async def call(self, ref: str, params: dict[str, Any], input: Any) -> ToolResult:
            return _NotAResult()  # type: ignore[return-value]

    host = Host(plugins=plugins.plugins)
    record = _check(
        asyncio.run(
            execute(
                _pipeline(*FLOW),
                run_id="r",
                context=CONTEXT,
                started_at=START,
                plugins=host,
                tools=host,
                llm=FakeLlm(),
            )
        )
    )
    assert _error_kind(record, 0) == "source_failed"
    assert record.steps[0].output == [
        {"source": "src.read", "params": {}, "error_kind": "output_invalid"}
    ]


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def test_output_depth_and_size_bounds(tmp_path: Path) -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.read"] = lambda p, i: _nested(MAX_DEPTH + 1)
    record = _run(_pipeline(*FLOW), plugins, FakeLlm())
    assert _error_kind(record, 0) == "source_failed"
    assert record.steps[0].output[0]["error_kind"] == "output_invalid"

    plugins.handlers["src.read"] = lambda p, i: "x" * MAX_OUTPUT_BYTES
    record = _run(_pipeline(*FLOW), plugins, FakeLlm())
    assert record.steps[0].output[0]["error_kind"] == "output_too_large"

    # The deepest accepted output survives the store.
    plugins.handlers["src.read"] = lambda p, i: _nested(MAX_DEPTH)
    record = _run(_pipeline({"collect": ["src.read"]}), plugins)
    assert record.status == "ok"
    with Store.open(tmp_path / "state.db") as store:
        store.begin_run(record.id, record.pipeline, START)
        store.save_run(record)
        assert store.get_run(record.id) == record


def test_rejected_intents_fail_the_run_and_record_egress() -> None:
    plugins = _daily_brief_plugins()
    post = {"type": "http.post", "url": "https://evil.example.org/x", "body": {}}
    record = _run(_daily_brief(), plugins, _brief_llm(intents=[PUSH, post]))
    assert record.status == "failed"
    assert _error_kind(record, 2) == "intent_rejected"
    assert record.intents == []
    assert record.input_hash is None

    plugins.add(_manifest("pack-daily-brief", ("http.post",), skills=[SKILL]))
    record = _run(_daily_brief(), plugins, _brief_llm(intents=[post]))
    assert record.status == "failed"
    assert [v.kind for v in record.violations] == ["egress_blocked"]


def test_run_wide_intent_cap() -> None:
    plugins = _flow_plugins([])
    plugins.handlers["src.read"] = lambda p, i: ToolResult(intents=[PUSH] * MAX_RUN_INTENTS)
    plugins.handlers["src.shape"] = lambda p, i: ToolResult(intents=[PUSH])
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF)]))
    assert record.status == "failed"
    assert _error_kind(record, 1) == "run_intents_exceeded"
    assert record.intents == []  # the notify step that would record them never ran


def test_stderr_is_shared_between_calls_head_and_tail() -> None:
    plugins = _daily_brief_plugins()
    huge = "HEAD" + "x" * 100_000 + "TAIL"
    for ref in ("github.review_requests", "calendar.events"):
        plugins.handlers[ref] = _fail("tool_error", "x", stderr=huge)
    record = _run(_daily_brief(), plugins, _brief_llm())
    stderr = record.steps[0].stderr
    assert stderr is not None
    assert len(stderr) <= 65536
    assert stderr.startswith("--- github ---\nHEAD")
    assert "TAIL\n--- calendar ---\nHEAD" in stderr
    assert stderr.endswith("TAIL")


def test_timestamps_never_go_backwards() -> None:
    plugins = _flow_plugins([])
    ticks = itertools.count()

    def backwards() -> datetime.datetime:
        return START - datetime.timedelta(seconds=next(ticks))

    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF)]), clock=backwards)
    assert record.finished_at == START
    assert all(s.finished_at is None or s.started_at <= s.finished_at for s in record.steps)  # type: ignore[operator]


# --- the llm step ----------------------------------------------------------------------------


def test_llm_schema_failure_retries_once_then_degrades() -> None:
    plugins = _flow_plugins([])
    bad = llm_response({"oops": 1}, schema_errors=["items: missing"], intents=[CREATE])
    llm = FakeLlm([bad, llm_response({"still": "bad"}, schema_errors=["items: missing"])])
    record = _run(_pipeline(*FLOW), plugins, llm)
    assert record.status == "degraded"
    assert _statuses(record) == ["ok", "ok", "degraded", "skipped", "skipped"]
    assert _error_kind(record, 2) == "schema_invalid"
    assert [r.attempt for r in llm.requests] == [1, 2]
    assert llm.requests[1].repair == ("items: missing",)
    assert llm.requests[1].input == llm.requests[0].input
    assert llm.requests[1].budget.usd == pytest.approx(0.999)
    assert [c.schema_valid for c in record.llm] == [False, False]
    assert record.intents == []  # invalid output is never applied
    assert record.input_hash is None


def test_llm_repair_succeeds() -> None:
    plugins = _flow_plugins([])
    llm = FakeLlm([llm_response({}, schema_errors=["x"]), llm_response(BRIEF)])
    record = _run(_pipeline(*FLOW), plugins, llm)
    assert record.status == "ok"
    assert [c.attempt for c in record.llm] == [1, 2]
    assert record.steps[2].output == BRIEF
    assert record.cost.tokens_out == 20


@pytest.mark.parametrize(
    ("script", "kind"),
    [
        ([LlmError("backend_unavailable", "503")], "backend_unavailable"),
        ([LlmError("Not A Kind", "503")], "host_error"),
        ([llm_response(BRIEF, tokens_out=100)], "budget_exceeded"),
        ([llm_response({}, schema_errors=["x"], usd=1.0)], "budget_exceeded"),
        ([llm_response({"x": float("inf")})], "output_invalid"),
        ([llm_response({"x": "lone \udfff"})], "output_invalid"),
        ([llm_response(_nested(MAX_DEPTH + 1))], "output_invalid"),
        ([llm_response("x" * (MAX_OUTPUT_BYTES + 1))], "output_too_large"),
    ],
)
def test_llm_failures_degrade(script: list[LlmResponse | LlmError], kind: str) -> None:
    record = _run(_pipeline(*FLOW), _flow_plugins([]), FakeLlm(script))
    assert record.status == "degraded"
    assert _error_kind(record, 2) == kind
    assert _statuses(record)[3:] == ["skipped", "skipped"]
    assert record.input_hash is None


def test_failed_attempt_usage_counts_towards_cost_and_budget() -> None:
    plugins = _flow_plugins([])
    usage = llm_response(None, tokens_out=40, usd=0.4).usage
    llm = FakeLlm([LlmError("backend_error", "cut off", usage=usage)])
    record = _run(_pipeline(*FLOW), plugins, llm)
    assert record.status == "degraded"
    assert record.cost.usd == pytest.approx(0.4)
    assert record.cost.tokens_out == 40


def test_cached_responses_cost_nothing() -> None:
    plugins = _flow_plugins([])
    cached = llm_response(BRIEF, tokens_out=500, usd=5.0, cached=True)  # over budget if counted
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([cached]))
    assert record.status == "ok", record.error
    assert record.llm[0].cached is True
    assert record.cost.usd == 0.0
    assert record.llm[0].usage.tokens_out == 0


def test_one_deadline_covers_both_attempts() -> None:
    clock = _Monotonic(step=0.0)
    seen: list[float] = []

    class Slowish:
        async def complete(self, request: LlmRequest) -> LlmResponse:
            seen.append(request.timeout_s)
            clock.now += 0.75
            return llm_response({}, schema_errors=["x"])

    steps: list[dict[str, Any]] = list(FLOW)
    steps[2] = {"llm": {**LLM, "timeout_s": 1.0}}
    record = _run(_pipeline(*steps), _flow_plugins([]), Slowish(), monotonic=clock)
    assert seen == [pytest.approx(1.0), pytest.approx(0.25)]
    assert record.status == "degraded"

    clock.now, seen[:] = 0.0, []

    class TooSlow(Slowish):
        async def complete(self, request: LlmRequest) -> LlmResponse:
            clock.now += 1.5
            return llm_response({}, schema_errors=["x"])

    record = _run(_pipeline(*steps), _flow_plugins([]), TooSlow(), monotonic=clock)
    assert _error_kind(record, 2) == "timeout"
    assert len(record.llm) == 1  # no second attempt after the deadline


class _SlowLlm:
    async def complete(self, request: LlmRequest) -> LlmResponse:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")


class _BuggyLlm:
    async def complete(self, request: LlmRequest) -> LlmResponse:
        raise KeyError("bug")


class _WrongTypeLlm:
    async def complete(self, request: LlmRequest) -> LlmResponse:
        return {"output": 1}  # type: ignore[return-value]


class _BadModelLlm:
    async def complete(self, request: LlmRequest) -> LlmResponse:
        response = llm_response(BRIEF)
        return LlmResponse(**{**response.__dict__, "model": "has spaces in it"})


@pytest.mark.parametrize(
    ("backend", "kind"),
    [
        (_SlowLlm(), "timeout"),
        (_BuggyLlm(), "host_error"),
        (_WrongTypeLlm(), "output_invalid"),
        (_BadModelLlm(), "output_invalid"),
    ],
)
def test_misbehaving_backends_degrade(backend: Any, kind: str) -> None:
    steps: list[dict[str, Any]] = list(FLOW)
    steps[2] = {"llm": {**LLM, "timeout_s": 0.01}}
    record = _run(_pipeline(*steps), _flow_plugins([]), backend, monotonic=_Monotonic(0.0))
    assert record.status == "degraded"
    assert _error_kind(record, 2) == kind


def test_llm_intents_are_validated_against_the_skill_plugin() -> None:
    plugins = _flow_plugins([])
    plugins.add(_manifest("skills", skills=["skills/rank"]))
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF, intents=[PUSH])]))
    assert record.status == "failed"  # `skills` declares no intents
    assert record.steps[2].error is not None
    assert "'skill:skills/rank'" in record.steps[2].error.message

    plugins.add(_manifest("skills", ("notify.push",), skills=["skills/rank"]))
    record = _run(_pipeline(*FLOW), plugins, FakeLlm([llm_response(BRIEF, intents=[PUSH])]))
    assert record.status == "ok"
    assert record.intents[0].proposed_by == "skills/rank"


# --- unchanged input (D2) --------------------------------------------------------------------


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


def test_unchanged_input_reuses_the_previous_output_and_routes_again() -> None:
    plugins = _daily_brief_plugins()
    first = _run(_daily_brief(), plugins, _brief_llm())
    llm = FakeLlm()
    second = _run(_daily_brief(), plugins, llm, previous=_previous(first))
    assert llm.requests == []
    assert second.status == "ok"
    assert _statuses(second) == ["ok"] * 5
    assert second.input_hash == first.input_hash
    assert second.llm[0].cached is True
    assert second.llm[0].usage.usd == 0.0
    assert second.cost.usd == 0.0
    assert second.steps[2].output == BRIEF
    assert [i.intent for i in second.intents] == [i.intent for i in first.intents]
    # The reused run can be reused again.
    third = _run(_daily_brief(), plugins, FakeLlm(), previous=_previous(second))
    assert third.status == "ok"


def test_unchanged_input_with_nothing_to_apply_is_a_noop() -> None:
    plugins = _daily_brief_plugins()
    first = _run(_daily_brief(), plugins, _brief_llm(intents=[]))
    assert first.status == "ok"
    second = _run(_daily_brief(), plugins, FakeLlm(), previous=_previous(first))
    assert second.status == "noop"
    assert _statuses(second) == ["ok"] * 5


def test_changed_source_data_calls_the_llm() -> None:
    plugins = _daily_brief_plugins()
    plugins.handlers["tasks.normalize"] = lambda p, i: {"data": [s["data"] for s in i]}
    previous = _previous(_run(_daily_brief(), plugins, _brief_llm()))
    plugins.handlers["obsidian.open_tasks"] = lambda p, i: [{"done": "Buy milk"}]
    llm = _brief_llm()
    record = _run(_daily_brief(), plugins, llm, previous=previous)
    assert len(llm.requests) == 1
    assert record.input_hash != previous.input_hash


@pytest.mark.parametrize("change", ["version", "commit", "model_target", "model_alias", "skill"])
def test_cache_key_covers_skill_plugin_and_model(change: str) -> None:
    plugins = _daily_brief_plugins()
    models = {"default": "model-a", "other": "model-a"}
    previous = _previous(_run(_daily_brief(), plugins, _brief_llm(), models=models))
    pipeline = _daily_brief()
    pack = _manifest("pack-daily-brief", ("note.replace_owned_block", "notify.push"))
    if change == "version":
        plugins.add({**pack, "version": "0.2.0", "provides": {"skills": [SKILL]}}, commit="a" * 40)
    elif change == "commit":
        plugins.add({**pack, "provides": {"skills": [SKILL]}}, commit="b" * 40)
    elif change == "model_target":
        models = {"default": "model-b"}
    elif change == "model_alias":
        models = {"default": "model-a"}  # `other` is unknown: the alias itself is hashed
        llm_step = pipeline.steps[2]
        llm_step.llm.model = "other-unknown"  # type: ignore[union-attr]
    else:
        plugins.add({**pack, "provides": {"skills": [SKILL, "daily-brief/other"]}}, commit="a" * 40)
        pipeline.steps[2].llm.skill = "daily-brief/other"  # type: ignore[union-attr]
    llm = _brief_llm()
    record = _run(pipeline, plugins, llm, previous=previous, models=models)
    assert len(llm.requests) == 1, change
    assert record.input_hash != previous.input_hash


def test_same_model_target_keeps_the_key() -> None:
    plugins = _daily_brief_plugins()
    previous = _previous(_run(_daily_brief(), plugins, _brief_llm(), models={"default": "m"}))
    llm = FakeLlm()
    _run(_daily_brief(), plugins, llm, previous=previous, models={"default": "m"})
    assert llm.requests == []


def test_reused_intents_that_no_longer_route_are_a_cache_miss() -> None:
    """The cache key covers the LLM input, not the context: a reused intent with an explicit
    `Daily/<day 1>` path no longer routes to the day-2 entry, so the LLM is called."""
    plugins = _daily_brief_plugins()
    explicit = {**NOTE, "path": NOTE_PATH}
    first = _run(_daily_brief(), plugins, _brief_llm(intents=[explicit, PUSH]))
    assert first.status == "ok", first.error
    day2 = make_context(START + datetime.timedelta(days=1), "Europe/Paris")
    llm = _brief_llm()
    second = _run(_daily_brief(), plugins, llm, previous=_previous(first), context=day2)
    assert len(llm.requests) == 1
    assert second.status == "ok", second.error
    assert second.input_hash == first.input_hash
    assert [c.cached for c in second.llm] == [False]  # the reuse attempt is not recorded
    assert second.violations == []
    assert second.intents[0].intent.model_dump()["path"] == "Daily/2026-10-28.assistant.md"


def test_unusable_previous_output_calls_the_llm() -> None:
    plugins = _daily_brief_plugins()
    first = _run(_daily_brief(), plugins, _brief_llm())
    previous = _previous(first)
    broken = PreviousLlm(**{**previous.__dict__, "output": {"x": float("nan")}})
    llm = _brief_llm()
    record = _run(_daily_brief(), plugins, llm, previous=broken)
    assert len(llm.requests) == 1
    assert record.llm[0].cached is False


def _store_run(
    store: Store, run_id: str, *, llm: FakeLlm, dry_run: bool = False, **kwargs: Any
) -> RunRecord:
    """One real run through the store, as #19 will do it."""
    started = START + datetime.timedelta(minutes=len(store.list_runs()))
    store.begin_run(run_id, "daily-brief", started, dry_run=dry_run)
    record = _check(
        asyncio.run(
            execute(
                _daily_brief(),
                run_id=run_id,
                context=CONTEXT,
                started_at=started,
                plugins=kwargs.get("plugins") or _daily_brief_plugins(),
                tools=kwargs.get("plugins") or _daily_brief_plugins(),
                llm=llm,
                dry_run=dry_run,
                previous=store.last_accepted_llm("daily-brief"),
            )
        )
    )
    if "status" in kwargs:  # e.g. the apply step (#21/#22) failed
        record = record.model_copy(update={"status": kwargs["status"]})
    store.save_run(record)
    return record


def test_store_previous_output_is_reused(tmp_path: Path) -> None:
    with Store.open(tmp_path / "state.db") as store:
        assert store.last_accepted_llm("daily-brief") is None
        first = _store_run(store, "r1", llm=_brief_llm())
        assert store.get_run("r1") == first
        llm = FakeLlm()
        second = _store_run(store, "r2", llm=llm)
        assert llm.requests == []
        assert second.llm[0].cached is True


@pytest.mark.parametrize("case", ["degraded", "dry", "failed"])
def test_store_reruns_after_an_unaccepted_run(tmp_path: Path, case: str) -> None:
    with Store.open(tmp_path / "state.db") as store:
        if case == "degraded":
            plugins = _daily_brief_plugins()
            plugins.handlers["coach.brief"] = _fail()
            first = _store_run(store, "r1", llm=_brief_llm(), plugins=plugins)
            assert first.status == "degraded"
        elif case == "dry":
            _store_run(store, "r1", llm=_brief_llm(), dry_run=True)
        else:
            _store_run(store, "r1", llm=_brief_llm(), status="failed")
        assert store.last_accepted_llm("daily-brief") is None
        llm = _brief_llm()
        _store_run(store, "r2", llm=llm)
        assert len(llm.requests) == 1


# --- planning --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "kind", "index"),
    [
        ([{"collect": ["nobody.read"]}], "plugin_unknown", 0),
        ([{"collect": ["src.read"]}, {"act": ["src.shape"]}], "tool_not_provided", 1),
        ([{"act": [{"sink.write": {"p": "{{nope}}"}}]}], "template_error", 0),
        ([{"collect": [{"src.read": {"{{date}}": 1}}]}], "template_error", 0),
        ([{"collect": [{"src.read": {"x": float("nan")}}]}], "params_invalid", 0),
        ([{"collect": [{"src.read": {"x": [float("inf")]}}]}], "params_invalid", 0),
        ([{"collect": [{"src.read": {"x": START}}]}], "params_invalid", 0),
        ([{"collect": ["src.read"]}, {"llm": LLM}], "llm_unavailable", 1),
        ([{"llm": {**LLM, "skill": "nobody/rank"}}], "plugin_unknown", 0),
        ([{"llm": {**LLM, "skill": "skills/other"}}], "tool_not_provided", 0),
        ([{"collect": ["src.read"] * 200}, {"collect": ["src.read"] * 57}], "plan_too_large", 1),
    ],
)
def test_planning_errors_fail_before_any_call(
    steps: list[dict[str, Any]], kind: str, index: int
) -> None:
    plugins = _flow_plugins([])
    has_llm = kind != "llm_unavailable"
    record = _run(_pipeline(*steps), plugins, FakeLlm() if has_llm else None)
    assert record.status == "failed"
    assert plugins.calls == []
    assert _error_kind(record, index) == kind
    assert [s.status for i, s in enumerate(record.steps) if i != index] == ["skipped"] * (
        len(steps) - 1
    )


def test_too_many_plugins_is_a_plan_error() -> None:
    plugins = FixturePlugins()
    for n in range(257):
        plugins.add(_manifest(f"p{n}", sources=[f"p{n}.read"]), {f"p{n}.read": lambda p, i: 1})
    record = _run(
        _pipeline(
            {"collect": [f"p{n}.read" for n in range(200)]},
            {"collect": [f"p{n}.read" for n in range(200, 257)]},
        ),
        plugins,
    )
    assert _error_kind(record, 1) == "plan_too_large"
    assert plugins.calls == []


def test_context_vars_cannot_override_now_or_timezone() -> None:
    context = CONTEXT.model_copy(update={"vars": {"date": "2026-10-27", "now": "never"}})
    plugins = _flow_plugins([])
    record = _check(
        asyncio.run(
            execute(
                _pipeline({"collect": ["src.read"]}),
                run_id="r",
                context=context,
                started_at=START,
                plugins=plugins,
                tools=plugins,
            )
        )
    )
    assert _error_kind(record, 0) == "template_error"


# --- hooks, cancellation, the store ----------------------------------------------------------


def test_on_step_sees_every_step_in_order(tmp_path: Path) -> None:
    plugins = _daily_brief_plugins()
    seen: list[tuple[int, str]] = []
    with Store.open(tmp_path / "state.db") as store:
        store.begin_run("r1", "daily-brief", START)

        async def hook(index: int, step: StepRecord) -> None:
            seen.append((index, step.kind))
            store.record_step("r1", index, step)

        record = _run(_daily_brief(), plugins, _brief_llm(), on_step=hook)
        assert seen == [(i, s.kind) for i, s in enumerate(record.steps)]
        assert store.steps("r1") == record.steps
        store.save_run(record.model_copy(update={"id": "r1"}))
        assert store.get_run("r1") is not None


def test_cancellation_carries_the_partial_record() -> None:
    plugins = _daily_brief_plugins()
    started = asyncio.Event()

    async def scenario() -> RunRecord:
        class Blocking:
            async def complete(self, request: LlmRequest) -> LlmResponse:
                started.set()
                await asyncio.sleep(60)
                raise AssertionError("unreachable")

        task = asyncio.create_task(
            execute(
                _daily_brief(),
                run_id="r",
                context=CONTEXT,
                started_at=START,
                plugins=plugins,
                tools=plugins,
                llm=Blocking(),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(RunCancelled) as raised:
            await task
        assert isinstance(raised.value, asyncio.CancelledError)
        return raised.value.record

    record = _check(asyncio.run(scenario()))
    assert record.status == "failed"
    assert _statuses(record) == ["ok", "ok", "failed", "skipped", "skipped"]
    assert _error_kind(record, 2) == "cancelled"


def test_store_needs_an_accepted_llm_step(tmp_path: Path) -> None:
    plugins = _flow_plugins([])
    with_llm = _pipeline({"collect": ["src.read"]}, {"llm": LLM})
    no_llm = _run(_pipeline({"collect": ["src.read"]}), plugins)
    invalid = llm_response({}, schema_errors=["x"])
    degraded = _run(with_llm, plugins, FakeLlm([invalid, invalid]))
    ok = _run(with_llm, plugins, FakeLlm([llm_response(BRIEF)]))
    forged = degraded.model_copy(update={"status": "ok", "input_hash": "0" * 64})
    elsewhere = ok.model_copy(update={"llm": [ok.llm[0].model_copy(update={"step": 5})]})
    for n, record in enumerate([no_llm, forged, elsewhere]):
        with Store.open(tmp_path / f"state-{n}.db") as store:
            store.save_run(record)
            assert store.last_accepted_llm("test") is None


def test_route_fields_go_together() -> None:
    with pytest.raises(ValidationError, match="go together"):
        ProposedIntent.model_validate(
            {"step": 0, "proposed_by": "a.b", "intent": PUSH, "applied_by": "c.d"}
        )


def _brief_record() -> RunRecord:
    return _run(_daily_brief(), _daily_brief_plugins(), _brief_llm())


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"step": 9}, "step 9 is out of range"),
        ({"applied_by_step": 2}, "not a later step"),
        ({"applied_by_step": 9}, "not a later step"),
        ({"applied_by_step": 4}, "must be applied by a step of kind act"),
        ({"applied_by_call": 1}, "does not list it"),
        ({"applied_by": "ntfy.digest"}, "does not list it"),
    ],
)
def test_run_record_rejects_impossible_routes(change: dict[str, Any], match: str) -> None:
    data = json.loads(_brief_record().model_dump_json())
    data["intents"][0].update(change)
    with pytest.raises(ValidationError, match=match):
        RunRecord.model_validate(data)


def test_run_record_entry_must_list_its_intent() -> None:
    data = json.loads(_brief_record().model_dump_json())
    data["steps"][4]["output"][0]["intents"] = []
    with pytest.raises(ValidationError, match="intent 1: entry 0 of step 4 does not list it"):
        RunRecord.model_validate(data)
    data["intents"][1]["intent"] = NOTE | {"path": NOTE_PATH}
    with pytest.raises(ValidationError, match="must be applied by a step of kind act"):
        RunRecord.model_validate(data)
    # A skipped entry step records no entries: only the step and kind are checked.
    data = json.loads(_brief_record().model_dump_json())
    data["steps"][3]["output"] = None
    RunRecord.model_validate(data)


def test_store_ignores_an_unreadable_or_intentless_record(tmp_path: Path) -> None:
    with Store.open(tmp_path / "state.db") as store:
        record = _store_run(store, "r1", llm=_brief_llm())
        assert store.last_accepted_llm("daily-brief") is not None
        broken = json.loads(record.model_dump_json())
        broken["intents"][0]["applied_by_step"] = 0
        store._conn.execute("UPDATE runs SET record = ? WHERE id = ?", (json.dumps(broken), "r1"))
        assert store.last_accepted_llm("daily-brief") is None

        legacy = record.model_copy(
            update={"llm": [record.llm[0].model_copy(update={"intents": None})]}
        )
        store.save_run(legacy)
        assert store.last_accepted_llm("daily-brief") is None
