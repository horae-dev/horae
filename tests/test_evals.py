# SPDX-License-Identifier: Apache-2.0
"""`horae eval` (#36, schemas decision 48): golden cases per skill, offline (recorded
responses, CI) and live (a fake provider: an `httpx2.MockTransport`, no network)."""

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from typer.testing import CliRunner

import horae.cli_eval as cli_eval
from horae.cli import app
from horae.contracts.evals import EvalAssertion, EvalCase
from horae.evals import (
    EvalError,
    load_case,
    load_cases,
    load_recorded,
    resolve,
    unified,
)
from horae.evals._harness import _ABSENT, assertion_check
from horae.llm.backends import AnthropicBackend
from horae.store import Store

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLUGINS = FIXTURES / "plugins"
BUILTIN = Path(__file__).resolve().parents[1] / "src" / "horae" / "plugins" / "builtin"
SKILL = "daily-brief/rank-and-word"
COMMAND = "eval"
SECRET_VALUE = "sentinel-value-for-the-eval-key"  # noqa: S105 (synthetic, never a real key)


def _invoke(*args: str, input: str | None = None) -> tuple[int, Any, str]:
    result = runner.invoke(app, [COMMAND, *args], input=input)
    try:
        # CliRunner echoes a prompt's answer to stdout (a terminal would not).
        document = json.loads(result.stdout[result.stdout.find("{") :])
    except ValueError:
        document = None
    return result.exit_code, document, result.output


@pytest.fixture
def pack(tmp_path: Path) -> Path:
    """A copy of `pack-daily-brief` a test may change."""
    root = tmp_path / "plugins" / "pack-daily-brief"
    shutil.copytree(PLUGINS / "pack-daily-brief", root)
    return root


def _evals(pack: Path) -> Path:
    return pack / "skills" / "daily-brief" / "rank-and-word" / "evals"


def _recorded(pack: Path, case: str) -> Path:
    return _evals(pack) / "recorded" / "anthropic" / f"{case}.json"


def _edit_json(path: Path, change: Callable[[Any], None]) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def _edit_yaml(path: Path, change: Callable[[Any], None]) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


# --- the golden cases of the repository (CI) --------------------------------------------------


def _skills_with_evals() -> list[tuple[Path, str]]:
    found = []
    for directory in (PLUGINS, BUILTIN):
        for manifest in sorted(directory.glob("*/plugin.yaml")):
            data = yaml.safe_load(manifest.read_text())
            for skill in (data.get("provides") or {}).get("skills") or []:
                namespace, name = skill.split("/")
                if (manifest.parent / "skills" / namespace / name / "evals").is_dir():
                    found.append((manifest.parent, skill))
    return found


def test_the_repository_has_golden_cases() -> None:
    assert (PLUGINS / "pack-daily-brief", SKILL) in _skills_with_evals()


@pytest.mark.parametrize(("plugin", "skill"), _skills_with_evals(), ids=lambda v: str(v))
def test_every_skill_passes_its_golden_cases_offline(plugin: Path, skill: str) -> None:
    code, document, _ = _invoke(skill, "--plugin-dir", str(plugin), "--json")
    assert code == 0, json.dumps(document, indent=2)
    assert document["summary"]["errors"] == document["summary"]["failed"] == 0
    assert document["summary"]["stale"] == 0, "re-record the stale cases"


def test_pack_daily_brief_has_three_cases_with_recordings() -> None:
    cases = load_cases(_evals(PLUGINS / "pack-daily-brief"))
    assert len(cases) >= 3
    assert all("anthropic" in case.recorded for case in cases)


# --- offline ----------------------------------------------------------------------------------


def test_offline_report_shape() -> None:
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(PLUGINS), "--json")
    assert code == 0
    assert set(document) == {
        "schema_version",
        "mode",
        "skill",
        "skill_version",
        "skill_digest",
        "plugin",
        "alias",
        "cases",
        "summary",
        "estimate",
        "report",
        "error",
    }
    assert document["mode"] == "offline"
    assert document["plugin"] == "pack-daily-brief"
    assert [c["case"] for c in document["cases"]] == [
        "injected-embed",
        "overdue-first",
        "tasks-only",
    ]
    embed = document["cases"][0]
    assert embed["guard"] == "violation"
    assert "image or embed" in embed["guard_detail"]
    assert "tracker" not in embed["guard_detail"]  # content-free (decision 46)
    first = document["cases"][1]
    assert first["guard"] == "pass"
    assert first["schema_valid"] is True
    assert {check["name"] for check in first["checks"]} >= {"schema_valid", "guard"}
    assert document["summary"]["passed"] == 3


def test_offline_text_report() -> None:
    code, _, output = _invoke(SKILL, "--plugin-dir", str(PLUGINS))
    assert code == 0
    assert "PASS  tasks-only [anthropic]" in output
    assert "3 cases: 3 passed, 0 failed, 0 errors" in output


def test_one_case(pack: Path) -> None:
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 0
    assert [c["case"] for c in document["cases"]] == ["tasks-only"]


def test_a_different_output_fails_with_a_diff(pack: Path) -> None:
    def change(data: Any) -> None:
        data["expect"]["output"]["summary"] = "Two tasks are due today."

    _edit_yaml(_evals(pack) / "tasks-only.yaml", change)
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4
    (case,) = document["cases"]
    assert case["status"] == "fail"
    (failed,) = [check for check in case["checks"] if not check["ok"]]
    assert failed["name"] == "output"
    assert '-  "summary": "Two tasks are due today."' in failed["diff"]
    assert '+  "summary": "One task is due today."' in failed["diff"]
    assert document["error"]["kind"] == "eval_failed"
    code, _, output = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only")
    assert code == 4
    assert "FAIL  tasks-only [anthropic]" in output
    assert "  - output: different" in output
    assert "--- expected" in output


def test_assertions_and_intents_fail(pack: Path) -> None:
    def change(data: Any) -> None:
        data["expect"]["assertions"] = [
            {"pointer": "/output/ranked", "length": 2},
            {"pointer": "/output/nope", "exists": True},
            {"pointer": "/output/ranked/0/why", "equals": "later"},
            {"pointer": "/output/summary", "contains": "nothing"},
        ]
        data["expect"]["intent_counts"] = {"note.create": 2}
        data["expect"]["intents"] = []

    _edit_yaml(_evals(pack) / "overdue-first.yaml", change)
    code, document, _ = _invoke(
        SKILL, "--plugin-dir", str(pack), "--case", "overdue-first", "--json"
    )
    assert code == 4
    failed = {c["name"]: c for c in document["cases"][0]["checks"] if not c["ok"]}
    assert set(failed) == {
        "length /output/ranked",
        "exists /output/nope",
        "equals /output/ranked/0/why",
        "contains /output/summary",
        "intent_counts",
        "intents",
    }
    assert failed["length /output/ranked"]["message"] == "length 3, not 2"


def test_a_schema_invalid_recording_is_reported(pack: Path) -> None:
    def change(data: Any) -> None:
        data["output"]["ranked"][0]["why"] = "soon"

    _edit_json(_recorded(pack, "overdue-first"), change)
    code, document, _ = _invoke(
        SKILL, "--plugin-dir", str(pack), "--case", "overdue-first", "--json"
    )
    assert code == 4
    (case,) = document["cases"]
    assert case["schema_valid"] is False
    assert case["schema_errors"]
    assert "soon" not in json.dumps(case["schema_errors"])  # never a value of the output
    (schema,) = [c for c in case["checks"] if c["name"] == "schema_valid"]
    assert schema["ok"] is False
    # An invalid output proposes nothing: its intents are dropped, the guard does not run.
    assert case["guard"] == "not_run"

    # A case may expect an invalid output (e.g. the repair path).
    def expect_invalid(data: Any) -> None:
        data["expect"] = {"schema_valid": False, "intents": []}

    _edit_yaml(_evals(pack) / "overdue-first.yaml", expect_invalid)
    code, document, _ = _invoke(
        SKILL, "--plugin-dir", str(pack), "--case", "overdue-first", "--json"
    )
    assert code == 0, document
    # ...and a valid one where invalid was expected fails.
    _edit_yaml(_evals(pack) / "tasks-only.yaml", expect_invalid)
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4
    assert document["cases"][0]["checks"][0]["message"] == "the output is valid"


def test_a_guard_violation_is_reported(pack: Path) -> None:
    def change(data: Any) -> None:
        data["expect"]["guard"] = "pass"

    _edit_yaml(_evals(pack) / "injected-embed.yaml", change)
    code, document, _ = _invoke(
        SKILL, "--plugin-dir", str(pack), "--case", "injected-embed", "--json"
    )
    assert code == 4
    (guard,) = [c for c in document["cases"][0]["checks"] if c["name"] == "guard"]
    assert guard["ok"] is False
    assert "image or embed" in guard["message"]
    # Expecting a violation from an output the guard accepts fails too.
    _edit_yaml(_evals(pack) / "tasks-only.yaml", lambda d: d["expect"].update(guard="violation"))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4


def test_invalid_intents_are_reported(pack: Path) -> None:
    # The note's routed target is not a valid note path: `validate_intents` rejects it.
    def change(data: Any) -> None:
        data["targets"] = {"note.create": "/etc/abs.md"}

    _edit_yaml(_evals(pack) / "overdue-first.yaml", change)
    code, document, _ = _invoke(
        SKILL, "--plugin-dir", str(pack), "--case", "overdue-first", "--json"
    )
    assert code == 4
    (case,) = document["cases"]
    assert case["intents_error"] is not None
    assert case["guard"] == "not_run"


def test_targets_fill_routed_fields(pack: Path) -> None:
    # Without a target, a note intent gets `Evals/<case>.md`; an `http.post` none.
    def change(data: Any) -> None:
        data.pop("targets")

    _edit_yaml(_evals(pack) / "overdue-first.yaml", change)
    code, _, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "overdue-first", "--json")
    assert code == 0


def test_a_stale_recording_is_a_warning(pack: Path) -> None:
    prompt = pack / "skills" / "daily-brief" / "rank-and-word" / "prompt.md"
    prompt.write_text(prompt.read_text() + "\nBe brief.\n")
    code, document, output = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 0
    assert all(case["stale"] == ["skill", "prompt"] for case in document["cases"])
    assert document["summary"]["stale"] == 3
    code, _, output = _invoke(SKILL, "--plugin-dir", str(pack))
    assert "stale recording (skill, prompt changed)" in output
    assert "(3 stale)" in output


def test_a_case_without_recording_is_an_error(pack: Path) -> None:
    _recorded(pack, "tasks-only").unlink()
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    (missing,) = [c for c in document["cases"] if c["status"] == "error"]
    assert missing["case"] == "tasks-only"
    assert missing["error"]["kind"] == "no_recording"
    assert document["error"]["kind"] == "eval_errors"
    code, _, output = _invoke(SKILL, "--plugin-dir", str(pack))
    assert "ERROR tasks-only: no_recording" in output


def test_an_input_outside_the_view_is_an_error(pack: Path) -> None:
    _edit_yaml(_evals(pack) / "tasks-only.yaml", lambda d: d.update(input=[1, 2]))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 1
    assert document["cases"][0]["error"]["kind"] == "input_invalid"


def test_a_backend_without_factory_is_an_error(pack: Path) -> None:
    source = _recorded(pack, "tasks-only")
    target = _evals(pack) / "recorded" / "claude-cli" / "tasks-only.json"
    target.parent.mkdir()
    data = json.loads(source.read_text())
    data["backend"] = "claude-cli"
    target.write_text(json.dumps(data))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 1
    by_backend = {c["backend"]: c for c in document["cases"]}
    assert by_backend["claude-cli"]["error"]["kind"] == "backend_unavailable"
    assert by_backend["anthropic"]["status"] == "pass"


@pytest.mark.parametrize(
    ("args", "code", "kind"),
    [
        (["nope/missing"], 3, "unknown_skill"),
        (["not-a-skill"], 2, "usage"),
        ([SKILL, "--case", "nope"], 3, "unknown_case"),
        ([SKILL, "--record"], 2, "usage"),
        ([SKILL, "--yes"], 2, "usage"),
        ([SKILL, "--timeout", "0"], 2, "usage"),
        ([SKILL, "--timeout", "601"], 2, "usage"),
    ],
)
def test_errors(pack: Path, args: list[str], code: int, kind: str) -> None:
    got, document, _ = _invoke(*args, "--plugin-dir", str(pack.parent), "--json")
    assert got == code
    assert document == {
        "schema_version": 1,
        "error": {"kind": kind, "message": document["error"]["message"]},
    }
    assert document["error"]["kind"] == kind


def test_a_plugin_dir_that_is_not_a_directory(tmp_path: Path) -> None:
    code, _document, _ = _invoke(SKILL, "--plugin-dir", str(tmp_path / "none"), "--json")
    assert code == 2


def test_an_invalid_case_file_is_a_problem(pack: Path) -> None:
    (_evals(pack) / "broken.yaml").write_text("schema_version: 1\nexpect: {}\n")
    code, document, _output = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "eval_invalid"
    assert "broken.yaml" in document["error"]["message"]


def test_a_skill_without_cases_is_a_problem(pack: Path) -> None:
    shutil.rmtree(_evals(pack))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "no_cases"


def test_an_invalid_skill_is_a_problem(pack: Path) -> None:
    (pack / "skills" / "daily-brief" / "rank-and-word" / "prompt.md").write_text("{{x}}")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "skill_invalid"


def test_plugin_dir_skips_folders_without_a_valid_manifest(pack: Path) -> None:
    (pack.parent / "junk").mkdir()
    (pack.parent / "broken").mkdir()
    (pack.parent / "broken" / "plugin.yaml").write_text("name: [")
    code, _, _ = _invoke(SKILL, "--plugin-dir", str(pack.parent), "--json")
    assert code == 0


# --- the eval files ---------------------------------------------------------------------------


def test_case_contract() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        EvalAssertion.model_validate({"pointer": "/a"})
    with pytest.raises(ValueError, match="exactly one"):
        EvalAssertion.model_validate({"pointer": "/a", "equals": 1, "length": 1})
    assert EvalAssertion.model_validate({"pointer": "", "equals": None}).check == "equals"
    case = EvalCase.model_validate({"schema_version": 1, "input": None})
    assert case.expect.schema_valid is True
    assert case.expect.has_output is False
    assert case.expect.guard == "pass"


def test_case_files_are_checked(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("schema_version: 1\ninput: { due: 2026-10-28 }\n")
    with pytest.raises(EvalError, match="quote dates"):
        load_case(path)
    path.write_bytes(b"\xff")
    with pytest.raises(EvalError, match="UTF-8"):
        load_case(path)
    with pytest.raises(EvalError, match="not found"):
        load_case(tmp_path / "missing.yaml")
    path.write_text("schema_version: 1\ninput: " + "x" * (256 * 1024))
    with pytest.raises(EvalError, match="larger than"):
        load_case(path)
    link = tmp_path / "link.yaml"
    path.write_text("schema_version: 1\ninput: {}\n")
    link.symlink_to(path)
    with pytest.raises(EvalError, match="symbolic link"):
        load_case(link)
    with pytest.raises(EvalError, match="not a regular file"):
        load_case(tmp_path)


def test_recorded_files_are_checked(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    assert load_recorded(path) is None
    path.write_text('{"a": 1, "a": 2}')
    with pytest.raises(EvalError, match="duplicate key"):
        load_recorded(path)
    path.write_text('{"a": NaN}')
    with pytest.raises(EvalError, match="not valid JSON"):
        load_recorded(path)
    path.write_text('{"a": 1e999}')
    with pytest.raises(EvalError, match="not valid JSON"):
        load_recorded(path)
    path.write_text("[" * 100_000)
    with pytest.raises(EvalError):
        load_recorded(path)
    path.write_text('{"schema_version": 1}')
    with pytest.raises(EvalError, match="backend"):
        load_recorded(path)


def test_eval_folders_are_checked(pack: Path) -> None:
    folder = _evals(pack)
    (folder / "Bad Name.yaml").write_text("x")
    with pytest.raises(EvalError, match="not a case name"):
        load_cases(folder)
    (folder / "Bad Name.yaml").unlink()
    (folder / "recorded" / "gpt").mkdir()
    with pytest.raises(EvalError, match="not a backend folder"):
        load_cases(folder)
    (folder / "recorded" / "gpt").rmdir()
    _edit_json(_recorded(pack, "tasks-only"), lambda d: d.update(backend="ollama"))
    with pytest.raises(EvalError, match="recorded for backend"):
        load_cases(folder)
    shutil.rmtree(folder / "recorded")
    (folder / "recorded").write_text("")
    with pytest.raises(EvalError, match="not a directory"):
        load_cases(folder)
    shutil.rmtree(folder.parent / "evals")
    folder.parent.joinpath("elsewhere").mkdir()
    folder.symlink_to(folder.parent / "elsewhere")
    with pytest.raises(EvalError, match="not a directory"):
        load_cases(folder)


def test_pointers_and_assertions() -> None:
    document = {"output": {"a/b": [1, {"~k": "v"}], "s": "text", "n": 3}, "intents": []}
    assert resolve(document, "") is document
    assert resolve(document, "/output/a~1b/1/~0k") == "v"
    assert resolve(document, "/output/a~1b/01") is _ABSENT
    assert resolve(document, "/output/a~1b/9") is _ABSENT
    assert resolve(document, "/output/a~1b/x") is _ABSENT
    assert resolve(document, "/output/s/0") is _ABSENT
    assert resolve(document, "/missing") is _ABSENT

    def check(**spec: Any) -> tuple[bool, str | None]:
        result = assertion_check(EvalAssertion.model_validate(spec), document)
        return result.ok, result.message

    assert check(pointer="/output/n", length=1) == (False, "not a string, array or object (int)")
    assert check(pointer="/output/s", length=4) == (True, None)
    assert check(pointer="/output/a~1b", contains=1) == (True, None)
    assert check(pointer="/output/a~1b", contains={"~k": "v"}) == (True, None)
    assert check(pointer="/output/n", contains=3) == (False, "not a string or an array (int)")
    assert check(pointer="/output/s", contains="ex") == (True, None)
    assert check(pointer="/output/s", contains=1) == (False, "not contained")
    assert check(pointer="/output/x", exists=False) == (True, None)
    assert check(pointer="/output/s", exists=False) == (False, "found: true")
    assert check(pointer="/output/x", equals=1) == (False, "nothing there")
    assert check(pointer="/intents", equals=[]) == (True, None)


def test_unified_diff_is_bounded() -> None:
    assert unified([1], [1]) is None
    diff = unified(list(range(1000)), list(range(1000, 2000)))
    assert diff is not None
    assert diff.endswith("more lines)")


# --- through an instance ----------------------------------------------------------------------


ANTHROPIC = {
    "backend": "anthropic",
    "model": "claude-fixture-1",
    "secret": "ANTHROPIC_API_KEY",
    "price": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
}


def _instance(root: Path, models: dict[str, Any] | None = None, **config: Any) -> Path:
    (root / "pipelines").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "secrets" / "ANTHROPIC_API_KEY").write_text(SECRET_VALUE, encoding="utf-8")
    document = {"models": models or {"default": ANTHROPIC}, "secrets": {"dir": "secrets"}, **config}
    (root / "horae.yaml").write_text(yaml.safe_dump(json.loads(json.dumps(document))), "utf-8")
    permissions = {"intents": ["note.create"]}
    lock = {
        "schema_version": 1,
        "plugins": {
            "pack-daily-brief": {
                "version": "0.1.0",
                "repo": "https://github.com/horae-dev/pack-fixture.git",
                "commit": "0" * 40,
                "permissions": permissions,
            }
        },
    }
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    shutil.copytree(PLUGINS / "pack-daily-brief", root / "plugins" / "pack-daily-brief")
    return root


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    return _instance(tmp_path / "instance")


def test_offline_through_the_instance(instance: Path) -> None:
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--json")
    assert code == 0, document
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--alias", "default", "--json")
    assert code == 0, document
    assert document["alias"] == "default"
    assert {c["backend"] for c in document["cases"]} == {"anthropic"}
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--alias", "nope", "--json")
    assert code == 3
    assert document["error"]["kind"] == "unknown_alias"
    code, document, _ = _invoke("nope/missing", "-i", str(instance), "--json")
    assert code == 3


def test_offline_with_an_alias_of_another_backend(tmp_path: Path) -> None:
    models = {
        "local": {"backend": "ollama", "model": "llama-fixture", "base_url": "http://ollama:11434"}
    }
    root = _instance(tmp_path / "instance", models)
    code, document, _ = _invoke(SKILL, "-i", str(root), "--alias", "local", "--json")
    assert code == 1
    assert {c["error"]["kind"] for c in document["cases"]} == {"no_recording"}


def test_the_instance_plugins_must_match_the_lock(instance: Path) -> None:
    shutil.rmtree(instance / "plugins" / "pack-daily-brief")
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--json")
    assert code == 1
    assert document["error"]["kind"] == "plugins_invalid"


def test_an_invalid_instance(tmp_path: Path) -> None:
    (tmp_path / "horae.yaml").write_text("nope: 1\n")
    code, document, _ = _invoke(SKILL, "-i", str(tmp_path), "--json")
    assert code == 1
    assert document["error"]["kind"] == "config_invalid"


# --- live, against a fake provider ------------------------------------------------------------


class FakeProvider:
    """Answers every Messages API request with the next output (the last one repeats)."""

    def __init__(self, *outputs: Any, status: int = 200) -> None:
        self.outputs = list(outputs)
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        output = self.outputs[0] if len(self.outputs) == 1 else self.outputs.pop(0)
        if self.status != 200:
            error = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
            return httpx2.Response(self.status, json=error)
        body = {
            "id": "msg_01SyntheticEval",
            "type": "message",
            "role": "assistant",
            "model": "claude-fixture-1",
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 600, "output_tokens": 40},
        }
        return httpx2.Response(200, json=body, headers={"request-id": "req_01Synthetic"})


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeProvider]:
    original = AnthropicBackend.__init__

    def install(*outputs: Any, status: int = 200) -> FakeProvider:
        fake = FakeProvider(*outputs, status=status)

        def patched(self: AnthropicBackend, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs, transport=httpx2.MockTransport(fake))

        monkeypatch.setattr(AnthropicBackend, "__init__", patched)
        return fake

    return install


def _recorded_outputs() -> dict[str, Any]:
    return {
        case.name: case.recorded["anthropic"].output
        for case in load_cases(_evals(PLUGINS / "pack-daily-brief"))
    }


def test_live_needs_a_bound(instance: Path, provider: Callable[..., FakeProvider]) -> None:
    fake = provider({"summary": "x", "ranked": []})
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--live", "--yes", "--json")
    assert code == 2
    assert "--max-usd" in document["error"]["message"]
    assert fake.requests == []
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--live", "--max-usd", "-1", "--json")
    assert code == 2


def test_live_refuses_a_worst_case_over_max_usd(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    fake = provider({"summary": "x", "ranked": []})
    code, document, _ = _invoke(
        SKILL, "-i", str(instance), "--live", "--yes", "--max-usd", "0.001", "--json"
    )
    assert code == 3
    assert document["error"]["kind"] == "budget_exceeded"
    assert fake.requests == []


def test_live_needs_a_confirmation(
    instance: Path, provider: Callable[..., FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = provider({"summary": "x", "ranked": []})
    args = [SKILL, "-i", str(instance), "--live", "--max-usd", "1", "--json"]
    code, document, output = _invoke(*args)
    assert code == 2
    assert "--yes" in document["error"]["message"]
    assert "worst case" in output  # the estimate is shown first (on stderr)
    monkeypatch.setattr(cli_eval, "interactive", lambda: True)
    code, document, _ = _invoke(*args, input="n\n")
    assert code == 3
    assert document["error"]["kind"] == "not_confirmed"
    assert fake.requests == []
    outputs = _recorded_outputs()
    provider(*(outputs[name] for name in sorted(outputs)))
    code, document, _ = _invoke(*args, input="y\n")
    assert code == 0, document


def test_live_sends_each_case_and_writes_a_report(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    outputs = _recorded_outputs()
    fake = provider(*(outputs[name] for name in sorted(outputs)))
    code, document, _ = _invoke(
        SKILL, "-i", str(instance), "--live", "--yes", "--max-usd", "1", "--json"
    )
    assert code == 0, json.dumps(document, indent=2)
    assert len(fake.requests) == 3
    assert document["mode"] == "live"
    assert document["alias"] == "default"
    estimate = document["estimate"]
    assert estimate["usd"] > 0
    assert estimate["tokens_out"] == 3 * 1024
    for case in document["cases"]:
        assert case["usage"]["tokens_in"] == 600
        assert case["worst_case"]["usd"] > case["usage"]["usd"]
        assert case["recorded_diff"] is None  # the same answer as recorded
    spent = sum(case["usage"]["tokens_out"] for case in document["cases"])
    assert document["summary"]["usage"]["tokens_out"] == spent
    report = Path(document["report"])
    assert report.parent == instance / "state" / "evals" / "daily-brief" / "rank-and-word"
    assert json.loads(report.read_text())["report"] == str(report)
    text = report.with_suffix(".txt").read_text()
    assert "3 cases: 3 passed" in text
    assert f"spent 1800 tokens in, {spent} out" in text
    # Nothing is written to the store or its ledger: the eval is not a run.
    assert not (instance / "state" / "horae.db").exists()
    # Every request carried the alias' key and nothing else.
    assert all(r.headers["x-api-key"] == SECRET_VALUE for r in fake.requests)


def test_live_reports_differences_and_backend_errors(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    outputs = _recorded_outputs()
    changed = {**outputs["tasks-only"], "summary": "Something else."}
    provider(changed)
    code, document, output = _invoke(
        SKILL, "-i", str(instance), "--live", "--yes", "--max-usd", "1", "--case", "tasks-only"
    )
    assert code == 4
    assert "differs from the recorded response:" in output
    assert '+  "summary": "Something else."' in output
    assert "report: " in output
    provider(changed, status=500)
    code, document, _ = _invoke(
        SKILL,
        "-i",
        str(instance),
        "--live",
        "--yes",
        "--max-usd",
        "1",
        "--case",
        "tasks-only",
        "--json",
    )
    assert code == 1
    assert document["cases"][0]["status"] == "error"


def test_live_record_writes_redacted_recordings(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    root = _instance(tmp_path / "instance")
    leaky = {
        "summary": f"One task is due today. key={SECRET_VALUE}",
        "ranked": [
            {"title": "Book the train tickets", "why": "today"},
            {"title": "Read the onboarding guide", "why": "later"},
        ],
    }
    provider(leaky)
    code, document, _ = _invoke(
        SKILL,
        "-i",
        str(root),
        "--live",
        "--yes",
        "--max-usd",
        "1",
        "--case",
        "tasks-only",
        "--record",
        "--json",
    )
    assert code == 4  # the redacted summary differs from the expected one
    target = root / "plugins" / "pack-daily-brief" / "skills" / "daily-brief" / "rank-and-word"
    path = target / "evals" / "recorded" / "anthropic" / "tasks-only.json"
    assert document["cases"][0]["recorded_path"] == str(path)
    text = path.read_text()
    assert SECRET_VALUE not in text
    assert "[REDACTED:ANTHROPIC_API_KEY]" in text
    recorded = load_recorded(path)
    assert recorded is not None
    assert recorded.usage.tokens_in == 600
    assert recorded.usage.priced is True
    assert not list(path.parent.glob(".*.tmp"))
    for report in (root / "state" / "evals").rglob("*"):
        if report.is_file():
            assert SECRET_VALUE not in report.read_text()
    # The new recording is what an offline eval now replays.
    code, document, _ = _invoke(SKILL, "-i", str(root), "--case", "tasks-only", "--json")
    assert code == 4
    assert document["cases"][0]["stale"] == []


def test_live_record_reports_a_write_failure(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    root = _instance(tmp_path / "instance")
    provider(_recorded_outputs()["tasks-only"])
    backend = (
        root
        / "plugins"
        / "pack-daily-brief"
        / "skills"
        / "daily-brief"
        / "rank-and-word"
        / "evals"
        / "recorded"
        / "anthropic"
    )
    os.chmod(backend, 0o500)
    try:
        code, _document, output = _invoke(
            SKILL,
            "-i",
            str(root),
            "--live",
            "--yes",
            "--max-usd",
            "1",
            "--case",
            "tasks-only",
            "--record",
        )
    finally:
        os.chmod(backend, 0o700)
    assert code == 0
    assert "cannot be written" in output


def test_live_with_an_unrenderable_case(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    fake = provider({"summary": "x", "ranked": []})
    evals = (
        instance
        / "plugins"
        / "pack-daily-brief"
        / "skills"
        / "daily-brief"
        / "rank-and-word"
        / "evals"
    )
    _edit_yaml(evals / "tasks-only.yaml", lambda d: d.update(input=[1]))
    code, document, _ = _invoke(
        SKILL,
        "-i",
        str(instance),
        "--live",
        "--yes",
        "--max-usd",
        "1",
        "--case",
        "tasks-only",
        "--json",
    )
    assert code == 1
    assert document["cases"][0]["error"]["kind"] == "input_invalid"
    assert fake.requests == []


def test_live_needs_an_alias_when_there_are_several(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    root = _instance(tmp_path / "instance", {"a": dict(ANTHROPIC), "b": dict(ANTHROPIC)})
    code, document, _ = _invoke(
        SKILL, "-i", str(root), "--live", "--yes", "--max-usd", "1", "--json"
    )
    assert code == 2
    assert "--alias" in document["error"]["message"]


def test_live_respects_the_daily_budget(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    outputs = _recorded_outputs()
    # A daily budget bounds the eval: no --max-usd needed.
    root = _instance(tmp_path / "a", budgets={"daily": {"usd": 5.0}})
    fake = provider(*(outputs[name] for name in sorted(outputs)))
    code, document, _ = _invoke(SKILL, "-i", str(root), "--live", "--yes", "--json")
    assert code == 0, document
    assert len(fake.requests) == 3
    # Too small a daily budget refuses before anything is sent.
    root = _instance(tmp_path / "b", budgets={"daily": {"usd": 0.01}})
    fake = provider(outputs["tasks-only"])
    code, document, _ = _invoke(SKILL, "-i", str(root), "--live", "--yes", "--json")
    assert code == 3
    assert "daily budget" in document["error"]["message"]
    assert fake.requests == []
    root = _instance(tmp_path / "c", budgets={"daily": {"tokens_out": 100}})
    code, document, _ = _invoke(SKILL, "-i", str(root), "--live", "--yes", "--json")
    assert code == 3
    assert "output tokens" in document["error"]["message"]


def test_live_counts_what_the_ledger_spent_today(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    from datetime import UTC, datetime

    from horae.runner._budget import local_day

    root = _instance(tmp_path / "instance", budgets={"daily": {"usd": 1.0}})
    now = datetime.now(UTC)
    with Store.open(root / "state" / "horae.db") as store:
        store.begin_run("r1", "brief", now)
        entry = store.reserve_usage(
            "r1",
            step=0,
            attempt=1,
            alias="default",
            at=now,
            day=local_day(now, "UTC"),
            usd=0.99,
            tokens_out=10,
        )
        assert entry
    fake = provider(_recorded_outputs()["tasks-only"])
    code, document, _ = _invoke(
        SKILL, "-i", str(root), "--live", "--yes", "--case", "tasks-only", "--json"
    )
    assert code == 3, document
    assert "has $0.0100 left" in document["error"]["message"]
    assert fake.requests == []


def test_live_reports_a_broken_store(tmp_path: Path, provider: Callable[..., FakeProvider]) -> None:
    root = _instance(tmp_path / "instance", budgets={"daily": {"usd": 1.0}})
    (root / "state").mkdir()
    (root / "state" / "horae.db").write_text("not a database")
    provider({"summary": "x", "ranked": []})
    code, document, _ = _invoke(SKILL, "-i", str(root), "--live", "--yes", "--json")
    assert code == 1
    assert document["error"]["kind"] == "store_error"
