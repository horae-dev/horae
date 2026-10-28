# SPDX-License-Identifier: Apache-2.0
"""Schema-validated LLM output with one repair retry (#31, schemas decision 44).

Acceptance: invalid output is never applied; the repair prompt includes only validation
errors, not new data; the degraded status is visible in `horae runs`."""

import asyncio
import datetime
import io
import json
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from typer.testing import CliRunner

from budget_fixtures import FakeProvider, Prompts
from budget_fixtures import write_instance as write_llm_instance
from horae.cli import app
from horae.config import load_instance
from horae.contracts import Pipeline, RunRecord, Usage
from horae.contracts.instance import ModelAlias
from horae.engine import execute, make_context
from horae.engine.testing import FixturePlugins
from horae.llm import ModelBackends
from horae.llm import validate as validate_module
from horae.llm._schema_errors import (
    MAX_ERROR_CHARS,
    MAX_ERRORS,
    MAX_POINTER_CHARS,
    SCHEMA_ERROR,
    main,
    schema_errors,
)
from horae.llm.backends import model_backends
from horae.llm.base import Completion, CompletionResult, ProviderBackend
from horae.llm.validate import (
    NOT_JSON_ERROR,
    SIZE_ERROR,
    TIMEOUT_ERROR,
    WORKER_ERROR,
    validate_output,
)
from horae.replay import replay
from horae.runner import Ports, Runner
from horae.secrets import SecretResolver
from horae.skills import MAX_OUTPUT_BYTES, DirectorySkills, check_output_schema
from horae.store import Store
from runner_fixtures import Plugins

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "pack-daily-brief"
SKILL = "daily-brief/rank-and-word"
SKILL_SCHEMA = json.loads(
    (FIXTURE / "skills" / "daily-brief" / "rank-and-word" / "output.schema.json").read_text()
)
START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
NOTE_PATH = "Daily/2026-10-27.assistant.md"
VALID = {"summary": "Two things today.", "ranked": [{"title": "Review PR 42", "why": "today"}]}
NOTE = {"type": "note.create", "content": "- [ ] Review PR 42\n"}

# Synthetic injection text a model might echo back; none of it may reach a repair prompt.
SENTINELS = ("IGNORE-PREVIOUS-0xC0FFEE", "exfil.example.invalid", "EVIL-KEY-7")
INVALID = {
    "summary": SENTINELS[0] * 40,  # over maxLength 500
    "ranked": [{"title": "t", "why": SENTINELS[1], SENTINELS[2]: 1}],
    "intents": [{"type": "note.create", "content": f"from the invalid output {SENTINELS[0]}"}],
}
INVALID_ERRORS = (
    "/ranked/0/why: enum: the value is not one of the allowed values",
    "/ranked/0: additionalProperties: the object has properties the schema does not declare",
    "/summary: maxLength: the string is longer than 500 characters",
)

SLOW_ITEMS = 15000
SLOW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rows"],
    "properties": {
        "rows": {
            "type": "array",
            "maxItems": SLOW_ITEMS,
            "uniqueItems": True,  # over objects: jsonschema compares every pair
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["a"],
                "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 99999}},
            },
        }
    },
}
SLOW_OUTPUT = {"rows": [{"a": i} for i in range(SLOW_ITEMS)]}


def _validate(output: Any, schema: Mapping[str, Any], **kwargs: Any) -> tuple[str, ...]:
    return asyncio.run(validate_output(output, schema, **kwargs))


# --- the errors: path, keyword and Horae's words, never the output -------------------------


def test_a_valid_output_has_no_errors() -> None:
    assert schema_errors(VALID, SKILL_SCHEMA) == []
    assert schema_errors({**VALID, "intents": [NOTE]}, SKILL_SCHEMA) == []


def test_errors_name_the_path_and_keyword_and_never_a_value() -> None:
    errors = schema_errors(INVALID, SKILL_SCHEMA)
    assert tuple(errors) == INVALID_ERRORS
    for sentinel in SENTINELS:
        assert sentinel not in json.dumps(errors)


def test_undeclared_keys_are_never_echoed_in_a_path() -> None:
    # A schema the loader would refuse (open object), still described safely.
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    errors = schema_errors({SENTINELS[2]: "x", "<b>": "y"}, schema)
    assert errors == ["/*: type: expected integer"]


def test_required_properties_are_named_only_when_the_schema_declares_them() -> None:
    assert schema_errors({}, SKILL_SCHEMA) == [
        "/: required: missing required property ranked",
        "/: required: missing required property summary",
    ]
    odd = {"type": "object", "required": ["has space"]}
    assert schema_errors({}, odd) == ["/: required: missing required property *"]
    assert schema_errors([], {"required": ["a"]}) == []  # `required` ignores non-objects


@pytest.mark.parametrize(
    ("schema", "output", "error"),
    [
        ({"type": ["string", "null"]}, 1, "/: type: expected null or string"),
        ({"type": "integer", "minimum": 3}, 1, "/: minimum: the number is below 3"),
        (
            {"type": "number", "maximum": 10**40},
            10**41,
            "/: maximum: the number is above the schema's limit",
        ),
        (
            {"type": "number", "multipleOf": 0.5},
            0.3,
            "/: multipleOf: the number is not a multiple of 0.5",
        ),
        ({"type": "array", "maxItems": 1}, [1, 2], "/: maxItems: the array has more than 1 items"),
        ({"const": "x"}, "y", "/: const: the value is not the allowed constant"),
        (
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            1,
            "/: anyOf: the value matches none of the allowed alternatives",
        ),
        (
            {"type": "array", "maxItems": 2, "prefixItems": [{"type": "integer"}], "items": False},
            [1, 2],
            "/: items: the array has items the schema does not allow",
        ),
        ({"not": {"type": "integer"}}, 1, "/: not: the value matches a schema it must not match"),
        (
            {"dependentRequired": {"a": ["b"]}},
            {"a": 1},
            "/: dependentRequired: a property that another property requires is missing",
        ),
        (
            {"type": "string", "minLength": 2},
            "a",
            "/: minLength: the string is shorter than 2 characters",
        ),
        (
            {"unevaluatedProperties": False},
            {"k": 1},
            "/: unevaluatedProperties: the object has properties the schema does not declare",
        ),
        ({"properties": {"a": False}}, {"a": 1}, "/: false: no value is allowed here"),
        (
            {"type": "array", "maxItems": 9, "uniqueItems": True},
            [1, 1],
            "/: uniqueItems: the array items are not unique",
        ),
    ],
)
def test_each_keyword_has_a_fixed_description(schema: Any, output: Any, error: str) -> None:
    assert schema_errors(output, schema) == [error]


def test_errors_are_sorted_deduplicated_and_capped() -> None:
    schema = {
        "type": "object",
        "properties": {"v": {"type": "array", "items": {"type": "string", "maxLength": 1}}},
    }
    output = {"v": [SENTINELS[0]] * 40}
    errors = schema_errors(output, schema)
    assert len(errors) == MAX_ERRORS
    assert errors[-1] == f"/: more: {40 - MAX_ERRORS + 1} more errors not shown"
    assert errors[:-1] == sorted(errors[:-1])
    assert errors == schema_errors(output, schema)  # deterministic
    assert all(len(e) <= MAX_ERROR_CHARS for e in errors)
    # A deep path is cut, keeping whole segments.
    deep: dict[str, Any] = {"type": "integer"}
    value: Any = "x"
    for _ in range(40):
        deep = {"type": "object", "properties": {"segment-name": deep}}
        value = {"segment-name": value}
    (cut,) = schema_errors(value, deep)
    pointer = cut.split(": ", 1)[0]
    assert len(pointer) <= MAX_POINTER_CHARS
    assert pointer.endswith("/segment-name/...")


def test_the_reserved_intents_must_be_an_array_of_objects() -> None:
    open_schema: dict[str, Any] = {"type": "object"}
    assert schema_errors({"intents": "x"}, open_schema) == [
        "/intents: type: the reserved intents property must be an array"
    ]
    assert schema_errors({"intents": [{}, 3]}, open_schema) == [
        "/intents/1: type: an intent must be an object"
    ]
    # A skill that declares no intents: the property is not declared at all.
    without = {**SKILL_SCHEMA, "properties": dict(SKILL_SCHEMA["properties"])}
    del without["properties"]["intents"]
    assert schema_errors({**VALID, "intents": [NOTE]}, without) == [
        "/: additionalProperties: the object has properties the schema does not declare"
    ]


def test_an_unusable_schema_is_one_fixed_error() -> None:
    assert schema_errors({}, {"type": 3}) == [SCHEMA_ERROR]


def test_the_worker_entry_point() -> None:
    # The CPU ceiling is the child's own: never set on this (the test) process.
    limits: list[int] = []
    out = io.BytesIO()
    request = {"output": {}, "schema": SKILL_SCHEMA, "cpu_s": 5}
    assert main(io.BytesIO(json.dumps(request).encode()), out, limits.append) == 0
    assert json.loads(out.getvalue()) == schema_errors({}, SKILL_SCHEMA)
    assert limits == [5]
    for bad in (b"nope", b"{}", json.dumps({**request, "cpu_s": 0}).encode()):
        assert main(io.BytesIO(bad), io.BytesIO(), limits.append) == 2
    schema_list = json.dumps({**request, "schema": []}).encode()
    assert main(io.BytesIO(schema_list), io.BytesIO(), limits.append) == 2
    assert limits == [5]


# --- the killable worker ----------------------------------------------------------------------


def test_the_worker_gives_the_in_process_errors() -> None:
    assert _validate(INVALID, SKILL_SCHEMA) == INVALID_ERRORS
    assert _validate(VALID, SKILL_SCHEMA) == ()


def _no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no worker should be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)


def test_an_oversized_output_is_refused_without_a_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_worker(monkeypatch)
    # Measured as the loader bounds it: UTF-8 bytes of compact JSON (one é is two bytes).
    big = {"summary": "é" * (MAX_OUTPUT_BYTES // 2)}
    assert _validate(big, SKILL_SCHEMA) == (SIZE_ERROR,)
    assert _validate({"x": float("nan")}, SKILL_SCHEMA) == (NOT_JSON_ERROR,)


def test_a_slow_validation_is_killed_and_the_loop_stays_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert check_output_schema(SLOW_SCHEMA) <= MAX_OUTPUT_BYTES  # a schema skills may use
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)

    async def go() -> tuple[tuple[str, ...], int, float]:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        task = asyncio.create_task(ticker())
        begin = time.monotonic()
        errors = await validate_output(SLOW_OUTPUT, SLOW_SCHEMA, timeout_s=2.0)
        elapsed = time.monotonic() - begin
        task.cancel()
        return errors, ticks, elapsed

    errors, ticks, elapsed = asyncio.run(go())
    assert errors == (TIMEOUT_ERROR,)
    assert 2.0 <= elapsed < 6.0
    assert ticks >= 20  # the loop kept running while the worker did
    (process,) = started
    assert process.returncode is not None  # killed and reaped


def test_cancelling_the_validation_kills_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def tracked(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)

    async def go() -> None:
        task = asyncio.create_task(validate_output(SLOW_OUTPUT, SLOW_SCHEMA, timeout_s=60))
        while not started:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert started[0].returncode is not None


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.exit(1)",
        "print('not json')",
        "print('{}')",
        f"import json; print(json.dumps(['x'] * {MAX_ERRORS + 1}))",
        f"import json; print(json.dumps(['x' * {MAX_ERROR_CHARS + 1}]))",
        "import sys; sys.stdout.write('[' + '\"x\",' * 30000 + '\"x\"]')",
    ],
    ids=["exit-1", "garbage", "not-a-list", "too-many", "too-long", "too-big"],
)
def test_a_failing_worker_is_an_error_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(f"import sys\nsys.stdin.read()\n{script}\n", encoding="utf-8")
    monkeypatch.setattr(validate_module, "WORKER", worker)
    assert _validate(VALID, SKILL_SCHEMA) == (WORKER_ERROR,)


def test_a_worker_that_cannot_start_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("synthetic")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", broken)
    assert _validate(VALID, SKILL_SCHEMA) == (WORKER_ERROR,)


def test_the_production_backends_install_the_validator() -> None:
    llm = model_backends({}, SecretResolver({}, Path("/nonexistent"), {}))
    assert llm.validate is validate_output


# --- through ModelBackends and the engine: nothing of an invalid output is applied ------------


ALIAS = {"backend": "anthropic", "model": "fixture-model", "secret": "ANTHROPIC_API_KEY"}


class _Provider:
    """A provider answering `outputs` in order; it records every completion it renders."""

    name: Literal["anthropic"] = "anthropic"
    may_write_cache: bool = False

    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.completions: list[Completion] = []

    def render(self, completion: Completion) -> dict[str, Any]:
        self.completions.append(completion)
        return {"input": completion.input, "repair": list(completion.repair)}

    async def send(self, payload: Any, timeout_s: float) -> CompletionResult:
        return CompletionResult(self.outputs.pop(0), Usage(tokens_in=10, tokens_out=5, usd=0.0))

    async def aclose(self) -> None:
        return None


def _manifest(name: str, intents: tuple[str, ...] = (), **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0",
        "provides": provides,
        "permissions": {"intents": list(intents)},
    }


def _plugins() -> FixturePlugins:
    plugins = FixturePlugins()
    plugins.add(
        _manifest("tasks", sources=["tasks.today"], transforms=["tasks.merge"]),
        {
            "tasks.today": lambda p, i: {"tasks": [{"title": "Review PR 42"}]},
            "tasks.merge": lambda p, i: i[0]["data"],
        },
    )
    plugins.add(_manifest("pack-daily-brief", ("note.create",), skills=[SKILL]), commit="b" * 40)
    plugins.add(_manifest("obsidian", ("note.create",), sinks=["obsidian.write_note"]))
    return plugins


def _pipeline() -> Pipeline:
    return Pipeline.model_validate(
        {
            "name": "brief",
            "steps": [
                {"collect": ["tasks.today"]},
                {"transform": ["tasks.merge"]},
                {
                    "llm": {
                        "skill": SKILL,
                        "model": "default",
                        "budget": {"usd": 0.05, "tokens_out": 2000},
                    }
                },
                {"act": [{"obsidian.write_note": {"path": NOTE_PATH, "mode": "create"}}]},
            ],
        }
    )


@pytest.fixture
def skills(tmp_path: Path) -> DirectorySkills:
    root = tmp_path / "pack-daily-brief"
    shutil.copytree(FIXTURE, root)
    return DirectorySkills({"pack-daily-brief": root})


def _execute(
    provider: _Provider, skills: DirectorySkills, plugins: FixturePlugins, tmp_path: Path
) -> RunRecord:
    backends = ModelBackends(
        {"default": ModelAlias.model_validate(ALIAS)},
        SecretResolver({}, tmp_path),
        {"anthropic": lambda alias, spec, secrets: provider},
        validate=validate_output,
    )
    record = asyncio.run(
        execute(
            _pipeline(),
            run_id="20261027T053000Z-brief-31",
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=backends,
            skills=skills,
        )
    )
    RunRecord.model_validate_json(record.model_dump_json())
    return record


def test_an_output_invalid_twice_is_never_applied(skills: DirectorySkills, tmp_path: Path) -> None:
    plugins = _plugins()
    provider = _Provider(INVALID, {**INVALID, "summary": 7})
    record = _execute(provider, skills, plugins, tmp_path)
    _collect, _transform, llm_step, act = record.steps
    assert (record.status, llm_step.status, act.status) == ("degraded", "degraded", "skipped")
    assert llm_step.error is not None
    assert llm_step.error.kind == "schema_invalid"
    # Nothing of the step is applied: no intent recorded or routed, no sink called.
    assert record.intents == []
    assert plugins.refs == ["tasks.today", "tasks.merge"]
    first, second = record.llm
    assert (first.schema_valid, second.schema_valid) == (False, False)
    assert first.schema_errors == list(INVALID_ERRORS)
    assert second.schema_errors == [
        "/ranked/0/why: enum: the value is not one of the allowed values",
        "/ranked/0: additionalProperties: the object has properties the schema does not declare",
        "/summary: type: expected string",
    ]
    # The invalid output's intents are dropped, not even kept as proposals.
    assert first.intents == []
    assert second.intents == []
    assert SENTINELS[0] not in json.dumps([i.model_dump(mode="json") for i in record.intents])


def test_the_repair_prompt_holds_only_the_validation_errors(
    skills: DirectorySkills, tmp_path: Path
) -> None:
    plugins = _plugins()
    provider = _Provider(INVALID, {**VALID, "intents": [NOTE]})
    record = _execute(provider, skills, plugins, tmp_path)
    assert record.status == "ok", record.steps[2].error
    # Each attempt is rendered twice (the input bound, then the payload sent).
    assert [c.repair for c in provider.completions] == [(), (), INVALID_ERRORS, INVALID_ERRORS]
    first, second = provider.completions[1], provider.completions[3]
    # Exactly the errors, nothing of the output, and the same input as the first attempt.
    assert second.repair == INVALID_ERRORS
    assert second.input == first.input
    assert second.system == first.system
    sent = json.dumps(provider.completions[-1].repair)
    for sentinel in SENTINELS:
        assert sentinel not in sent
    assert record.llm[1].repair == list(INVALID_ERRORS)
    # Only the repaired output's intent is routed and applied.
    assert [i.intent.model_dump(mode="json")["content"] for i in record.intents] == [
        NOTE["content"]
    ]
    assert [i.applied_by for i in record.intents] == ["obsidian.write_note"]


def test_a_repaired_run_replays_identically(skills: DirectorySkills, tmp_path: Path) -> None:
    plugins = _plugins()
    record = _execute(_Provider(INVALID, {**VALID, "intents": [NOTE]}), skills, plugins, tmp_path)
    assert record.status == "ok"
    result = replay(record, _pipeline(), plugins=plugins, skills=skills)
    assert result.outcome == "identical", (result.mismatches, result.summary)
    degraded = _execute(_Provider(INVALID, INVALID), skills, _plugins(), tmp_path)
    again = replay(degraded, _pipeline(), plugins=_plugins(), skills=skills)
    assert again.outcome == "identical", (again.mismatches, again.summary)


# --- through the runner: the budget split, the ledger and `horae runs` -------------------------


def _runner_backends(
    models: Mapping[str, ModelAlias], made: list[FakeProvider], **kwargs: Any
) -> ModelBackends:
    def factory(alias: str, spec: ModelAlias, secrets: SecretResolver) -> ProviderBackend:
        fake = FakeProvider(spec, **kwargs)
        made.append(fake)
        return fake

    return ModelBackends(
        models,
        SecretResolver({}, Path("/nonexistent"), {}),
        {"anthropic": factory},
        Prompts(),
        validate_output,
    )


def _store_run(tmp_path: Path, script: list[Any]) -> tuple[RunRecord, FakeProvider, Path]:
    root = write_llm_instance(tmp_path / "instance", ("brief-a",))
    loaded = load_instance(root)
    made: list[FakeProvider] = []
    llm = _runner_backends(loaded.config.models, made, script=script, tokens_out=50)
    plugins = Plugins()
    with Store.open(loaded.store_path) as opened:
        record = asyncio.run(
            Runner(loaded, opened, Ports(plugins, plugins, llm=llm)).run("brief-a")
        )
        assert sum(g.calls for g in opened.usage_report()) == len(made[0].sent)
    return record, made[0], root


def test_the_repair_gets_the_remaining_budget_and_degrades_visibly(tmp_path: Path) -> None:
    # FakeProvider answers `{"items": "not a list", ...}` for a list item: invalid twice.
    record, provider, root = _store_run(tmp_path, [["first"], ["second"]])
    assert record.status == "degraded"
    step = record.steps[1]
    assert step.error is not None
    assert step.error.kind == "schema_invalid"
    assert record.llm[0].schema_errors == ["/items: type: expected array"]
    first, second = provider.sent
    assert first["repair"] == []
    assert second["repair"] == ["/items: type: expected array"]  # the errors, nothing else
    assert second["input"] == first["input"]
    # The repair attempt is given what the first one left of the step budget (decision 41).
    assert (first["max_tokens"], second["max_tokens"]) == (2000, 1950)
    assert record.cost.tokens_out == 100
    # `horae runs` shows the degraded run, in the table and in JSON.
    runs = CliRunner()
    result = runs.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == 0, result.output
    (row,) = [line for line in result.output.splitlines() if record.id in line]
    assert row.split()[1:3] == ["brief-a", "degraded"]
    result = runs.invoke(app, ["runs", "-i", str(root), "--status", "degraded", "--json"])
    assert result.exit_code == 0, result.output
    (listed,) = json.loads(result.output)["runs"]
    assert (listed["id"], listed["status"]) == (record.id, "degraded")


def test_a_repair_that_validates_is_ok_through_the_runner(tmp_path: Path) -> None:
    record, provider, _ = _store_run(tmp_path, [["first"], None])
    assert record.status == "ok", record.steps[2].error
    assert [call.schema_valid for call in record.llm] == [False, True]
    assert len(provider.sent) == 2
