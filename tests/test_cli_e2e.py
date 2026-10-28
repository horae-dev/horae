# SPDX-License-Identifier: Apache-2.0
"""End to end through the CLI (#26): `horae init` → `doctor` → `run --dry-run` → `diff` →
`run` → `runs` → `replay` → `undo`, with the real built-in plugins (stdio subprocesses).

The instance is exactly what `horae init` writes; the vault holds today's note, written by
its user, so undo is checked byte for byte. The dry run is under the write guard
(`write_guard.py`), which allows only Horae's own state and the two plugin processes."""

import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from horae.cli import app
from horae.plugins.builtin import BUILTIN
from horae.store import Store
from write_guard import arm, tree, under

runner = CliRunner()
ORIGINAL = b"# Today\r\n\r\nMy own words, kept byte for byte.\r\n"
BLOCK = (
    b"<!-- horae:begin fixture -->\r\n"
    b"- [ ] Water the plants (fx-1)\r\n"
    b"- [ ] Read chapter 3 (fx-2)\r\n"
    b"<!-- horae:end fixture -->\r\n"
)


def _cli(*args: str) -> tuple[int, Any]:
    result = runner.invoke(app, [*args, "--json"])
    return result.exit_code, json.loads(result.stdout)


def _plugin_process(args: Any) -> bool:
    argv = [os.fsdecode(arg) for arg in args] if isinstance(args, (list, tuple)) else []
    return argv[:2] == [sys.executable, "-m"] and argv[2:] in ([m] for m in BUILTIN.values())


def _contents(root: Path) -> dict[str, tuple[bytes | None, int]]:
    """Bytes and modes under `root` (mtimes change when undo rewrites a note)."""
    return {name: (data, mode) for name, (data, mode, _) in tree(root).items()}


def _today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def _same_day(day: str) -> None:
    """Skip when the date changed since `day`: the run wrote another day's note."""
    if _today() != day:  # pragma: no cover - the test ran across midnight UTC
        pytest.skip("the date changed during the test")


def test_init_doctor_run_runs_diff_replay_undo(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    code, created = _cli("init", str(root))
    assert code == 0, created
    root = Path(created["instance"])
    vault = Path(created["vault"])
    day = _today()
    note = f"Daily/{day}.assistant.md"
    (vault / "Daily").mkdir()
    (vault / note).write_bytes(ORIGINAL)
    original = _contents(vault)
    untouched = tree(vault)

    code, report = _cli("doctor", "-i", str(root))
    assert (code, report["ok"], report["problems"]) == (0, True, [])

    # --dry-run: the plan, and nothing written but Horae's own state.
    state = root / "state"
    Store.open(state / "horae.db").close()  # created (and migrated) before the guard
    db = os.path.realpath(state / "horae.db")
    state_files = {db + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    state_files.add(os.path.realpath(state))
    own = under(state / "locks", state / "plugin-runs")
    with pytest.MonkeyPatch.context() as patch:
        seen = arm(patch, lambda real: real in state_files or own(real), _plugin_process)
        code, dry = _cli("run", "daily-fixture", "-i", str(root), "--dry-run")
    assert seen == [], f"the dry run tried to write: {seen}"
    assert code == 0, dry
    _same_day(day)  # before any assertion on today's note
    assert dry["error"] is None
    assert dry["run"]["dry_run"] is True
    (planned,) = dry["plan"]["files"]
    assert (planned["path"], planned["status"]) == (note, "modify")
    assert "+- [ ] Water the plants (fx-1)\r\n" in planned["diff"]
    assert tree(vault) == untouched  # mtimes included

    # `horae diff` of the dry run, through the plugins: the same plan.
    code, diff = _cli("diff", dry["run"]["id"], "-i", str(root))
    assert code == 0, diff
    assert diff["files"] == dry["plan"]["files"]

    # The run writes the note through file-sink.
    code, run = _cli("run", "daily-fixture", "-i", str(root))
    assert code == 0, run
    _same_day(day)
    assert run["run"]["status"] == "ok"
    assert run["run"]["changes"] == [{"intent": 0, "target": note}]
    assert (vault / note).read_bytes() == ORIGINAL + b"\r\n" + BLOCK
    run_id = run["run"]["id"]

    code, listed = _cli("runs", "-i", str(root))
    assert code == 0
    assert [(r["id"], r["status"], r["dry_run"]) for r in listed["runs"]] == [
        (run_id, "ok", False),
        (dry["run"]["id"], "ok", True),
    ]

    # An applied run is refused by diff (exit 3); both runs replay identically.
    code, refused = _cli("diff", run_id, "-i", str(root))
    assert code == 3
    assert refused["refused"]["kind"] == "already_applied"
    for replayed in (run_id, dry["run"]["id"]):
        code, result = _cli("replay", replayed, "-i", str(root))
        assert (code, result["outcome"]) == (0, "identical"), result

    # --vault is the built-in local sink: another identity than file-sink's, refused.
    code, mismatch = _cli("undo", run_id, "-i", str(root), "--vault", str(vault))
    assert code == 1
    assert mismatch["refused"]["kind"] == "sink_mismatch"
    assert (vault / note).read_bytes() == ORIGINAL + b"\r\n" + BLOCK

    # Undo through the plugins restores the note byte for byte.
    code, undone = _cli("undo", run_id, "-i", str(root))
    assert code == 0, undone
    assert undone["complete"] is True
    (restored,) = undone["notes"]
    assert restored["sink_identity"].startswith("mcp:file-sink:local:")
    _same_day(day)
    assert _contents(vault) == original
    code, listed = _cli("runs", "-i", str(root), "--status", "undone")
    assert [r["id"] for r in listed["runs"]] == [run_id]
