# SPDX-License-Identifier: Apache-2.0
"""De-identification hardening (#34, the privacy review of PR #110; schemas decision 47).

One test (or a parametrized set) per finding of the review: re-identification is bounded
before it substitutes, matching survives white space, Unicode, case and separator tricks,
amounts and keys do not leak, a string stays a string, pseudonym-shaped input is neutralized,
a reused output must come from the same de-identified input, a failed re-identification
keeps the call and its cost, matching is linear, overlapping classes are refused, every
declared secret is checked, and `horae doctor` warns about an `ollama` alias that looks
hosted. All data is synthetic."""

import asyncio
import random
import string
import time
from pathlib import Path
from typing import Any

import pytest

from horae.contracts import RunRecord
from horae.contracts.instance import ModelAlias
from horae.engine import PreviousLlm, execute
from horae.llm import UNVALIDATED, ModelBackends
from horae.llm.deid import (
    MAX_VALUE_CHARS,
    Deidentifier,
    DeidError,
    Vocabulary,
    amount_renderings,
    pseudonymize,
)
from horae.replay import replay
from horae.secrets import SecretResolver, redact
from horae.skills import DirectorySkills
from test_deid import (
    CLOUD,
    DATA,
    OUTPUT,
    PLANTED,
    Provider,
    _answer,
    _backends,
    _policy,
    _run,
)
from test_deid import pack as pack
from test_engine_skills import CONTEXT, START, _pipeline, _plugins
from test_llm_wiring import LLM_STEP, OLLAMA, _doctor, _write

FREE = {"note": {"class": "free_text"}}


def _scan(text: str, vocabulary: bytes, options: dict[str, Any] | None = None) -> Any:
    data = {"note": text}
    return pseudonymize(data, options or FREE, Vocabulary.parse(vocabulary)).data["note"]


def _run_data(data: Any, llm: Any, pack: Path, **kwargs: Any) -> RunRecord:
    plugins = _plugins(data)
    record = asyncio.run(
        execute(
            _pipeline(),
            run_id="20261027T053000Z-brief-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=DirectorySkills({"pack-daily-brief": pack}),
            **kwargs,
        )
    )
    RunRecord.model_validate_json(record.model_dump_json())
    return record


# --- finding 1 and 8: re-identification is bounded first, and keeps the call ----------------


def test_a_repeated_pseudonym_is_refused_before_substituting(pack: Path, tmp_path: Path) -> None:
    long_name = "Bob " + "x" * (MAX_VALUE_CHARS - 10)
    data = {**DATA, "events": [{**DATA["events"][0], "attendees": [long_name]}]}
    flood = "[PERSON_1]" * 1000  # 10 KB of one pseudonym: about 1 MB re-identified
    provider = Provider("anthropic", {**OUTPUT, "summary": flood, "intents": []})
    record = _run_data(data, _backends(provider, CLOUD, tmp_path), pack, deid=_policy())
    error = record.steps[2].error
    assert error is not None
    assert (record.status, error.kind) == ("degraded", "output_too_large")
    assert "re-identified" in error.message
    # Finding 8: the call was made and billed: it stays in the record, with its cost.
    [call] = record.llm
    assert call.response["summary"] == flood
    assert call.usage.tokens_out == 5
    assert record.cost.tokens_out == 5
    assert "unresolved" not in call.request["deid"]


def test_a_classed_value_over_the_cap_is_withheld() -> None:
    result = pseudonymize(
        {"who": ["A" * (MAX_VALUE_CHARS + 1), "Ann"]}, {"who[]": {"class": "person"}}
    )
    assert result.data == {"who": ["[PERSON]", "[PERSON_1]"]}
    assert result.counts == {"person": 1, "withheld": 1}
    assert result.growth(["[PERSON_1]" * 3, {"[PERSON_1]": 1}]) == 0  # "Ann" is shorter
    assert (
        pseudonymize({"who": "x" * 600}, {"who": {"class": "person"}}).growth("[PERSON_1]") == 590
    )


# --- finding 2: matching that survives the usual evasions ----------------------------------

NAMES = b"person: Bob Martin\nperson: Alice\nperson: Strau\xc3\x9f\n"


@pytest.mark.parametrize(
    "text",
    [
        "Lunch with Bob  Martin",
        "Lunch with Bob\u00a0Martin",
        "Lunch with Bob\nMartin",
        "Lunch with Bob \u3000 Martin",
        "Lunch with \uff21\uff4c\uff49\uff43\uff45",  # fullwidth
        "mail alice_smith or user_alice or alice123",
        "Al\u200bice and A\u00adlice",  # zero-width space, soft hyphen
        "Herr STRAUSS and herr strauss",
        "BOB MARTIN, bob martin",
        "Martin said hi to bob",  # name parts on their own
    ],
)
def test_known_values_are_found_through_evasions(text: str) -> None:
    sent = _scan(text, NAMES)
    for leaked in ("alice", "bob", "martin", "strau"):
        assert leaked not in sent.casefold(), sent
    assert "[PERSON_" in sent


@pytest.mark.parametrize("text", ["Malice and Alicea", "Bobby Martinez", "Straussberg"])
def test_known_values_still_match_whole_letter_runs_only(text: str) -> None:
    assert _scan(text, NAMES) == text  # nothing found: sent byte-identical


@pytest.mark.parametrize(
    "text",
    [
        "call 06 12 34 56 78 now",
        "call 0612345678 now",
        "call +33 (0)6.12.34.56.78 now",
        "call 06-12-34-56-78 now",
        "call +33 6 12 34 56 78 now",
        "call 33612345678 now",
    ],
)
@pytest.mark.parametrize("phone", ["+33 6 12 34 56 78", 33612345678])
def test_a_phone_is_found_whatever_its_separators(text: str, phone: Any) -> None:
    options = {"phone": {"class": "phone"}, "note": {"class": "free_text"}}
    result = pseudonymize({"phone": phone, "note": text}, options)
    sent = result.data["note"]
    assert sent.startswith("call [PHONE_1]"), sent
    assert sent.endswith(" now")
    assert "12" not in sent
    assert "78" not in sent
    assert result.reidentify(sent)[0] == f"call {phone} now"


def test_a_short_phone_is_matched_as_written_only() -> None:
    options = {"phone": {"class": "phone"}, "note": {"class": "free_text"}}
    result = pseudonymize({"phone": "12 34 56", "note": "dial 12 34 56 or 123456"}, options)
    assert result.data["note"] == "dial [PHONE_1] or 123456"


# --- finding 3: amounts in text -------------------------------------------------------------


def test_an_amount_written_in_text_becomes_its_bucket() -> None:
    data = {
        "paid": 1234.56,
        "note": "Paid 1 234,56 EUR (1,234.56 / 1234.56 / 1234,56 / 1.234,56); about 1234",
    }
    result = pseudonymize(data, {"paid": {"class": "amount"}, "note": {"class": "free_text"}})
    assert result.data["paid"] == "1000-10000"
    sent = result.data["note"]
    assert "234" not in sent, sent
    assert sent.count("1000-10000") == 6
    assert result.counts == {"amount": 7}  # one field, six renderings: not tokens
    assert result.reidentify(sent) == (sent, [])  # not reversible


def test_amount_renderings() -> None:
    assert amount_renderings(120) == {"120", "120.00", "120,00"}
    assert {"1234.5", "1234,50", "1 234,5", "1,234.50", "1234"} <= amount_renderings(1234.5)
    assert amount_renderings(7) == {"7.00", "7,00"}  # a bare 7 would match everywhere
    assert amount_renderings(True) == amount_renderings(float("nan")) == set()
    assert amount_renderings(1e-7) == set()


# --- finding 4: object keys -----------------------------------------------------------------


def test_keys_under_a_token_class_become_pseudonyms() -> None:
    data = {"rsvp": {"Alice": "yes", "Bob": "no"}, "summary": {"Alice": "Lunch with Bob"}}
    result = pseudonymize(data, {"rsvp": {"class": "person"}})
    assert result.data == {
        "rsvp": {"[PERSON_1]": "[PERSON_2]", "[PERSON_3]": "[PERSON_4]"},
        "summary": {"[PERSON_1]": "Lunch with [PERSON_3]"},  # keys of any node are scanned
    }
    back, notes = result.reidentify(result.data)
    assert back == data
    assert notes == []


def test_keys_that_collide_once_pseudonymized_fail_closed() -> None:
    with pytest.raises(DeidError, match="same once pseudonymized"):
        pseudonymize({"rsvp": {"Alice": 1, "ALICE": 2}}, {"rsvp": {"class": "person"}})


# --- finding 5: a string stays a string -----------------------------------------------------


def test_a_whole_pseudonym_is_put_back_as_text() -> None:
    result = pseudonymize({"phone": 33612345678, "n": 7}, {"phone": {"class": "phone"}})
    assert result.reidentify({"call": "[PHONE_1]", "n": 7}) == (
        {"call": "33612345678", "n": 7},
        [],
    )


# --- finding 6: pseudonym-shaped input ------------------------------------------------------


def test_pseudonym_shaped_input_is_neutralized() -> None:
    data = {"who": "Bob", "note": "ask [PERSON_1], [person_2] and \uff3bPLACE_1\uff3d"}
    result = pseudonymize(data, {"who": {"class": "person"}, "note": {"class": "free_text"}})
    sent = result.data["note"]
    assert sent == "ask (PERSON_1), (person_2) and (PLACE_1)"
    assert result.counts["neutralized"] == 3
    back, _ = result.reidentify(sent)
    assert "Bob" not in back
    # Also in unclassed fields and keys.
    assert pseudonymize({"[EMAIL_1]": 1}, {"x": {"class": "person"}}).data == {"(EMAIL_1)": 1}


# --- finding 7: a reused output needs the same de-identified input -------------------------


def _previous(record: RunRecord, digest: str | None) -> PreviousLlm:
    assert record.input_hash is not None
    call = record.llm[-1]
    return PreviousLlm(
        input_hash=record.input_hash,
        output=call.response,
        intents=call.intents,
        skill_version=call.skill_version,
        model=call.model,
        backend=call.backend,
        deid_input_sha256=digest,
    )


def test_a_reused_output_from_another_de_identified_input_is_not_reused(
    pack: Path, tmp_path: Path
) -> None:
    provider = Provider("anthropic", _answer())
    llm = _backends(provider, CLOUD, tmp_path)
    first = _run(llm, pack, deid=_policy())
    for digest in (None, "0" * 64):
        _run(llm, pack, deid=_policy(), previous=_previous(first, digest))
    assert len(provider.sent) == 3  # neither was reused: the model was asked again


def test_a_replayed_reuse_reports_a_changed_vocabulary(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    llm = _backends(provider, CLOUD, tmp_path)
    first = _run(llm, pack, deid=_policy())
    digest = first.llm[0].request["deid"]["input_sha256"]
    reused = _run(llm, pack, deid=_policy(), previous=_previous(first, digest))
    assert reused.llm[0].cached is True
    assert reused.llm[0].request["deid"]["input_sha256"] == digest
    plugins = _plugins(DATA)
    skills = DirectorySkills({"pack-daily-brief": pack})
    same = replay(reused, _pipeline(), plugins=plugins, skills=skills, deid=_policy())
    assert same.outcome == "identical", (same.error, same.mismatches)
    other = Deidentifier(
        {"default": ModelAlias.model_validate(CLOUD)}, Vocabulary.parse(b"person: Bea\n")
    )
    changed = replay(reused, _pipeline(), plugins=plugins, skills=skills, deid=other)
    assert changed.outcome == "error"
    assert any("vocabulary changed" in m for m in changed.mismatches)


# --- finding 9: inside the deadline, and linear ---------------------------------------------


def test_matching_is_fast_on_a_large_input_and_a_full_vocabulary() -> None:
    rng = random.Random(34)  # noqa: S311 - synthetic test data

    def word(size: int) -> str:
        return "".join(rng.choice(string.ascii_lowercase) for _ in range(size))

    names = sorted({f"{word(8).title()} {word(7).title()}" for _ in range(4096)})[:4096]
    vocabulary = Vocabulary.parse("".join(f"person: {n}\n" for n in names).encode())
    assert len(vocabulary.terms) == 4096
    words = [word(rng.randint(3, 9)) for _ in range(5000)] + names[:50]
    text = " ".join(rng.choice(words) for _ in range(100_000))[:600_000]
    hostile = "ab " * 200_000  # every position starts a long known value
    data = {"who": [" ".join(["ab"] * 23) + " zz"], "note": text, "more": hostile}
    options = {"who[]": {"class": "person"}, "note": {"class": "free_text"}}
    started = time.perf_counter()
    result = pseudonymize(data, options, vocabulary)
    assert time.perf_counter() - started < 10.0
    assert result.counts["free_text"] > 0
    for name in names[:50]:
        assert name not in result.data["note"]


def test_a_slow_de_identification_times_the_step_out(pack: Path, tmp_path: Path) -> None:
    class Slow:
        def pseudonymize(self, *args: Any) -> Any:
            time.sleep(0.5)

    provider = Provider("anthropic", _answer())
    llm = _backends(provider, CLOUD, tmp_path)
    plugins = _plugins(DATA)
    pipeline = _pipeline()
    step = pipeline.steps[2].llm  # type: ignore[union-attr]
    quick = pipeline.model_copy(
        update={
            "steps": [
                *pipeline.steps[:2],
                pipeline.steps[2].model_copy(
                    update={"llm": step.model_copy(update={"timeout_s": 0.1})}
                ),
            ]
        }
    )
    record = asyncio.run(
        execute(
            quick,
            run_id="20261027T053000Z-brief-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=llm,
            skills=DirectorySkills({"pack-daily-brief": pack}),
            deid=Slow(),
        )
    )
    error = record.steps[2].error
    assert error is not None
    assert (error.kind, provider.sent) == ("timeout", [])
    assert "de-identifying" in error.message


# --- finding 10: overlapping classes --------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {"events[]": {"class": "free_text"}, "events[].who": {"class": "person"}},
        {"events[].who": {"class": "person"}, "events[]": {"class": "free_text"}},
        {"a": {"class": "person"}, "a.b.c": {"class": "amount"}},
    ],
)
def test_overlapping_classes_are_refused(options: dict[str, Any]) -> None:
    with pytest.raises(DeidError, match="classed path"):
        pseudonymize({"events": [{"who": "Ann"}]}, options)


# --- finding F: every declared secret is checked ------------------------------------------


def test_a_declared_secret_never_resolved_is_still_refused(pack: Path, tmp_path: Path) -> None:
    redact.forget_all()
    folder = tmp_path / "declared"
    folder.mkdir()
    (folder / "PLANTED_TOKEN").write_text(PLANTED, encoding="utf-8")
    provider = Provider("anthropic", _answer())
    llm = ModelBackends(
        {"default": ModelAlias.model_validate(CLOUD)},
        SecretResolver({}, folder),
        {"anthropic": lambda alias, spec, secrets: provider},
        validate=UNVALIDATED,
        redaction_names=("PLANTED_TOKEN",),
    )
    data = {**DATA, "tasks": [{"title": f"Pay {PLANTED}"}]}
    record = _run_data(data, llm, pack, deid=_policy())
    error = record.steps[2].error
    assert error is not None
    assert error.kind == "secret_in_request"
    assert provider.sent == []
    assert [v.kind for v in record.violations] == ["secret_in_request"]


# --- decision A: horae doctor and an ollama alias that looks hosted -------------------------


@pytest.mark.parametrize(
    ("spec", "warned"),
    [
        (OLLAMA, False),
        ({**OLLAMA, "base_url": "http://127.0.0.1:11434"}, False),
        ({**OLLAMA, "base_url": "http://192.168.1.20:11434"}, False),
        ({**OLLAMA, "base_url": "https://gpu.lan"}, False),
        ({**OLLAMA, "base_url": "https://ollama.example.com"}, True),
        ({**OLLAMA, "base_url": "https://203.0.113.7"}, True),
        ({**OLLAMA, "model": "gpt-oss:120b-cloud"}, True),
        ({**OLLAMA, "model": "gpt-oss:120b-cloud", "local": True}, False),
        ({**OLLAMA, "base_url": "https://ollama.example.com", "local": False}, False),
    ],
)
def test_doctor_warns_about_an_ollama_alias_that_looks_hosted(
    tmp_path: Path, spec: dict[str, Any], warned: bool
) -> None:
    root = _write(tmp_path / "instance", {"default": spec}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 0
    found = [p for p in report["problems"] if p["message"].startswith("models.default.local:")]
    assert bool(found) is warned
    assert all(p["severity"] == "warning" for p in found)


def test_a_cache_entry_carries_the_de_identified_digest(pack: Path, tmp_path: Path) -> None:
    from horae.runner._cache import _parse, accepted_entry

    record = _run(
        _backends(Provider("anthropic", _answer()), CLOUD, tmp_path), pack, deid=_policy()
    )
    entry = accepted_entry(record)
    assert entry is not None
    digest = record.llm[0].request["deid"]["input_sha256"]
    assert entry.response["deid_input_sha256"] == digest
    parsed = _parse(entry.response)
    assert parsed is not None
    assert parsed.deid_input_sha256 == digest


def test_outer_white_space_and_over_long_terms() -> None:
    result = pseudonymize({"who": " Ann ", "note": "see ann"}, {"who": {"class": "person"}})
    assert result.data == {"note": "see [PERSON_1]", "who": "[PERSON_1]"}
    with pytest.raises(DeidError, match="vocabulary line 1: a term has at most 48"):
        Vocabulary.parse(("person: " + "a " * 30).encode())
