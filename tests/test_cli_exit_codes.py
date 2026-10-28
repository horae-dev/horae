# SPDX-License-Identifier: Apache-2.0
"""One exit-code table and one `--json` convention for every command (#26, decision 37)."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import horae.cli as cli
from horae import cli_output
from horae.cli import app

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("init", "run", "runs", "diff", "undo", "replay", "doctor")
TABLE = {0: "ok", 1: "problems", 2: "usage", 3: "unknown or refused", 4: "different"}
runner = CliRunner()


def _plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # colours (forced under GITHUB_ACTIONS)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def test_the_table() -> None:
    assert sorted(cli_output.EXIT_CODES) == [0, 1, 2, 3, 4, 5, 70, 130, 143]
    assert {code: cli_output.EXIT_CODES[code] for code in TABLE} == TABLE
    assert (cli.EXIT_LOCKED, cli.EXIT_INTERNAL) == (5, 70)
    assert (cli.EXIT_SIGINT, cli.EXIT_SIGTERM) == (130, 143)
    # The aliases kept for the older commands are the shared codes.
    assert cli.EXIT_UNKNOWN_RUN == cli.EXIT_NOT_COMPARABLE == cli.EXIT_REFUSED == 3
    assert cli.EXIT_PLANNED_WITH_PROBLEMS == cli.EXIT_DIFFERENT == 4


def test_the_table_is_documented() -> None:
    decisions = (ROOT / "schemas" / "README.md").read_text(encoding="utf-8")
    (decision,) = re.findall(r"^37\. \*\*CLI.*?(?=^\S|\Z)", decisions, re.M | re.S)
    for code in cli_output.EXIT_CODES:
        assert f"`{code}`" in decision, code
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "horae init" in readme
    assert "Exit codes" in readme
    root_help = _plain(runner.invoke(app, ["--help"], terminal_width=200).output)
    assert "Exit codes (every command): 0 ok, 1 problems" in root_help
    assert "5 locked" in root_help
    assert "130/143 interrupted" in root_help


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_documents_its_exit_codes(command: str) -> None:
    result = runner.invoke(app, [command, "--help"], terminal_width=200)
    assert result.exit_code == 0
    text = _plain(result.output)
    assert "Exit codes: 0 " in text
    assert "2 usage error" in text
    assert "70 internal error" in text
    assert "With --json" in text


@pytest.mark.parametrize("command", COMMANDS)
def test_usage_errors_are_json_under_json(command: str) -> None:
    result = runner.invoke(app, [command, "--json", "--no-such-option"])
    assert result.exit_code == cli.EXIT_USAGE == 2
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "error": {"kind": "usage", "message": "No such option: --no-such-option"},
    }
    text = runner.invoke(app, [command, "--no-such-option"])
    assert text.exit_code == 2
    assert text.stdout == ""
    assert "No such option" in text.output
    # After `--`, `--json` is an argument, not the option.
    assert runner.invoke(app, [command, "--bogus", "--", "--json"]).stdout == ""


@pytest.mark.parametrize("command", ["run", "diff", "undo", "replay"])
def test_a_missing_argument_is_a_json_usage_error(command: str) -> None:
    result = runner.invoke(app, [command, "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["kind"] == "usage"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["run", "daily"], 1),
        (["runs"], 1),
        (["diff", "r1"], 1),
        (["undo", "r1"], 1),
        (["replay", "r1"], 1),
        (["doctor"], 1),
    ],
)
def test_every_command_prints_one_json_document(args: list[str], code: int, tmp_path: Path) -> None:
    """An instance without horae.yaml: a configuration problem, as one JSON document."""
    result = runner.invoke(app, [*args, "-i", str(tmp_path), "--json"])
    assert result.exit_code == code
    document: dict[str, Any] = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert result.stdout.isascii()
    if args[0] == "doctor":
        assert document["ok"] is False
    elif args[0] == "replay":
        assert document["outcome"] == "error"
    else:
        assert document["error"]["kind"] == "config_invalid"
    text = runner.invoke(app, [*args, "-i", str(tmp_path)])
    assert text.exit_code == code
    if args[0] not in {"doctor", "replay"}:
        assert text.stdout == ""
        assert text.stderr.startswith(f"horae {args[0]}: ")


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("doctor", "_report"),
        ("runs", "_list_runs"),
        ("replay", "_replay"),
        ("diff", "_diff"),
    ],
)
def test_internal_errors_are_json_under_json(
    command: str, target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*args: Any) -> Any:
        raise RuntimeError("sentinel-secret-value")

    monkeypatch.setattr(cli, target, crash)
    args = [command, *(["r1"] if command in {"replay", "diff"} else []), "-i", str(tmp_path)]
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == cli.EXIT_INTERNAL
    assert json.loads(result.stdout)["error"] == {
        "kind": "internal",
        "message": "internal error (RuntimeError); please report it",
    }
    assert "sentinel-secret-value" not in result.output
    text = runner.invoke(app, args)
    assert text.exit_code == cli.EXIT_INTERNAL
    assert text.stderr == f"horae {command}: internal error (RuntimeError); please report it\n"


def test_untrusted_text_is_escaped_in_errors(tmp_path: Path) -> None:
    hostile = "run" + chr(0x202E) + "\x1b[31m"
    result = runner.invoke(app, ["undo", hostile, "-i", str(tmp_path)])
    assert result.exit_code == 1
    assert chr(0x202E) not in result.stderr
    assert "\x1b" not in result.stderr
    as_json = runner.invoke(app, ["undo", hostile, "-i", str(tmp_path), "--json"])
    assert as_json.stdout.isascii()


def test_diff_and_undo_report_missing_plugins(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    assert runner.invoke(app, ["run", "daily-fixture", "-i", str(root), "--dry-run"]).exit_code == 0
    runs = json.loads(runner.invoke(app, ["runs", "-i", str(root), "--json"]).stdout)["runs"]
    for manifest in (root / "plugins").rglob("plugin.yaml"):
        manifest.unlink()
    for command in ("diff", "undo"):
        result = runner.invoke(app, [command, runs[0]["id"], "-i", str(root), "--json"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"]["kind"] == "plugins_invalid"


def test_no_literal_invisible_characters_in_the_repository() -> None:
    """Bidi controls and invisible characters are written as escapes (`\\u202e`, `chr()`)."""
    invisible = re.compile(
        "["
        + "".join(map(chr, [*range(0x200B, 0x2010), 0x2028, 0x2029]))
        + "".join(map(chr, [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF]))
        + "]"
    )
    found = []
    for folder in ("src", "tests", "docs", "schemas", "examples", "scripts"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in {
                ".py",
                ".md",
                ".yaml",
                ".yml",
                ".json",
                ".lock",
                ".sql",
            }:
                text = path.read_text(encoding="utf-8", errors="replace")
                found += [f"{path.relative_to(ROOT)}:{m.start()}" for m in invisible.finditer(text)]
    assert found == []
