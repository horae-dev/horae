# SPDX-License-Identifier: Apache-2.0
"""`horae serve` (#39): the scheduler's process, `HORAE_SCHEDULER`, and a graceful stop on
SIGTERM / SIGINT (the signal is sent by a timer thread once the process waits)."""

import json
import os
import signal
import threading
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from horae.cli import EXIT_OK, EXIT_PROBLEMS, app
from horae.cli_serve import build_scheduler
from horae.config import load_instance
from runner_fixtures import write_instance

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = write_instance(tmp_path / "instance")
    path = root / "pipelines" / "collect.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    data["trigger"] = {"cron": "30 5 * * *"}
    path.write_text(yaml.safe_dump(data), "utf-8")
    return root


def _signal_soon(signum: int) -> threading.Timer:
    timer = threading.Timer(0.3, os.kill, (os.getpid(), signum))
    timer.start()
    return timer


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_serve_runs_the_builtin_scheduler_until_a_signal(root: Path, signum: int) -> None:
    timer = _signal_soon(signum)
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": ""})
    timer.join()
    assert result.exit_code == EXIT_OK, result.output
    assert "built-in scheduler, 1 pipeline(s) with a trigger" in result.stderr
    assert "collect: 30 5 * * * (Europe/Paris)" in result.stderr
    assert "horae serve: stopped" in result.stderr


def test_horae_scheduler_external_disables_the_internal_cron(root: Path) -> None:
    instance = load_instance(root)
    assert build_scheduler(instance, {}).mode == "builtin"
    external = build_scheduler(instance, {"HORAE_SCHEDULER": "external"})
    assert external.mode == "external"
    timer = _signal_soon(signal.SIGTERM)
    result = runner.invoke(app, ["serve", "-i", str(root)], env={"HORAE_SCHEDULER": "external"})
    timer.join()
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
