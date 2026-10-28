# SPDX-License-Identifier: Apache-2.0
"""De-identification before a cloud model, and the T10 request check (#34, decision 47).

Acceptance: declared fields never reach a cloud backend in clear (the rendered request is
inspected); the mapping is stable within a run and stored locally only (in memory: the
record, the ledger and the cache hold no original of a classed value); rendered output uses
real values again. Threat model (#14): a request holding a resolved secret is refused before
it is sent, the run is `degraded`, the value is never logged."""

import asyncio
import json
import logging
import shutil
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile, RunRecord, Usage
from horae.contracts.instance import BudgetsConfig, ModelAlias
from horae.contracts.pipeline import Budget
from horae.engine import LlmError, LlmRequest, PreviousLlm, execute
from horae.engine.canonical import canonical_json, sha256_hex
from horae.engine.ports import LlmBackendName
from horae.llm import UNVALIDATED, Completion, CompletionResult, ModelBackends
from horae.llm.base import secrets_in
from horae.llm.deid import (
    DEID_NOTE,
    Deidentifier,
    DeidError,
    Vocabulary,
    bucket,
    field_classes,
    load_deidentifier,
    pseudonymize,
    vocabulary_problem,
)
from horae.replay import replay
from horae.runner import Ports, Runner
from horae.runner._budget import LedgerLlm
from horae.secrets import SecretResolver, redact
from horae.skills import DirectorySkills, skill_dir
from horae.store import Store
from llm_backend_kinds import Kind
from test_engine_skills import CONTEXT, FIXTURE, SKILL, START, _pipeline, _plugins
from test_llm_backends_shared import backends, request
from test_llm_backends_shared import kind as kind
from test_llm_backends_shared import secrets as secrets
from test_llm_wiring import ANTHROPIC, LLM_STEP, OLLAMA, OPENAI, _doctor, _write

# --- synthetic personal data ------------------------------------------------------------------

DATA: dict[str, Any] = {
    "tasks": [
        {"title": "Dentist for Alice", "due": "2026-10-27", "notes": "private"},
        {"title": "Call Zoë about Annecy"},
    ],
    "events": [
        {
            "start": "09:00",
            "summary": "Lunch with Bob Martin",
            "attendees": ["Bob Martin", "alice@example.org"],
            "budget": 120,
        }
    ],
}
VIEW: dict[str, Any] = {
    "tasks[].title": {"class": "free_text"},
    "tasks[].due": None,
    "events[].start": None,
    "events[].summary": {"class": "free_text"},
    "events[].attendees[]": {"class": "person"},
    "events[].budget": {"class": "amount", "buckets": [0, 50, 200]},
}
OPTIONS = {path: options for path, options in VIEW.items() if options}
VOCABULARY = b"# synthetic\nperson: Alice\nperson: Zo\xc3\xab\nplace: Annecy\n"
ORIGINALS = ("Alice", "alice@example.org", "Zoë", "Annecy", "Bob Martin")
"""Every classed value of `DATA` (and its vocabulary terms)."""
SENT = {
    "events": [
        {
            "attendees": ["[PERSON_1]", "[PERSON_2]"],
            "budget": "50-200",
            "start": "09:00",
            "summary": "Lunch with [PERSON_1]",
        }
    ],
    "tasks": [
        {"due": "2026-10-27", "title": "Dentist for [PERSON_3]"},
        {"title": "Call [PERSON_4] about [PLACE_1]"},
    ],
}
"""What a cloud model is sent for `DATA` (the input view, then the pseudonyms)."""
OUTPUT = {
    "summary": "Lunch with [PERSON_1]; ask [PERSON_9] and Person 7",
    "ranked": [
        {"title": "Dentist for [PERSON_3]", "why": "today"},
        {"title": "Call [PERSON_4] about [PLACE_1]", "why": "later"},
    ],
}
NOTE = {"type": "note.create", "content": "- [ ] Dentist for [PERSON_3]\n"}
REAL_OUTPUT = {
    "summary": "Lunch with Bob Martin; ask [PERSON_9] and Person 7",
    "ranked": [
        {"title": "Dentist for Alice", "why": "today"},
        {"title": "Call Zoë about Annecy", "why": "later"},
    ],
}
REAL_NOTE = {"type": "note.create", "content": "- [ ] Dentist for Alice\n"}
UNRESOLVED = ["Person 7", "[PERSON_9]"]
CLOUD = {"backend": "anthropic", "model": "fixture-model", "secret": "ANTHROPIC_API_KEY"}
LOCAL = {"backend": "ollama", "model": "fixture-model", "base_url": "http://ollama:11434"}
PLANTED = "Sentinel-Request-Value-0123456789"  # synthetic


def _vocabulary() -> Vocabulary:
    return Vocabulary.parse(VOCABULARY)


def _forms(value: str) -> set[str]:
    """`value` as it may appear in a JSON body (raw, escaped, NFD)."""
    return {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        unicodedata.normalize("NFD", value),
    }


def _assert_clear_of_originals(text: str) -> None:
    for original in ORIGINALS:
        for form in _forms(original):
            assert form not in text, original
    assert "alice" not in text.casefold()


# --- the transforms ---------------------------------------------------------------------------


def test_classed_fields_get_pseudonyms_numbered_in_canonical_order() -> None:
    viewed = json.loads(json.dumps(DATA))
    for task in viewed["tasks"]:
        task.pop("notes", None)
    result = pseudonymize(viewed, OPTIONS, _vocabulary())
    assert result.data == SENT
    assert result.counts == {"amount": 1, "free_text": 4, "person": 4, "place": 1}
    assert result.summary()["counts"] == result.counts
    assert set(result.summary()) == {"version", "input_sha256", "counts"}


def test_the_same_input_gives_the_same_pseudonyms() -> None:
    reordered = {"tasks": DATA["tasks"], "events": DATA["events"]}
    one = pseudonymize(DATA, OPTIONS, _vocabulary())
    two = pseudonymize(reordered, OPTIONS, _vocabulary())
    assert canonical_json(one.data) == canonical_json(two.data)
    assert one.summary() == two.summary()


def test_a_repeated_value_keeps_its_pseudonym() -> None:
    data = {"people": ["Ann", "Bea", "ann", "Ann"], "phones": ["+33 6 00 00 00 00", 600000000]}
    options = {"people[]": {"class": "person"}, "phones[]": {"class": "phone"}}
    result = pseudonymize(data, options)
    assert result.data == {
        "people": ["[PERSON_1]", "[PERSON_2]", "[PERSON_1]", "[PERSON_1]"],
        "phones": ["[PHONE_1]", "[PHONE_2]"],
    }
    back, notes = result.reidentify(["[PHONE_2]", "[PERSON_1] and [PERSON_2]"])
    assert back == [600000000, "Ann and Bea"]  # a whole pseudonym keeps its JSON type
    assert notes == []


def test_objects_nulls_and_empty_strings_under_a_class() -> None:
    data = {"who": {"name": "Ann", "age": 7, "nick": "", "x": None, "ok": True}}
    result = pseudonymize(data, {"who": {"class": "person"}})
    assert result.data == {
        "who": {"age": "[PERSON_1]", "name": "[PERSON_2]", "nick": "", "ok": True, "x": None}
    }


@pytest.mark.parametrize(
    ("value", "bounds", "label"),
    [
        (120, (0.0, 50.0, 200.0), "50-200"),
        (50, (0.0, 50.0, 200.0), "50-200"),
        (-1, (0.0, 50.0, 200.0), "<0"),
        (200, (0.0, 50.0, 200.0), ">=200"),
        (0.5, (0.0, 0.25, 1.5), "0.25-1.5"),
        ("12 EUR", (0.0,), "[AMOUNT]"),
        (True, (0.0,), "[AMOUNT]"),
        (float("inf"), (0.0,), "[AMOUNT]"),
    ],
)
def test_amounts_become_buckets(value: Any, bounds: tuple[float, ...], label: str) -> None:
    assert bucket(value, bounds) == label


def test_amount_buckets_default_to_powers_of_ten() -> None:
    data = {"a": [5, 12345, None], "b": 3}
    result = pseudonymize(
        data, {"a[]": {"class": "amount"}, "b": {"class": "amount", "buckets": ["x", True]}}
    )
    assert result.data == {"a": ["0-10", ">=10000", None], "b": "0-10"}
    assert result.reidentify(result.data) == (result.data, [])  # not reversible


@pytest.mark.parametrize(
    ("text", "sent"),
    [
        ("Dentist for Alice", "Dentist for <alice>"),
        ("dentist for ALICE.", "dentist for <alice>."),
        ("Alice's party", "<alice>'s party"),
        ("Alicea and Malice are not Alice", "Alicea and Malice are not <alice>"),
        ("Anne Marie and Anne", "<anne marie> and <anne>"),
        ("Zoë, zoë and ZOË", "<zoë>, <zoë> and <zoë>"),
        (unicodedata.normalize("NFD", "Call Zoë"), "Call <zoë>"),
        ("Zoëlle", "Zoëlle"),
        ("Élodie in Annecy-le-Vieux", "<élodie> in <annecy>-le-Vieux"),
        ("no names here", "no names here"),
    ],
)
def test_vocabulary_terms_are_matched_as_whole_words(text: str, sent: str) -> None:
    vocabulary = Vocabulary.parse(
        "person: Alice\nperson: Anne\nperson: Anne Marie\nperson: Zoë\nperson: Élodie\n"
        "place: Annecy\n".encode()
    )
    result = pseudonymize({"note": text}, {"note": {"class": "free_text"}}, vocabulary)
    shown = result.data["note"]
    for token, value in result._values.items():
        shown = shown.replace(token, f"<{str(value).casefold()}>")
    assert shown == sent
    back, notes = result.reidentify(result.data["note"])
    assert back.casefold() == unicodedata.normalize("NFC", text).casefold()
    assert notes == []


def test_classed_values_are_replaced_in_free_text_too() -> None:
    data = {"who": ["Bob Martin"], "note": "Lunch with bob martin, not Bob"}
    options = {"who[]": {"class": "person"}, "note": {"class": "free_text"}}
    result = pseudonymize(data, options)
    assert result.data == {"note": "Lunch with [PERSON_1], not Bob", "who": ["[PERSON_1]"]}


def test_free_text_without_terms_is_sent_as_it_is() -> None:
    data = {"note": "Lunch with Bob", "n": 3}
    options = {"note": {"class": "free_text"}, "n": {"class": "free_text"}}
    assert pseudonymize(data, options).data == data


def test_unclassed_fields_and_keys_are_left_alone() -> None:
    data = {"Alice": "Alice", "note": "Alice"}
    result = pseudonymize(data, {"note": {"class": "free_text"}}, _vocabulary())
    assert result.data == {"Alice": "Alice", "note": "[PERSON_1]"}


def test_reidentification_of_output_and_intents() -> None:
    result = pseudonymize(DATA, OPTIONS, _vocabulary())
    output, notes = result.reidentify(OUTPUT)
    assert output == REAL_OUTPUT
    assert notes == UNRESOLVED
    intents, notes = result.reidentify([NOTE, {"[PERSON_1]": 1, "[PERSON_3]": 2, "Alice": 3}])
    assert intents == [REAL_NOTE, {"Bob Martin": 1, "[PERSON_3]": 2, "Alice": 3}]
    assert notes == []


@pytest.mark.parametrize(
    "mangled", ["Person 1", "PERSON1", "[person_1]", "[PLACE-1]", "person#2", "[EMAIL_1]"]
)
def test_unknown_or_mangled_pseudonyms_are_left_and_noted(mangled: str) -> None:
    result = pseudonymize({"who": "Ann"}, {"who": {"class": "person"}})
    back, notes = result.reidentify({"text": f"see {mangled} today"})
    assert back == {"text": f"see {mangled} today"}
    assert notes == [mangled]


def test_notes_are_bounded() -> None:
    result = pseudonymize({"who": "Ann"}, {"who": {"class": "person"}})
    _, notes = result.reidentify([f"[PERSON_{n}]" for n in range(2, 60)])
    assert len(notes) == 16


def test_nothing_prints_the_mapping_or_the_vocabulary() -> None:
    result = pseudonymize(DATA, OPTIONS, _vocabulary())
    for text in (repr(result), repr(_vocabulary()), repr(Deidentifier({}, _vocabulary()))):
        _assert_clear_of_originals(text)


def test_field_classes_of_the_view_options() -> None:
    assert field_classes(OPTIONS) == {
        "events[].attendees[]": "person",
        "events[].budget": "amount",
        "events[].summary": "free_text",
        "tasks[].title": "free_text",
    }
    assert field_classes({"a": {"note": "x"}, "b": "not options"}) == {}


# --- the vocabulary -----------------------------------------------------------------------------


def test_the_vocabulary_format() -> None:
    vocabulary = Vocabulary.parse(b"\n# comment\n  person:  Alice  \n")
    assert vocabulary.terms == (("person", "Alice"),)
    assert Vocabulary.parse(b"person: Alice\nperson: ALICE\nplace: Alice\n").terms == (
        ("person", "Alice"),
        ("place", "Alice"),
    )


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (b"Alice\n", "line 1: expected `<class>: <term>`"),
        (b"friend: Alice\n", "line 1: expected"),
        (b"amount: Alice\n", "line 1: expected"),
        (b"person: A\n", "line 1: a term has 2 to 200"),
        (b"person: Al\x07ice\n", "line 1: a term has"),
        (b"person: " + b"x" * 201 + b"\n", "a term has"),
        (b"person: Al\xffice\n", "not valid UTF-8"),
        (b"".join(b"person: t%d\n" % n for n in range(4097)), "more than 4096 terms"),
    ],
)
def test_a_bad_vocabulary_is_refused_without_its_content(data: bytes, needle: str) -> None:
    with pytest.raises(DeidError) as info:
        Vocabulary.parse(data)
    assert needle in str(info.value)
    assert "Al" not in str(info.value)


def test_the_vocabulary_is_read_from_the_secrets_directory(tmp_path: Path) -> None:
    (tmp_path / "vocab.txt").write_bytes(VOCABULARY)
    resolver = SecretResolver({}, tmp_path)
    policy = load_deidentifier({}, resolver, "vocab.txt")
    assert policy.vocabulary is not None
    assert policy.vocabulary.terms == _vocabulary().terms
    assert resolver.resolved() == ()  # not a secret: never registered for redaction
    assert load_deidentifier({}, resolver, None).vocabulary is None


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        (lambda d: None, "cannot be read: file"),
        (lambda d: (d / "vocab.txt").mkdir(), "is not a regular file"),
        (lambda d: (d / "vocab.txt").write_bytes(b"x" * 70000), "is larger than"),
        (lambda d: (d / "vocab.txt").write_bytes(b"Alice\n"), "vocab.txt is invalid: vocabulary"),
    ],
)
def test_an_unusable_vocabulary_fails_closed(tmp_path: Path, setup: Any, needle: str) -> None:
    setup(tmp_path)
    resolver = SecretResolver({}, tmp_path)
    problem = vocabulary_problem(resolver, "vocab.txt")
    assert isinstance(problem, str)
    assert needle in problem
    policy = load_deidentifier({"default": ModelAlias.model_validate(CLOUD)}, resolver, "vocab.txt")
    with pytest.raises(DeidError):
        policy.pseudonymize("default", OPTIONS, DATA)
    assert policy.cache_settings("default") == {"version": 1, "vocabulary": "unavailable"}


@pytest.mark.parametrize("name", ["", "../vocab", "a/b", ".hidden", "a\\b"])
def test_the_vocabulary_file_name_is_one_segment(tmp_path: Path, name: str) -> None:
    problem = vocabulary_problem(SecretResolver({}, tmp_path), name)
    assert isinstance(problem, str)
    assert "invalid file name" in problem


# --- local or cloud ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "local"),
    [
        (CLOUD, False),
        ({"backend": "claude-cli", "model": "sonnet"}, False),
        ({"backend": "openai-compatible", "model": "m", "base_url": "http://vllm:8000/v1"}, False),
        (
            {
                "backend": "openai-compatible",
                "model": "m",
                "base_url": "http://vllm/v1",
                "local": True,
            },
            True,
        ),
        (LOCAL, True),
        ({**LOCAL, "local": False}, False),
        ({**LOCAL, "base_url": "https://ollama.example.com"}, True),  # never from base_url
    ],
)
def test_local_or_cloud_is_explicit(spec: Mapping[str, Any], local: bool) -> None:
    alias = ModelAlias.model_validate(spec)
    assert alias.is_local is local
    policy = Deidentifier({"default": alias}, _vocabulary())
    assert (policy.pseudonymize("default", OPTIONS, DATA) is None) is local
    assert (policy.cache_settings("default") is None) is local


def test_the_policy_skips_a_skill_without_classes_and_treats_unknown_aliases_as_cloud() -> None:
    policy = Deidentifier({"default": ModelAlias.model_validate(LOCAL)})
    assert policy.pseudonymize("default", OPTIONS, DATA) is None
    assert policy.pseudonymize("other", {"a": {"note": "x"}}, DATA) is None
    assert policy.pseudonymize("other", OPTIONS, DATA) is not None
    assert policy.cache_settings("other") == {"version": 1, "vocabulary": None}
    assert policy.cache_settings(None) is None


# --- the engine and ModelBackends ---------------------------------------------------------------


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """The fixture skill with field classes in its input view."""
    root = tmp_path / "pack-daily-brief"
    shutil.copytree(FIXTURE, root)
    manifest = skill_dir(root, SKILL) / "skill.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["input_view"] = VIEW
    manifest.write_text(yaml.safe_dump(data))
    return root


class Provider:
    """A provider backend that records the bodies it is asked to send and answers `output`."""

    may_write_cache = False

    def __init__(self, name: LlmBackendName, output: Any) -> None:
        self.name: LlmBackendName = name
        self.output = output
        self.completions: list[Completion] = []
        self.sent: list[bytes] = []

    def render(self, completion: Completion) -> dict[str, Any]:
        self.completions.append(completion)
        return {"model": completion.model, "system": completion.system, "input": completion.input}

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.sent.append(canonical_json(dict(payload)))
        return CompletionResult(self.output, Usage(tokens_in=10, tokens_out=5, usd=0.0))

    async def aclose(self) -> None:
        return None


def _backends(provider: Provider, spec: Mapping[str, Any], tmp_path: Path) -> ModelBackends:
    return ModelBackends(
        {"default": ModelAlias.model_validate(spec)},
        SecretResolver({}, tmp_path),
        {provider.name: lambda alias, spec, secrets: provider},
        validate=UNVALIDATED,
    )


def _policy(spec: Mapping[str, Any] = CLOUD) -> Deidentifier:
    return Deidentifier({"default": ModelAlias.model_validate(spec)}, _vocabulary())


def _run(llm: Any, pack: Path, **kwargs: Any) -> RunRecord:
    plugins = _plugins(DATA)
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


def _answer() -> dict[str, Any]:
    return {**OUTPUT, "intents": [NOTE]}


def test_classed_fields_never_reach_a_cloud_request(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    record = _run(_backends(provider, CLOUD, tmp_path), pack, deid=_policy())
    assert record.status == "ok", record.steps[2].error
    [body] = provider.sent
    _assert_clear_of_originals(body.decode())
    payload = json.loads(body)
    assert payload["input"] == SENT
    assert payload["system"].endswith(DEID_NOTE)
    # The record keeps what the provider saw and returned; the run goes on with real values.
    [call] = record.llm
    assert call.response == OUTPUT
    assert call.intents == [NOTE]
    _assert_clear_of_originals(json.dumps([c.model_dump(mode="json") for c in record.llm]))
    assert call.request["deid"]["counts"] == {"amount": 1, "free_text": 4, "person": 4, "place": 1}
    assert call.request["deid"]["unresolved"] == UNRESOLVED
    assert record.steps[2].output == REAL_OUTPUT
    assert [i.intent.model_dump(mode="json")["content"] for i in record.intents] == [
        REAL_NOTE["content"]
    ]


@pytest.mark.parametrize(
    ("name", "spec"),
    [
        ("ollama", LOCAL),
        (
            "openai-compatible",
            {
                "backend": "openai-compatible",
                "model": "m",
                "base_url": "http://vllm/v1",
                "local": True,
            },
        ),
    ],
)
def test_a_local_alias_is_sent_the_viewed_input_unchanged(
    pack: Path, tmp_path: Path, name: LlmBackendName, spec: Mapping[str, Any]
) -> None:
    real = {**REAL_OUTPUT, "intents": [REAL_NOTE]}
    provider = Provider(name, real)
    record = _run(_backends(provider, spec, tmp_path), pack, deid=_policy(spec))
    assert record.status == "ok", record.steps[2].error
    payload = json.loads(provider.sent[0])
    assert payload["input"]["tasks"][0]["title"] == "Dentist for Alice"
    assert payload["input"]["events"][0]["budget"] == 120
    assert DEID_NOTE not in payload["system"]
    assert "deid" not in record.llm[0].request
    assert record.llm[0].response == REAL_OUTPUT


def test_ollama_marked_cloud_is_de_identified(pack: Path, tmp_path: Path) -> None:
    spec = {**LOCAL, "local": False}
    provider = Provider("ollama", _answer())
    record = _run(_backends(provider, spec, tmp_path), pack, deid=_policy(spec))
    assert record.status == "ok"
    assert json.loads(provider.sent[0])["input"] == SENT


def test_without_a_policy_a_classified_cloud_request_is_refused(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    record = _run(_backends(provider, CLOUD, tmp_path), pack)
    assert record.status == "degraded"
    error = record.steps[2].error
    assert error is not None
    assert error.kind == "deid_required"
    assert provider.completions == []
    assert provider.sent == []


def test_an_unusable_vocabulary_degrades_before_anything_is_sent(
    pack: Path, tmp_path: Path
) -> None:
    provider = Provider("anthropic", _answer())
    policy = Deidentifier(
        {"default": ModelAlias.model_validate(CLOUD)}, vocabulary_error="unreadable"
    )
    record = _run(_backends(provider, CLOUD, tmp_path), pack, deid=policy)
    error = record.steps[2].error
    assert error is not None
    assert (record.status, error.kind) == ("degraded", "deid_unavailable")
    assert provider.sent == []


def test_a_broken_policy_is_contained(pack: Path, tmp_path: Path) -> None:
    class Broken:
        def pseudonymize(self, *args: Any) -> Any:
            raise RuntimeError("Alice")

    provider = Provider("anthropic", _answer())
    record = _run(_backends(provider, CLOUD, tmp_path), pack, deid=Broken())
    error = record.steps[2].error
    assert error is not None
    assert (error.kind, provider.sent) == ("host_error", [])
    assert "Alice" not in error.message


def test_two_runs_send_byte_identical_requests(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    llm = _backends(provider, CLOUD, tmp_path)
    one = _run(llm, pack, deid=_policy())
    two = _run(llm, pack, deid=_policy())
    assert provider.sent[0] == provider.sent[1]
    assert one.llm[0].input_hash == two.llm[0].input_hash
    assert one.llm[0].request == two.llm[0].request


def test_a_schema_failure_is_recorded_pseudonymized(pack: Path, tmp_path: Path) -> None:
    class Twice(Provider):
        async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
            self.sent.append(canonical_json(dict(payload)))
            output = {"summary": "[PERSON_3]"} if len(self.sent) == 1 else self.output
            return CompletionResult(output, Usage(tokens_in=10, tokens_out=5, usd=0.0))

    provider = Twice("anthropic", _answer())
    llm = ModelBackends(
        {"default": ModelAlias.model_validate(CLOUD)},
        SecretResolver({}, tmp_path),
        {"anthropic": lambda alias, spec, secrets: provider},
    )
    record = _run(llm, pack, deid=_policy())
    assert record.status == "ok", record.steps[2].error
    first, second = record.llm
    assert first.schema_valid is False
    assert first.response == {"summary": "[PERSON_3]"}
    assert "unresolved" not in first.request["deid"]
    assert second.request["deid"]["unresolved"] == UNRESOLVED
    assert json.loads(provider.sent[1])["input"] == SENT


def test_a_reused_output_is_re_identified(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    llm = _backends(provider, CLOUD, tmp_path)
    first = _run(llm, pack, deid=_policy())
    assert first.input_hash is not None
    previous = PreviousLlm(
        input_hash=first.input_hash,
        output=first.llm[0].response,
        intents=first.llm[0].intents,
        skill_version=first.llm[0].skill_version,
        model=first.llm[0].model,
        backend=first.llm[0].backend,
    )
    second = _run(llm, pack, deid=_policy(), previous=previous)
    assert len(provider.sent) == 1  # no call
    assert second.llm[0].cached is True
    assert second.llm[0].response == OUTPUT
    assert second.llm[0].request["deid"]["unresolved"] == UNRESOLVED
    assert second.steps[2].output == REAL_OUTPUT
    assert second.intents[0].intent.model_dump(mode="json")["content"] == REAL_NOTE["content"]


def test_a_de_identified_run_replays_identically(pack: Path, tmp_path: Path) -> None:
    provider = Provider("anthropic", _answer())
    record = _run(_backends(provider, CLOUD, tmp_path), pack, deid=_policy())
    plugins = _plugins(DATA)
    skills = DirectorySkills({"pack-daily-brief": pack})
    same = replay(record, _pipeline(), plugins=plugins, skills=skills, deid=_policy())
    assert same.outcome == "identical", (same.error, same.mismatches, same.intents_diff)
    other = Deidentifier(
        {"default": ModelAlias.model_validate(CLOUD)}, Vocabulary.parse(b"person: Bea\n")
    )
    changed = replay(record, _pipeline(), plugins=plugins, skills=skills, deid=other)
    assert changed.outcome == "error"
    assert any("the input differs" in m for m in changed.mismatches)
    without = replay(record, _pipeline(), plugins=plugins, skills=skills)
    assert without.outcome == "error"


# --- T10: no resolved secret leaves in a request ---------------------------------------------


def _plant(tmp_path: Path) -> None:
    folder = tmp_path / "planted"
    folder.mkdir()
    (folder / "PLANTED_TOKEN").write_text(PLANTED, encoding="utf-8")
    SecretResolver({}, folder).resolve("PLANTED_TOKEN")


@pytest.mark.parametrize(
    "encode",
    [
        lambda value: value,
        lambda value: f"prefix {value} suffix",
        lambda value: __import__("base64").b64encode(value.encode()).decode(),
        lambda value: value.encode().hex(),
        lambda value: {value: "as a key"},
    ],
    ids=["raw", "embedded", "base64", "hex", "key"],
)
def test_a_request_holding_a_secret_is_refused_by_every_backend(
    kind: Kind,
    secrets: SecretResolver,
    tmp_path: Path,
    encode: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _plant(tmp_path)
    recorder = kind.recorder("success")
    llm = backends(kind, secrets, recorder)
    req = request(input=[{"title": encode(PLANTED)}])

    async def go() -> LlmError:
        try:
            rendered = llm.render(req)
            assert secrets_in(rendered.body()) == {"PLANTED_TOKEN"}
            with pytest.raises(LlmError) as info:
                await llm.send(rendered)
            return info.value
        finally:
            await llm.aclose()

    with caplog.at_level(logging.DEBUG):
        error = asyncio.run(go())
    assert error.kind == "secret_in_request"
    assert "PLANTED_TOKEN" in error.message
    assert error.usage is None  # nothing was sent: nothing is billed
    assert recorder.requests == []
    assert PLANTED not in error.message
    assert PLANTED not in caplog.text
    assert "PLANTED_TOKEN" in caplog.text


def test_a_clean_request_passes_the_check(
    kind: Kind, secrets: SecretResolver, tmp_path: Path
) -> None:
    _plant(tmp_path)
    recorder = kind.recorder("success")
    llm = backends(kind, secrets, recorder)

    async def go() -> Any:
        try:
            return await llm.complete(request())
        finally:
            await llm.aclose()

    assert asyncio.run(go()).output is not None
    assert len(recorder.requests) == 1


def test_a_refused_request_is_not_billed(
    kind: Kind, secrets: SecretResolver, tmp_path: Path
) -> None:
    _plant(tmp_path)
    recorder = kind.recorder("success")
    llm = backends(kind, secrets, recorder)
    at = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    with Store.open(tmp_path / "horae.db") as store:
        store.begin_run("run-a", "brief", at)
        ledger = LedgerLlm(llm, store, "run-a", "brief", 1, BudgetsConfig(), "UTC", lambda: at)
        with pytest.raises(LlmError) as info:
            asyncio.run(ledger.complete(request(input=[{"title": PLANTED}])))
        assert info.value.kind == "secret_in_request"
        assert store.usage_report() == []
    assert recorder.requests == []


def test_the_engine_degrades_and_records_a_violation(pack: Path, tmp_path: Path) -> None:
    _plant(tmp_path)
    data = {**DATA, "tasks": [{"title": f"Pay {PLANTED}"}]}
    provider = Provider("anthropic", _answer())
    plugins = _plugins(data)
    record = asyncio.run(
        execute(
            _pipeline(),
            run_id="20261027T053000Z-brief-01",
            context=CONTEXT,
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=_backends(provider, CLOUD, tmp_path),
            skills=DirectorySkills({"pack-daily-brief": pack}),
            deid=_policy(),
        )
    )
    assert record.status == "degraded"
    error = record.steps[2].error
    assert error is not None
    assert error.kind == "secret_in_request"
    [violation] = record.violations
    assert violation.kind == "secret_in_request"
    assert "PLANTED_TOKEN" in violation.detail
    assert provider.sent == []
    assert record.llm == []
    shown = json.dumps(
        [error.model_dump(mode="json"), violation.model_dump(mode="json")], ensure_ascii=False
    )
    assert PLANTED not in shown


def test_without_resolved_secrets_nothing_is_found() -> None:
    redact.forget_all()
    assert secrets_in(b'{"a":"b"}') == set()


def test_a_numeric_secret_in_a_body_is_found(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "PIN").write_text("1234567890123", encoding="utf-8")
    SecretResolver({}, folder).resolve("PIN")
    assert secrets_in(canonical_json({"n": 1234567890123})) == {"PIN"}


# --- through the runner: what the store keeps ---------------------------------------------------


def _instance(root: Path) -> Instance:
    secrets = root / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "vocab.txt").write_bytes(VOCABULARY)
    (secrets / "ANTHROPIC_API_KEY").write_text("sentinel-key-for-the-fixture", encoding="utf-8")
    config = InstanceConfig.model_validate(
        {
            "models": {"default": CLOUD},
            "secrets": {"dir": "secrets"},
            "deidentification": {"vocabulary": "vocab.txt"},
        }
    )
    return Instance(
        root=root, config=config, pipelines={"brief": _pipeline()}, lock=Lockfile(schema_version=1)
    )


@pytest.fixture
def inst(tmp_path: Path) -> Instance:
    return _instance(tmp_path / "instance")


@pytest.fixture
def store(inst: Instance) -> Iterator[Store]:
    with Store.open(inst.store_path) as opened:
        yield opened


_TABLES = {"llm_usage": "SELECT * FROM llm_usage", "llm_cache": "SELECT * FROM llm_cache"}


def _rows(path: Path, table: str) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        rows = conn.execute(_TABLES[table]).fetchall()
    return json.dumps(rows, default=str, ensure_ascii=False)


def test_the_store_keeps_no_original_of_a_classed_value(
    inst: Instance, store: Store, pack: Path
) -> None:
    provider = Provider("anthropic", _answer())
    llm = ModelBackends(
        inst.config.models,
        inst.secret_resolver(),
        {"anthropic": lambda alias, spec, secrets: provider},
        validate=UNVALIDATED,
    )
    plugins = _plugins(DATA)
    skills = DirectorySkills({"pack-daily-brief": pack})
    runner = Runner(inst, store, Ports(plugins, plugins, llm, skills=skills))
    first = asyncio.run(runner.run("brief"))
    assert first.status == "ok", first.steps[2].error
    _assert_clear_of_originals(provider.sent[0].decode())
    assert first.steps[2].output == REAL_OUTPUT
    # The ledger, the cache and the LLM part of the record hold no original.
    for table in ("llm_usage", "llm_cache"):
        text = _rows(inst.store_path, table)
        assert text != "[]", table
        _assert_clear_of_originals(text)
    stored = store.get_run(first.id)
    assert stored is not None
    _assert_clear_of_originals(json.dumps([c.model_dump(mode="json") for c in stored.llm]))
    # A cache hit: no call, re-identified with the mapping the same input gives again.
    second = asyncio.run(runner.run("brief"))
    assert len(provider.sent) == 1
    assert second.llm[0].cached is True
    assert second.steps[2].output == REAL_OUTPUT
    assert second.intents[0].intent.model_dump(mode="json")["content"] == REAL_NOTE["content"]


def test_another_vocabulary_is_a_cache_miss(inst: Instance, store: Store, pack: Path) -> None:
    provider = Provider("anthropic", _answer())
    llm = ModelBackends(
        inst.config.models,
        inst.secret_resolver(),
        {"anthropic": lambda alias, spec, secrets: provider},
        validate=UNVALIDATED,
    )
    plugins = _plugins(DATA)
    skills = DirectorySkills({"pack-daily-brief": pack})
    runner = Runner(inst, store, Ports(plugins, plugins, llm, skills=skills))
    asyncio.run(runner.run("brief"))
    (inst.secrets_dir / "vocab.txt").write_bytes(VOCABULARY + b"person: Bea\n")
    asyncio.run(runner.run("brief"))
    assert len(provider.sent) == 2


def test_a_budget_request_shape_is_unchanged_without_classes() -> None:
    req = LlmRequest("s", "default", [], Budget(usd=1.0, tokens_out=10), 1.0, 1)
    assert (req.classified, req.deidentified) == (False, None)


# --- horae doctor: local or cloud, the vocabulary ---------------------------------------------


@pytest.mark.parametrize(
    ("spec", "warned"),
    [
        (OLLAMA, False),
        ({**OLLAMA, "local": False}, True),
        (OPENAI, True),
        ({**OPENAI, "local": True}, False),
    ],
)
def test_doctor_expects_a_price_from_cloud_aliases_only(
    tmp_path: Path, spec: Mapping[str, Any], warned: bool
) -> None:
    root = _write(tmp_path / "instance", {"default": spec}, [LLM_STEP])
    code, report = _doctor(root)
    assert code == 0
    prices = [p for p in report["problems"] if "has no price" in p["message"]]
    assert bool(prices) is warned


def test_an_unset_local_flag_leaves_the_config_digest_unchanged(tmp_path: Path) -> None:
    inst = _instance(tmp_path / "instance")
    data = inst.config.model_dump(mode="json", include={"models", "plugins"})
    del data["models"]["default"]["local"]  # as recorded before #34
    assert inst.config_digest() == sha256_hex(data)
    marked = InstanceConfig.model_validate({"models": {"default": {**CLOUD, "local": False}}})
    other = Instance(root=inst.root, config=marked, pipelines={}, lock=inst.lock)
    assert other.config_digest() != inst.config_digest()


def _with_vocabulary(root: Path, content: bytes | None) -> None:
    config = yaml.safe_load((root / "horae.yaml").read_text(encoding="utf-8"))
    config["deidentification"] = {"vocabulary": "vocab.txt"}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if content is not None:
        (root / "secrets" / "vocab.txt").write_bytes(content)


def test_doctor_reports_an_unusable_vocabulary(tmp_path: Path) -> None:
    root = _write(tmp_path / "instance", {"default": ANTHROPIC}, [LLM_STEP])
    _with_vocabulary(root, VOCABULARY)
    code, report = _doctor(root)
    assert (code, report["problems"]) == (0, [])
    _with_vocabulary(root, b"Alice\n")
    code, report = _doctor(root)
    assert code == 1
    [problem] = report["problems"]
    assert problem["message"].startswith("deidentification.vocabulary: the de-identification")
    assert "Alice" not in problem["message"]
    (root / "secrets" / "vocab.txt").unlink()
    code, report = _doctor(root)
    assert code == 1
    assert "cannot be read" in report["problems"][0]["message"]
