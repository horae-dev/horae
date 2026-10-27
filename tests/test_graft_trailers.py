# SPDX-License-Identifier: Apache-2.0
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

GIT = shutil.which("git") or "git"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "graft_trailers.py"
FULL_ENV = {"GRAFT_AGENT": "claude-code", "GRAFT_MODEL": "claude-sonnet-5-5", "GRAFT_TASK": "15"}
GRAFT = [
    "Graft-Agent: claude-code",
    "Graft-Model: claude-sonnet-5-5",
    "Graft-Task: #15",
]


def _env(extra: dict[str, str]) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("GRAFT_", "PRE_COMMIT"))}
    return {**base, **extra}


def _hook(msg: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), str(msg), *args], env=_env(env), check=True
    )


def _trailers(msg: Path) -> list[str]:
    out = subprocess.run(  # noqa: S603
        [GIT, "interpret-trailers", "--parse", str(msg)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


@pytest.fixture
def msg(tmp_path: Path) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text("Do a thing\n\nBody text.\n")
    return path


def test_adds_trailers_with_normalised_task(msg: Path) -> None:
    _hook(msg, FULL_ENV, "message")
    assert _trailers(msg) == GRAFT


@pytest.mark.parametrize("task", ["15", "#15", " 15 "])
def test_task_is_normalised(msg: Path, task: str) -> None:
    _hook(msg, {**FULL_ENV, "GRAFT_TASK": task})
    assert _trailers(msg)[-1] == "Graft-Task: #15"


def test_joins_existing_trailer_block(msg: Path) -> None:
    co = "Co-Authored-By: Claude <noreply@anthropic.com>"
    msg.write_text(f"Do a thing\n\nBody.\n\n{co}\n")
    _hook(msg, FULL_ENV)
    assert _trailers(msg) == [co, *GRAFT]
    assert "\n\nGraft-Agent" not in msg.read_text()


def test_idempotent(msg: Path) -> None:
    _hook(msg, FULL_ENV)
    first = msg.read_text()
    _hook(msg, FULL_ENV)
    assert msg.read_text() == first


@pytest.mark.parametrize("missing", ["GRAFT_AGENT", "GRAFT_MODEL", "GRAFT_TASK"])
def test_noop_when_a_variable_is_missing(msg: Path, missing: str) -> None:
    before = msg.read_text()
    _hook(msg, {k: v for k, v in FULL_ENV.items() if k != missing})
    assert msg.read_text() == before


def test_noop_when_task_is_not_a_number(msg: Path) -> None:
    before = msg.read_text()
    _hook(msg, {**FULL_ENV, "GRAFT_TASK": "abc"})
    assert msg.read_text() == before


@pytest.mark.parametrize("source", ["merge", "squash"])
def test_skips_merge_and_squash(msg: Path, source: str) -> None:
    before = msg.read_text()
    _hook(msg, FULL_ENV, source)
    assert msg.read_text() == before


def test_source_from_pre_commit_env(msg: Path) -> None:
    before = msg.read_text()
    _hook(msg, {**FULL_ENV, "PRE_COMMIT_COMMIT_MSG_SOURCE": "merge"})
    assert msg.read_text() == before


def test_no_args_is_noop() -> None:
    subprocess.run([sys.executable, str(SCRIPT)], env=_env(FULL_ENV), check=True)  # noqa: S603


def test_missing_file_is_noop(tmp_path: Path) -> None:
    _hook(tmp_path / "nope", FULL_ENV)
