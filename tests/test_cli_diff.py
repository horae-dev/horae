# SPDX-License-Identifier: Apache-2.0
"""`horae diff <run-id>`: the planned changes of a stored run, as text or JSON (#21).

That it writes nothing (store included) is tested under the guard of `test_dry_run.py`."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from apply_fixtures import DAILY, INBOX, RUN_ID, record
from horae import cli
from horae.apply import dedupe_key
from horae.cli import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PLANNED_WITH_PROBLEMS,
    EXIT_PROBLEMS,
    EXIT_UNKNOWN_RUN,
    EXIT_USAGE,
    app,
)
from horae.config import STORE_FILE
from horae.store import Store

runner = CliRunner()


@pytest.fixture
def stored(instance_dir: Path) -> Path:
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record())
    return instance_dir


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Daily").mkdir(parents=True)
    (root / INBOX).write_text("# Inbox\n")
    return root


def _diff(*args: str) -> Any:
    return runner.invoke(app, ["diff", *args])


def _json_error(result: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.stdout)
    assert list(data) == ["schema_version", "error"]
    assert data["schema_version"] == 1
    assert result.stderr == ""
    return data["error"]  # type: ignore[no-any-return]


def test_text_diff_of_a_stored_run(stored: Path, vault: Path) -> None:
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    result = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault))
    assert result.exit_code == EXIT_OK, result.output
    out = result.stdout
    assert f"--- /dev/null\n+++ b/{DAILY}\n" in out
    assert f"--- a/{INBOX}\n+++ b/{INBOX}\n" in out
    assert "\x1b[" not in out
    assert "Notifications:\n  via ntfy.push: Brief: 2 items today\n" in out
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before
    colored = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault), "--color")
    assert colored.exit_code == EXIT_OK
    assert "\x1b[32m+" in colored.stdout


def test_json_diff_and_already_sent_notifications_are_not_problems(
    stored: Path, vault: Path
) -> None:
    run = record()
    with Store.open(stored / "state" / STORE_FILE) as store:
        store.record_notification(dedupe_key(run, 5), at=run.finished_at)
    result = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault), "--json")
    assert result.exit_code == EXIT_OK
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    assert data["run"]["id"] == RUN_ID
    assert [f["status"] for f in data["files"]] == ["create", "modify"]
    assert data["notifications"][0]["duplicate"] is True


def test_planned_with_problems(stored: Path) -> None:
    result = _diff(RUN_ID, "-i", str(stored))
    assert result.exit_code == EXIT_PLANNED_WITH_PROBLEMS
    assert "intent 0 (vault.write_note): vault_unset" in result.stdout
    result = _diff(RUN_ID, "-i", str(stored), "--json")
    assert result.exit_code == EXIT_PLANNED_WITH_PROBLEMS
    assert json.loads(result.stdout)["problems"][0]["kind"] == "vault_unset"


def test_the_vault_comes_from_the_flag_only(
    stored: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORAE_VAULT", str(vault))
    assert _diff(RUN_ID, "-i", str(stored)).exit_code == EXIT_PLANNED_WITH_PROBLEMS


def test_refused_runs(instance_dir: Path) -> None:
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record().model_copy(update={"status": "failed", "error": "boom"}))
    result = _diff(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == EXIT_UNKNOWN_RUN
    assert "Nothing to apply: a failed run is never applied (run_failed)." in result.stdout
    result = _diff(RUN_ID, "-i", str(instance_dir), "--json")
    assert result.exit_code == EXIT_UNKNOWN_RUN
    assert json.loads(result.stdout)["refused"]["kind"] == "run_failed"


def test_unknown_runs(instance_dir: Path) -> None:
    result = _diff(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == EXIT_UNKNOWN_RUN
    assert result.stdout == ""
    assert "no store" in result.stderr
    assert not (instance_dir / "state").exists()
    error = _json_error(_diff(RUN_ID, "-i", str(instance_dir), "--json"))
    assert error["kind"] == "unknown_run"
    with Store.open(instance_dir / "state" / STORE_FILE):
        pass
    result = _diff("nope", "-i", str(instance_dir))
    assert result.exit_code == EXIT_UNKNOWN_RUN
    assert "unknown or unfinished run 'nope'" in result.stderr


def test_configuration_and_store_problems(instance_dir: Path) -> None:
    (instance_dir / "state").mkdir()
    (instance_dir / "state" / STORE_FILE).write_bytes(b"not a database" * 100)
    result = _diff(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == EXIT_PROBLEMS
    assert result.stderr.startswith("horae diff:")
    assert _json_error(_diff(RUN_ID, "-i", str(instance_dir), "--json"))["kind"] == "store_error"
    (instance_dir / "horae.yaml").write_text("timezone: 3\n")
    result = _diff(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == EXIT_PROBLEMS
    assert "problem" in result.stderr
    error = _json_error(_diff(RUN_ID, "-i", str(instance_dir), "--json"))
    assert error["kind"] == "config_invalid"


def test_os_errors_opening_the_store(stored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Store, "open", denied)
    result = _diff(RUN_ID, "-i", str(stored))
    assert result.exit_code == EXIT_PROBLEMS
    assert "Permission denied" in result.stderr


def test_an_old_store_is_not_migrated(instance_dir: Path) -> None:
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record())
    db = instance_dir / "state" / STORE_FILE
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 1")
    conn.close()
    result = _diff(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == EXIT_PROBLEMS
    assert "older" in result.stderr
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_usage_and_internal_errors(
    stored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _diff().exit_code == EXIT_USAGE
    assert _diff(RUN_ID, "--vault", str(tmp_path / "missing")).exit_code == EXIT_USAGE

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("s3cr3t")

    monkeypatch.setattr(cli, "plan_changes", boom)
    result = _diff(RUN_ID, "-i", str(stored))
    assert result.exit_code == EXIT_INTERNAL
    assert "internal error (RuntimeError)" in result.stderr
    assert "s3cr3t" not in result.output
    result = _diff(RUN_ID, "-i", str(stored), "--json")
    assert result.exit_code == EXIT_INTERNAL
    error = _json_error(result)
    assert error == {
        "kind": "internal",
        "message": "internal error (RuntimeError); please report it",
    }
