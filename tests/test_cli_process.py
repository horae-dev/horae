# SPDX-License-Identifier: Apache-2.0
"""The `horae` CLI as a real process (#26): the installed console script, and a signal sent
from outside to a running `horae run`."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from horae.config import STORE_FILE
from horae.store import Store
from runner_fixtures import write_instance

FIXTURES = Path(__file__).resolve().parent / "runner_fixtures.py"


def _console_script() -> str:
    """The `horae` script installed next to this interpreter (`uv sync`), else on `PATH`."""
    beside = Path(sys.executable).parent / "horae"
    found = str(beside) if beside.is_file() else shutil.which("horae")
    assert found is not None, "the `horae` console script is not installed"
    return found


def _horae(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (our own console script)
        [_console_script(), *args], capture_output=True, text=True, timeout=120, check=False
    )


def test_the_console_script_runs_help_init_and_doctor(tmp_path: Path) -> None:
    helped = _horae("--help")
    assert helped.returncode == 0, helped.stderr
    assert "Exit codes (every command)" in helped.stdout
    created = _horae("init", str(tmp_path / "instance"), "--json")
    assert created.returncode == 0, created.stderr
    root = json.loads(created.stdout)["instance"]
    checked = _horae("doctor", "-i", root, "--json")
    assert checked.returncode == 0, checked.stdout
    assert json.loads(checked.stdout)["ok"] is True
    unknown = _horae("nope", "--json")
    assert unknown.returncode == 2
    assert json.loads(unknown.stdout)["error"]["kind"] == "usage"
    as_module = subprocess.run(
        [sys.executable, "-m", "horae", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert as_module.returncode == 0
    assert as_module.stdout.startswith("horae ")


def _wait_for(path: Path, proc: subprocess.Popen[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"child exited early ({proc.returncode}): {out} {err}")
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            pytest.fail("child did not start in time")
        time.sleep(0.02)


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_from_outside_stops_horae_run_and_saves_its_record(
    tmp_path: Path, signum: int, code: int
) -> None:
    root = write_instance(tmp_path / "instance")
    ready, release = tmp_path / "ready", tmp_path / "release"
    args = ["--cli", ready, release, "run", "slow", "-i", root, "--json"]
    child = subprocess.Popen(  # noqa: S603 (our own interpreter and test script)
        [sys.executable, str(FIXTURES), *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(ready, child)
        os.kill(child.pid, signum)
        out, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:  # pragma: no cover - only when the test fails
            child.kill()
            child.communicate()
    assert child.returncode == code, (out, err)
    report = json.loads(out)
    assert report["error"]["kind"] == "cancelled"
    assert signal.Signals(signum).name in report["error"]["message"]
    run_id = report["run_id"]
    assert report["run"]["id"] == run_id
    with Store.open(root / "state" / STORE_FILE) as store:
        record = store.get_run(run_id)
    assert record is not None
    assert record.status == "failed"
    assert "cancelled" in (record.error or "")
