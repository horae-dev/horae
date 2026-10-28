# SPDX-License-Identifier: Apache-2.0
"""`horae doctor` reports configuration problems with file and line, and never secret values."""

import json
import logging
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horae.cli import app

runner = CliRunner()


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _doctor(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["doctor", *args])
    return result.exit_code, _plain(result.output)


def test_doctor_ok(with_secrets: Path) -> None:
    code, out = _doctor("--instance", str(with_secrets))
    assert code == 0
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


def test_doctor_reports_problems_with_file_and_line(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    brief = instance_dir / "pipelines" / "daily-brief.yaml"
    brief.write_text(
        brief.read_text(encoding="utf-8").replace("model: default", "model: fancy"),
        encoding="utf-8",
    )
    code, out = _doctor("-i", str(instance_dir))
    assert code == 1
    lines = out.strip().splitlines()
    assert lines[-1] == "3 problems found"
    assert f"{instance_dir / 'horae.lock'}:10:17: secret GITHUB_TOKEN: not set" in lines[0]
    assert lines[1].startswith(f"{instance_dir / 'horae.yaml'}:5:60: secret ANTHROPIC_API_KEY")
    assert lines[2].startswith(f"{brief}:13:7: unknown model alias 'fancy'")


def test_doctor_missing_instance(tmp_path: Path) -> None:
    code, out = _doctor("-i", str(tmp_path))
    assert code == 1
    assert out.splitlines() == [f"{tmp_path / 'horae.yaml'}: file not found", "1 problem found"]


def test_doctor_json(instance_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code, out = _doctor("-i", str(instance_dir), "--json")
    assert code == 1
    report = json.loads(out)
    assert report["ok"] is False
    assert report["instance"] == str(instance_dir)
    assert [(p["file"], p["line"], p["column"]) for p in report["problems"]] == [
        (str(instance_dir / "horae.lock"), 10, 17),
        (str(instance_dir / "horae.yaml"), 5, 60),
    ]
    assert all(p["severity"] == "error" for p in report["problems"])


def test_doctor_json_ok(with_secrets: Path) -> None:
    code, out = _doctor("-i", str(with_secrets), "--json")
    assert code == 0
    assert json.loads(out) == {"ok": True, "instance": str(with_secrets), "problems": []}


def test_doctor_never_prints_secret_values(
    with_secrets: Path,
    secret_values: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # One secret resolves, the others are broken in ways that read their values.
    anthropic_key, github_token = secret_values
    config = with_secrets / "horae.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  personal: { backend: claude-cli, model: sonnet }",
            "  personal: { backend: claude-cli, model: sonnet, secret: CLAUDE_CODE_OAUTH_TOKEN }"
            "\n  bad: { backend: ollama, model: m, secret: BROKEN_KEY }",
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
    assert "secret ANTHROPIC_API_KEY resolved from file" in caplog.text
    for text in outputs:
        assert anthropic_key not in text
        assert github_token not in text


def test_tracebacks_never_show_local_variables() -> None:
    # Rich tracebacks with locals would print resolved secret values on a crash.
    assert app.pretty_exceptions_show_locals is False
