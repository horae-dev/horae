# SPDX-License-Identifier: Apache-2.0
"""The public replay backend, the request digest of a case and the `not_contains` / `matches`
assertions (#134, schemas decision 64)."""

import asyncio
import json
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from horae import cli_run
from horae.apply import StaticSinks
from horae.cli import app
from horae.config import Instance
from horae.contracts import InstanceConfig, Lockfile
from horae.contracts.evals import EvalAssertion, RecordedRequest, RecordedResponse
from horae.contracts.plugin import PluginManifest
from horae.contracts.run import Usage
from horae.engine import LlmRequest
from horae.evals import EvalError, SkillUnderEval, load_cases, request_digest
from horae.evals import _match as match
from horae.evals._harness import _Replay, assertion_check
from horae.llm import UNVALIDATED, CompletionResult, Prompt
from horae.llm.replay import Recordings, ReplayBackend, replay_backends
from horae.runner import Ports, Runner
from horae.skills import load_skill
from horae.store import Store
from runner_fixtures import PIPELINES, Plugins, pipeline, write_instance
from test_llm_anthropic import SCHEMA, SYSTEM

runner = CliRunner()
PACK = Path(__file__).resolve().parent / "fixtures" / "plugins" / "pack-daily-brief"
SKILL = "daily-brief/rank-and-word"
ANTHROPIC = {"backend": "anthropic", "model": "claude-fixture-1", "secret": "ANTHROPIC_API_KEY"}


class Prompts:
    def prompt(self, request: LlmRequest) -> Prompt:
        return Prompt(SYSTEM, request.input, SCHEMA, "1.2.0")


def _recording(request: dict[str, Any], output: Any) -> RecordedResponse:
    return RecordedResponse.model_validate(
        {
            "schema_version": 1,
            "backend": "anthropic",
            "model": "claude-fixture-1",
            "skill": "pack/brief",
            "skill_version": "1.2.0",
            "skill_digest": "0" * 64,
            "request": {k: request[k] for k in ("system_sha256", "input_sha256", "schema_sha256")},
            "output": output,
            "usage": Usage(tokens_in=10, tokens_out=5, usd=0.0).model_dump(),
        }
    )


# --- the assertions ---------------------------------------------------------------------------

DOCUMENT = {
    "output": {"summary": "Two reviews are waiting.", "tags": ["a", "b"], "n": 3},
    "intents": [],
}


def check(**spec: Any) -> tuple[bool, str | None]:
    result = assertion_check(EvalAssertion.model_validate(spec), DOCUMENT)
    return result.ok, result.message


def test_not_contains() -> None:
    assert check(pointer="/output/summary", not_contains="TODO") == (True, None)
    assert check(pointer="/output/summary", not_contains="reviews") == (False, "contained")
    assert check(pointer="/output/tags", not_contains="c") == (True, None)
    assert check(pointer="/output/tags", not_contains="a") == (False, "contained")
    # something must be there: a mistyped pointer cannot pass; a number is not a container
    assert check(pointer="/output/nope", not_contains="x") == (False, "nothing there")
    ok, message = check(pointer="/output/n", not_contains=3)
    assert (ok, message) == (False, "not a string or an array (int)")
    ok, message = check(pointer="/output/summary", not_contains=3)
    assert (ok, message) == (False, "not_contains on a string needs a string")


def test_matches() -> None:
    assert check(pointer="/output/summary", matches=r"^Two \w+ are waiting\.$") == (True, None)
    assert check(pointer="/output/summary", matches="(?i)REVIEWS") == (True, None)
    assert check(pointer="/output/summary", matches=r"\d") == (False, "no match")
    assert check(pointer="/output/n", matches="3") == (False, "not a string (int)")
    assert check(pointer="/output/nope", matches="x") == (False, "nothing there")


def test_an_assertion_has_exactly_one_check_and_a_valid_pattern() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        EvalAssertion.model_validate({"pointer": "", "matches": "a", "not_contains": "a"})
    with pytest.raises(ValidationError, match="not a valid regular expression"):
        EvalAssertion.model_validate({"pointer": "", "matches": "("})
    with pytest.raises(ValidationError):
        EvalAssertion.model_validate({"pointer": "", "matches": "a" * 257})
    with pytest.raises(ValidationError):
        EvalAssertion.model_validate({"pointer": "", "matches": ""})


def test_matches_is_bounded_against_redos() -> None:
    """`(a+)+$` takes exponential time in `re`: the search is killed, and the assertion fails
    (never hangs, never passes)."""
    hostile = "a" * 40 + "b"
    started = time.monotonic()
    ok, message = check_text(r"(a+)+$", hostile)
    assert time.monotonic() - started < match.MATCH_TIMEOUT_S + 5
    assert not ok
    assert message is not None
    assert message.startswith("the pattern could not be searched")


def check_text(pattern: str, text: str) -> tuple[bool, str | None]:
    result = assertion_check(
        EvalAssertion.model_validate({"pointer": "/t", "matches": pattern}), {"t": text}
    )
    return result.ok, result.message


def test_matches_bounds_the_text_and_the_worker() -> None:
    ok, message = check_text("a", "a" * (match.MAX_SUBJECT_CHARS + 1))
    assert (ok, message) == (False, f"the text is longer than {match.MAX_SUBJECT_CHARS} characters")
    assert check_text("a", "a" * match.MAX_SUBJECT_CHARS) == (True, None)
    with pytest.raises(match.MatchTimeout):
        match.search(r"(a+)+$", "a" * 40 + "b", timeout_s=0.5)
    assert match.search(r"(a+)+$", "aaa") is True


def test_the_exported_schema_has_the_new_checks() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/eval-case/v1.json").read_text()
    )
    assertion = schema["$defs"]["EvalAssertion"]
    assert {"not_contains", "matches"} <= set(assertion["properties"])
    assert {"required": ["matches"]} in assertion["oneOf"]
    assert {"required": ["not_contains"]} in assertion["oneOf"]


# --- the request digest of a case -------------------------------------------------------------


def _sue() -> SkillUnderEval:
    with (PACK / "plugin.yaml").open(encoding="utf-8") as handle:
        plugin = PluginManifest.model_validate(yaml.safe_load(handle))
    skill = load_skill(PACK, SKILL, allowed_intents=plugin.permissions.intents)
    return SkillUnderEval(skill, plugin)


def test_the_request_digest_of_a_case_is_the_one_recorded() -> None:
    sue = _sue()
    cases = load_cases(PACK / "skills/daily-brief/rank-and-word/evals")
    assert cases
    for loaded in cases:
        digest = request_digest(sue, loaded)
        assert isinstance(digest, RecordedRequest)
        assert digest == loaded.recorded["anthropic"].request
        # independent of the backend and the model (a local backend: no de-identification)
        assert request_digest(sue, loaded, backend="anthropic", model="other") == digest


def test_the_request_digest_follows_the_input() -> None:
    sue = _sue()
    first = load_cases(PACK / "skills/daily-brief/rank-and-word/evals")[0]
    before = request_digest(sue, first)
    first.case.input["tasks"] = [] if isinstance(first.case.input, dict) else None
    after = request_digest(sue, first)
    assert before.system_sha256 == after.system_sha256
    assert before.schema_sha256 == after.schema_sha256


def test_a_case_that_cannot_be_rendered_has_no_digest() -> None:
    sue = _sue()
    loaded = load_cases(PACK / "skills/daily-brief/rank-and-word/evals")[0]
    loaded.case.input = 42
    with pytest.raises(EvalError, match=f"case {loaded.name}"):
        request_digest(sue, loaded)


# --- a stale recording fails the eval ---------------------------------------------------------


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    root = tmp_path / "plugins" / "pack-daily-brief"
    shutil.copytree(PACK, root)
    return root


def test_a_stale_recording_fails_the_eval_naming_the_case(pack: Path) -> None:
    prompt = pack / "skills/daily-brief/rank-and-word/prompt.md"
    prompt.write_text(prompt.read_text() + "\nBe brief.\n")
    result = runner.invoke(app, ["eval", SKILL, "--plugin-dir", str(pack), "--json"])
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert {c["status"] for c in document["cases"]} == {"error"}
    for case in document["cases"]:
        assert case["error"]["kind"] == "stale_recording"
        assert f"case {case['case']}:" in case["error"]["message"]
        assert "prompt" in case["error"]["message"]
        assert "--live --record" in case["error"]["message"]
    text = runner.invoke(app, ["eval", SKILL, "--plugin-dir", str(pack)])
    assert text.exit_code == 1
    assert "stale_recording: case overdue-first: the recording for backend anthropic" in (
        text.output
    )


# --- the replay backend and `horae run --llm-replay` ------------------------------------------


def _instance(root: Path) -> Instance:
    return Instance(
        root=root,
        config=InstanceConfig.model_validate(
            {"models": {"default": ANTHROPIC}, "secrets": {"dir": "secrets"}}
        ),
        pipelines={"brief": pipeline("brief")},
        lock=Lockfile(schema_version=1),
    )


def _replay_run(inst: Instance, source: Any) -> Any:
    llm = replay_backends(inst.config.models, source, validate=UNVALIDATED, prompts=Prompts())
    with Store.open(inst.store_path) as store:
        plugins = Plugins()
        record = asyncio.run(Runner(inst, store, Ports(plugins, plugins, llm)).run("brief"))
    asyncio.run(llm.aclose())
    return record


OUTPUT = {"items": ["Review PR 42"], "summary": "One review is waiting."}


def test_a_run_replays_a_recording_by_digest(tmp_path: Path) -> None:
    inst = _instance(tmp_path / "instance")
    first = _replay_run(inst, CompletionResult(OUTPUT, Usage(tokens_in=10, tokens_out=5, usd=0.0)))
    assert first.status == "ok"
    recordings = Recordings()
    recordings.add("test", _recording(first.llm[0].request, OUTPUT))
    record = _replay_run(_instance(tmp_path / "second"), recordings)  # no cached output
    assert record.status == "ok"
    assert record.llm[0].response == OUTPUT
    assert record.llm[0].request["input_sha256"] == first.llm[0].request["input_sha256"]


def test_a_request_without_a_recording_is_a_miss_never_a_live_call(tmp_path: Path) -> None:
    inst = _instance(tmp_path / "instance")
    recordings = Recordings()
    recordings.add("test", _recording({k: "1" * 64 for k in _KEYS}, OUTPUT))
    record = _replay_run(inst, recordings)
    assert record.status == "degraded"
    error = record.steps[1].error
    assert error is not None
    assert error.kind == "replay_miss"
    assert "no recording answers this request" in error.message


_KEYS = ("system_sha256", "input_sha256", "schema_sha256")


def test_recordings_load_from_a_folder_or_a_file(tmp_path: Path) -> None:
    folder = PACK / "skills/daily-brief/rank-and-word/evals/recorded"
    found = Recordings.load(folder)
    assert len(found.responses) == len(list(folder.rglob("*.json")))
    one = next(folder.rglob("*.json"))
    assert len(Recordings.load(one).responses) == 1
    with pytest.raises(EvalError, match="not a recording file or a folder"):
        Recordings.load(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(EvalError, match="no recorded response"):
        Recordings.load(tmp_path / "empty")
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(EvalError, match=r"bad\.json"):
        Recordings.load(tmp_path / "bad.json")


def test_conflicting_recordings_of_one_request_are_refused() -> None:
    request = {k: "2" * 64 for k in _KEYS}
    recordings = Recordings()
    recordings.add("a.json", _recording(request, {"x": 1}))
    recordings.add("b.json", _recording(request, {"x": 1}))  # the same answer: fine
    with pytest.raises(EvalError, match=r"b\.json: answers the same request as a\.json"):
        recordings.add("b.json", _recording(request, {"x": 2}))


def _cli_instance(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance", models={"default": ANTHROPIC})
    return root


@pytest.fixture
def fake_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """`horae run` with fixture ports; the replay backend gets the test's prompts and no
    validator worker."""

    @asynccontextmanager
    async def live_ports(instance: Instance, *, llm: Any = None) -> AsyncIterator[Any]:
        plugins = Plugins()
        yield SimpleNamespace(ports=Ports(plugins, plugins, llm), sinks=StaticSinks())

    def backends(models: Any, source: Any) -> Any:
        return replay_backends(models, source, validate=UNVALIDATED, prompts=Prompts())

    monkeypatch.setattr(cli_run, "live_ports", live_ports)
    monkeypatch.setattr(cli_run, "replay_backends", backends)


def test_horae_run_llm_replay(tmp_path: Path, fake_host: None) -> None:
    root = _cli_instance(tmp_path)
    inst = _instance(tmp_path / "scratch")
    first = _replay_run(inst, CompletionResult(OUTPUT, Usage(tokens_in=10, tokens_out=5, usd=0.0)))
    folder = tmp_path / "recorded"
    folder.mkdir()
    (folder / "brief.json").write_text(
        _recording(first.llm[0].request, OUTPUT).model_dump_json(), "utf-8"
    )
    result = runner.invoke(
        app, ["run", "brief", "-i", str(root), "--llm-replay", str(folder), "--json"]
    )
    report = json.loads(result.stdout)
    assert result.exit_code == 0, report
    assert report["run"]["status"] == "ok"
    assert report["run"]["cost"]["tokens_in"] == 10


def test_horae_run_llm_replay_miss_and_missing_path(tmp_path: Path, fake_host: None) -> None:
    root = _cli_instance(tmp_path)
    folder = tmp_path / "recorded"
    folder.mkdir()
    other = _recording({k: "3" * 64 for k in _KEYS}, OUTPUT)
    (folder / "other.json").write_text(other.model_dump_json(), "utf-8")
    result = runner.invoke(
        app, ["run", "brief", "-i", str(root), "--llm-replay", str(folder), "--json"]
    )
    report = json.loads(result.stdout)
    assert report["run"]["status"] == "degraded"
    assert report["run"]["error_kinds"] == ["replay_miss"]
    result = runner.invoke(
        app, ["run", "brief", "-i", str(root), "--llm-replay", str(tmp_path / "nope"), "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["kind"] == "replay_unavailable"


def test_the_private_replay_still_works() -> None:
    assert issubclass(_Replay, ReplayBackend)
    assert PIPELINES  # the fixtures import
