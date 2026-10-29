# SPDX-License-Identifier: Apache-2.0
"""The HTTP API served by `horae serve` (#37, schemas decision 51 A, E): a real socket on an
ephemeral loopback port, the stop together with the scheduler on SIGTERM (the two-step stop
of decision 49 E2), and a port that cannot be bound."""

import asyncio
import os
import signal
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from typer.testing import CliRunner

from horae import cli_serve
from horae.api import ApiServer, BindError, bind
from horae.cli import EXIT_INTERNAL, EXIT_OK, EXIT_PROBLEMS, EXIT_SIGTERM, EXIT_USAGE, app
from horae.cli_output import CommandFailed
from horae.cli_serve import build_api, build_scheduler, prepare_store, serve_until_signal
from horae.config import load_instance
from horae.scheduler import Scheduler
from runner_fixtures import write_instance


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return write_instance(tmp_path / "instance")


async def _get(port: int, path: str, host: str | None = None) -> httpx2.Response:
    headers = {} if host is None else {"Host": host}
    async with httpx2.AsyncClient(trust_env=False, timeout=5) as client:
        return await client.get(f"http://127.0.0.1:{port}{path}", headers=headers)


@pytest.mark.parametrize("signals", [1, 2])
def test_the_api_answers_on_a_real_socket_and_stops_with_the_scheduler(
    root: Path, signals: int
) -> None:
    instance = load_instance(root)
    prepare_store(instance)
    scheduler = build_scheduler(instance, {})
    api = build_api(instance, scheduler, "127.0.0.1", 0, ["horae.example.org"])
    port = api.address[1]

    async def body() -> tuple[int, int, int, bool]:
        ready = asyncio.Event()
        task = asyncio.create_task(serve_until_signal(scheduler, ready.set, api=api))
        await ready.wait()
        for _ in range(200):
            if api.started:
                break
            await asyncio.sleep(0.01)
        health = await _get(port, "/healthz")
        runs = await _get(port, "/runs")
        assert health.json()["scheduler"]["active"] is True
        assert "server" not in health.headers
        # DNS rebinding: a page whose own name points at 127.0.0.1 sends that name.
        rebound = await _get(port, "/runs", host="rebind.attacker.example")
        assert rebound.status_code == 400
        assert rebound.json()["error"]["kind"] == "invalid_host"
        assert (await _get(port, "/healthz", host="horae.example.org")).status_code == 200
        for _ in range(signals):
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0)
        code = await asyncio.wait_for(task, 10)
        return code, health.status_code, runs.status_code, scheduler.state().active

    code, health, runs, active = asyncio.run(body())
    assert (health, runs, active) == (200, 200, False)
    assert code == (EXIT_OK if signals == 1 else EXIT_SIGTERM)
    # The socket is closed: nothing listens any more.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_a_port_in_use_is_a_configuration_problem(root: Path) -> None:
    taken = bind("127.0.0.1", 0)
    port = taken.getsockname()[1]
    try:
        with pytest.raises(BindError, match=rf"cannot listen on 127\.0\.0\.1:{port}: "):
            bind("127.0.0.1", port)
        result = CliRunner().invoke(app, ["serve", "-i", str(root), "--port", str(port)])
    finally:
        taken.close()
    assert result.exit_code == EXIT_PROBLEMS
    assert f"horae serve: cannot listen on 127.0.0.1:{port}" in result.stderr


def test_a_bad_host_is_a_configuration_problem(root: Path) -> None:
    result = CliRunner().invoke(
        app, ["serve", "-i", str(root), "--host", "256.1.1.1", "--port", "0"]
    )
    assert result.exit_code == EXIT_PROBLEMS
    assert "horae serve: cannot listen on 256.1.1.1:0" in result.stderr


def test_serve_announces_the_api_and_binds_loopback_by_default(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli_serve.serve_until_signal
    seen: list[tuple[str, int]] = []

    async def stop_when_ready(
        scheduler: Scheduler, on_ready: Callable[[], object] | None = None, **kwargs: Any
    ) -> int:
        api = cast(ApiServer, kwargs["api"])
        seen.append(api.address)
        loop = asyncio.get_running_loop()
        on = lambda: loop.call_soon(os.kill, os.getpid(), signal.SIGTERM)  # noqa: E731
        return await real(scheduler, on_ready=on, **kwargs)

    monkeypatch.setattr(cli_serve, "serve_until_signal", stop_when_ready)
    result = CliRunner().invoke(app, ["serve", "-i", str(root)], env={"HORAE_PORT": "0"})
    assert result.exit_code == EXIT_OK, result.output
    host, port = seen[0]
    assert host == "127.0.0.1"
    assert f"horae serve: HTTP API on http://127.0.0.1:{port}\n" in result.stderr
    assert (root / "state" / "horae.db").is_file()  # created at start, for /healthz


class _DyingServer:
    """An API server that ends on its own, unasked."""

    should_exit = False

    def __init__(self, error: BaseException | None) -> None:
        self.error = error
        self.stops: list[bool] = []

    def request_stop(self, *, force: bool = False) -> None:
        self.stops.append(force)

    async def run_until_stopped(self) -> None:
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize("error", [None, RuntimeError("boom")])
def test_a_server_that_ends_on_its_own_stops_the_scheduler(
    root: Path, error: BaseException | None
) -> None:
    scheduler = build_scheduler(load_instance(root), {})
    dying = _DyingServer(error)

    async def body() -> int:
        return await serve_until_signal(scheduler, api=cast(ApiServer, dying))

    assert asyncio.run(body()) == EXIT_INTERNAL
    assert scheduler.state().active is False


@pytest.mark.parametrize("value", ["*.example.org", "horae.example.org:443", "a/b"])
def test_a_bad_allowed_host_is_a_usage_error(root: Path, value: str) -> None:
    result = CliRunner().invoke(
        app, ["serve", "-i", str(root), "--port", "0", "--allowed-host", value]
    )
    assert result.exit_code == EXIT_USAGE
    assert "horae serve: --allowed-host: an allowed host is a DNS name" in result.stderr


def test_allowed_hosts_come_from_the_environment_too(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def fake_build_api(*args: Any) -> Any:
        seen.append(len(args))
        assert list(args[4]) == ["horae.example.org", "horae.lan"]
        raise CommandFailed(EXIT_PROBLEMS, "bind_failed", "stop here")

    monkeypatch.setattr(cli_serve, "build_api", fake_build_api)
    result = CliRunner().invoke(
        app,
        ["serve", "-i", str(root)],
        env={"HORAE_ALLOWED_HOSTS": "horae.example.org horae.lan", "HORAE_PORT": "0"},
    )
    assert seen == [5]
    assert result.exit_code == EXIT_PROBLEMS
