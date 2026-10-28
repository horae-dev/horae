# SPDX-License-Identifier: Apache-2.0
"""`horae diff <run-id>`: the planned changes of a stored run, as text or JSON (#21)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from apply_fixtures import DAILY, INBOX, RUN_ID, record
from horae import cli
from horae.apply import dedupe_key
from horae.cli import EXIT_INTERNAL, EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, STORE_FILE, app
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


def _diff(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["diff", *args])
    return result.exit_code, result.output


def test_text_diff_of_a_stored_run(stored: Path, vault: Path) -> None:
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    code, out = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault))
    assert code == EXIT_OK, out
    assert f"--- /dev/null\n+++ b/{DAILY}\n" in out
    assert f"--- a/{INBOX}\n+++ b/{INBOX}\n" in out
    assert "\x1b[" not in out
    assert "Notifications:\n  via ntfy.push: Brief: 2 items today\n" in out
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before
    code, colored = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault), "--color")
    assert code == EXIT_OK
    assert "\x1b[32m+" in colored


def test_json_diff_and_already_sent_notifications(stored: Path, vault: Path) -> None:
    run = record()
    with Store.open(stored / "state" / STORE_FILE) as store:
        store.record_notification(dedupe_key(run, run.intents[5].intent), at=run.finished_at)
    code, out = _diff(RUN_ID, "-i", str(stored), "--vault", str(vault), "--json")
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["schema_version"] == 1
    assert data["run"]["id"] == RUN_ID
    assert [f["status"] for f in data["files"]] == ["create", "modify"]
    assert data["notifications"][0]["duplicate"] is True


def test_without_a_vault_notes_are_problems(stored: Path) -> None:
    code, out = _diff(RUN_ID, "-i", str(stored))
    assert code == EXIT_PROBLEMS
    assert "intent 0 (vault.write_note): vault_unset" in out


def test_a_failed_run_is_refused(instance_dir: Path) -> None:
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record().model_copy(update={"status": "failed", "error": "boom"}))
    code, out = _diff(RUN_ID, "-i", str(instance_dir))
    assert code == EXIT_PROBLEMS
    assert "Nothing to apply: a failed run is never applied." in out


def test_unknown_runs(instance_dir: Path) -> None:
    code, out = _diff(RUN_ID, "-i", str(instance_dir))
    assert code == EXIT_PROBLEMS
    assert "no store" in out
    assert not (instance_dir / "state").exists()
    with Store.open(instance_dir / "state" / STORE_FILE):
        pass
    code, out = _diff("nope", "-i", str(instance_dir))
    assert code == EXIT_PROBLEMS
    assert "unknown or unfinished run 'nope'" in out


def test_configuration_and_store_problems(instance_dir: Path) -> None:
    (instance_dir / "state").mkdir()
    (instance_dir / "state" / STORE_FILE).write_bytes(b"not a database" * 100)
    code, out = _diff(RUN_ID, "-i", str(instance_dir))
    assert code == EXIT_PROBLEMS
    assert "horae diff:" in out
    (instance_dir / "horae.yaml").write_text("timezone: 3\n")
    code, out = _diff(RUN_ID, "-i", str(instance_dir))
    assert code == EXIT_PROBLEMS
    assert "problem" in out


def test_usage_and_internal_errors(
    stored: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _diff()[0] == EXIT_USAGE
    assert _diff(RUN_ID, "--vault", str(tmp_path / "missing"))[0] == EXIT_USAGE

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("s3cr3t")

    monkeypatch.setattr(cli, "plan_changes", boom)
    code, out = _diff(RUN_ID, "-i", str(stored))
    assert code == EXIT_INTERNAL
    assert "internal error (RuntimeError)" in out
    assert "s3cr3t" not in out
