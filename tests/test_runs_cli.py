# SPDX-License-Identifier: Apache-2.0
"""`horae runs`: lists runs with status, duration and cost (#19)."""

import asyncio
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horae import cli
from horae.cli import app
from horae.config import STORE_FILE, load_instance
from horae.contracts import RunRecord, Usage
from horae.runner import PipelineLock, Ports, Runner, RunTimedOut
from horae.store import SCHEMA_VERSION, Store
from runner_fixtures import Plugins, write_instance

runner = CliRunner()
T0 = datetime(2020, 1, 1, 5, 30, tzinfo=UTC)


def _plain(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _runs(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["runs", *args])
    return result.exit_code, _plain(result.output)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An instance with four runs: ok, failed (timeout), abandoned and still running."""
    root = write_instance(tmp_path / "instance")
    inst = load_instance(root)
    plugins = Plugins()
    with Store.open(inst.store_path) as store:
        ports = Ports(plugins, plugins)
        ok = asyncio.run(Runner(inst, store, ports).run("collect"))
        # Give the ok run a known duration and cost.
        store.save_run(
            ok.model_copy(
                update={
                    "finished_at": ok.started_at + timedelta(seconds=75),
                    "cost": Usage(tokens_in=1200, tokens_out=300, usd=0.0123),
                }
            )
        )
        with pytest.raises(RunTimedOut):
            asyncio.run(Runner(inst, store, ports, timeout_s=0.05).run("slow"))
        store.begin_run("20200101T000000000000Z-partial-aaaaaa", "partial", T0, pid=1, host="x")
        store.abandon_run(
            "20200101T000000000000Z-partial-aaaaaa", at=T0 + timedelta(hours=2), error="gone"
        )
        store.begin_run("29990101T000000000000Z-brief-bbbbbb", "brief", datetime.now(UTC))
    return root


def test_runs_lists_status_duration_and_cost(root: Path) -> None:
    code, out = _runs("-i", str(root))
    assert code == cli.EXIT_OK == 0
    header, *rows = out.strip().splitlines()
    assert header.split() == [
        "RUN",
        "PIPELINE",
        "STATUS",
        "STARTED",
        "DURATION",
        "COST",
        "TOKENS",
        "IN/OUT",
        "ERROR",
    ]
    assert len(rows) == 4
    running, slow, ok, abandoned = (row.split() for row in rows)
    # Its process is gone (the pipeline lock is free): flagged, read-only.
    assert running[1:4] == ["brief", "running", "(stale?)"]
    assert running[5] == "-"
    assert slow[1:3] == ["slow", "failed"]
    assert " ".join(slow[7:]) == "step 0 (collect): the run timed out after 0.05 s"
    assert ok[1:3] == ["collect", "ok"]
    assert ok[4:] == ["1m15s", "$0.0123", "1200/300"]
    assert abandoned[1:4] == ["partial", "failed", "(abandoned)"]
    assert abandoned[5] == "2h00m"
    assert abandoned[8:] == ["gone"]


def test_a_running_run_holding_its_lock_is_not_stale(root: Path) -> None:
    held = PipelineLock.try_acquire(root / "state", "brief")
    assert held is not None
    with held:
        code, out = _runs("-i", str(root), "--status", "running", "--json")
        assert code == 0
        (running,) = json.loads(out)["runs"]
        assert running["stale"] is False
        _code, out = _runs("-i", str(root), "--status", "running")
        assert "stale" not in out
    code, out = _runs("-i", str(root), "--status", "running", "--json")
    assert json.loads(out)["runs"][0]["stale"] is True


def test_the_error_column_is_one_safe_capped_line(tmp_path: Path) -> None:
    assert cli._reason(None) == ""
    assert cli._reason("a\nb\x1b[31m\u202ec") == "a b [31m c"
    long = cli._reason("x" * 500)
    assert len(long) == cli.MAX_ERROR_COLUMN
    assert long.endswith("…")


def test_runs_filters_and_limit(root: Path) -> None:
    code, out = _runs("-i", str(root), "--pipeline", "collect")
    assert code == 0
    assert len(out.strip().splitlines()) == 2
    code, out = _runs("-i", str(root), "--status", "failed", "--json")
    assert code == 0
    assert [r["pipeline"] for r in json.loads(out)["runs"]] == ["slow", "partial"]
    code, out = _runs("-i", str(root), "-n", "1", "--json")
    assert [r["status"] for r in json.loads(out)["runs"]] == ["running"]
    code, out = _runs("-i", str(root), "--pipeline", "nope")
    assert (code, out.strip()) == (0, "no runs")


def test_runs_json(root: Path) -> None:
    code, out = _runs("-i", str(root), "--json")
    assert code == 0
    report = json.loads(out)
    assert report["schema_version"] == cli.RUNS_SCHEMA_VERSION == 1
    assert report["instance"] == str(root.resolve())
    running, slow, ok, abandoned = report["runs"]
    assert running["status"] == "running"
    assert running["stale"] is True
    assert slow["stale"] is False
    assert running["finished_at"] is None
    assert running["duration_s"] is None
    assert slow["error"] == "step 0 (collect): the run timed out after 0.05 s"
    assert ok["duration_s"] == 75.0
    assert ok["cost"] == {"usd": 0.0123, "tokens_in": 1200, "tokens_out": 300}
    assert ok["dry_run"] is False
    assert ok["abandoned"] is False
    assert abandoned == {
        "id": "20200101T000000000000Z-partial-aaaaaa",
        "pipeline": "partial",
        "status": "failed",
        "stale": False,
        "dry_run": False,
        "abandoned": True,
        "started_at": "2020-01-01T05:30:00Z",
        "finished_at": "2020-01-01T07:30:00Z",
        "duration_s": 7200.0,
        "cost": {"usd": 0.0, "tokens_in": 0, "tokens_out": 0},
        "error": "gone",
    }


def test_runs_dry_run_and_short_durations(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    inst = load_instance(root)
    plugins = Plugins()
    with Store.open(inst.store_path) as store:
        record = asyncio.run(
            Runner(inst, store, Ports(plugins, plugins)).run("collect", dry_run=True)
        )
        assert isinstance(record, RunRecord)
    code, out = _runs("-i", str(root))
    assert code == 0
    row = out.strip().splitlines()[1].split()
    assert row[2:4] == ["ok", "(dry"]
    assert re.fullmatch(r"\d+\.\ds", row[6])


def test_runs_without_a_store_lists_nothing_and_creates_nothing(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    assert _runs("-i", str(root)) == (0, "no runs\n")
    _code, out = _runs("-i", str(root), "--json")
    assert json.loads(out) == {"schema_version": 1, "instance": str(root.resolve()), "runs": []}
    assert not (root / "state").exists()


def test_runs_instance_is_absolute_in_json(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root.parent)
    _code, out = _runs("-i", root.name, "--json")
    assert json.loads(out)["instance"] == str(root.resolve())


def _files(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "state").iterdir() if p.is_file())


def test_runs_opens_the_store_read_only(root: Path) -> None:
    db = root / "state" / STORE_FILE
    lock = STORE_FILE + ".init.lock"  # left by the writers' first open (Store.open)
    assert _files(root) == [STORE_FILE, lock]  # every writer closed: no -wal / -shm
    before = db.read_bytes()
    db.chmod(0o400)
    try:
        code, out = _runs("-i", str(root), "--json")
        assert code == 0, out
        assert len(json.loads(out)["runs"]) == 4
    finally:
        db.chmod(0o600)
    assert _files(root) == [STORE_FILE, lock]  # nothing created, no -wal / -shm
    assert db.read_bytes() == before


def test_runs_reads_while_a_writer_is_open(root: Path) -> None:
    with Store.open(root / "state" / STORE_FILE) as store:
        store.begin_run("29990101T000000000000Z-collect-cccccc", "collect", datetime.now(UTC))
        code, out = _runs("-i", str(root), "--json")
        assert code == 0, out
        assert len(json.loads(out)["runs"]) == 5


def _set_user_version(db: Path, version: int) -> None:
    conn = sqlite3.connect(db)
    # Literal statements only (PRAGMA values cannot be bound).
    statement = {0: "PRAGMA user_version = 0", 9999: "PRAGMA user_version = 9999"}[version]
    conn.execute(statement)
    conn.close()


def test_runs_never_migrates_an_older_store(root: Path) -> None:
    db = root / "state" / STORE_FILE
    _set_user_version(db, 0)
    result = runner.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == 1
    assert "run `horae run` once to migrate it" in _plain(result.output)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    finally:
        conn.close()
    assert SCHEMA_VERSION > 0


def test_runs_refuses_a_newer_store(root: Path) -> None:
    _set_user_version(root / "state" / STORE_FILE, 9999)
    result = runner.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == 1
    assert "newer than this Horae" in _plain(result.output)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_runs_unreadable_store_exits_1(root: Path) -> None:
    db = root / "state" / STORE_FILE
    db.chmod(0)
    try:
        result = runner.invoke(app, ["runs", "-i", str(root)])
    finally:
        db.chmod(0o600)
    assert result.exit_code == 1
    assert "horae runs:" in _plain(result.output)


def test_runs_os_error_exits_1(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Store, "open", denied)
    result = runner.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == 1
    assert "Permission denied" in _plain(result.output)


def test_runs_reads_the_instance_from_the_environment(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORAE_INSTANCE", str(root))
    code, out = _runs("--json")
    assert code == 0
    assert len(json.loads(out)["runs"]) == 4


def test_runs_invalid_config_exits_1(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    (root / "horae.yaml").write_text("runs: { timeout_s: 0 }\n", encoding="utf-8")
    code, out = _runs("-i", str(root))
    assert code == cli.EXIT_PROBLEMS == 1
    # One error line on stderr, like every command (decision 37).
    assert out.splitlines()[0].startswith(f"horae runs: {root / 'horae.yaml'}:1:")
    assert out.splitlines()[0].endswith("; 1 problem found")
    code, out = _runs("-i", str(tmp_path))
    assert (code, out.splitlines()) == (
        1,
        [f"horae runs: {tmp_path / 'horae.yaml'}: file not found; 1 problem found"],
    )
    code, out = _runs("-i", str(tmp_path), "--json")
    assert code == 1
    assert json.loads(out)["error"]["kind"] == "config_invalid"


def test_runs_store_problem_exits_1(tmp_path: Path) -> None:
    root = write_instance(tmp_path / "instance")
    (root / "state").mkdir()
    (root / "state" / STORE_FILE).write_bytes(b"not a database" * 100)
    result = runner.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == 1
    assert "horae runs:" in _plain(result.output)


def test_runs_usage_errors_exit_2(root: Path) -> None:
    assert _runs("--status", "bogus")[0] == cli.EXIT_USAGE == 2
    assert _runs("--limit", "-1")[0] == 2
    assert _runs("--limit", "1001")[0] == 2
    assert _runs("--no-such-option")[0] == 2


def test_runs_internal_error_exits_70(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("detail that must not be printed")

    monkeypatch.setattr(cli, "_list_runs", boom)
    result = runner.invoke(app, ["runs", "-i", str(root)])
    assert result.exit_code == cli.EXIT_INTERNAL == 70
    assert "RuntimeError" in result.output
    assert "detail that must not be printed" not in result.output


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.25, "0.2s"), (59.94, "59.9s"), (60, "1m00s"), (3599, "59m59s"), (3600, "1h00m")],
)
def test_duration_format(seconds: float, text: str) -> None:
    assert cli._duration(timedelta(seconds=seconds)) == text
