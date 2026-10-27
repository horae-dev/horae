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
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GRAFT_", "PRE_COMMIT"))
        and k not in {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}
    }
    return {
        **base,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **extra,
    }


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


@pytest.mark.parametrize("content", ["", "\n\n", "# Please enter a message\n# more\n"])
def test_noop_when_message_has_no_content(msg: Path, content: str) -> None:
    msg.write_text(content)
    _hook(msg, FULL_ENV)
    assert msg.read_text() == content


def test_unexpected_error_fails_open(tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.write_bytes(b"\xff\xfe not utf-8\n")
    _hook(bad, FULL_ENV)  # check=True: must exit 0


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, *args],
        cwd=repo,
        env=_env(env or {}),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    hook = path / ".git" / "hooks" / "commit-msg"
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT}" "$1"\n')
    hook.chmod(0o755)
    return path


def _task(repo: Path, rev: str = "HEAD") -> str:
    fmt = "--format=%(trailers:key=Graft-Task,valueonly)"
    return _git(repo, "log", "-1", fmt, rev).strip()


def test_e2e_commit_with_message_flag(repo: Path) -> None:
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "Add a", env=FULL_ENV)
    assert _task(repo) == "#15"


def test_e2e_commit_without_vars_has_no_trailers(repo: Path) -> None:
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "Add a")
    assert _task(repo) == ""


def test_e2e_editor_flow(repo: Path, tmp_path: Path) -> None:
    editor = tmp_path / "editor.sh"
    editor.write_text(
        '#!/bin/sh\nprintf "Edited subject\\n" | cat - "$1" > "$1.new"\nmv "$1.new" "$1"\n'
    )
    editor.chmod(0o755)
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", env={**FULL_ENV, "GIT_EDITOR": str(editor)})
    assert _git(repo, "log", "-1", "--format=%s").strip() == "Edited subject"
    assert _task(repo) == "#15"


def test_e2e_aborted_editor_commit_stays_aborted(repo: Path, tmp_path: Path) -> None:
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    result = subprocess.run(  # noqa: S603
        [GIT, "commit", "-q"],
        cwd=repo,
        env=_env({**FULL_ENV, "GIT_EDITOR": "true"}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert (
        subprocess.run(  # noqa: S603
            [GIT, "rev-parse", "-q", "--verify", "HEAD"], cwd=repo, env=_env({}), check=False
        ).returncode
        != 0
    )


def test_e2e_merge_commit_gets_no_trailers(repo: Path) -> None:
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "c.txt").write_text("c")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side", env=FULL_ENV)
    assert len(_git(repo, "log", "-1", "--format=%P").split()) == 2
    assert _task(repo) == ""


def test_no_args_is_noop() -> None:
    subprocess.run([sys.executable, str(SCRIPT)], env=_env(FULL_ENV), check=True)  # noqa: S603


def test_missing_file_is_noop(tmp_path: Path) -> None:
    _hook(tmp_path / "nope", FULL_ENV)
