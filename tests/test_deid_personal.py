# SPDX-License-Identifier: Apache-2.0
"""Accent folding, and plugin-declared personal fields the core enforces (#162, decision 68).

Every name below is synthetic."""

import asyncio
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.contracts import Lockfile, PluginManifest, RunRecord
from horae.contracts.instance import ModelAlias
from horae.contracts.lockfile import LockedPlugin
from horae.contracts.pipeline import Pipeline
from horae.contracts.plugin import merge_personal_fields
from horae.engine import execute
from horae.engine.personal import merged_options, under, values
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.llm.deid import (
    DEID_VERSION,
    Deidentifier,
    DeidError,
    Vocabulary,
    fold,
    pseudonymize,
)
from horae.plugins.host.host import HostRegistry
from horae.replay import LockRegistry
from horae.skills import DirectorySkills
from test_engine_skills import CONTEXT, START

NFD_ZOE = unicodedata.normalize("NFD", "Zoé")
CLOUD = ModelAlias.model_validate(
    {"backend": "anthropic", "model": "fixture-model", "secret": "ANTHROPIC_API_KEY"}
)
LOCAL = ModelAlias.model_validate(
    {"backend": "ollama", "model": "fixture-model", "base_url": "http://ollama:11434"}
)
POLICY = Deidentifier({"cloud": CLOUD, "local": LOCAL})
SOURCE = "school.homework"
FIELDS = ["children[].first_name", "homework[].teacher"]
DATA: dict[str, Any] = {
    "children": [{"id": "c1", "first_name": "Zoé"}, {"id": "c2", "first_name": "Léo"}],
    "homework": [
        {"child_id": "c1", "teacher": "Mme Quillard", "description": "zoe lit le chapitre 3"},
        {"child_id": "c2", "teacher": None, "description": "Léonard de Vinci, pour LEO"},
    ],
}
NAMES = ("Zoé", "Léo", "Quillard")


def _person(text: str) -> Any:
    return pseudonymize({"name": "Zoé", "text": text}, {"name": {"class": "person"}}).data["text"]


# --- 1. accent folding -----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Zoé", "Zoe", "ZOE", "zoé", "ZOÉ", NFD_ZOE, "zoé"])
def test_every_accent_and_case_form_of_a_name_is_matched(text: str) -> None:
    assert _person(f"Merci {text} !") == "Merci [PERSON_1] !"


def test_a_declared_name_without_an_accent_matches_the_accented_text() -> None:
    sent = pseudonymize({"name": "Zoe", "text": f"{NFD_ZOE} et ZOÉ"}, {"name": {"class": "person"}})
    assert sent.data["text"] == "[PERSON_1] et [PERSON_1]"


def test_a_name_is_never_matched_inside_a_word() -> None:
    data = {"name": "Léo", "text": "Léonard, Leonardo et CLÉO, puis Léo."}
    sent = pseudonymize(data, {"name": {"class": "person"}})
    assert sent.data["text"] == "Léonard, Leonardo et CLÉO, puis [PERSON_1]."


def test_a_name_inside_a_word_matches_only_when_that_word_is_declared() -> None:
    data = {"names": ["Léo", "Léonard"], "text": "Leonard et leo"}
    sent = pseudonymize(data, {"names[]": {"class": "person"}})
    assert sent.data["text"] == "[PERSON_2] et [PERSON_1]"


def test_a_combining_mark_nfkc_cannot_compose_does_not_cut_a_name() -> None:
    sent = pseudonymize({"name": "Zoe", "text": "Zo̱e est là"}, {"name": {"class": "person"}})
    assert sent.data["text"] == "[PERSON_1] est là"


def test_restoration_puts_back_the_canonical_name() -> None:
    sent = pseudonymize({"name": "Zoé", "text": "ZOE et zoe"}, {"name": {"class": "person"}})
    assert sent.data["text"] == "[PERSON_1] et [PERSON_1]"
    assert sent.reidentify("Bravo [PERSON_1]")[0] == "Bravo Zoé"


def test_vocabulary_terms_are_accent_folded_too() -> None:
    vocabulary = Vocabulary.parse("person: Zoé\nplace: Orléans\n".encode())
    sent = pseudonymize({"t": "zoe va a ORLEANS"}, {"t": {"class": "free_text"}}, vocabulary)
    assert sent.data["t"] == "[PERSON_1] va a [PLACE_1]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Malice", "Malice"),  # still whole letter runs only
        ("alice_smith", "[PERSON_1]_smith"),
        ("STRAUSS", "STRAUSS"),
        ("Al", "Al"),  # a two-letter part is not a name part
    ],
)
def test_existing_matching_is_unchanged(text: str, expected: str) -> None:
    data = {"name": "Alice Al", "text": text}
    assert pseudonymize(data, {"name": {"class": "person"}}).data["text"] == expected


def test_fold_never_returns_an_empty_symbol() -> None:
    assert fold("É") == "e"
    assert fold("́") == "́"
    assert fold("Straße") == "strasse"


def test_the_transforms_version_was_bumped() -> None:
    assert DEID_VERSION == 4  # 4: #165 (scripts without spaces, personal numbers)


# --- 2. contracts ------------------------------------------------------------------------------


def _manifest(**provides: Any) -> dict[str, Any]:
    return {
        "name": "school",
        "version": "0.1.0",
        "horae": ">=0",
        "provides": provides,
        "permissions": {},
    }


def test_a_manifest_declares_personal_fields_of_its_tools() -> None:
    manifest = PluginManifest.model_validate(
        _manifest(sources=[SOURCE], personal_fields={SOURCE: FIELDS})
    )
    assert manifest.provides.personal_fields == {SOURCE: FIELDS}


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ({"school.other": ["a"]}, "not one of the plugin's sources"),
        ({SOURCE: []}, "at least 1"),
        ({SOURCE: ["a..b"]}, "pattern"),
        ({SOURCE: ["a", "a.b"]}, "overlapping"),
        ({SOURCE: ["a.b", "a[]"]}, "disagree"),
        ({SOURCE: ["a", "a"]}, "duplicate"),
        ({SOURCE: [f"f{i}" for i in range(65)]}, "at most 64"),
    ],
)
def test_a_bad_personal_field_declaration_is_refused(
    declared: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        PluginManifest.model_validate(_manifest(sources=[SOURCE], personal_fields=declared))


LOCKED = {
    "version": "0.1.0",
    "repo": "https://github.com/example/plugin-school",
    "commit": "a" * 40,
    "permissions": {},
}


def test_a_lock_without_personal_fields_is_written_as_before() -> None:
    lock = Lockfile.model_validate({"schema_version": 1, "plugins": {"school": LOCKED}})
    dumped = lock.model_dump(mode="json")
    assert "personal_fields" not in dumped["plugins"]["school"]
    assert Lockfile.model_validate(dumped) == lock


def test_a_lock_records_personal_fields_sorted() -> None:
    entry = LockedPlugin.model_validate(
        {**LOCKED, "personal_fields": {SOURCE: ["homework[].teacher", "children[].first_name"]}}
    )
    assert entry.model_dump(mode="json")["personal_fields"] == {
        SOURCE: ["children[].first_name", "homework[].teacher"]
    }


def test_the_union_of_two_declarations_keeps_the_outermost_paths() -> None:
    union = merge_personal_fields(
        {SOURCE: ["children[].first_name"], "school.gone": ["x"]},
        {SOURCE: ["children[]", "homework[].teacher"]},
        tools={SOURCE},
    )
    assert union == {SOURCE: ["children[]", "homework[].teacher"]}


def test_the_host_enforces_the_manifest_and_the_lock_together() -> None:
    manifest = PluginManifest.model_validate(
        _manifest(sources=[SOURCE], personal_fields={SOURCE: ["homework[].teacher"]})
    )
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                "school": {**LOCKED, "personal_fields": {SOURCE: ["children[].first_name"]}}
            },
        }
    )
    plugin = HostRegistry(lock, {"school": manifest}).plugin_for_tool(SOURCE)
    assert plugin.manifest.provides.personal_fields == {SOURCE: FIELDS}


def test_replay_reads_personal_fields_from_the_lock() -> None:
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {"school": {**LOCKED, "personal_fields": {SOURCE: FIELDS}}},
        }
    )
    registry = LockRegistry(lock, _pipeline())
    assert registry.plugin_for_tool(SOURCE).manifest.provides.personal_fields == {SOURCE: FIELDS}


# --- 2. the shared helpers ------------------------------------------------------------------------


def test_values_are_read_at_the_declared_paths() -> None:
    found, mismatched = values(DATA, FIELDS)
    assert found == ["Zoé", "Léo", "Mme Quillard"]
    assert mismatched == []


def test_a_declared_path_of_the_wrong_shape_is_reported() -> None:
    _, mismatched = values({"children": {"first_name": "Zoé"}}, ["children[].first_name"])
    assert mismatched == ["children[].first_name"]


def test_paths_are_seen_from_the_collect_output() -> None:
    assert under("[].data", "children[].first_name") == "[].data.children[].first_name"
    assert under("[].data", "[].name") == "[].data[].name"


def test_the_personal_class_overrides_free_text_and_respects_token_classes() -> None:
    options = {
        "[].data.children": {"class": "free_text"},
        "[].data.homework[].teacher": {"class": "email"},
        "[].data.homework[].note": {"class": "amount", "buckets": [0, 10]},
    }
    merged = merged_options(
        options,
        ["[].data.children[].first_name", "[].data.homework[].teacher", "[].data.homework[]"],
    )
    assert merged == {
        "[].data.children[].first_name": {"class": "person"},
        "[].data.homework[]": {"class": "person"},
        "[].data.homework[].note": {"buckets": [0, 10]},
    }


# --- 2. the engine ---------------------------------------------------------------------------


def _pipeline(model: str = "cloud", transform: str | None = None) -> Pipeline:
    steps: list[dict[str, Any]] = [{"collect": [SOURCE]}]
    if transform is not None:
        steps.append({"transform": [transform]})
    steps.append(
        {"llm": {"skill": "school/summary", "model": model, "budget": {"usd": 1, "tokens_out": 99}}}
    )
    return Pipeline.model_validate({"name": "school", "steps": steps})


def _plugins(data: Any = DATA, **personal: list[str]) -> FixturePlugins:
    plugins = FixturePlugins()
    declared = {SOURCE: FIELDS, **{ref.replace("_", "."): p for ref, p in personal.items()}}
    plugins.add(
        _manifest(
            sources=[SOURCE],
            transforms=["school.flatten", "school.reshape"],
            skills=["school/summary"],
            personal_fields={ref: paths for ref, paths in declared.items() if paths},
        ),
        {
            SOURCE: lambda p, i: data,
            "school.flatten": lambda p, i: {
                "lines": [h["description"] for h in i[0]["data"]["homework"]]
            },
            "school.reshape": lambda p, i: {"kids": i[0]["data"]["children"]},
        },
    )
    return plugins


SKILL_YAML = """name: school/summary
version: 1.0.0
description: Summarise the homework.
input_view: [{view}]
"""
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string", "maxLength": 500}},
}


def _skills(tmp_path: Path, view: str) -> DirectorySkills:
    folder = tmp_path / "skills" / "school" / "summary"
    folder.mkdir(parents=True)
    (folder / "skill.yaml").write_text(SKILL_YAML.format(view=view), encoding="utf-8")
    (folder / "prompt.md").write_text("Summarise.\n", encoding="utf-8")
    (folder / "output.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    return DirectorySkills({"school": tmp_path})


def _run(
    plugins: FixturePlugins, llm: FakeLlm, skills: Any, pipeline: Pipeline, **kwargs: Any
) -> RunRecord:
    record = asyncio.run(
        execute(
            pipeline,
            run_id="20261027T053000Z-school-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=skills,
            **kwargs,
        )
    )
    RunRecord.model_validate_json(record.model_dump_json())
    return record


def _sent(llm: FakeLlm) -> str:
    return json.dumps([request.input for request in llm.requests], ensure_ascii=False)


def _clear_of_names(text: str) -> None:
    """No declared name, in any accent or case form, as a whole word of `text`."""
    words = set(re.findall(r"[^\W\d_]+", fold(text)))
    for name in NAMES:
        assert fold(name) not in words, name


WHOLE_VIEW = '"[].data"'


def test_a_skill_without_classes_no_longer_sends_declared_names(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "Pour [PERSON_1] et [PERSON_3]"})])
    record = _run(_plugins(), llm, _skills(tmp_path, WHOLE_VIEW), _pipeline(), deid=POLICY)
    assert record.status == "ok", record.steps
    _clear_of_names(_sent(llm))
    sent = llm.requests[0].input[0]["data"]
    assert sent["homework"][0]["description"] == "[PERSON_1] lit le chapitre 3"
    assert sent["homework"][1]["description"] == "Léonard de Vinci, pour [PERSON_2]"
    assert llm.requests[0].classified is True
    assert record.steps[1].output == {"summary": "Pour Zoé et Mme Quillard"}
    _clear_of_names(json.dumps(record.llm[0].model_dump(mode="json"), ensure_ascii=False))


def test_names_in_text_are_found_even_when_the_field_is_not_viewed(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    view = '"[].data.homework[].description"'
    record = _run(_plugins(), llm, _skills(tmp_path, view), _pipeline(), deid=POLICY)
    assert record.status == "ok", record.steps
    _clear_of_names(_sent(llm))


def test_a_local_alias_is_sent_the_viewed_input_unchanged(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    record = _run(_plugins(), llm, _skills(tmp_path, WHOLE_VIEW), _pipeline("local"), deid=POLICY)
    assert record.status == "ok", record.steps
    assert llm.requests[0].input[0]["data"] == DATA
    assert llm.requests[0].deidentified is None


def test_without_a_policy_the_request_is_marked_classified(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    _run(_plugins(), llm, _skills(tmp_path, WHOLE_VIEW), _pipeline())
    # `ModelBackends` refuses a classified, non de-identified request to a cloud alias.
    assert llm.requests[0].classified is True
    assert llm.requests[0].deidentified is None


def test_a_plugin_without_declarations_is_unchanged(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _plugins(school_homework=[])
    record = _run(plugins, llm, _skills(tmp_path, WHOLE_VIEW), _pipeline(), deid=POLICY)
    assert record.status == "ok"
    assert llm.requests[0].input[0]["data"] == DATA
    assert llm.requests[0].classified is False


def test_an_undeclared_transform_fails_closed_before_any_call(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _plugins()
    record = _run(
        plugins,
        llm,
        _skills(tmp_path, '"lines"'),
        _pipeline(transform="school.flatten"),
        deid=POLICY,
    )
    assert record.status == "failed"
    assert record.steps[2].error is not None
    assert record.steps[2].error.kind == "deid_unavailable"
    assert "school.flatten" in record.steps[2].error.message
    assert plugins.calls == []
    assert llm.requests == []


def test_an_undeclared_transform_is_fine_with_a_local_alias(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    record = _run(
        _plugins(),
        llm,
        _skills(tmp_path, '"lines"'),
        _pipeline("local", transform="school.flatten"),
        deid=POLICY,
    )
    assert record.status == "ok", record.steps


def test_a_declaring_transform_carries_the_protection(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _plugins(school_reshape=["kids[].first_name"])
    record = _run(
        plugins,
        llm,
        _skills(tmp_path, '"kids"'),
        _pipeline(transform="school.reshape"),
        deid=POLICY,
    )
    assert record.status == "ok", record.steps
    assert llm.requests[0].input == {
        "kids": [{"first_name": "[PERSON_1]", "id": "c1"}, {"first_name": "[PERSON_2]", "id": "c2"}]
    }


def test_an_output_of_the_wrong_shape_fails_closed(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    data = {**DATA, "children": {"first_name": "Zoé"}}
    record = _run(_plugins(data), llm, _skills(tmp_path, WHOLE_VIEW), _pipeline(), deid=POLICY)
    assert record.steps[1].status == "degraded"
    assert record.steps[1].error is not None
    assert record.steps[1].error.kind == "deid_unavailable"
    assert "Zoé" not in record.steps[1].error.message
    assert llm.requests == []
    assert record.llm == []


def test_a_cached_output_is_reused_only_with_the_same_pseudonyms(tmp_path: Path) -> None:
    skills = _skills(tmp_path, WHOLE_VIEW)
    llm = FakeLlm([llm_response({"summary": "[PERSON_1]"})])
    first = _run(_plugins(), llm, skills, _pipeline(), deid=POLICY)
    call = first.llm[0]
    from horae.engine import PreviousLlm

    previous = PreviousLlm(
        input_hash=first.input_hash or "",
        output=call.response,
        intents=call.intents,
        backend=call.backend,
        model=call.model,
        skill_version=call.skill_version,
        deid_input_sha256=call.request["deid"]["input_sha256"],
    )
    again = FakeLlm([])
    reused = _run(_plugins(), again, skills, _pipeline(), deid=POLICY, previous=previous)
    assert reused.llm[0].cached is True
    assert reused.steps[1].output == {"summary": "Zoé"}
    assert again.requests == []


# --- the fail-closed check of the pseudonymizer -----------------------------------------------


def test_a_too_long_personal_value_fails_closed() -> None:
    with pytest.raises(DeidError, match="cannot be found in text"):
        pseudonymize({"t": "x"}, {}, personal=["a " * 30])


def test_a_personal_value_left_in_clear_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import horae.llm.deid as deid

    monkeypatch.setattr(deid._Pseudonymizer, "scan", lambda self, text: text)
    with pytest.raises(DeidError, match="sent in clear"):
        pseudonymize({"t": "Bonjour Zoé"}, {}, personal=["Zoé"])


# --- horae eval ------------------------------------------------------------------------------


def test_an_eval_case_is_de_identified_like_a_run(tmp_path: Path) -> None:
    from horae.evals._harness import CaseResult, SkillUnderEval, case_personal, deidentify
    from horae.skills import load_skill

    _skills(tmp_path, WHOLE_VIEW)
    manifest = PluginManifest.model_validate(
        _manifest(sources=[SOURCE], skills=["school/summary"], personal_fields={SOURCE: FIELDS})
    )
    skill = load_skill(tmp_path, "school/summary", allowed_intents=[])
    sue = SkillUnderEval(skill, manifest)
    case_input = [{"source": SOURCE, "params": {}, "data": DATA}]
    personal = case_personal(sue, case_input)
    assert personal.paths == (
        "[].data.children[].first_name",
        "[].data.homework[].teacher",
    )
    viewed = skill.view(case_input)
    ok, pseudo = deidentify(sue, POLICY, "cloud", viewed, CaseResult("c"), personal)
    assert ok
    assert pseudo is not None
    _clear_of_names(json.dumps(pseudo.data, ensure_ascii=False))
    ok, local = deidentify(sue, POLICY, "local", viewed, CaseResult("c"), personal)
    assert ok
    assert local is None
    bad = case_personal(sue, [{"source": SOURCE, "data": {"children": {"first_name": "Zoé"}}}])
    result = CaseResult("c")
    ok, _ = deidentify(sue, POLICY, "cloud", viewed, result, bad)
    assert not ok
    assert result.error is not None
    assert result.error["kind"] == "deid_unavailable"


# --- 5. review hardening: hidden names -------------------------------------------------------


def _personal(text: str, *personal: str) -> Any:
    return pseudonymize({"text": text}, {}, None, personal).data["text"]


@pytest.mark.parametrize(
    "invisible",
    [0xFE0F, 0x180B, 0xE0041, 0x3164, 0x17B4, 0x034F, 0x200B, 0x2060],
)
def test_an_invisible_character_inside_a_name_does_not_hide_it(invisible: int) -> None:
    assert _personal(f"Merci Zo{chr(invisible)}é !", "Zoé") == "Merci [PERSON_1] !"


CURLY = chr(0x2019)


@pytest.mark.parametrize("mark", [0x2019, 0x02BC, 0x2018, 0x27])
def test_typographic_apostrophes_are_the_plain_one(mark: int) -> None:
    text = f"O{chr(mark)}Brien"
    assert _personal(f"Hi {text}", f"O{CURLY}Brien") == "Hi [PERSON_1]"
    assert _personal(f"Hi {text}", "Sean O'Brien") == "Hi [PERSON_1]"


def test_typographic_dashes_are_the_plain_hyphen() -> None:
    assert _personal(f"Marie{chr(0x2011)}Zoé", "Marie-Zoé") == "[PERSON_1]"


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Mr Dupont", "Dupont, Marie"),
        ("Quillard est là", "Mme. Quillard."),
        ("Quillard est là", "Quillard,"),
        ("Lopez", "Quillard/Lopez"),
        ("Zoé", '"Zoé" Martin'),
    ],
)
def test_a_name_part_is_found_whatever_punctuation_surrounds_it_in_the_value(
    text: str, value: str
) -> None:
    assert "[PERSON_1]" in _personal(text, value)


@pytest.mark.parametrize(
    "text",
    ["https://x.fr/?eleve=Zo%C3%A9", "Zo&eacute; et Zo&#233;", "q=zo%C3%A9&r=1"],
)
def test_a_name_hidden_by_percent_escapes_or_entities_is_found(text: str) -> None:
    sent = _personal(text, "Zoé")
    assert "[PERSON_1]" in sent
    assert "%C3" not in sent
    assert "&eacute;" not in sent
    assert "&#233;" not in sent


def test_a_hidden_name_that_survives_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from horae.llm import deid

    monkeypatch.setattr(deid._Pseudonymizer, "_finds", lambda self, text: False)
    with pytest.raises(DeidError, match="in clear"):
        pseudonymize({"text": "Zo%C3%A9"}, {}, None, ("Zoé",))


@pytest.mark.parametrize(
    "text",
    ["q=Zo%25C3%25A9", "Zo&amp;eacute;", "Zo&amp;#233;", "Zo%26eacute%3B", "%255A%256F%25C3%25A9"],
)
def test_a_name_encoded_twice_is_found(text: str) -> None:
    sent = _personal(text, "Zoé")
    assert "[PERSON_1]" in sent
    assert "%" not in sent
    assert "&" not in sent


def test_a_string_encoded_too_many_times_fails_closed() -> None:
    with pytest.raises(DeidError, match="too many times"):
        _personal("%2525252525C3%2525252525A9", "Zoé")


def test_every_decoded_form_is_rechecked(monkeypatch: pytest.MonkeyPatch) -> None:
    from horae.llm import deid

    monkeypatch.setattr(deid._Pseudonymizer, "_finds", lambda self, text: False)
    with pytest.raises(DeidError, match="in clear"):
        pseudonymize({"text": "Zo%25C3%25A9"}, {}, None, ("Zoé",))


def test_name_parts_are_taken_next_to_an_address() -> None:
    assert _personal("Zoé et Martin", "Zoé Martin <zoe@example.org>") == "[PERSON_1] et [PERSON_1]"
    assert _personal("example", "Zoé Martin <zoe@example.org>") == "example"
