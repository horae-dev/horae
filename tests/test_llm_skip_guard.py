# SPDX-License-Identifier: Apache-2.0
"""`skip_when_empty` on an `llm` step and a skill's `task_fingerprint` rule (#160, decision 67)."""

import asyncio
import datetime
import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.contracts import Pipeline, RunRecord, SkillManifest
from horae.contracts.intents import TaskCreate
from horae.engine import PreviousLlm, execute, make_context
from horae.engine.guard import check_intents
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.skills import DirectorySkills, skill_dir

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "pack-daily-brief"
SKILL = "daily-brief/rank-and-word"
START = datetime.datetime(2026, 10, 27, 5, 30, tzinfo=datetime.UTC)
CONTEXT = make_context(START, "Europe/Paris")
RULE = "task_fingerprint:\n  ids: items[].id\n  prefix: 'gmail:'\n"
OUTPUT = {"summary": "ok"}


def _create(fingerprint: str) -> dict[str, Any]:
    return {"type": "task.create", "title": "Reply", "fingerprint": fingerprint}


class _Cache:
    def __init__(self) -> None:
        self.looked_up = 0

    async def lookup(
        self, key: str, *, model_alias: str, output_schema: Mapping[str, Any] | None
    ) -> PreviousLlm | None:
        self.looked_up += 1
        return None

    async def discard(self, key: str, *, model_alias: str) -> None: ...


def _manifest(name: str, intents: tuple[str, ...] = (), **provides: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.1.0",
        "horae": ">=0",
        "provides": provides,
        "permissions": {"intents": list(intents)},
    }


def _plugins(data: Any) -> FixturePlugins:
    plugins = FixturePlugins()
    plugins.add(
        _manifest("tasks", sources=["tasks.today"], transforms=["tasks.merge"]),
        {"tasks.today": lambda p, i: data, "tasks.merge": lambda p, i: i[0]["data"]},
    )
    plugins.add(_manifest("pack-daily-brief", ("task.create",), skills=[SKILL]), commit="b" * 40)
    plugins.add(_manifest("todo", ("task.create", "task.update_status"), sinks=["todo.write"]))
    return plugins


def _pack(tmp_path: Path, rule: str = "") -> DirectorySkills:
    root = tmp_path / "pack-daily-brief"
    shutil.copytree(FIXTURE, root)
    folder = skill_dir(root, SKILL)
    (folder / "skill.yaml").write_text(
        "name: daily-brief/rank-and-word\nversion: 1.0.0\ndescription: Triage.\n"
        "input_view:\n  - items[].id\n  - items[].subject\n  - note\n"
        f"intents: [task.create]\n{rule}"
    )
    schema = json.loads((folder / "output.schema.json").read_text())
    schema["properties"]["intents"] = {
        "type": "array",
        "maxItems": 4,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "title", "fingerprint"],
            "properties": {
                "type": {"type": "string", "enum": ["task.create"]},
                "title": {"type": "string", "maxLength": 100},
                "fingerprint": {"type": "string", "maxLength": 100},
            },
        },
    }
    (folder / "output.schema.json").write_text(json.dumps(schema))
    return DirectorySkills({"pack-daily-brief": root})


def _run(
    skills: DirectorySkills,
    data: Any,
    llm: FakeLlm,
    skip: Any = None,
    cache: _Cache | None = None,
) -> RunRecord:
    spec: dict[str, Any] = {
        "skill": SKILL,
        "model": "default",
        "budget": {"usd": 0.05, "tokens_out": 2000},
    }
    if skip is not None:
        spec["skip_when_empty"] = skip
    pipeline = Pipeline.model_validate(
        {
            "name": "brief",
            "steps": [
                {"collect": ["tasks.today"]},
                {"transform": ["tasks.merge"]},
                {"llm": spec},
                {"act": [{"todo.write": {"mode": "tasks"}}]},
            ],
        }
    )
    plugins = _plugins(data)
    record = asyncio.run(
        execute(
            pipeline,
            run_id="20261027T053000Z-brief-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=skills,
            cache=cache,
        )
    )
    RunRecord.model_validate_json(record.model_dump_json())
    return record


EMPTY = {"items": [], "note": ""}
FULL = {"items": [{"id": "m1", "subject": "Hi"}], "note": ""}


# --- skip_when_empty ---------------------------------------------------------------------------


def test_an_empty_input_skips_the_step_with_no_call_and_no_cache(tmp_path: Path) -> None:
    llm, cache = FakeLlm([]), _Cache()
    record = _run(_pack(tmp_path), EMPTY, llm, skip=True, cache=cache)
    assert llm.requests == []
    assert cache.looked_up == 0
    assert record.llm == []
    assert record.input_hash is None  # nothing was accepted, so nothing can be cached
    assert record.cost.usd == 0
    assert record.steps[2].status == "skipped"
    assert record.steps[2].output == {"skipped": "empty_input", "skill": SKILL}
    assert record.steps[2].error is None
    assert record.steps[3].status == "ok"
    assert (record.status, record.intents) == ("noop", [])


def test_a_non_empty_input_still_calls_the_model(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[_create("gmail:m1")])])
    record = _run(_pack(tmp_path), FULL, llm, skip=True)
    assert len(llm.requests) == 1
    assert record.steps[2].status == "ok"
    assert record.status == "ok"
    assert len(record.llm) == 1


def test_without_the_option_an_empty_input_still_calls_the_model(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[])])
    record = _run(_pack(tmp_path), EMPTY, llm)
    assert len(llm.requests) == 1
    assert record.steps[2].status == "ok"


def test_listed_paths_decide_when_other_fields_are_not_empty(tmp_path: Path) -> None:
    data = {"items": [], "note": "context"}
    skills = _pack(tmp_path)
    llm = FakeLlm([])
    assert _run(skills, data, llm, skip=["items"]).steps[2].status == "skipped"
    assert llm.requests == []
    # One listed field with something in it: the step runs.
    llm = FakeLlm([llm_response(OUTPUT)])
    assert (
        _run(skills, {"items": [], "note": "x"}, llm, skip=["items", "note"]).steps[2].status
        == "ok"
    )
    assert len(llm.requests) == 1


def test_whole_input_emptiness_ignores_nothing_that_is_a_value(tmp_path: Path) -> None:
    # A non-empty string anywhere in the viewed input is content: the step runs.
    llm = FakeLlm([llm_response(OUTPUT)])
    record = _run(_pack(tmp_path), {"items": [], "note": "context"}, llm, skip=True)
    assert len(llm.requests) == 1
    assert record.steps[2].status == "ok"


def test_a_skip_path_outside_the_input_view_fails_while_planning(tmp_path: Path) -> None:
    llm = FakeLlm([])
    record = _run(_pack(tmp_path), EMPTY, llm, skip=["itemz"])
    assert record.status == "failed"
    assert record.error is not None
    assert "itemz" in record.error
    assert llm.requests == []


@pytest.mark.parametrize("skip", [[], "yes", 1, ["not a path!"]])
def test_skip_when_empty_is_validated(skip: Any) -> None:
    with pytest.raises(ValidationError):
        Pipeline.model_validate(
            {
                "name": "p",
                "steps": [
                    {
                        "llm": {
                            "skill": SKILL,
                            "model": "m",
                            "budget": {"usd": 1, "tokens_out": 1},
                            "skip_when_empty": skip,
                        }
                    }
                ],
            }
        )


# --- task_fingerprint --------------------------------------------------------------------------


def test_an_invented_id_is_refused_as_a_guard_violation(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[_create("gmail:invented")])])
    record = _run(_pack(tmp_path, RULE), FULL, llm)
    assert record.status == "degraded"
    error = record.steps[2].error
    assert error is not None
    assert error.kind == "guard_violation"
    assert record.violations[0].kind == "guard"
    assert record.violations[0].detail == (
        f"skill:{SKILL} proposed task.create #0 with a fingerprint not from the input "
        "in `fingerprint`"
    )
    assert "invented" not in record.violations[0].detail
    assert record.intents == []


def test_the_right_id_without_the_prefix_is_refused(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[_create("m1")])])
    assert _run(_pack(tmp_path, RULE), FULL, llm).status == "degraded"


def test_a_valid_id_passes(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[_create("gmail:m1")])])
    record = _run(_pack(tmp_path, RULE), FULL, llm)
    assert record.status == "ok", record.error
    assert [i.intent.type for i in record.intents] == ["task.create"]


def test_one_bad_fingerprint_refuses_the_whole_batch(tmp_path: Path) -> None:
    intents = [_create("gmail:m1"), _create("gmail:zzz")]
    record = _run(_pack(tmp_path, RULE), FULL, FakeLlm([llm_response(OUTPUT, intents=intents)]))
    assert record.status == "degraded"
    assert record.intents == []


def test_the_rule_is_opt_in(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response(OUTPUT, intents=[_create("gmail:invented")])])
    record = _run(_pack(tmp_path), FULL, llm)
    assert record.status == "ok", record.error
    assert len(record.intents) == 1


def test_the_guard_reads_ids_from_a_list_and_ignores_other_intents() -> None:
    manifest = SkillManifest.model_validate(
        {
            "name": SKILL,
            "version": "1.0.0",
            "description": "d",
            "input_view": ["ids"],
            "intents": ["task.create"],
            "task_fingerprint": {"ids": "ids", "prefix": "x:"},
        }
    )
    rule = manifest.task_fingerprint
    assert rule is not None
    create = TaskCreate(type="task.create", title="t", fingerprint="x:b")
    other = TaskCreate(type="task.create", title="t", fingerprint="x:c")
    shown = {"ids": ["a", "b"]}
    kwargs: dict[str, Any] = {"shown": shown, "source": f"skill:{SKILL}", "task_fingerprint": rule}
    assert check_intents([create], **kwargs) is None
    assert check_intents([other], **kwargs) is not None
    assert check_intents([other], shown=shown, source=f"skill:{SKILL}") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"intents": ["note.create"]},  # the rule needs task.create
        {"task_fingerprint": {"ids": "other[].id", "prefix": "g:"}},  # not in the view
        {"task_fingerprint": {"ids": "items[].id", "prefix": ""}},
        {"task_fingerprint": {"ids": "items[].id", "prefix": "a b"}},
        {"task_fingerprint": {"ids": "items[].id", "prefix": "g:", "extra": 1}},
    ],
)
def test_a_task_fingerprint_rule_is_validated(changes: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "name": SKILL,
        "version": "1.0.0",
        "description": "d",
        "input_view": ["items[].id"],
        "intents": ["task.create"],
        "task_fingerprint": {"ids": "items[].id", "prefix": "g:"},
    }
    SkillManifest.model_validate(base)
    with pytest.raises(ValidationError):
        SkillManifest.model_validate({**base, **changes})
