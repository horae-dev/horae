# SPDX-License-Identifier: Apache-2.0
"""`horae runs`: lists runs with status, duration and cost (#19)."""

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horae import cli
from horae.cli import app
from horae.config import STORE_FILE, load_instance
from horae.contracts import RunRecord, Usage
from horae.runner import Ports, Runner
from horae.store import Store
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
    ]
    assert len(rows) == 4
    running, slow, ok, abandoned = (row.split() for row in rows)
    assert running[1:3] == ["brief", "running"]
    assert running[4] == "-"
    assert slow[1:3] == ["slow", "failed"]
    assert ok[1:3] == ["collect", "ok"]
    assert ok[4:] == ["1m15s", "$0.0123", "1200/300"]
    assert abandoned[1:4] == ["partial", "failed", "(abandoned)"]
    assert abandoned[5] == "2h00m"


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
    assert report["instance"] == str(root)
    running, slow, ok, abandoned = report["runs"]
    assert running["status"] == "running"
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
    assert json.loads(out) == {"schema_version": 1, "instance": str(root), "runs": []}
    assert not (root / "state").exists()


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
    assert out.splitlines()[0].startswith(f"{root / 'horae.yaml'}:1:")
    assert out.splitlines()[-1] == "1 problem found"
    code, out = _runs("-i", str(tmp_path))
    assert (code, out.splitlines()) == (
        1,
        [f"{tmp_path / 'horae.yaml'}: file not found", "1 problem found"],
    )


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
