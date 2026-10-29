# SPDX-License-Identifier: Apache-2.0
"""Push monitors (#39, schemas decision 49 F): `up` / `down` per run, against a fake HTTP
server on 127.0.0.1. The URL is a secret; a failed ping never fails the run; the network
posture is the LLM backends' (no ambient proxy, no redirect, bounded time and body)."""

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from typer.testing import CliRunner

from horae import cli_run, cli_serve
from horae.apply import StaticSinks
from horae.cli import EXIT_LOCKED, EXIT_OK, EXIT_PROBLEMS, app
from horae.config import Instance, load_instance
from horae.contracts.instance import PushMonitor
from horae.runner import PipelineLock, Ports
from horae.scheduler import RunOutcome, Schedule, Scheduler, ping, ping_url
from horae.scheduler import _monitor as monitor_module
from runner_fixtures import Plugins, write_instance
from test_scheduler import FakeClock, settle

runner = CliRunner()
TOKEN = "pushtoken0123456789"  # noqa: S105 - synthetic
"""The synthetic secret part of the push URL (must never be printed or logged)."""
ENV = "HORAE_TEST_PUSH_URL"


class _Server:
    """A fake push monitor: records each request; `status`, `delay`, `headers` and `body`
    shape the answer."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []
        self.status = 200
        self.delay = 0.0
        self.headers: dict[str, str] = {}
        self.body = b'{"ok":true}'
        self.release = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                server.requests.append(
                    (
                        parts.path,
                        parse_qs(parts.query),
                        {k.lower(): v for k, v in self.headers.items()},
                    )
                )
                if server.delay:
                    server.release.wait(server.delay)
                self.send_response(server.status)
                for name, value in server.headers.items():
                    self.send_header(name, value)
                if "content-length" not in {k.lower() for k in server.headers}:
                    self.send_header("content-length", str(len(server.body)))
                self.end_headers()
                with contextlib.suppress(OSError):  # the client gave up
                    self.wfile.write(server.body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str = f"/api/push/{TOKEN}") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture
def server() -> Iterator[_Server]:
    fake = _Server()
    fake.thread.start()
    yield fake
    fake.release.set()
    fake.httpd.shutdown()
    fake.httpd.server_close()


@pytest.fixture
def fake_ports(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    holder = SimpleNamespace(plugins=Plugins())

    @asynccontextmanager
    async def live_ports(instance: Instance) -> AsyncIterator[Any]:
        ports = Ports(plugins=holder.plugins, tools=holder.plugins, sinks=None)
        yield SimpleNamespace(ports=ports, sinks=StaticSinks())

    monkeypatch.setattr(cli_run, "live_ports", live_ports)
    return holder


def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, **monitor: Any) -> Path:
    monkeypatch.setenv(ENV, url)
    scheduler = {
        "monitors": {
            "collect": {"secret": "PUSH_URL", **monitor},
            "broken": {"secret": "PUSH_URL", **monitor},
            "slow": {"secret": "PUSH_URL", **monitor},
        }
    }
    return write_instance(
        tmp_path / "instance",
        secrets={"refs": {"PUSH_URL": {"env": ENV}}},
        scheduler=scheduler,
    )


def _invoke(*args: str) -> Any:
    return runner.invoke(app, list(args))


# --- the ping URL -----------------------------------------------------------------------------


def test_uptime_kuma_and_healthchecks_urls() -> None:
    kuma = PushMonitor(secret="PUSH_URL")  # noqa: S106 - a secret name
    base = f"https://kuma.example.org/api/push/{TOKEN}?status=up&msg=OK&ping="
    assert ping_url(base, kuma, RunOutcome("ok"), 1.2346) == (
        f"https://kuma.example.org/api/push/{TOKEN}?status=up&msg=ok&ping=1235"
    )
    assert ping_url(base, kuma, RunOutcome("failed"), 0) == (
        f"https://kuma.example.org/api/push/{TOKEN}?status=down&msg=failed&ping=0"
    )
    checks = PushMonitor(secret="PUSH_URL", kind="healthchecks")  # noqa: S106
    hc = "https://hc.example.org/ping/0f4b7c1e-synthetic/"
    assert ping_url(hc, checks, RunOutcome("degraded"), 3) == hc
    assert ping_url(hc, checks, RunOutcome("timeout"), 3) == (
        "https://hc.example.org/ping/0f4b7c1e-synthetic/fail"
    )
    assert RunOutcome("noop").up
    assert not RunOutcome("cancelled").up


# --- `horae run` pings up / down --------------------------------------------------------------


def test_a_run_pings_up_with_its_status_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    result = _invoke("run", "collect", "-i", str(root), "--json")
    assert result.exit_code == EXIT_OK
    [(path, query, headers)] = server.requests
    assert path == f"/api/push/{TOKEN}"
    assert query["status"] == ["up"]
    assert query["msg"] == ["ok"]
    assert int(query["ping"][0]) >= 0
    assert headers["user-agent"].startswith("horae/")
    assert TOKEN not in result.output


def test_a_failed_run_pings_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    result = _invoke("run", "broken", "-i", str(root), "--json")
    assert result.exit_code == EXIT_PROBLEMS
    [(_, query, _)] = server.requests
    assert query["status"] == ["down"]
    assert query["msg"] == ["failed"]


def test_a_timed_out_run_pings_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    config = yaml.safe_load((root / "horae.yaml").read_text("utf-8"))
    config["runs"] = {"timeout_s": 1}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), "utf-8")
    result = _invoke("run", "slow", "-i", str(root), "--json")
    assert result.exit_code == EXIT_PROBLEMS
    [(_, query, _)] = server.requests
    assert (query["status"], query["msg"]) == (["down"], ["timeout"])


def test_healthchecks_fail_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url("/ping/0f4b7c1e-synthetic"), kind="healthchecks")
    assert _invoke("run", "collect", "-i", str(root)).exit_code == EXIT_OK
    assert _invoke("run", "broken", "-i", str(root)).exit_code == EXIT_PROBLEMS
    assert [path for path, _, _ in server.requests] == [
        "/ping/0f4b7c1e-synthetic",
        "/ping/0f4b7c1e-synthetic/fail",
    ]


def test_a_dry_run_or_a_locked_pipeline_pings_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    assert _invoke("run", "collect", "-i", str(root), "--dry-run").exit_code == EXIT_OK
    lock = PipelineLock.try_acquire(root / "state", "collect")
    assert lock is not None
    with lock:
        assert _invoke("run", "collect", "-i", str(root)).exit_code == EXIT_LOCKED
    assert server.requests == []


def test_a_pipeline_without_a_monitor_pings_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    assert _invoke("run", "partial", "-i", str(root)).exit_code == EXIT_OK
    assert server.requests == []


# --- a failed ping never fails the run, and never shows the URL -------------------------------


def _assert_quiet(result: Any, caplog: pytest.LogCaptureFixture, reason: str) -> None:
    assert result.exit_code == EXIT_OK
    assert json.loads(result.stdout)["run"]["status"] == "ok"
    warnings = [r.getMessage() for r in caplog.records if r.name == "horae.scheduler.monitor"]
    assert len(warnings) == 1
    assert "push monitor of pipeline collect: the up ping failed" in warnings[0]
    assert reason in warnings[0]
    assert TOKEN not in caplog.text
    assert TOKEN not in result.output


@pytest.mark.parametrize(
    ("status", "headers", "reason"),
    [
        (500, {}, "status 500"),
        (302, {"location": "http://127.0.0.1:1/elsewhere"}, "status 302"),
        (200, {"content-length": str(10 << 20)}, "an unreadable or oversized response"),
    ],
)
def test_a_failed_ping_is_a_warning_and_the_run_still_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _Server,
    fake_ports: Any,
    caplog: pytest.LogCaptureFixture,
    status: int,
    headers: dict[str, str],
    reason: str,
) -> None:
    server.status, server.headers = status, headers
    root = _root(tmp_path, monkeypatch, server.url())
    with caplog.at_level(logging.WARNING):
        result = _invoke("run", "collect", "-i", str(root), "--json")
    _assert_quiet(result, caplog, reason)
    assert len(server.requests) == 1  # no retry, no redirect followed


def test_a_slow_monitor_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _Server,
    fake_ports: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(monitor_module, "PING_TIMEOUT_S", 0.2)
    server.delay = 5.0
    root = _root(tmp_path, monkeypatch, server.url())
    with caplog.at_level(logging.WARNING):
        result = _invoke("run", "collect", "-i", str(root), "--json")
    _assert_quiet(result, caplog, "timed out")


def test_an_unreachable_monitor_is_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: _Server,
    fake_ports: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = server.url()
    server.httpd.shutdown()
    server.httpd.server_close()
    root = _root(tmp_path, monkeypatch, url)
    with caplog.at_level(logging.WARNING):
        result = _invoke("run", "collect", "-i", str(root), "--json")
    _assert_quiet(result, caplog, "connection failed")


def test_an_ambient_proxy_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    root = _root(tmp_path, monkeypatch, server.url())
    assert _invoke("run", "collect", "-i", str(root)).exit_code == EXIT_OK
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "value",
    [
        f"http://kuma.example.org/api/push/{TOKEN}",  # plain http off the LAN
        f"ftp://kuma.example.org/api/push/{TOKEN}",
        f"https://user:pw@kuma.example.org/api/push/{TOKEN}",
    ],
)
def test_a_url_outside_the_network_rules_is_refused_without_showing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_ports: Any,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    root = _root(tmp_path, monkeypatch, value)
    with caplog.at_level(logging.WARNING):
        result = _invoke("run", "collect", "-i", str(root), "--json")
    _assert_quiet(result, caplog, "secret PUSH_URL is not an https:// URL")


def test_an_unresolvable_secret_is_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_ports: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = _root(tmp_path, monkeypatch, "unused-value")
    monkeypatch.delenv(ENV)
    with caplog.at_level(logging.WARNING):
        result = _invoke("run", "collect", "-i", str(root), "--json")
    assert result.exit_code == EXIT_OK
    assert "secret PUSH_URL could not be resolved" in caplog.text


def test_the_url_is_redacted_if_it_is_ever_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    # The secret is primed for redaction with the instance (#53): a var holding the URL is
    # refused, never echoed.
    root = _root(tmp_path, monkeypatch, server.url())
    result = _invoke("run", "collect", "-i", str(root), "--var", f"x={server.url()}", "--json")
    assert TOKEN not in result.output
    assert server.url() not in result.output


# --- scheduled runs in `horae serve` ----------------------------------------------------------


T0 = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)


def test_a_scheduled_run_runs_the_pipeline_and_pings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    instance = load_instance(root)

    async def body() -> None:
        clock = FakeClock(T0)
        scheduler = cli_serve.build_scheduler(instance, {})
        scheduler = Scheduler(
            [Schedule("collect", "1 5 * * *", "UTC")],
            mode=scheduler.mode,
            run=lambda name: cli_serve.run_scheduled(instance, name),
            ping=lambda name, outcome, duration: ping(instance, name, outcome, duration),
            clock=clock,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await settle()
        await clock.set(T0 + timedelta(minutes=1))
        for _ in range(200):
            if not scheduler.state().pipelines[0].running and server.requests:
                break
            await asyncio.sleep(0.01)
        entry = scheduler.state().pipelines[0]
        assert entry.last_outcome == "ok"
        assert entry.last_run_id is not None
        scheduler.stop()
        await task

    asyncio.run(body())
    [(_, query, _)] = server.requests
    assert query["status"] == ["up"]


def test_a_scheduled_run_never_overlaps_a_manual_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _Server, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, server.url())
    instance = load_instance(root)
    lock = PipelineLock.try_acquire(instance.state_dir, "collect")
    assert lock is not None
    with lock:
        outcome = asyncio.run(cli_serve.run_scheduled(instance, "collect"))
    assert outcome.status == "locked"
    assert server.requests == []


def test_scheduled_run_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ports: Any
) -> None:
    root = _root(tmp_path, monkeypatch, "unused-value")
    config = yaml.safe_load((root / "horae.yaml").read_text("utf-8"))
    config["runs"] = {"timeout_s": 1}
    (root / "horae.yaml").write_text(yaml.safe_dump(config), "utf-8")
    instance = load_instance(root)
    assert asyncio.run(cli_serve.run_scheduled(instance, "broken")).status == "failed"
    assert asyncio.run(cli_serve.run_scheduled(instance, "slow")).status == "timeout"
