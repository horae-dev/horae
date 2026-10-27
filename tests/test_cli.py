# SPDX-License-Identifier: Apache-2.0
from typer.testing import CliRunner

from horae import __version__
from horae.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output
    assert "--version" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"horae {__version__}"
