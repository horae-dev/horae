# SPDX-License-Identifier: Apache-2.0
"""`horae doctor` reports configuration problems with file and line, and never secret values."""

import json
import logging
import re
from pathlib import Path

import pytest
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
