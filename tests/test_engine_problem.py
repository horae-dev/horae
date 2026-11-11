# SPDX-License-Identifier: Apache-2.0
"""`engine_problem` (#194): the check `horae doctor` makes of the local Docker engine, with a
fake Docker CLI (a shell script): nothing is started, no engine needed."""

import stat
from pathlib import Path

import pytest

from horae.contracts.instance import ContainerConfig
from horae.plugins.host import container
from horae.plugins.host.container import engine_problem


def _cli(tmp_path: Path, context: str, version: tuple[int, str]) -> ContainerConfig:
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$1" in context) echo "{context}";; '
        f'version) echo "{version[1]}" >&{1 if version[0] == 0 else 2}; exit {version[0]};; '
        "*) exit 9;; esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return ContainerConfig(docker=str(script))


def test_a_local_engine_that_answers_is_usable(tmp_path: Path) -> None:
    config = _cli(tmp_path, "unix:///var/run/docker.sock", (0, "27.0.0"))
    assert engine_problem(config, {}) is None


def test_a_missing_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "BASE_PATH", str(tmp_path))
    assert engine_problem(ContainerConfig(), {}) == "the Docker CLI is not installed"
    config = ContainerConfig(docker=str(tmp_path / "absent"))
    assert engine_problem(config, {}) == "the Docker CLI is not installed"


def test_a_remote_docker_host_is_refused_before_anything_runs(tmp_path: Path) -> None:
    config = _cli(tmp_path, "unix:///var/run/docker.sock", (0, "27.0.0"))
    problem = engine_problem(config, {"DOCKER_HOST": "tcp://192.0.2.1:2375"})
    assert problem == "DOCKER_HOST is not a local unix socket (tcp://)"


def test_a_remote_context_is_refused(tmp_path: Path) -> None:
    config = _cli(tmp_path, "ssh://someone@docker.example.test", (0, "27.0.0"))
    assert engine_problem(config, {}) == ("the Docker engine is not a local unix socket (ssh://)")


def test_a_daemon_that_does_not_answer(tmp_path: Path) -> None:
    config = _cli(tmp_path, "unix:///var/run/docker.sock", (1, "Cannot connect to the daemon"))
    problem = engine_problem(config, {})
    assert problem == "the Docker engine is unreachable: Cannot connect to the daemon"


def test_a_cli_that_cannot_inspect_its_context(tmp_path: Path) -> None:
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\necho 'no such context' >&2\nexit 1\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    problem = engine_problem(ContainerConfig(docker=str(script)), {})
    assert problem == "the Docker context cannot be inspected: no such context"


def test_a_cli_that_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "docker"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(container, "ENGINE_TIMEOUT_S", 0.2)
    problem = engine_problem(ContainerConfig(docker=str(script)), {})
    assert problem is not None
    assert "did not answer in time" in problem
