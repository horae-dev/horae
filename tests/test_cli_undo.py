# SPDX-License-Identifier: Apache-2.0
"""`horae undo <run-id>` (#22): restores a real vault through the provisional local sink."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import horae.cli_undo as cli_undo
from apply_fixtures import DAILY, INBOX, RUN_ID, START, record
from horae.apply import LocalFileSink, StaticSinks, apply_changes, plan_changes
from horae.apply.testing import FixtureSink
from horae.cli import app
from horae.config import STORE_FILE
from horae.runner import PipelineLock
from horae.store import Store

runner = CliRunner()
INBOX_BYTES = b"# Inbox\r\nkeep\r\n"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / INBOX).write_bytes(INBOX_BYTES)
    return root


@pytest.fixture
def applied(instance_dir: Path, vault: Path) -> Path:
    """A run of the synthetic pipeline applied to `vault` (notes) and fixtures (the rest)."""
    run = record(dry_run=False)
    other = FixtureSink()
    sinks = StaticSinks(
        sinks={"vault.tasks": other, "hooks.post": other, "ntfy.push": other},
        default=LocalFileSink(vault),
    )
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.begin_run(run.id, run.pipeline, run.started_at)

        async def go() -> Any:
            planned = await plan_changes(run, sinks)
            return await apply_changes(run, planned, sinks, store, clock=lambda: START)

        result = asyncio.run(go())
        assert result.failure is None
        store.save_run(result.record)
    assert (vault / DAILY).exists()
    return instance_dir


def _undo(*args: str) -> Any:
    return runner.invoke(app, ["undo", *args])


def test_undo_restores_the_vault_and_lists_what_cannot_be_undone(
    applied: Path, vault: Path
) -> None:
    result = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault))
    assert result.exit_code == cli_undo.EXIT_OK, result.output
    assert (vault / INBOX).read_bytes() == INBOX_BYTES
    assert not (vault / DAILY).exists()
    assert (vault / "Daily").is_dir()  # created directories are left in place
    out = result.stdout
    assert f"{INBOX}: restored" in out
    assert f"{DAILY}: removed (the run created it)" in out
    assert "Not undoable (left as they are):\n" in out
    assert "notify.push ntfy.push via ntfy.push" in out
    assert "The run is now marked undone." in out
    again = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault), "--json")
    assert again.exit_code == cli_undo.EXIT_OK
    data = json.loads(again.stdout)
    assert data["run"]["status"] == "undone"
    assert data["complete"] is True
    assert {note["action"] for note in data["notes"]} == {"already_undone"}


def test_undo_refuses_a_changed_note_unless_forced(applied: Path, vault: Path) -> None:
    (vault / INBOX).write_bytes(b"# Inbox\nrewritten by the user\n")
    result = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault), "--json")
    assert result.exit_code == cli_undo.EXIT_CHANGED
    data = json.loads(result.stdout)
    assert data["refused"]["kind"] == "changed"
    assert (vault / DAILY).exists()  # nothing written
    forced = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault), "--force")
    assert forced.exit_code == cli_undo.EXIT_OK, forced.output
    assert (vault / INBOX).read_bytes() == INBOX_BYTES


def test_undo_errors(applied: Path, vault: Path, tmp_path: Path, instance_dir: Path) -> None:
    unknown = _undo("nope", "-i", str(applied), "--json")
    assert unknown.exit_code == cli_undo.EXIT_UNKNOWN_RUN
    assert json.loads(unknown.stdout)["error"]["kind"] == "unknown_run"
    empty = tmp_path / "empty"
    (empty / "pipelines").mkdir(parents=True)
    (empty / "horae.yaml").write_text("timezone: Europe/Paris\n")
    assert _undo(RUN_ID, "-i", str(empty)).exit_code == cli_undo.EXIT_UNKNOWN_RUN
    (empty / "horae.yaml").write_text("timezone: 12\n")
    bad = _undo(RUN_ID, "-i", str(empty))
    assert bad.exit_code == cli_undo.EXIT_PROBLEMS
    assert "horae undo:" in bad.stderr
    no_vault = _undo(RUN_ID, "-i", str(applied))
    assert no_vault.exit_code == cli_undo.EXIT_PROBLEMS
    assert "vault_unset" in no_vault.stdout
    lock = PipelineLock.try_acquire(applied / "state", "dry-brief")
    assert lock is not None
    with lock:
        locked = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault))
    assert locked.exit_code == cli_undo.EXIT_PROBLEMS
    assert "is running" in locked.stderr
    assert _undo().exit_code == 2


def test_undo_of_a_dry_run_is_refused(instance_dir: Path) -> None:
    with Store.open(instance_dir / "state" / STORE_FILE) as store:
        store.save_run(record())
    result = _undo(RUN_ID, "-i", str(instance_dir))
    assert result.exit_code == cli_undo.EXIT_UNKNOWN_RUN
    assert "dry run" in result.stdout


def test_undo_store_and_internal_errors(
    applied: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = applied / "state" / STORE_FILE
    os.chmod(db, 0o000)
    try:
        if os.access(db, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("permissions are not enforced")
        result = _undo(RUN_ID, "-i", str(applied), "--json")
        assert result.exit_code == cli_undo.EXIT_PROBLEMS
        assert json.loads(result.stdout)["error"]["kind"] == "store_error"
    finally:
        os.chmod(db, 0o600)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("s3cr3t")

    monkeypatch.setattr(cli_undo, "undo_run", boom)
    result = _undo(RUN_ID, "-i", str(applied), "--vault", str(vault))
    assert result.exit_code == cli_undo.EXIT_INTERNAL
    assert "internal error (RuntimeError)" in result.stderr
    assert "s3cr3t" not in result.output
