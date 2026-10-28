# SPDX-License-Identifier: Apache-2.0
"""`horae run` (#26) with in-memory ports: outcomes, exit codes, `--json`, signals.

`horae.cli_run.live_ports` is replaced by fixture ports (no plugin process); the real
plugins are exercised by `tests/test_cli_e2e.py`."""

import json
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import horae.runner._runner as runner_module
from apply_fixtures import INBOX
from apply_fixtures import PIPELINE as DRY_BRIEF
from apply_fixtures import plugins as brief_plugins
from horae import cli_run
from horae.apply import StaticSinks
from horae.apply.testing import FixtureSink
from horae.cli import (
    EXIT_DIFFERENT,
    EXIT_INTERNAL,
    EXIT_LOCKED,
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_SIGINT,
    EXIT_SIGTERM,
    EXIT_USAGE,
    app,
)
from horae.config import Instance
from horae.engine import ToolResult
from horae.engine.testing import FixturePlugins
from horae.runner import LocksUnsupported, PipelineLock, Ports, try_acquire_apply_lock
from horae.store import Store
from runner_fixtures import Plugins, write_instance

runner = CliRunner()
APPEND = {"type": "note.append_section", "path": INBOX, "heading": "B", "content": "x"}


class _Fake:
    """The ports `horae run` gets instead of the plugin host's."""

    def __init__(self) -> None:
        self.plugins: FixturePlugins = Plugins()
        self.sinks: Any = None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    holder = _Fake()

    @asynccontextmanager
    async def live_ports(instance: Instance) -> AsyncIterator[Any]:
        ports = Ports(plugins=holder.plugins, tools=holder.plugins, sinks=holder.sinks)
        yield SimpleNamespace(ports=ports, sinks=holder.sinks or StaticSinks())

    monkeypatch.setattr(cli_run, "live_ports", live_ports)
    return holder


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance")
    definition = DRY_BRIEF.model_dump(mode="json", exclude_none=True)
    (root / "pipelines" / "dry-brief.yaml").write_text(yaml.safe_dump(definition), "utf-8")
    return root


def _run(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["run", *args])
    return result.exit_code, result.output


def _json(*args: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["run", *args, "--json"])
    return result.exit_code, json.loads(result.stdout)


def _runs(root: Path) -> list[dict[str, Any]]:
    result = runner.invoke(app, ["runs", "-i", str(root), "--json"])
    runs: list[dict[str, Any]] = json.loads(result.stdout)["runs"]
    return runs


def test_a_run_succeeds(fake: _Fake, root: Path) -> None:
    code, report = _json("collect", "-i", str(root))
    assert code == EXIT_OK
    assert sorted(report) == ["error", "plan", "run", "schema_version"]
    assert report["schema_version"] == 1
    assert report["error"] is None
    assert report["plan"] is None
    run = report["run"]
    assert run["pipeline"] == "collect"
    assert run["status"] == "ok"
    assert run["dry_run"] is False
    assert run["changes"] == []
    assert sorted(run) == [
        "changes",
        "cost",
        "dry_run",
        "error",
        "error_kinds",
        "finished_at",
        "id",
        "intents",
        "pipeline",
        "started_at",
        "status",
        "violations",
    ]
    assert [r["id"] for r in _runs(root)] == [run["id"]]
    code, out = _run("collect", "-i", str(root))
    assert code == EXIT_OK
    assert out.startswith("run ")
    assert out.splitlines()[0].endswith(": ok")


def test_a_run_applies_through_the_sinks(fake: _Fake, root: Path) -> None:
    fake.plugins = brief_plugins([APPEND])
    sink = FixtureSink(files={INBOX: b"# Inbox\n"})
    fake.sinks = StaticSinks(default=sink)
    code, out = _run("dry-brief", "-i", str(root))
    assert code == EXIT_OK, out
    assert "  changed Inbox.assistant.md" in out.splitlines()
    assert sink.files[INBOX].startswith(b"# Inbox\n")
    assert b"## B" in sink.files[INBOX]


def test_a_dry_run_shows_the_plan(fake: _Fake, root: Path) -> None:
    fake.plugins = brief_plugins()
    fake.sinks = StaticSinks()  # no sink: every intent is a problem
    code, report = _json("dry-brief", "-i", str(root), "--dry-run")
    assert code == EXIT_DIFFERENT == 4
    assert report["run"]["dry_run"] is True
    assert report["plan"]["schema_version"] == 1
    assert report["plan"]["problems"]
    assert report["error"]["kind"] == "planned_with_problems"
    code, out = _run("dry-brief", "-i", str(root), "--dry-run")
    assert code == EXIT_DIFFERENT
    assert out.startswith("dry run ")
    assert "Run " in out  # the change set, rendered as `horae diff` renders it


def test_a_dry_run_without_problems_exits_0(fake: _Fake, root: Path) -> None:
    code, report = _json("collect", "-i", str(root), "--dry-run")
    assert code == EXIT_OK
    assert report["plan"]["problems"] == []


def test_vars_are_template_variables(fake: _Fake, root: Path) -> None:
    code, report = _json("collect", "-i", str(root), "--var", "who=me", "--var", "x=a=b")
    assert code == EXIT_OK
    with Store.open(root / "state" / "horae.db") as store:
        record = store.get_run(report["run"]["id"])
    assert record is not None
    assert record.context.vars == {"date": record.context.vars["date"], "who": "me", "x": "a=b"}


@pytest.mark.parametrize("value", ["novalue", "=x", "1x=y", "date=today", "k=" + "v" * 4097])
def test_a_malformed_var_is_a_usage_error(fake: _Fake, root: Path, value: str) -> None:
    code, report = _json("collect", "-i", str(root), "--var", value)
    assert code == EXIT_USAGE == 2
    assert report["error"]["kind"] == "usage"
    assert _runs(root) == []


def test_unknown_pipeline_and_invalid_instance(fake: _Fake, root: Path, tmp_path: Path) -> None:
    code, report = _json("nope", "-i", str(root))
    assert code == EXIT_REFUSED == 3
    assert report == {
        "schema_version": 1,
        "error": {"kind": "unknown_pipeline", "message": report["error"]["message"]},
    }
    code, report = _json("collect", "-i", str(tmp_path / "missing"))
    assert code == EXIT_PROBLEMS
    assert report["error"]["kind"] == "config_invalid"


def test_an_llm_step_fails_with_llm_unavailable(fake: _Fake, root: Path) -> None:
    code, report = _json("brief", "-i", str(root))
    assert code == EXIT_PROBLEMS == 1
    assert report["run"]["status"] == "failed"
    assert report["error"]["kind"] == "run_failed"
    assert "(llm_unavailable)" in report["error"]["message"]
    assert report["run"]["error_kinds"] == ["llm_unavailable"]
    code, out = _run("brief", "-i", str(root))
    assert code == EXIT_PROBLEMS
    assert "horae run: " in out
    assert "llm_unavailable" in out


def test_a_running_pipeline_is_locked(fake: _Fake, root: Path) -> None:
    lock = PipelineLock.try_acquire(root / "state", "collect")
    assert lock is not None
    with lock:
        code, report = _json("collect", "-i", str(root))
    assert code == EXIT_LOCKED == 5
    assert report["error"]["kind"] == "pipeline_locked"
    assert _runs(root) == []


def test_a_busy_apply_lock_fails_the_run_locked(
    fake: _Fake, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "APPLY_LOCK_WAIT_S", 0.05)
    fake.plugins = brief_plugins([APPEND])
    fake.sinks = StaticSinks(default=FixtureSink(files={INBOX: b"# Inbox\n"}))
    held = try_acquire_apply_lock(root / "state")
    assert held is not None
    with held:
        code, report = _json("dry-brief", "-i", str(root))
    assert code == EXIT_LOCKED
    assert report["error"]["kind"] == "apply_locked"
    assert report["run"]["status"] == "failed"


def test_a_timeout_saves_the_partial_record(fake: _Fake, root: Path, tmp_path: Path) -> None:
    (root / "horae.yaml").write_text(
        yaml.safe_dump(
            {
                "timezone": "Europe/Paris",
                "models": {"default": {"backend": "claude-cli", "model": "sonnet"}},
                "runs": {"timeout_s": 1},
            }
        ),
        "utf-8",
    )
    code, report = _json("slow", "-i", str(root))
    assert code == EXIT_PROBLEMS
    assert report["error"]["kind"] == "timeout"
    assert report["run"]["status"] == "failed"
    assert _runs(root)[0]["status"] == "failed"


class _Signalling(Plugins):
    """`fixture.slow` sends this process `signum` once it has started, then waits."""

    def __init__(self, signum: int) -> None:
        super().__init__()
        self.signum = signum

    async def call(
        self, ref: str, params: dict[str, Any], input: Any, *, timeout_s: float | None = None
    ) -> ToolResult:
        if ref == "fixture.slow":
            os.kill(os.getpid(), self.signum)
        return await super().call(ref, params, input, timeout_s=timeout_s)


@pytest.mark.parametrize(
    ("signum", "code"), [(signal.SIGTERM, EXIT_SIGTERM), (signal.SIGINT, EXIT_SIGINT)]
)
def test_a_signal_cancels_the_run_and_saves_it(
    fake: _Fake, root: Path, signum: int, code: int
) -> None:
    fake.plugins = _Signalling(signum)
    exit_code, report = _json("slow", "-i", str(root))
    assert exit_code == code == 128 + signum
    assert report["error"]["kind"] == "cancelled"
    assert signal.Signals(signum).name in report["error"]["message"]
    assert report["run"]["status"] == "failed"
    assert "cancelled" in (report["run"]["error"] or "")
    (saved,) = _runs(root)
    assert saved["status"] == "failed"
    assert saved["id"] == report["run"]["id"]
    # The handlers are removed: the next run is not affected.
    fake.plugins = Plugins()
    assert _run("collect", "-i", str(root))[0] == EXIT_OK


def test_a_crash_is_an_internal_error(
    fake: _Fake, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(*args: Any) -> Any:
        raise RuntimeError("sentinel-secret-value")

    monkeypatch.setattr(cli_run, "_execute", crash)
    result = runner.invoke(app, ["run", "collect", "-i", str(root), "--json"])
    assert result.exit_code == EXIT_INTERNAL == 70
    assert json.loads(result.stdout)["error"]["kind"] == "internal"
    assert "sentinel-secret-value" not in result.output
    assert "RuntimeError" in result.output


def test_store_and_lost_runs_are_problems(
    fake: _Fake, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lost(*args: Any) -> Any:
        raise runner_module.RunLost("r1")

    monkeypatch.setattr(cli_run, "_execute", lost)
    code, report = _json("collect", "-i", str(root))
    assert code == EXIT_PROBLEMS
    assert report["error"]["kind"] == "run_lost"
    assert report["run"] is None

    async def unsupported(*args: Any) -> Any:
        raise LocksUnsupported("no fcntl")

    monkeypatch.setattr(cli_run, "_execute", unsupported)
    assert _json("collect", "-i", str(root))[1]["error"]["kind"] == "locks_unsupported"

    async def broken(*args: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli_run, "_execute", broken)
    code, report = _json("collect", "-i", str(root))
    assert code == EXIT_PROBLEMS
    assert report["error"]["kind"] == "store_error"


def test_missing_plugins_are_reported_before_anything_starts(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    for manifest in (root / "plugins").rglob("plugin.yaml"):
        manifest.unlink()
    code, report = _json("daily-fixture", "-i", str(root))
    assert code == EXIT_PROBLEMS
    assert report["error"]["kind"] == "plugins_invalid"
    assert "horae init" in report["error"]["message"]
    assert not (root / "state" / "horae.db").exists()
