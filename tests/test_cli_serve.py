# SPDX-License-Identifier: Apache-2.0
"""`horae serve` (#39): the scheduler's process, `HORAE_SCHEDULER`, and a graceful stop on
SIGTERM / SIGINT. The signal is sent from inside the event loop once the handlers are
installed (`serve_until_signal(on_ready=)`), never after a fixed delay."""

import asyncio
import json
import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_serve
from horae.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_SIGINT, EXIT_SIGTERM, app
from horae.cli_serve import build_scheduler, serve_until_signal
from horae.config import load_instance
from horae.scheduler import PingResult, RunOutcome, Schedule, Scheduler
from runner_fixtures import write_instance
from test_scheduler import FakeClock, settle

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance")
    path = root / "pipelines" / "collect.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["trigger"] = {"cron": "30 5 * * *"}
    path.write_text(yaml.safe_dump(data), "utf-8")
    return root


def _signal_when_ready(monkeypatch: pytest.MonkeyPatch, *signums: int) -> list[bool]:
    """Make `horae serve` send itself `signums` once its handlers are installed; the list
    returned says whether the scheduler was active when they were sent."""
    real = cli_serve.serve_until_signal
    seen: list[bool] = []

    async def ready_then_signal(
        scheduler: Scheduler, on_ready: Callable[[], object] | None = None
    ) -> int:
        loop = asyncio.get_running_loop()

        def send() -> None:
            seen.append(scheduler.state().active)
            for signum in signums:
                os.kill(os.getpid(), signum)

        # `call_soon`: once `run_forever` has started (it runs before this callback).
        return await real(scheduler, on_ready=lambda: loop.call_soon(send))

    monkeypatch.setattr(cli_serve, "serve_until_signal", ready_then_signal)
    return seen


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_serve_runs_the_builtin_scheduler_until_a_signal(
    root: Path, monkeypatch: pytest.MonkeyPatch, signum: int
) -> None:
    seen = _signal_when_ready(monkeypatch, signum)
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": ""})
    assert result.exit_code == EXIT_OK, result.output
    assert seen == [True]
    assert "built-in scheduler, 1 pipeline(s) with a trigger, at most 1 run(s)" in result.stderr
    assert "collect: 30 5 * * * (Europe/Paris)" in result.stderr
    assert "horae serve: stopped\n" in result.stderr


T0 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("signum", "code"), [(signal.SIGTERM, EXIT_SIGTERM), (signal.SIGINT, EXIT_SIGINT)]
)
def test_a_second_signal_stops_waiting_for_the_pings(signum: int, code: int) -> None:
    async def body() -> tuple[int, Any]:
        clock = FakeClock(T0)
        stopping, pinging, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(name: str) -> RunOutcome:
            try:
                await never.wait()
            except asyncio.CancelledError:
                stopping.set()
            return RunOutcome("ok", "run-1")  # its record was saved

        async def stuck_ping(name: str, outcome: RunOutcome, duration_s: float) -> PingResult:
            pinging.set()
            await never.wait()
            return "sent"

        scheduler = Scheduler(
            [Schedule("brief", "1 5 * * *", "UTC")],
            mode="builtin",
            run=run,
            ping=stuck_ping,
            clock=clock,
        )
        task = asyncio.create_task(serve_until_signal(scheduler))
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        os.kill(os.getpid(), signum)
        await stopping.wait()
        await pinging.wait()
        assert not task.done()
        os.kill(os.getpid(), signum)
        return await task, scheduler.state().pipelines[0]

    exit_code, entry = asyncio.run(body())
    assert exit_code == code
    assert (entry.last_outcome, entry.last_ping, entry.running) == ("ok", "skipped", False)


def test_serve_reports_a_forced_stop(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _signal_when_ready(monkeypatch, signal.SIGTERM, signal.SIGTERM)
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": ""})
    assert result.exit_code == EXIT_SIGTERM, result.output
    assert "horae serve: stopped (forced)" in result.stderr


def test_horae_scheduler_external_disables_the_internal_cron(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = load_instance(root)
    assert build_scheduler(instance, {}).mode == "builtin"
    external = build_scheduler(instance, {"HORAE_SCHEDULER": "External"})
    assert external.mode == "external"
    seen = _signal_when_ready(monkeypatch, signal.SIGTERM)
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": "external"})
    assert seen == [True]
    assert result.exit_code == EXIT_OK, result.output
    assert "external scheduler, no trigger fires" in result.stderr


def test_an_invalid_horae_scheduler_is_a_config_problem(root: Path) -> None:
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": "cron"})
    assert result.exit_code == EXIT_PROBLEMS
    assert "HORAE_SCHEDULER='cron'" in result.stderr


def test_an_invalid_instance_is_a_config_problem(tmp_path: Path) -> None:
    (tmp_path / "horae.yaml").write_text("scheduler: {mode: cron}\n", "utf-8")
    result = runner.invoke(app, ["serve", "-i", str(tmp_path)])
    assert result.exit_code == EXIT_PROBLEMS
    assert "scheduler.mode: Input should be 'builtin' or 'external'" in result.stderr


def test_the_example_instance_config_declares_monitors() -> None:
    example = Path(__file__).parent.parent / "schemas/examples/instance/valid/horae.yaml"
    data = yaml.safe_load(example.read_text("utf-8"))
    assert json.dumps(data["scheduler"]["monitors"]["daily-brief"]) == (
        '{"secret": "DAILY_BRIEF_PUSH_URL"}'
    )
