# SPDX-License-Identifier: Apache-2.0
"""De-identification follow-ups (#165, schemas decision 68 J to M): scripts without word
separators, scan-only free-text fields, personal numbers, and `horae eval` across plugins.

Every name and number below is synthetic."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from horae.contracts import Lockfile, PluginManifest
from horae.contracts.pipeline import Pipeline
from horae.contracts.plugin import merge_personal_fields, reduce_personal, split_personal
from horae.engine.personal import MIN_NUMBER_DIGITS, merged_options, under, values
from horae.engine.testing import FakeLlm, FixturePlugins, llm_response
from horae.evals import ManifestRegistry, SkillUnderEval
from horae.evals._harness import CaseResult, case_personal, deidentify
from horae.llm import deid
from horae.llm.deid import Deidentifier, DeidError, Vocabulary, pseudonymize
from horae.plugins.host.host import HostRegistry
from horae.skills import load_skill
from test_deid_personal import (
    CLOUD,
    LOCAL,
    LOCKED,
    POLICY,
    SOURCE,
    WHOLE_VIEW,
    _manifest,
    _pipeline,
    _run,
    _sent,
    _skills,
)


def _personal(text: str, *personal: str) -> Any:
    return pseudonymize({"text": text}, {}, None, personal).data["text"]


# --- 1. scripts without word separators, names glued to another script ------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Zoé様の宿題", "[PERSON_1]様の宿題"),
        ("宿題はZoéが終えた", "宿題は[PERSON_1]が終えた"),
        ("ZOEさん", "[PERSON_1]さん"),
        ("ZoéΣ", "[PERSON_1]Σ"),
        ("Привет,Zoé!", "Привет,[PERSON_1]!"),
    ],
)
def test_a_name_glued_to_another_script_is_found(text: str, expected: str) -> None:
    assert _personal(text, "Zoé") == expected


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("山田さんは来ました", "山田", "[PERSON_1]さんは来ました"),
        ("今日は山田花子さんが来た", "山田花子", "今日は[PERSON_1]さんが来た"),
        ("花子が来た", "山田 花子", "[PERSON_1]が来た"),  # a two-letter part
        ("マリーさん", "マリー・デュポン", "[PERSON_1]さん"),
        ("玛丽来了", "玛丽·杜邦", "[PERSON_1]来了"),
        ("민수가 왔다", "김 민수", "[PERSON_1]가 왔다"),
        ("สมชายไปโรงเรียน", "สมชาย", "[PERSON_1]ไปโรงเรียน"),
    ],
)
def test_a_name_in_a_script_without_spaces_is_found_inside_a_sentence(
    text: str, value: str, expected: str
) -> None:
    assert _personal(text, value) == expected


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("MarieZoé", "Zoé"),  # the whole-word rule stands within one script (a limit)
        ("Léonard", "Léo"),
        ("山が見える", "山"),  # a single letter is never looked for
        ("花子", "山田 花"),  # nor a one-letter part
    ],
)
def test_the_whole_word_rule_still_holds_where_it_should(text: str, value: str) -> None:
    assert _personal(text, value) == text


def test_a_glued_name_is_put_back_canonical() -> None:
    sent = pseudonymize({"name": "Zoé", "t": "ZOE様"}, {"name": {"class": "person"}})
    assert sent.data["t"] == "[PERSON_1]様"
    assert sent.reidentify("[PERSON_1]様")[0] == "Zoé様"


def test_the_backstop_sees_glued_names_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deid._Pseudonymizer, "scan", lambda self, text: text)
    for text, value in (("Zoé様", "Zoé"), ("山田さん", "山田"), ("宿題はZoéが", "Zoé")):
        with pytest.raises(DeidError, match="sent in clear"):
            pseudonymize({"t": text}, {}, personal=[value])


def test_a_vocabulary_term_in_a_script_without_spaces_is_found() -> None:
    vocabulary = Vocabulary.parse("place: 東京\n".encode())
    sent = pseudonymize({"t": "東京都に住む"}, {"t": {"class": "free_text"}}, vocabulary)
    assert sent.data["t"] == "[PLACE_1]都に住む"


# --- 2. scan-only free-text fields --------------------------------------------------------------

MAIL = "mail.inbox"
BODY = "x " * 1500  # longer than 1 KiB and 48 tokens


def test_a_scan_only_field_is_declared_with_a_prefix() -> None:
    declared = ["messages[].from", "free_text:messages[].body"]
    manifest = PluginManifest.model_validate(
        _manifest(sources=[MAIL], personal_fields={MAIL: declared})
    )
    assert manifest.provides.personal_fields == {MAIL: declared}
    assert split_personal(declared) == (("messages[].from",), ("messages[].body",))


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        (["free_text:a..b"], "pattern"),
        (["text:a"], "pattern"),
        (["free_text:"], "pattern"),
        (["a", "free_text:a"], "overlapping"),
        (["free_text:a", "a.b"], "overlapping"),
        (["free_text:a.b", "a[]"], "disagree"),
    ],
)
def test_a_bad_scan_only_declaration_is_refused(declared: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        PluginManifest.model_validate(_manifest(sources=[MAIL], personal_fields={MAIL: declared}))


def test_personal_wins_over_scan_only_in_the_union() -> None:
    """A manifest turning a locked personal field into a scan-only one keeps it personal;
    a scan-only path under a personal path is covered by it."""
    union = merge_personal_fields(
        {MAIL: ["free_text:messages[].body", "free_text:notes[].text"]},
        {MAIL: ["messages[]"]},
        tools={MAIL},
    )
    assert union == {MAIL: ["free_text:notes[].text", "messages[]"]}
    assert reduce_personal(["free_text:a", "free_text:a.b"]) == ("free_text:a",)


def test_a_lock_records_scan_only_fields() -> None:
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {"mail": {**LOCKED, "personal_fields": {MAIL: ["free_text:body"]}}},
        }
    )
    dumped = lock.model_dump(mode="json")
    assert dumped["plugins"]["mail"]["personal_fields"] == {MAIL: ["free_text:body"]}
    assert Lockfile.model_validate(dumped) == lock


def test_a_scan_only_field_is_located_but_not_collected() -> None:
    data = {"messages": [{"from": "Zoé Martin", "body": BODY}]}
    found, mismatched = values(data, ["messages[].from", "free_text:messages[].body"])
    assert found == ["Zoé Martin"]
    assert mismatched == []
    _, mismatched = values({"messages": {"body": "x"}}, ["free_text:messages[].body"])
    assert mismatched == ["free_text:messages[].body"]  # its shape still counts
    assert under("[].data", "free_text:a[].b") == "free_text:[].data.a[].b"


def test_a_scan_only_field_is_classed_free_text_unless_a_class_is_near() -> None:
    merged = merged_options(
        {"[].data.notes[].who": {"class": "person"}},
        [
            "[].data.messages[].from",
            "free_text:[].data.messages[].body",
            "free_text:[].data.notes[]",
            "free_text:[].data.messages[]",
        ],
    )
    assert merged == {
        "[].data.messages[].from": {"class": "person"},
        "[].data.notes[].who": {"class": "person"},
    }
    alone = merged_options({}, ["free_text:[].data.messages[].body"])
    assert alone == {"[].data.messages[].body": {"class": "free_text"}}


def _mail_plugins(data: Any, declared: list[str], transform: list[str] | None = None) -> Any:
    plugins = FixturePlugins()
    personal = {MAIL: declared}
    if transform:
        personal["mail.reshape"] = transform
    plugins.add(
        {
            **_manifest(
                sources=[MAIL],
                transforms=["mail.reshape", "mail.flatten"],
                skills=["school/summary"],
                personal_fields=personal,
            ),
        },
        {
            MAIL: lambda p, i: data,
            "mail.reshape": lambda p, i: {"bodies": [m["body"] for m in i[0]["data"]["messages"]]},
            "mail.flatten": lambda p, i: {"bodies": [m["body"] for m in i[0]["data"]["messages"]]},
        },
    )
    return plugins


def _mail_pipeline(model: str = "cloud", transform: str | None = None) -> Pipeline:
    steps: list[dict[str, Any]] = [{"collect": [MAIL]}]
    if transform is not None:
        steps.append({"transform": [transform]})
    budget = {"usd": 1, "tokens_out": 99}
    steps.append({"llm": {"skill": "school/summary", "model": model, "budget": budget}})
    return Pipeline.model_validate({"name": "mail", "steps": steps})


LONG_MAIL = {"messages": [{"from": "Zoé Martin", "body": f"Bonjour, ici ZOE. {BODY}"}]}


def test_a_long_scan_only_body_is_sent_scanned_not_refused(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins(LONG_MAIL, ["messages[].from", "free_text:messages[].body"])
    record = _run(plugins, llm, _skills(tmp_path, WHOLE_VIEW), _mail_pipeline(), deid=POLICY)
    assert record.status == "ok", record.steps
    sent = llm.requests[0].input[0]["data"]["messages"][0]
    assert sent["from"] == "[PERSON_1]"
    assert sent["body"].startswith("Bonjour, ici [PERSON_1]. x x")
    assert "zoe" not in _sent(llm).casefold()


def test_the_same_long_body_declared_personal_still_fails_closed(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins(LONG_MAIL, ["messages[].from", "messages[].body"])
    record = _run(plugins, llm, _skills(tmp_path, WHOLE_VIEW), _mail_pipeline(), deid=POLICY)
    assert record.steps[1].status == "degraded"
    assert record.steps[1].error is not None
    assert record.steps[1].error.kind == "deid_unavailable"
    assert "cannot be found in text" in record.steps[1].error.message
    assert llm.requests == []


def test_a_scan_only_field_alone_still_de_identifies_with_the_vocabulary(tmp_path: Path) -> None:
    vocabulary = Vocabulary.parse(b"person: Zoe Martin\n")
    policy = Deidentifier({"cloud": CLOUD, "local": LOCAL}, vocabulary)
    data = {"messages": [{"body": f"de Zoé Martin: {BODY}"}]}
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins(data, ["free_text:messages[].body"])
    record = _run(plugins, llm, _skills(tmp_path, WHOLE_VIEW), _mail_pipeline(), deid=policy)
    assert record.status == "ok", record.steps
    assert llm.requests[0].classified is True
    assert llm.requests[0].input[0]["data"]["messages"][0]["body"].startswith("de [PERSON_1]:")


def test_without_a_policy_a_scan_only_field_marks_the_request_classified(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins({"messages": [{"body": "x"}]}, ["free_text:messages[].body"])
    _run(plugins, llm, _skills(tmp_path, WHOLE_VIEW), _mail_pipeline())
    assert llm.requests[0].classified is True


def test_a_transform_that_drops_a_scan_only_field_fails_closed(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins(LONG_MAIL, ["free_text:messages[].body"])
    pipeline = _mail_pipeline(transform="mail.flatten")
    record = _run(plugins, llm, _skills(tmp_path, '"bodies"'), pipeline, deid=POLICY)
    assert record.status == "failed"
    assert record.steps[2].error is not None
    assert record.steps[2].error.kind == "deid_unavailable"
    assert llm.requests == []


def test_a_transform_declaring_a_scan_only_field_carries_it(tmp_path: Path) -> None:
    llm = FakeLlm([llm_response({"summary": "ok"})])
    plugins = _mail_plugins(
        LONG_MAIL, ["messages[].from", "free_text:messages[].body"], ["free_text:bodies[]"]
    )
    pipeline = _mail_pipeline(transform="mail.reshape")
    record = _run(plugins, llm, _skills(tmp_path, '"bodies"'), pipeline, deid=POLICY)
    assert record.status == "ok", record.steps
    assert llm.requests[0].input["bodies"][0].startswith("Bonjour, ici [PERSON_1].")


# --- 3. personal numbers -------------------------------------------------------------------------

PHONE = 33612345678


def test_whole_numbers_under_a_personal_path_are_values() -> None:
    data = {"kids": [PHONE, 123456, 9, 2026, True, 1.5, 33612345678.0]}
    found, _ = values(data, ["kids[]"])
    assert found == ["33612345678", "123456"]
    assert MIN_NUMBER_DIGITS == 6
    assert values({"n": -987654}, ["n"])[0] == ["987654"]


def test_a_personal_number_is_found_in_text_and_in_other_numbers() -> None:
    data = {
        "text": "Appeler le 06 12 34 56 78, ou 33612345678, en 2026",
        "other": {"contact": PHONE, "count": 12},
    }
    sent = pseudonymize(data, {}, None, ["33612345678"]).data
    assert sent["text"] == "Appeler le [PERSON_1], ou [PERSON_1], en 2026"
    assert sent["other"] == {"contact": "[PERSON_1]", "count": 12}


def test_a_personal_number_hidden_by_percent_escapes_is_found() -> None:
    sent = pseudonymize({"text": "tel=06%2012%2034%2056%2078"}, {}, None, ["33612345678"])
    assert sent.data["text"] == "tel=[PERSON_1]"


def test_a_short_personal_number_is_left_where_it_is_not_declared() -> None:
    sent = pseudonymize({"text": "12345 et 123456"}, {}, None, ["123456"]).data
    assert sent["text"] == "12345 et [PERSON_1]"


def test_the_backstop_catches_a_personal_number_left_in_clear() -> None:
    assert deid._leaked({"n": PHONE}, ["33612345678"])
    assert deid._leaked({"t": "tel +33 6 12 34 56 78"}, ["33612345678"])
    assert not deid._leaked({"n": 2026, "t": "12 34"}, ["33612345678"])


def test_a_number_in_a_declared_field_reaches_the_model_only_pseudonymized(tmp_path: Path) -> None:
    data = {
        "children": [{"first_name": "Zoé", "phone": PHONE}],
        "homework": [{"teacher": "Mme Quillard", "description": "rappeler le 0612345678"}],
    }
    plugins = FixturePlugins()
    plugins.add(
        _manifest(
            sources=[SOURCE],
            skills=["school/summary"],
            personal_fields={SOURCE: ["children[]", "homework[].teacher"]},
        ),
        {SOURCE: lambda p, i: data},
    )
    llm = FakeLlm([llm_response({"summary": "ok"})])
    view = '"[].data.homework"'  # the phone's own field is not even viewed
    record = _run(plugins, llm, _skills(tmp_path, view), _pipeline(), deid=POLICY)
    assert record.status == "ok", record.steps
    assert "612345678" not in _sent(llm)


# --- 4. horae eval: the declarations of every plugin --------------------------------------------

OTHER = {
    "name": "contacts",
    "version": "0.1.0",
    "horae": ">=0",
    "provides": {"sources": ["contacts.list"], "personal_fields": {"contacts.list": ["[].name"]}},
    "permissions": {},
}
CASE = [
    {"source": "contacts.list", "params": {}, "data": [{"name": "Zoé Martin"}]},
    {"source": SOURCE, "params": {}, "data": {"homework": [{"description": "pour zoe"}]}},
]


def _sue(tmp_path: Path, plugins: Any) -> SkillUnderEval:
    _skills(tmp_path, WHOLE_VIEW)
    manifest = PluginManifest.model_validate(_manifest(skills=["school/summary"]))
    return SkillUnderEval(
        load_skill(tmp_path, "school/summary", allowed_intents=[]), manifest, plugins
    )


def test_an_eval_case_fed_by_another_plugin_is_protected(tmp_path: Path) -> None:
    other = PluginManifest.model_validate(OTHER)
    sue = _sue(tmp_path, ManifestRegistry([sue_manifest(), other]))
    personal = case_personal(sue, CASE)
    assert personal.paths == ("[].data[].name",)
    assert personal.values == ("Zoé Martin",)
    viewed = sue.skill.view(CASE)
    ok, pseudo = deidentify(sue, POLICY, "cloud", viewed, CaseResult("c"), personal)
    assert ok
    assert pseudo is not None
    assert pseudo.data[1]["data"]["homework"][0]["description"] == "pour [PERSON_1]"


def sue_manifest() -> PluginManifest:
    return PluginManifest.model_validate(_manifest(skills=["school/summary"]))


def test_without_a_registry_only_the_skill_plugin_is_known(tmp_path: Path) -> None:
    sue = _sue(tmp_path, None)
    assert case_personal(sue, CASE).paths == ()


def test_eval_reads_declarations_as_the_run_does_lock_included(tmp_path: Path) -> None:
    """Through `HostRegistry`, the lock's declaration is in force even when the installed
    manifest dropped it (decision 68 B), as in a run."""
    lock = Lockfile.model_validate(
        {
            "schema_version": 1,
            "plugins": {
                "contacts": {**LOCKED, "personal_fields": {"contacts.list": ["[].name"]}},
                "school": LOCKED,
            },
        }
    )
    dropped = {**OTHER, "provides": {"sources": ["contacts.list"]}}
    registry = HostRegistry(
        lock,
        {"contacts": PluginManifest.model_validate(dropped), "school": sue_manifest()},
    )
    sue = _sue(tmp_path, registry)
    assert case_personal(sue, CASE).values == ("Zoé Martin",)


def test_a_folder_of_plugins_providing_one_tool_twice_is_refused() -> None:
    other = PluginManifest.model_validate(OTHER)
    twin = PluginManifest.model_validate({**OTHER, "name": "twin"})
    with pytest.raises(ValueError, match="provided by both"):
        ManifestRegistry([other, twin])


def test_eval_plugin_dir_resolves_the_sources_of_every_plugin(tmp_path: Path) -> None:
    from horae.cli_eval import locate

    school = tmp_path / "school"
    _skills(school, WHOLE_VIEW)
    (school / "plugin.yaml").write_text(yaml.safe_dump(_manifest(skills=["school/summary"])))
    (tmp_path / "contacts").mkdir()
    (tmp_path / "contacts" / "plugin.yaml").write_text(yaml.safe_dump(OTHER))
    located = locate("school/summary", None, tmp_path)
    assert case_personal(located.sue, CASE).values == ("Zoé Martin",)


# --- review of #187: short personal numbers, number-to-number, the approval text ----------------


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("call 9123 4567", "91234567"),
        ("call +65 9123 4567", "91234567"),
        ("call (65) 9123-4567", "91234567"),
        ("id 123 456", "123456"),
        ("id 123-456", "123456"),
        ("id 0123456", "123456"),
        ("id 1234567", "1234567"),
    ],
)
def test_a_short_personal_number_is_found_with_separators_or_a_prefix(
    text: str, number: str
) -> None:
    sent = _personal(text, number)
    assert "[PERSON_1]" in sent
    assert number[-4:] not in sent
    assert not deid._leaked({"t": sent}, [number])
    assert deid._leaked({"t": text}, [number])  # the backstop sees it too


@pytest.mark.parametrize(
    "text",
    [
        "order 99123456789",  # a longer number that only contains it
        "id 9123456789",  # more digits after it in the same run
        "ref 1234591234567",  # a prefix of more than four digits
    ],
)
def test_a_short_personal_number_inside_a_longer_one_is_not_matched(text: str) -> None:
    assert _personal(text, "91234567") == text
    assert not deid._leaked({"t": text}, ["91234567"])


@pytest.mark.parametrize("number", [612345678, 612345678.0, 33612345678.0, 6591234567])
def test_a_number_matches_a_personal_number_by_its_phone_digits(number: Any) -> None:
    personal = ["91234567"] if number == 6591234567 else ["33612345678"]
    sent = pseudonymize({"n": number, "t": "x"}, {}, None, personal).data
    assert sent["n"] == "[PERSON_1]"
    assert deid._leaked({"n": number}, personal)


def test_other_numbers_are_left_alone() -> None:
    data = {"a": 2026, "b": 1.5, "c": 12345678, "d": True, "e": 1e300, "f": float("nan")}
    sent = pseudonymize({**data, "t": "x"}, {}, None, ["91234567"]).data
    assert {key: sent[key] for key in "abcde"} == {key: data[key] for key in "abcde"}
    assert not deid._leaked({key: data[key] for key in "abcde"}, ["91234567"])


def test_integral_floats_under_a_personal_path_are_values() -> None:
    assert values({"n": 612345678.0}, ["n"])[0] == ["612345678"]


def test_the_approval_text_lists_scan_only_fields_apart() -> None:
    from horae.contracts.plugin import Permissions
    from horae.marketplace.approval import PERSONAL_NOTE, SCAN_ONLY_NOTE, describe

    lines = describe(
        "mail",
        "1.0.0",
        Permissions(),
        Permissions(),
        {MAIL: ["messages[].from", "free_text:messages[].body"], "mail.notes": ["free_text:t"]},
    )
    text = "\n".join(lines)
    personal, scanned = text.index(PERSONAL_NOTE), text.index(SCAN_ONLY_NOTE)
    assert personal < text.index(f"- {MAIL}: messages[].from") < scanned
    assert scanned < text.index(f"- {MAIL}: messages[].body")
    assert "- mail.notes: t" in text
    assert "free_text:" not in text.replace("(`free_text:`)", "")
    only = "\n".join(describe("m", "1.0.0", Permissions(), Permissions(), {MAIL: ["free_text:b"]}))
    assert PERSONAL_NOTE not in only
    assert SCAN_ONLY_NOTE in only


def test_the_upgrade_diff_shows_a_field_turned_scan_only_as_dropped() -> None:
    from horae.contracts.lockfile import LockedPlugin
    from horae.marketplace.upgrade import describe_diff, permission_diff, widens

    old = LockedPlugin.model_validate({**LOCKED, "personal_fields": {MAIL: ["body"]}})
    new = old.model_copy(update={"personal_fields": {MAIL: ["free_text:body"]}})
    changes = permission_diff(old, new)
    assert widens(changes)
    text = "\n".join(describe_diff(changes))
    assert f"- {MAIL}: body  (new: needs approval)" in text
    assert f"+ {MAIL}: body (scan-only free text, not de-identified as a value)" in text


# --- 5. a given name alone, or a one-letter surname and a title (#189, decision 68 N) ----------


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("小明的作业", "王小明", "[PERSON_1]的作业"),
        ("민수야", "김민수", "[PERSON_1]야"),
        ("花子さん", "山田花子", "[PERSON_1]さん"),
        ("王老师", "王 小明", "[PERSON_1]老师"),
        ("山田さんが来た", "山田花子", "[PERSON_1]さんが来た"),  # the surname of a 2 + 2 name
        ("娜娜来了", "欧阳娜娜", "[PERSON_1]来了"),  # a compound Chinese surname
        ("欧阳老师", "欧阳娜娜", "[PERSON_1]老师"),
        ("민수가 왔다", "남궁민수", "[PERSON_1]가 왔다"),  # a compound Korean surname
        ("남궁 선배", "남궁민수", "[PERSON_1] 선배"),
        ("一郎くん", "長谷川一郎", "[PERSON_1]くん"),  # 3 + 2
        ("長谷川さん", "長谷川一郎", "[PERSON_1]さん"),
        ("花子が来た", "林花子", "[PERSON_1]が来た"),  # 1 + 2 in Japanese
    ],
)
def test_a_given_name_alone_of_an_unspaced_full_name_is_found(
    text: str, value: str, expected: str
) -> None:
    assert _personal(text, value) == expected


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("王老师好", "王小明", "[PERSON_1]老师好"),
        ("王先生", "王明", "[PERSON_1]先生"),  # a two-letter full name
        ("王小姐", "王小明", "[PERSON_1]小姐"),
        ("請問王老師", "王小明", "請問[PERSON_1]老師"),
        ("林さん", "林 花子", "[PERSON_1]さん"),
        ("林様へ", "林花子", "[PERSON_1]様へ"),
        ("林ちゃん", "林花子", "[PERSON_1]ちゃん"),
        ("김씨", "김민수", "[PERSON_1]씨"),
        ("김 선생님", "김민수", "[PERSON_1] 선생님"),
        ("김선생님께", "김 민수", "[PERSON_1]선생님께"),
        ("김님", "김민수", "[PERSON_1]님"),
    ],
)
def test_a_one_letter_surname_is_found_before_a_title(text: str, value: str, expected: str) -> None:
    assert _personal(text, value) == expected


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("王是我的朋友", "王小明"),  # a single letter is never matched alone
        ("国王", "王小明"),
        ("김가 왔다", "김민수"),
        ("林の中", "林花子"),
        ("老王来了", "王小明"),  # a title before the surname is not looked for (a limit)
        ("山さん", "山田 花子"),  # a two-letter surname is not cut further when spaced
        ("小姐", "小明"),
        ("マリさん", "マリー"),  # kana alone is not split
    ],
)
def test_a_single_letter_is_never_matched_without_a_title(text: str, value: str) -> None:
    assert _personal(text, value) == text


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("小明的作业", "王小明"),
        ("민수야", "김민수"),
        ("花子さん", "山田花子"),
        ("王老师", "王 小明"),
        ("김 선생님", "김민수"),
    ],
)
def test_the_backstop_finds_a_given_name_alone_too(
    text: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deid._Pseudonymizer, "scan", lambda self, text: text)
    with pytest.raises(DeidError, match="sent in clear"):
        pseudonymize({"t": text}, {}, personal=[value])


def test_the_name_parts_of_unspaced_names() -> None:
    assert deid._name_parts("王小明") == ["王小明", "小明", "王小"]
    assert deid._name_parts("김민수") == ["김민수", "민수"]
    assert deid._name_parts("남궁민수") == ["남궁민수", "남궁", "민수"]
    assert deid._name_parts("山田花子") == ["山田花子", "田花子", "山田", "花子"]
    assert deid._name_parts("山田 花子") == ["山田", "花子"]  # spaced: unchanged
    assert deid._name_parts("王明") == ["王明"]  # two one-letter parts: titles only
    assert deid._titles("王明")[0] == ("王", "老", "师")


@pytest.mark.parametrize(
    ("value", "parts"),
    [
        ("Bob Martin", ["Bob", "Martin"]),
        ("Иван Петров", ["Иван", "Петров"]),
        ("Dupont, Marie-Zoé", ["Dupont", "Marie", "Zoé"]),
        ("Al Martin", ["Martin"]),
        ("MarieZoé", ["MarieZoé"]),  # same-script glued Latin: still a limit
        ("マリー・デュポン", ["マリー", "デュポン"]),
    ],
)
def test_names_in_scripts_with_spaces_are_split_as_before(value: str, parts: list[str]) -> None:
    assert deid._name_parts(value) == parts
    assert deid._titles(value) == []


def test_latin_and_cyrillic_matching_is_unchanged() -> None:
    assert _personal("MarieZoé est là", "Zoé") == "MarieZoé est là"
    assert _personal("Петров пришёл", "Иван Петров") == "[PERSON_1] пришёл"
    assert _personal("Mr Martin", "Bob Martin") == "Mr [PERSON_1]"
    assert _personal("M Bob", "B Martin") == "M Bob"


def test_a_surname_with_a_title_is_put_back_as_the_whole_name() -> None:
    sent = pseudonymize({"name": "王小明", "t": "王老师说小明很好"}, {"name": {"class": "person"}})
    assert sent.data["t"] == "[PERSON_1]老师说[PERSON_1]很好"
    assert sent.reidentify("[PERSON_1]老师")[0] == "王小明老师"  # privacy over meaning


def test_a_given_name_over_matches_inside_another_word() -> None:
    """The documented tradeoff: `小明` is also found inside `大小明显`. It pseudonymizes
    more than the name, it never sends the name."""
    assert _personal("大小明显", "王小明") == "大[PERSON_1]显"
