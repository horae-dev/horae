# SPDX-License-Identifier: Apache-2.0
"""`horae eval` (#36, schemas decision 48): golden cases per skill, offline (recorded
responses, CI) and live (a fake provider: an `httpx2.MockTransport`, no network)."""

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import yaml
from typer.testing import CliRunner

import horae.cli_eval as cli_eval
from horae.cli import app
from horae.cli_output import CommandFailed
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
        "run",
        "cases",
        "summary",
        "estimate",
        "report",
        "report_error",
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


def test_a_backend_without_factory_is_an_error(pack: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from horae.llm.backends import FACTORIES

    monkeypatch.delitem(cast("dict[str, Any]", FACTORIES), "claude-cli")
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
    # A broken case is an error result of its own; the other cases still run.
    (_evals(pack) / "broken.yaml").write_text("schema_version: 1\nexpect: {}\n")
    code, document, _output = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "eval_errors"
    by_case = {c["case"]: c for c in document["cases"]}
    assert by_case["broken"]["error"]["kind"] == "eval_invalid"
    assert "broken.yaml" in by_case["broken"]["error"]["message"]
    assert by_case["tasks-only"]["status"] == "pass"


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
    # The spend is in the usage ledger, under a run of pipeline `eval:<skill>`.
    with Store.open(instance / "state" / "horae.db", read_only=True) as store:
        (run,) = store.list_runs(status="ok")
        assert run.id == document["run"]
        assert run.pipeline == f"eval:{SKILL}"
        assert run.cost.tokens_out == spent
        groups = store.usage_report()
    assert [(g.pipeline, g.calls, g.tokens_out) for g in groups] == [(f"eval:{SKILL}", 3, spent)]
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
    tmp_path: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    root = _instance(tmp_path / "instance")
    leaky = {
        "summary": f"One task is due today. key={SECRET_VALUE}",
        "ranked": [
            {"title": "Book the train tickets", "why": "today"},
            {"title": "Read the onboarding guide", "why": "later"},
        ],
    }
    # The case asserts what the answer must hold, not its exact words: a leaky one passes.
    _edit_yaml(
        _evals(pack) / "tasks-only.yaml",
        lambda d: d.update(expect={"assertions": [{"pointer": "/output/ranked", "length": 2}]}),
    )
    provider(leaky)
    code, document, _ = _invoke(
        SKILL,
        "-i",
        str(root),
        "--plugin-dir",
        str(pack),
        "--live",
        "--yes",
        "--max-usd",
        "1",
        "--case",
        "tasks-only",
        "--record",
        "--json",
    )
    assert code == 0, document
    path = _recorded(pack, "tasks-only")
    assert document["cases"][0]["recorded_path"] == str(path)
    assert oct(path.stat().st_mode & 0o777) == oct(0o644)
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
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 0, document
    assert document["cases"][0]["stale"] == []


def test_live_record_reports_a_write_failure(
    tmp_path: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    root = _instance(tmp_path / "instance")
    provider(_recorded_outputs()["tasks-only"])
    backend = _evals(pack) / "recorded" / "anthropic"
    os.chmod(backend, 0o500)
    try:
        code, _document, output = _invoke(
            SKILL,
            "-i",
            str(root),
            "--plugin-dir",
            str(pack),
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
    # A tokens_out-only ceiling does not bound the cost: --max-usd is still required.
    root = _instance(tmp_path / "c", budgets={"daily": {"tokens_out": 100}})
    code, document, _ = _invoke(SKILL, "-i", str(root), "--live", "--yes", "--json")
    assert code == 2
    assert "--max-usd" in document["error"]["message"]
    code, document, _ = _invoke(
        SKILL, "-i", str(root), "--live", "--yes", "--max-usd", "1", "--json"
    )
    assert code == 3
    assert "output tokens" in document["error"]["message"]
    assert fake.requests == []


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


# --- review fixes (decision 48 J to N) --------------------------------------------------------


def _live_args(root: Path, *extra: str) -> list[str]:
    return [SKILL, "-i", str(root), "--live", "--yes", "--max-usd", "1", *extra, "--json"]


def test_live_refuses_an_unpriced_cloud_alias(
    tmp_path: Path, provider: Callable[..., FakeProvider]
) -> None:
    unpriced = {k: v for k, v in ANTHROPIC.items() if k != "price"}
    root = _instance(tmp_path / "instance", {"default": unpriced})
    fake = provider(_recorded_outputs()["tasks-only"])
    code, document, _ = _invoke(*_live_args(root, "--case", "tasks-only"))
    assert code == 3
    assert document["error"]["kind"] == "unpriced_alias"
    assert fake.requests == []
    code, document, _ = _invoke(*_live_args(root, "--case", "tasks-only", "--allow-unpriced"))
    assert code == 0, document
    assert len(fake.requests) == 1
    assert document["cases"][0]["usage"]["priced"] is False


@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "0"])
def test_live_refuses_a_non_finite_max_usd(instance: Path, value: str) -> None:
    code, document, _ = _invoke(SKILL, "-i", str(instance), "--live", "--max-usd", value, "--json")
    assert code == 2
    assert "finite" in document["error"]["message"]


@pytest.mark.parametrize(
    "args",
    [
        ["--allow-unpriced"],
        ["--live", "--record"],  # --record needs --plugin-dir
        ["--case", "Bad Name"],
        ["--case", "../tasks-only"],
    ],
)
def test_more_usage_errors(instance: Path, args: list[str]) -> None:
    code, document, _ = _invoke(SKILL, "-i", str(instance), *args, "--json")
    assert code == 2, document
    assert document["error"]["kind"] == "usage"


def test_runs_hides_live_evals_unless_all(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    provider(_recorded_outputs()["tasks-only"])
    code, document, _ = _invoke(*_live_args(instance, "--case", "tasks-only"))
    assert code == 0, document
    listed = runner.invoke(app, ["runs", "-i", str(instance), "--json"])
    assert json.loads(listed.stdout)["runs"] == []
    listed = runner.invoke(app, ["runs", "-i", str(instance), "--all", "--json"])
    (run,) = json.loads(listed.stdout)["runs"]
    assert run["id"] == document["run"]
    assert run["pipeline"] == f"eval:{SKILL}"
    assert run["status"] == "ok"
    listed = runner.invoke(app, ["runs", "-i", str(instance), "-p", f"eval:{SKILL}", "--json"])
    assert len(json.loads(listed.stdout)["runs"]) == 1
    # The ledger counts the eval's spend with the other pipelines' (`horae usage`).
    with Store.open(instance / "state" / "horae.db", read_only=True) as store:
        (group,) = store.usage_report()
        assert group.pipeline == f"eval:{SKILL}"
        assert group.tokens_out == document["summary"]["usage"]["tokens_out"]


def test_live_rechecks_the_allowance_after_the_confirmation(
    tmp_path: Path, provider: Callable[..., FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _instance(tmp_path / "instance", budgets={"daily": {"usd": 1.0}})
    fake = provider(_recorded_outputs()["tasks-only"])
    seen = iter([(1.0, None), (0.0001, None)])
    monkeypatch.setattr(cli_eval, "_allowance", lambda *args: next(seen))
    code, document, _ = _invoke(*_live_args(root, "--case", "tasks-only"))
    assert code == 3
    assert document["error"]["kind"] == "budget_exceeded"
    assert fake.requests == []
    assert not (root / "state" / "evals").exists()  # refused: no run, no report


def test_the_ledger_reservation_refuses_what_the_preflight_missed(
    tmp_path: Path, provider: Callable[..., FakeProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pre-flight saw an allowance another run then spent: the ledger's own reservation
    # (read inside `BEGIN IMMEDIATE`) refuses the case, and nothing is sent.
    from datetime import UTC, datetime

    from horae.runner._budget import local_day

    root = _instance(tmp_path / "instance", budgets={"daily": {"usd": 1.0}})
    fake = provider(_recorded_outputs()["tasks-only"])
    monkeypatch.setattr(cli_eval, "_allowance", lambda *args: (1.0, None))
    confirm = cli_eval._confirm

    def confirm_then_spend(*args: Any) -> None:
        confirm(*args)
        now = datetime.now(UTC)
        with Store.open(root / "state" / "horae.db") as store:
            store.begin_run("other", "brief", now)
            day = local_day(now, "UTC")
            store.reserve_usage(
                "other", step=0, attempt=1, alias="default", at=now, day=day, usd=0.999,
                tokens_out=10,
            )  # fmt: skip

    monkeypatch.setattr(cli_eval, "_confirm", confirm_then_spend)
    code, document, _ = _invoke(*_live_args(root, "--case", "tasks-only"))
    assert code == 1, document
    assert document["cases"][0]["error"]["kind"] == "budget_exceeded"
    assert fake.requests == []


def test_live_is_refused_while_another_eval_of_the_skill_runs(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    from horae.runner import PipelineLock

    fake = provider(_recorded_outputs()["tasks-only"])
    lock = PipelineLock.try_acquire(instance / "state", f"eval:{SKILL}")
    assert lock is not None
    with lock:
        code, document, _ = _invoke(*_live_args(instance, "--case", "tasks-only"))
    assert code == 3
    assert document["error"]["kind"] == "eval_busy"
    assert fake.requests == []


def test_a_dead_eval_is_abandoned_by_the_next_one(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    from datetime import UTC, datetime

    with Store.open(instance / "state" / "horae.db") as store:
        store.begin_run("dead", f"eval:{SKILL}", datetime.now(UTC))
    provider(_recorded_outputs()["tasks-only"])
    code, document, _ = _invoke(*_live_args(instance, "--case", "tasks-only"))
    assert code == 0, document
    with Store.open(instance / "state" / "horae.db", read_only=True) as store:
        dead = store.run_summary("dead")
    assert dead is not None
    assert dead.status == "failed"
    assert dead.abandoned_at is not None


class _Interrupting(FakeProvider):
    """Answers the first request, then raises `error` (a Ctrl-C, say) on the next."""

    def __init__(self, output: Any, error: BaseException) -> None:
        super().__init__(output)
        self.error = error

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if self.requests:
            self.requests.append(request)
            raise self.error
        return super().__call__(request)


def test_ctrl_c_midway_writes_the_report_and_settles_the_ledger(
    instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Interrupting(_recorded_outputs()["injected-embed"], KeyboardInterrupt())
    original = AnthropicBackend.__init__

    def patched(self: AnthropicBackend, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs, transport=httpx2.MockTransport(fake))

    monkeypatch.setattr(AnthropicBackend, "__init__", patched)
    code, document, output = _invoke(*_live_args(instance))
    assert code == 130
    assert document["error"]["kind"] == "interrupted"
    assert len(fake.requests) == 2
    (report,) = (instance / "state" / "evals").rglob("*.json")
    written = json.loads(report.read_text())
    assert written["error"]["kind"] == "interrupted"
    assert [case["case"] for case in written["cases"]] == ["injected-embed"]
    assert "interrupted" in output
    with Store.open(instance / "state" / "horae.db", read_only=True) as store:
        (run,) = store.list_runs()
        assert run.status == "failed"
        assert run.error is not None
        rows = store._conn.execute("SELECT state, estimated FROM llm_usage ORDER BY id").fetchall()
    # The answered case is settled at its cost; the interrupted one keeps its reservation.
    assert rows == [("settled", 0), ("settled", 1)]


def test_an_exception_while_sending_fails_that_case_only() -> None:
    import asyncio

    from horae.contracts.instance import ModelAlias
    from horae.evals import LiveEval, plan_live, send_live
    from horae.secrets import SecretResolver

    sue = cli_eval.locate(SKILL, None, PLUGINS).sue
    cases = load_cases(_evals(PLUGINS / "pack-daily-brief"))
    models = {"default": ModelAlias.model_validate(ANTHROPIC)}
    live = LiveEval.build("default", models, SecretResolver({}, Path("/nonexistent"), {}))
    planned, errors = plan_live(live, sue, cases, 10.0)
    assert not errors
    sent: list[int] = []

    async def complete(item: Any, index: int) -> Any:
        sent.append(index)
        raise RuntimeError("boom")

    async def go() -> list[Any]:
        try:
            return await send_live(live, sue, planned, complete=complete)
        finally:
            await live.backends.aclose()

    results = asyncio.run(go())
    assert sent == [0, 1, 2]
    assert {r.error["kind"] for r in results if r.error} == {"case_failed"}
    assert all("boom" not in r.error["message"] for r in results if r.error)


def test_report_files_and_folders_are_private(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    provider(_recorded_outputs()["tasks-only"])
    code, document, _ = _invoke(*_live_args(instance, "--case", "tasks-only"))
    assert code == 0, document
    report = Path(document["report"])
    for folder in (report.parent, report.parent.parent, report.parent.parent.parent):
        assert folder.stat().st_mode & 0o777 == 0o700, folder
    for path in (report, report.with_suffix(".txt")):
        assert path.stat().st_mode & 0o777 == 0o600, path
    assert not [p for p in report.parent.iterdir() if p.name.startswith(".")]


def test_a_report_that_cannot_be_written_is_said_and_the_json_still_printed(
    instance: Path, provider: Callable[..., FakeProvider]
) -> None:
    provider(_recorded_outputs()["tasks-only"])
    (instance / "state").mkdir()
    (instance / "state" / "evals").write_text("not a folder")
    code, document, output = _invoke(*_live_args(instance, "--case", "tasks-only"))
    assert code == 1
    assert document["report"] is None
    assert "evals" in document["report_error"]
    assert document["cases"][0]["status"] == "pass"
    assert "the report cannot be written" in output


def test_report_links_are_refused(instance: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (instance / "state").mkdir()
    (instance / "state" / "evals").symlink_to(elsewhere)
    document: dict[str, Any] = {"skill": SKILL, "cases": [], "summary": {}, "report": None}
    with pytest.raises(CommandFailed):
        cli_eval.write_report(instance / "state", document, datetime.now(UTC))
    assert list(elsewhere.iterdir()) == []


def test_record_writes_only_answers_that_pass(
    instance: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    before = _recorded(pack, "tasks-only").read_text()
    different = {**_recorded_outputs()["tasks-only"], "summary": "Something else."}
    provider(different)
    code, document, _ = _invoke(
        *_live_args(instance, "--plugin-dir", str(pack), "--case", "tasks-only", "--record")
    )
    assert code == 4
    (case,) = document["cases"]
    assert case["recorded_path"] is None
    assert "not recorded" in case["recorded_skipped"]
    assert _recorded(pack, "tasks-only").read_text() == before


def test_record_keeps_a_bad_answer_a_case_expects(
    instance: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    # A case that proves an invalid output is caught records the invalid answer it got.
    _edit_yaml(_evals(pack) / "tasks-only.yaml", lambda d: d.update(expect={"schema_valid": False}))
    invalid = {"summary": "x", "ranked": [{"title": "t", "why": "soon"}]}
    provider(invalid)
    code, document, _ = _invoke(
        *_live_args(instance, "--plugin-dir", str(pack), "--case", "tasks-only", "--record")
    )
    assert code == 0, document
    recorded = load_recorded(_recorded(pack, "tasks-only"))
    assert recorded is not None
    assert recorded.output == invalid


def test_write_recorded_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    from horae.evals import write_recorded

    recorded = load_recorded(_recorded(PLUGINS / "pack-daily-brief", "tasks-only"))
    assert recorded is not None
    path = write_recorded(tmp_path / "evals", "tasks-only", recorded)
    assert load_recorded(path) == recorded
    assert path.stat().st_mode & 0o777 == 0o644
    assert write_recorded(tmp_path / "evals", "tasks-only", recorded) == path
    assert sorted(p.name for p in path.parent.iterdir()) == ["tasks-only.json"]
    with pytest.raises(EvalError, match="not a case name"):
        write_recorded(tmp_path / "evals", "Bad", recorded)


def test_dotfiles_in_eval_folders_are_skipped(pack: Path) -> None:
    folder = _evals(pack)
    (folder / ".DS_Store").write_bytes(b"\x00\x01")
    (folder / ".hidden.yaml").write_text("not: a case")
    (folder / "recorded" / ".DS_Store").write_bytes(b"\x00")
    (folder / "recorded" / "anthropic" / ".DS_Store").write_bytes(b"\x00")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 0, document
    assert len(document["cases"]) == 3


def test_a_bad_case_file_name_is_an_error_result(pack: Path) -> None:
    (_evals(pack) / "Bad Name.yaml").write_text("schema_version: 1\ninput: {}\n")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    by_case = {c["case"]: c for c in document["cases"]}
    assert "not a case name" in by_case["Bad Name.yaml"]["error"]["message"]
    assert by_case["tasks-only"]["status"] == "pass"
    code, _, output = _invoke(SKILL, "--plugin-dir", str(pack))
    assert "ERROR Bad Name.yaml: eval_invalid" in output


def test_an_invalid_recording_is_an_error_result(pack: Path) -> None:
    _recorded(pack, "tasks-only").write_text("{")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    by_case = {c["case"]: c for c in document["cases"]}
    assert by_case["tasks-only"]["error"]["kind"] == "eval_invalid"
    assert by_case["overdue-first"]["status"] == "pass"


def test_an_invalid_target_manifest_is_a_problem(pack: Path) -> None:
    (pack / "plugin.yaml").write_text("name: [")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "plugins_invalid"
    # In a folder of plugins, the skill may be the invalid one's: not "unknown".
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack.parent), "--json")
    assert code == 1
    assert document["error"]["kind"] == "plugins_invalid"
    assert "invalid manifests" in document["error"]["message"]


def test_two_plugins_providing_the_skill_are_refused(pack: Path) -> None:
    shutil.copytree(pack, pack.parent / "pack-copy")
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack.parent), "--json")
    assert code == 1
    assert document["error"]["kind"] == "plugins_invalid"
    assert "several plugins" in document["error"]["message"]


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "length", "content_filter"])
def test_a_recording_that_stopped_on_a_truncation_or_refusal_fails(pack: Path, stop: str) -> None:
    _edit_json(_recorded(pack, "tasks-only"), lambda d: d.update(stop_reason=stop))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4
    (check,) = [c for c in document["cases"][0]["checks"] if c["name"] == "stop_reason"]
    assert check["ok"] is False
    assert stop in check["message"]
    assert document["cases"][0]["stop_reason"] == stop


def test_the_backends_share_their_stop_reasons() -> None:
    from horae.llm.backends import (
        REFUSAL_STOP_REASONS,
        TRUNCATION_STOP_REASONS,
        UNUSABLE_STOP_REASONS,
    )
    from horae.llm.backends.openai_compatible import TRUNCATED

    assert set(TRUNCATED) <= TRUNCATION_STOP_REASONS
    assert {"max_tokens", "model_context_window_exceeded"} <= TRUNCATION_STOP_REASONS
    assert {"refusal", "content_filter"} == REFUSAL_STOP_REASONS
    assert UNUSABLE_STOP_REASONS == REFUSAL_STOP_REASONS | TRUNCATION_STOP_REASONS
    assert not {"end_turn", "stop", "stop_sequence"} & UNUSABLE_STOP_REASONS


def test_usage_over_the_case_budget_fails(pack: Path) -> None:
    _edit_yaml(
        _evals(pack) / "tasks-only.yaml",
        lambda d: d.update(budget={"usd": 0.05, "tokens_out": 10}),
    )
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4
    (check,) = [c for c in document["cases"][0]["checks"] if c["name"] == "usage_within_budget"]
    assert check["ok"] is False
    assert "over the budget's 10" in check["message"]


def _task_pack(pack: Path) -> None:
    """The pack's skill, allowed to propose `task.update_status` too."""
    both = ["note.create", "task.update_status"]
    _edit_yaml(pack / "plugin.yaml", lambda d: d["permissions"].update(intents=both))
    folder = pack / "skills" / "daily-brief" / "rank-and-word"
    _edit_yaml(folder / "skill.yaml", lambda d: d.update(intents=both))

    def schema(data: Any) -> None:
        data["$defs"]["update"] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "task", "status"],
            "properties": {
                "type": {"type": "string", "const": "task.update_status"},
                "task": {"type": "string", "maxLength": 128},
                "status": {"type": "string", "enum": ["done", "todo"]},
            },
        }
        data["properties"]["intents"]["items"] = {
            "anyOf": [{"$ref": "#/$defs/note"}, {"$ref": "#/$defs/update"}]
        }
        data["properties"]["intents"]["maxItems"] = 2

    _edit_json(folder / "output.schema.json", schema)


def test_a_hallucinated_task_fingerprint_is_reported(pack: Path) -> None:
    _task_pack(pack)
    case = yaml.safe_load((_evals(pack) / "tasks-only.yaml").read_text())
    case["input"]["tasks"][0]["title"] = "task-1"
    case["expect"] = {}
    (_evals(pack) / "tasks-only.yaml").write_text(yaml.safe_dump(case))

    def answer(task: str) -> Callable[[Any], None]:
        def change(data: Any) -> None:
            data["output"]["intents"] = [
                {"type": "task.update_status", "task": task, "status": "done"}
            ]

        return change

    _edit_json(_recorded(pack, "tasks-only"), answer("task-1"))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 0, document  # a task the model was shown
    _edit_json(_recorded(pack, "tasks-only"), answer("task-999"))
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 4
    (result,) = document["cases"]
    assert "not shown" in result["intents_error"]


def test_engine_helpers_shared_with_the_eval() -> None:
    from horae.contracts.intents import TaskUpdateStatus
    from horae.engine import check_task_fingerprints, fill_target

    filled = fill_target({"type": "note.create", "content": "x"}, {"path": "A.md"})
    assert filled == {"type": "note.create", "content": "x", "path": "A.md"}
    kept = {"type": "note.create", "content": "x", "path": "B.md"}
    assert fill_target(kept, {"path": "A.md"}) == kept
    push = {"type": "notify.push", "title": "t"}
    assert fill_target(push, {"path": "A.md"}) == push
    assert fill_target("not a dict", {"path": "A.md"}) == "not a dict"
    update = TaskUpdateStatus(type="task.update_status", task="t-1", status="done")
    other = TaskUpdateStatus(type="task.update_status", task="t-2", status="done")
    assert check_task_fingerprints([update, other], ({"tasks": [{"id": "t-1"}]},)) == [1]
    assert check_task_fingerprints([], ({"tasks": []},)) == []


def test_eval_pipeline_names_fit_the_store() -> None:
    assert cli_eval.eval_pipeline(SKILL) == f"eval:{SKILL}"
    long = "a" * 30 + "/" + "b" * 60
    name = cli_eval.eval_pipeline(long)
    assert len(name) == 64
    assert name.startswith("eval:aaa")
    assert name != cli_eval.eval_pipeline("a" * 30 + "/" + "b" * 59 + "c")


# --- D4: every skill renders through every backend ---------------------------------------------


def _all_skills() -> list[tuple[Path, str]]:
    found = []
    for directory in (PLUGINS, BUILTIN):
        for manifest in sorted(directory.glob("*/plugin.yaml")):
            data = yaml.safe_load(manifest.read_text())
            for skill in (data.get("provides") or {}).get("skills") or []:
                found.append((manifest.parent, skill))
    return found


@pytest.mark.parametrize(("plugin", "skill"), _all_skills(), ids=lambda v: str(v))
def test_every_skill_renders_for_every_backend(plugin: Path, skill: str) -> None:
    from horae.evals import CaseResult as Result
    from horae.evals import LoadedCase as Loaded
    from horae.evals import offline_alias
    from horae.evals._harness import request_for, viewed_input
    from horae.llm.backends import FACTORIES, schema_problems
    from horae.llm.base import ModelBackends
    from horae.secrets import SecretResolver

    sue = cli_eval.locate(skill, None, plugin).sue
    assert schema_problems(sue.skill.output_schema) == {}
    loaded = Loaded("render", EvalCase.model_validate({"schema_version": 1, "input": {}}))
    viewed = viewed_input(sue, loaded, Result("render"))
    for backend in FACTORIES:
        backends = ModelBackends(
            models={"x": offline_alias(backend, "render-model")},
            secrets=SecretResolver({}, Path("/nonexistent"), {}),
            factories=FACTORIES,
        )
        rendered = backends.render(request_for(sue, "x", loaded, viewed, 10.0))  # never sent
        assert rendered.backend == backend


def test_an_enum_without_type_is_refused_by_the_loader(pack: Path) -> None:
    folder = pack / "skills" / "daily-brief" / "rank-and-word"
    _edit_json(
        folder / "output.schema.json",
        lambda d: d["$defs"]["line"]["properties"]["why"].pop("type"),
    )
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--json")
    assert code == 1
    assert document["error"]["kind"] == "skill_invalid"
    assert "`enum` needs a `type` beside it" in document["error"]["message"]
    assert "#/$defs/line/properties/why" in document["error"]["message"]


def test_the_anthropic_render_error_names_the_schema_path() -> None:
    from horae.engine.ports import LlmError
    from horae.llm.backends.anthropic import _schema, schema_problem

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "object", "properties": {"b": {"enum": ["x"]}}}},
    }
    problem = schema_problem(schema)
    assert problem is not None
    assert problem.startswith("#/properties/a/properties/b: ValueError")
    with pytest.raises(LlmError, match="#/properties/a/properties/b") as raised:
        _schema(schema)
    assert raised.value.__cause__ is not None
    assert schema_problem({"type": "object", "properties": {}}) is None


# --- de-identification (decision 47) on the eval's path ----------------------------------------


def _classed_pack(pack: Path) -> None:
    """The pack's skill with its task titles classed `person`."""
    view = {
        "tasks[].title": {"class": "person"},
        "tasks[].due": None,
        "events[].start": None,
        "events[].summary": None,
    }
    skill = pack / "skills" / "daily-brief" / "rank-and-word" / "skill.yaml"
    _edit_yaml(skill, lambda d: d.update(input_view=view))


def test_a_cloud_alias_is_sent_the_pseudonymized_input(
    instance: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    _classed_pack(pack)
    pseudonymous = {
        "summary": "One task is due today.",
        "ranked": [
            {"title": "[PERSON_1]", "why": "today"},
            {"title": "[PERSON_2]", "why": "later"},
        ],
    }
    fake = provider(pseudonymous)
    args = ("--plugin-dir", str(pack), "--case", "tasks-only", "--record")
    code, document, _ = _invoke(*_live_args(instance, *args))
    assert code == 0, document  # re-identified before it is compared: the real titles
    (request,) = fake.requests
    assert b"Book the train tickets" not in request.content
    assert b"[PERSON_1]" in request.content
    # The recording keeps what the provider saw, never the real values.
    text = _recorded(pack, "tasks-only").read_text()
    assert "[PERSON_1]" in text
    assert "Book the train tickets" not in text
    # Offline, the same pseudonymization gives the same request: not stale, and it passes.
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 0, document
    assert document["cases"][0]["stale"] == []


def test_a_local_alias_is_sent_the_viewed_input(pack: Path) -> None:
    from horae.contracts.instance import ModelAlias
    from horae.evals import CaseResult as Result
    from horae.evals import offline_alias
    from horae.evals._harness import deidentify
    from horae.llm.deid import Deidentifier

    _classed_pack(pack)
    sue = cli_eval.locate(SKILL, None, pack).sue
    local = ModelAlias.model_validate(
        {"backend": "ollama", "model": "m", "base_url": "http://ollama:11434"}
    )
    case = load_cases(_evals(pack), "tasks-only")[0]
    viewed = sue.skill.view(case.case.input)
    assert deidentify(sue, Deidentifier({"x": local}), "x", viewed, Result("c")) == (True, None)
    cloud = Deidentifier({"x": offline_alias("anthropic", "m")})
    ok, pseudo = deidentify(sue, cloud, "x", viewed, Result("c"))
    assert ok
    assert pseudo is not None
    assert "Book the train tickets" not in json.dumps(pseudo.data)


def test_an_unreadable_vocabulary_sends_nothing(
    tmp_path: Path, provider: Callable[..., FakeProvider], pack: Path
) -> None:
    _classed_pack(pack)
    root = _instance(tmp_path / "instance", deidentification={"vocabulary": "missing-vocab"})
    fake = provider(_recorded_outputs()["tasks-only"])
    args = ("--plugin-dir", str(pack), "--case", "tasks-only")
    code, document, _ = _invoke(*_live_args(root, *args))
    assert code == 1, document
    assert document["cases"][0]["error"]["kind"] == "deid_unavailable"
    assert fake.requests == []


def _plant_title_fragment(tmp_path: Path) -> None:
    """A resolved secret that is part of a classed task title (synthetic)."""
    from horae.secrets import SecretResolver

    folder = tmp_path / "planted"
    folder.mkdir()
    (folder / "TITLE_TOKEN").write_text("train tickets", encoding="utf-8")
    SecretResolver({}, folder).resolve("TITLE_TOKEN")


def test_a_secret_in_the_viewed_input_is_refused_before_deid_live(
    instance: Path, provider: Callable[..., FakeProvider], pack: Path, tmp_path: Path
) -> None:
    _classed_pack(pack)
    _plant_title_fragment(tmp_path)
    fake = provider(_recorded_outputs()["tasks-only"])
    args = ("--plugin-dir", str(pack), "--case", "tasks-only")
    code, document, _ = _invoke(*_live_args(instance, *args))
    assert code == 1, document
    error = document["cases"][0]["error"]
    assert error["kind"] == "secret_in_request"
    assert "TITLE_TOKEN" in error["message"]
    assert "train tickets" not in json.dumps(document)
    assert fake.requests == []  # nothing sent, nothing billed


def test_a_secret_in_the_viewed_input_is_refused_before_deid_offline(
    pack: Path, tmp_path: Path
) -> None:
    _classed_pack(pack)
    _plant_title_fragment(tmp_path)
    code, document, _ = _invoke(SKILL, "--plugin-dir", str(pack), "--case", "tasks-only", "--json")
    assert code == 1, document
    error = document["cases"][0]["error"]
    assert error["kind"] == "secret_in_request"
    assert "TITLE_TOKEN" in error["message"]
