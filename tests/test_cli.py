# SPDX-License-Identifier: Apache-2.0
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horae import __version__
from horae.cli import app

runner = CliRunner()


def _plain(s: str) -> str:
    """Strip ANSI colour codes (typer forces rich output when GITHUB_ACTIONS is set)."""
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in _plain(result.output)
    assert "--version" in _plain(result.output)


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert _plain(result.output).strip() == f"horae {__version__}"


def test_no_args_shows_usage() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage" in _plain(result.output)


def test_python_dash_m_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "horae", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Usage" in _plain(result.stdout)


def test_console_script_help() -> None:
    exe = shutil.which("horae", path=str(Path(sys.executable).parent))
    if exe is None:
        pytest.skip("horae console script not installed")
    result = subprocess.run([exe, "--help"], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0
    assert "Usage" in _plain(result.stdout)


def test_main_module_runs_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["horae", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("horae", run_name="__main__")
    assert excinfo.value.code == 0
