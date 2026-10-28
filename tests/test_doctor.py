# SPDX-License-Identifier: Apache-2.0
"""`horae doctor` reports configuration problems with file and line, and never secret values."""

import json
import logging
import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli
from horae.cli import app
from horae.secrets import Secret, SecretResolver

runner = CliRunner()


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _doctor(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["doctor", *args])
    return result.exit_code, _plain(result.output)


def test_doctor_ok(with_secrets: Path) -> None:
    code, out = _doctor("--instance", str(with_secrets))
    assert code == cli.EXIT_OK == 0
    assert out.strip() == "OK: 2 pipelines, 1 plugin, 2 model aliases"


def test_doctor_reads_the_instance_from_the_environment(
    with_secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORAE_INSTANCE", str(with_secrets))
    assert _doctor()[0] == 0


def test_doctor_defaults_to_the_current_directory(
    with_secrets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HORAE_INSTANCE", raising=False)
    monkeypatch.chdir(with_secrets)
    code, out = _doctor()
    assert code == 0
    assert out.startswith("OK: ")


def test_doctor_reports_problems_with_file_and_line(instance_dir: Path) -> None:
    brief = instance_dir / "pipelines" / "daily-brief.yaml"
    brief.write_text(
        brief.read_text(encoding="utf-8").replace("model: default", "model: fancy"),
        encoding="utf-8",
    )
    code, out = _doctor("-i", str(instance_dir))
    assert code == cli.EXIT_PROBLEMS == 1
    lines = out.strip().splitlines()
    assert lines[-1] == "3 problems found"
    assert lines[0].startswith(f"{instance_dir / 'horae.lock'}:10:17: secret GITHUB_TOKEN: file ")
    assert lines[1].startswith(f"{instance_dir / 'horae.yaml'}:5:68: secret ANTHROPIC_API_KEY")
    assert lines[2].startswith(f"{brief}:13:14: unknown model alias 'fancy'")


def test_doctor_missing_instance(tmp_path: Path) -> None:
    code, out = _doctor("-i", str(tmp_path))
    assert code == 1
    assert out.splitlines() == [f"{tmp_path / 'horae.yaml'}: file not found", "1 problem found"]


def test_doctor_usage_error_exits_2(tmp_path: Path) -> None:
    assert _doctor("--no-such-option")[0] == cli.EXIT_USAGE == 2
    (tmp_path / "file").write_text("x", encoding="utf-8")
    assert _doctor("-i", str(tmp_path / "file"))[0] == 2


def test_doctor_json(instance_dir: Path) -> None:
    code, out = _doctor("-i", str(instance_dir), "--json")
    assert code == 1
    report = json.loads(out)
    assert report["schema_version"] == cli.REPORT_SCHEMA_VERSION == 1
    assert report["ok"] is False
    assert report["instance"] == str(instance_dir)
    assert [(p["file"], p["line"], p["column"]) for p in report["problems"]] == [
        (str(instance_dir / "horae.lock"), 10, 17),
        (str(instance_dir / "horae.yaml"), 5, 68),
    ]
    assert all(p["severity"] == "error" for p in report["problems"])


def test_doctor_json_ok(with_secrets: Path) -> None:
    code, out = _doctor("-i", str(with_secrets), "--json")
    assert code == 0
    assert json.loads(out) == {
        "schema_version": 1,
        "ok": True,
        "instance": str(with_secrets),
        "problems": [],
    }


def test_doctor_never_prints_secret_values(
    with_secrets: Path,
    secret_values: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two secrets resolve, the others are broken in ways that read their values.
    anthropic_key, github_token = secret_values
    config = with_secrets / "horae.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  personal: { backend: claude-cli, model: sonnet }",
            "  personal: { backend: claude-cli, model: sonnet, secret: CLAUDE_CODE_OAUTH_TOKEN }"
            "\n  bad: { backend: ollama, model: m, base_url: 'http://ollama:11434',"
            " secret: BROKEN_KEY }",
        ),
        encoding="utf-8",
    )
    secrets = with_secrets / "secrets"
    (secrets / "CLAUDE_CODE_OAUTH_TOKEN").write_bytes(github_token.encode() + b"\x00")
    (secrets / "BROKEN_KEY").write_bytes(b"\xfe" + anthropic_key.encode())
    outputs = []
    with caplog.at_level(logging.DEBUG):
        for args in ([], ["--json"]):
            code, out = _doctor("-i", str(with_secrets), *args)
            assert code == 1
            outputs.append(out)
    outputs += [caplog.text, *capsys.readouterr()]
    assert "BROKEN_KEY" in outputs[0]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in outputs[0]
    for text in outputs:
        assert anthropic_key not in text
        assert github_token not in text


@pytest.mark.parametrize("json_output", [False, True])
def test_internal_error_with_a_resolved_secret_prints_no_value(
    with_secrets: Path,
    secret_values: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    json_output: bool,
) -> None:
    # A bug after a secret resolved: the exception message, its traceback and the frame
    # locals all hold the value. The doctor must print none of them.
    anthropic_key = secret_values[0]
    real_resolve = SecretResolver.resolve

    def buggy(self: SecretResolver, name: str) -> Secret:
        secret = real_resolve(self, name)
        value = secret.reveal()
        raise RuntimeError(f"bug while handling {value}")

    monkeypatch.setattr(SecretResolver, "resolve", buggy)
    with caplog.at_level(logging.DEBUG):
        result = runner.invoke(
            app, ["doctor", "-i", str(with_secrets), *(["--json"] if json_output else [])]
        )
    assert result.exit_code == cli.EXIT_INTERNAL == 70
    assert "internal error (RuntimeError)" in result.output
    assert anthropic_key not in result.output
    assert anthropic_key not in caplog.text
    assert "Traceback" not in result.output


def test_tracebacks_never_show_local_variables() -> None:
    # Rich tracebacks with locals would print resolved secret values on a crash.
    assert app.pretty_exceptions_show_locals is False


def _created(tmp_path: Path, name: str = "instance") -> Path:
    result = runner.invoke(app, ["init", str(tmp_path / name), "--json"])
    assert result.exit_code == 0, result.output
    return Path(json.loads(result.stdout)["instance"])


def _report(root: Path) -> tuple[int, dict[str, object]]:
    result = runner.invoke(app, ["doctor", "-i", str(root), "--json"])
    report: dict[str, object] = json.loads(result.stdout)
    return result.exit_code, report


def _messages(report: dict[str, object]) -> list[str]:
    problems = report["problems"]
    assert isinstance(problems, list)
    return [f"{p['file']}: {p['message']}" for p in problems]


def test_doctor_json_instance_is_the_resolved_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _created(tmp_path)
    monkeypatch.chdir(root)
    code, report = _report(Path())
    assert code == 0, report
    assert report["instance"] == str(root.resolve())


def test_doctor_reports_a_copied_instance(tmp_path: Path) -> None:
    """`cp -R`: the copy's lock still writes to the original's vault."""
    original = _created(tmp_path)
    copy = tmp_path / "copy"
    shutil.copytree(original, copy, symlinks=True)
    code, report = _report(copy)
    assert code == cli.EXIT_PROBLEMS
    (message,) = _messages(report)
    assert message.startswith("horae.lock: the instance has its own vault/")
    assert f"horae.lock writes to {original / 'vault'}" in message
    assert "not relocatable" in message
    assert _report(original)[0] == 0


def test_doctor_reports_a_moved_instance(tmp_path: Path) -> None:
    """`mv`: the lock's vault no longer exists."""
    original = _created(tmp_path)
    moved = tmp_path / "moved"
    original.rename(moved)
    code, report = _report(moved)
    assert code == cli.EXIT_PROBLEMS
    messages = _messages(report)
    assert (
        f"horae.lock: plugins.file-sink.permissions.paths.write: {original / 'vault'} does not "
        "exist"
    ) in messages[0]
    assert any("has its own vault/" in m for m in messages)
    text = runner.invoke(app, ["doctor", "-i", str(moved)])
    assert text.exit_code == cli.EXIT_PROBLEMS
    assert text.output.splitlines()[-1] == "2 problems found"


def test_doctor_reports_a_broken_plugin_interpreter(tmp_path: Path) -> None:
    root = _created(tmp_path)
    manifest = root / "plugins" / "file-sink" / "plugin.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["runtime"]["command"][0] = str(tmp_path / "gone" / "python")
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    code, report = _report(root)
    assert code == cli.EXIT_PROBLEMS
    (message,) = _messages(report)
    assert message.startswith("plugins/file-sink/plugin.yaml: runtime.command: ")
    assert "not found or not executable" in message


def test_doctor_reports_plugins_that_do_not_match_the_lock(tmp_path: Path) -> None:
    root = _created(tmp_path)
    (root / "plugins" / "fixture-source" / "plugin.yaml").unlink()
    code, report = _report(root)
    assert code == cli.EXIT_PROBLEMS
    (message,) = _messages(report)
    assert message.startswith("plugins/: ")
    assert "horae init" in message


PACK = Path(__file__).parent / "fixtures" / "plugins" / "pack-daily-brief"
SKILL = "daily-brief/rank-and-word"


def _with_skill(tmp_path: Path) -> Path:
    """An `horae init` instance plus the fixture skill pack and a pipeline using its skill."""
    root = _created(tmp_path)
    shutil.copytree(PACK, root / "plugins" / "pack-daily-brief")
    lock = yaml.safe_load((root / "horae.lock").read_text(encoding="utf-8"))
    lock["plugins"]["pack-daily-brief"] = {
        "version": "0.1.0",
        "repo": "https://git.example.org/horae/pack-daily-brief",
        "commit": "0" * 40,
        "permissions": {"intents": ["note.create"]},
    }
    (root / "horae.lock").write_text(yaml.safe_dump(lock), encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "ANTHROPIC_API_KEY").write_text("fixture-key", encoding="utf-8")
    with (root / "horae.yaml").open("a", encoding="utf-8") as config:
        config.write(
            "secrets: { dir: secrets }\nmodels:\n  default: { backend: anthropic, model: m, "
            "secret: ANTHROPIC_API_KEY, price: { input_per_mtok: 1, output_per_mtok: 1 } }\n"
        )
    pipeline = {
        "name": "brief",
        "steps": [
            {"collect": [{"fixture-source.serve": {}}]},
            {
                "llm": {
                    "skill": SKILL,
                    "model": "default",
                    "budget": {"usd": 0.05, "tokens_out": 2000},
                }
            },
        ],
    }
    (root / "pipelines" / "brief.yaml").write_text(yaml.safe_dump(pipeline), encoding="utf-8")
    return root


def test_doctor_loads_the_skills_of_the_pipelines(tmp_path: Path) -> None:
    root = _with_skill(tmp_path)
    code, report = _report(root)
    assert code == 0, report
    folder = root / "plugins" / "pack-daily-brief" / "skills" / SKILL
    (folder / "prompt.md").write_text("Bidi " + chr(0x202E) + " trick\n", encoding="utf-8")
    code, report = _report(root)
    assert code == cli.EXIT_PROBLEMS
    (message,) = _messages(report)
    assert message.startswith(f"plugins/pack-daily-brief/skills/{SKILL}/: skill_invalid: ")
    assert "prompt.md:1: control or invisible character U+202E" in message
    # The text report escapes what it prints (`safe`): the message holds no raw control.
    text = runner.invoke(app, ["doctor", "-i", str(root)])
    assert text.exit_code == cli.EXIT_PROBLEMS
    assert chr(0x202E) not in text.output
    assert "skill_invalid" in text.output
