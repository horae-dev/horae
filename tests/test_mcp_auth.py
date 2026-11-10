# SPDX-License-Identifier: Apache-2.0
"""MCP server authentication (#56, schemas decision 75): tokens, the verifier, the HTTP
transport behind `TokenAuth`, `horae mcp token create|list|revoke`. Synthetic values only."""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context
from typer.testing import CliRunner

import horae.secrets.logs
from horae.api._server import ApiServer
from horae.cli import app
from horae.config import STORE_FILE
from horae.contracts.mcp import RunsRecent
from horae.mcp_auth import (
    DIGEST_LABEL,
    SCOPE_PIPELINE_RUN,
    SCOPE_READ,
    STDIO_SCOPES,
    TOKEN_CHARS,
    RefusalLog,
    TokenVerifier,
    granted_scopes,
    match,
    new_token,
    require_scope,
    token_digest,
    well_formed,
)
from horae.mcp_server import MCP_PATH, Reader, build_http_app, build_server, http_app
from horae.secrets.logs import MCP_TOKEN_REDACTED, redact_all
from horae.store import McpTokenExists, Store
from runner_fixtures import write_instance

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
TOOLS = {"tasks.today", "brief.latest", "runs.recent"}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return write_instance(tmp_path / "instance")


@pytest.fixture
def store_path(root: Path) -> Path:
    return root / "state" / STORE_FILE


def add(
    store_path: Path,
    name: str,
    scopes: tuple[str, ...] = (SCOPE_READ,),
    expires_at: datetime | None = None,
) -> str:
    token = new_token()
    with Store.open(store_path) as store:
        store.mcp_token_add(name, token_digest(token), scopes, at=NOW, expires_at=expires_at)
    return token


def revoke(store_path: Path, name: str) -> None:
    with Store.open(store_path) as store:
        assert store.mcp_token_revoke(name, at=NOW)


# --- tokens and the digest ------------------------------------------------------------------


def test_a_token_is_256_random_bits_with_a_recognisable_prefix() -> None:
    tokens = {new_token() for _ in range(64)}
    assert len(tokens) == 64
    for token in tokens:
        assert well_formed(token)
        assert len(token) == TOKEN_CHARS == 48
        assert re.fullmatch(r"hmcp_[A-Za-z0-9_-]{43}", token)


def test_the_store_keeps_a_labelled_sha256_never_the_token(store_path: Path) -> None:
    import hashlib

    token = add(store_path, "laptop")
    assert token_digest(token) == hashlib.sha256(DIGEST_LABEL + token.encode()).digest()
    data = store_path.read_bytes()
    for name in ("-wal",):
        wal = store_path.with_name(store_path.name + name)
        if wal.exists():
            data += wal.read_bytes()
    assert token.encode() not in data
    assert token[len("hmcp_") :].encode() not in data


def test_the_store_file_is_private(store_path: Path) -> None:
    add(store_path, "laptop")
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700


def test_a_name_is_unique_among_tokens_not_revoked(store_path: Path) -> None:
    add(store_path, "laptop")
    with pytest.raises(McpTokenExists):
        add(store_path, "laptop")
    revoke(store_path, "laptop")
    add(store_path, "laptop")  # free again once revoked
    with Store.open(store_path) as store:
        rows = store.mcp_tokens()
    assert [(r.name, r.revoked_at is None) for r in rows] == [
        ("laptop", False),
        ("laptop", True),
    ]


# --- the verifier: one answer for every bad token ---------------------------------------------


def verifier(store_path: Path, now: datetime = NOW) -> TokenVerifier:
    return TokenVerifier(store_path, lambda: now)


def test_a_valid_token_authenticates_with_its_scopes(store_path: Path) -> None:
    token = add(store_path, "laptop", (SCOPE_READ, SCOPE_PIPELINE_RUN))
    principal = verifier(store_path).verify(token)
    assert principal is not None
    assert principal.name == "laptop"
    assert principal.scopes == {SCOPE_READ, SCOPE_PIPELINE_RUN}


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "hmcp_",
        "Bearer",
        "hmcp_" + "!" * 43,
        "hmcp_" + "A" * 42,
        "hmcp_" + "A" * 44,
        "xmcp_" + "A" * 43,
        "hmcp_" + "A" * 10_000,
        "hmcp_" + "A" * 43,  # well formed, unknown (also the dummy digested for malformed ones)
    ],
)
def test_missing_malformed_and_wrong_tokens_are_refused(store_path: Path, presented: str) -> None:
    add(store_path, "laptop")
    assert verifier(store_path).verify(presented) is None


def test_a_token_differing_by_one_character_is_refused(store_path: Path) -> None:
    token = add(store_path, "laptop")
    flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verifier(store_path).verify(flipped) is None


def test_a_revoked_token_is_refused_from_the_next_request(store_path: Path) -> None:
    token = add(store_path, "laptop")
    v = verifier(store_path)
    assert v.verify(token) is not None
    revoke(store_path, "laptop")
    assert v.verify(token) is None


def test_an_expired_token_is_refused(store_path: Path) -> None:
    token = add(store_path, "laptop", expires_at=NOW + timedelta(days=1))
    assert verifier(store_path, NOW + timedelta(hours=23)).verify(token) is not None
    assert verifier(store_path, NOW + timedelta(days=1)).verify(token) is None


def test_no_store_means_no_token(store_path: Path) -> None:
    assert not store_path.exists()
    assert verifier(store_path).verify(new_token()) is None
    assert verifier(store_path).usable_count() == 0
    assert not store_path.exists()  # never created by the verifier


def test_every_row_is_compared_in_constant_time_whatever_the_token(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hmac.compare_digest` against every row, for a good, unknown, malformed, revoked and
    expired token alike: the same work, so timing tells nothing."""
    good = add(store_path, "a")
    gone = add(store_path, "b")
    old = add(store_path, "c", expires_at=NOW - timedelta(seconds=1))
    revoke(store_path, "b")
    with Store.open(store_path) as store:
        rows = store.mcp_tokens()
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", spy)
    for presented in (good, gone, old, new_token(), "nonsense", ""):
        calls.clear()
        result = match(rows, presented, NOW)
        assert len(calls) == len(rows) == 3
        assert {a for a, _ in calls} == {r.token_sha256 for r in rows}
        assert (result is not None) == (presented == good)


def test_last_use_is_recorded_at_most_once_a_minute(store_path: Path) -> None:
    token = add(store_path, "laptop")
    verifier(store_path).verify(token)
    verifier(store_path, NOW + timedelta(seconds=30)).verify(token)
    with Store.open(store_path) as store:
        assert store.mcp_tokens()[0].last_used_at == NOW
    verifier(store_path, NOW + timedelta(minutes=2)).verify(token)
    with Store.open(store_path) as store:
        assert store.mcp_tokens()[0].last_used_at == NOW + timedelta(minutes=2)


def test_stdio_is_granted_every_scope_http_only_its_tokens() -> None:
    assert granted_scopes(None, stdio=True) == STDIO_SCOPES == {SCOPE_READ, SCOPE_PIPELINE_RUN}
    assert granted_scopes(None, stdio=False) == frozenset()  # fail closed

    class Bare:
        scope: dict[str, Any] = {}  # noqa: RUF012

    assert granted_scopes(Bare(), stdio=False) == frozenset()


# --- log redaction ------------------------------------------------------------------------------


def test_the_log_scrubber_replaces_anything_shaped_like_a_token() -> None:
    token = new_token()
    text = redact_all(f"header was Bearer {token} and {token[:30]}")
    assert token not in text
    assert token[5:30] not in text
    assert text.count(MCP_TOKEN_REDACTED) == 2


# --- the HTTP transport -------------------------------------------------------------------------


class Served:
    def __init__(self, url: str) -> None:
        self.url = url


@contextlib.contextmanager
def serving(app: Any) -> Iterator[Served]:
    """`app` on a loopback port, in a thread."""
    sock = socket.create_server(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on")
    server = ApiServer(config, sock)  # leaves the process's signal handlers alone
    port = sock.getsockname()[1]
    thread = threading.Thread(target=lambda: asyncio.run(server.run_until_stopped()), daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        threading.Event().wait(0.025)
    assert server.started
    try:
        yield Served(f"http://127.0.0.1:{port}{MCP_PATH}")
    finally:
        server.request_stop()
        thread.join(10)


def http_reader(root: Path, store_path: Path) -> Reader:
    return Reader(root / "state", store_path, "Europe/Paris", "daily-brief", lambda: NOW)


@pytest.fixture
def served(root: Path, store_path: Path) -> Iterator[Served]:
    """The real HTTP app (`build_http_app`) on a loopback port."""
    with serving(build_http_app(http_reader(root, store_path), TokenVerifier(store_path))) as s:
        yield s


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def post(url: str, headers: dict[str, str]) -> httpx2.Response:
    return httpx2.post(url, json=INIT, headers={**ACCEPT, **headers}, timeout=10)


def tools_over_http(url: str, token: str) -> tuple[set[str], dict[str, Any]]:
    async def go() -> tuple[set[str], dict[str, Any]]:
        headers = {"authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=10) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("runs.recent", {"limit": 1})
            assert not result.is_error
            return names, dict(result.structured_content or {})

    return asyncio.run(asyncio.wait_for(go(), 30))


def test_a_valid_token_reaches_the_read_only_tools(store_path: Path, served: Served) -> None:
    token = add(store_path, "laptop")
    names, out = tools_over_http(served.url, token)
    assert names == TOOLS
    assert RunsRecent.model_validate(out).runs == []


def test_every_bad_request_gets_the_same_401(store_path: Path, served: Served) -> None:
    token = add(store_path, "laptop")
    gone = add(store_path, "gone")
    old = add(store_path, "old", expires_at=NOW - timedelta(days=1))
    revoke(store_path, "gone")
    cases: list[dict[str, str]] = [
        {},  # missing
        {"authorization": ""},
        {"authorization": f"Basic {token}"},  # wrong scheme
        {"authorization": "Bearer"},
        {"authorization": "Bearer not-a-token"},  # malformed
        {"authorization": f"Bearer {new_token()}"},  # wrong
        {"authorization": f"Bearer {gone}"},  # revoked
        {"authorization": f"Bearer {old}"},  # expired
        {"authorization": f"Bearer {token}x"},
    ]
    answers = set()
    for headers in cases:
        response = post(served.url, headers)
        assert response.status_code == 401
        answers.add(
            (
                response.content,
                response.headers["www-authenticate"],
                response.headers["content-type"],
            )
        )
    assert len(answers) == 1
    body = json.loads(next(iter(answers))[0])
    assert body["error"] == "invalid_token"
    assert post(served.url, {"authorization": f"bearer {token}"}).status_code == 200


def test_a_token_is_not_accepted_in_the_query_string(store_path: Path, served: Served) -> None:
    token = add(store_path, "laptop")
    response = httpx2.post(
        served.url, params={"access_token": token}, json=INIT, headers=ACCEPT, timeout=10
    )
    assert response.status_code == 401


def test_every_path_needs_a_token(served: Served) -> None:
    base = served.url.removesuffix(MCP_PATH)
    for path in ("/", "/nope", "/.well-known/oauth-protected-resource", MCP_PATH):
        assert httpx2.get(base + path, timeout=10).status_code == 401


def test_revocation_applies_to_the_next_request(store_path: Path, served: Served) -> None:
    token = add(store_path, "laptop")
    assert post(served.url, {"authorization": f"Bearer {token}"}).status_code == 200
    revoke(store_path, "laptop")
    assert post(served.url, {"authorization": f"Bearer {token}"}).status_code == 401


def test_a_token_without_the_read_scope_cannot_call_a_tool(
    store_path: Path, served: Served
) -> None:
    token = add(store_path, "runner", (SCOPE_PIPELINE_RUN,))

    async def go() -> Any:
        headers = {"authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=10) as http,
            Client(streamable_http_client(served.url, http_client=http)) as client,
        ):
            return await client.call_tool("runs.recent", {})

    result = asyncio.run(asyncio.wait_for(go(), 30))
    assert result.is_error
    assert "insufficient_scope" in json.dumps([c.model_dump() for c in result.content])


def test_a_foreign_host_header_is_refused_even_with_a_token(
    store_path: Path, served: Served
) -> None:
    token = add(store_path, "laptop")
    headers = {"authorization": f"Bearer {token}", "host": "evil.example"}
    assert post(served.url, headers).status_code in (400, 421)


def test_no_token_reaches_the_logs(
    store_path: Path,
    served: Served,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing logs the token itself: the scrubber's token shape is switched off here, so a
    token in a record would show."""
    monkeypatch.setattr(horae.secrets.logs, "MCP_TOKEN_SHAPE", re.compile(r"(?!)"))
    caplog.set_level(logging.DEBUG)
    token = add(store_path, "laptop")
    tools_over_http(served.url, token)
    post(served.url, {"authorization": f"Bearer {new_token()}"})
    post(served.url, {"authorization": f"Bearer {token}x"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "refused" in text  # the refusals are logged, without the token
    assert "hmcp_" not in text


# --- horae mcp token ----------------------------------------------------------------------------


def cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def test_create_prints_the_token_once_and_stores_its_digest(root: Path, store_path: Path) -> None:
    result = cli("mcp", "token", "create", "laptop", "-i", str(root), "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    token = doc["token"]
    assert well_formed(token)
    assert doc["scopes"] == ["read"]
    assert doc["expires_at"] is None
    assert verifier(store_path, datetime.now(UTC)).verify(token) is not None
    text = cli("mcp", "token", "create", "phone", "-i", str(root), "--expires-in", "30")
    assert text.exit_code == 0
    lines = text.stdout.splitlines()
    assert lines[0].startswith("created MCP token phone (scopes read; expires ")
    assert well_formed(lines[1])
    assert "shown only once" in text.stderr


def test_create_refuses_a_bad_name_scope_or_a_name_in_use(root: Path) -> None:
    assert cli("mcp", "token", "create", "laptop", "-i", str(root)).exit_code == 0
    used = cli("mcp", "token", "create", "laptop", "-i", str(root), "--json")
    assert used.exit_code == 3
    assert json.loads(used.stdout)["error"]["kind"] == "token_exists"
    for bad in ("", "-x", "a b", "x" * 65, "ünïcode"):
        assert cli("mcp", "token", "create", bad, "-i", str(root)).exit_code == 2
    scope = cli("mcp", "token", "create", "x", "-i", str(root), "--scope", "admin")
    assert scope.exit_code == 2
    run = cli("mcp", "token", "create", "y", "-i", str(root), "--scope", "pipeline.run", "--json")
    assert json.loads(run.stdout)["scopes"] == ["pipeline.run"]


def test_list_never_shows_a_secret(root: Path, store_path: Path) -> None:
    first = json.loads(cli("mcp", "token", "create", "a", "-i", str(root), "--json").stdout)
    cli("mcp", "token", "create", "b", "-i", str(root), "--expires-in", "1")
    assert cli("mcp", "token", "revoke", "a", "-i", str(root)).exit_code == 0
    listed = cli("mcp", "token", "list", "-i", str(root), "--json")
    assert listed.exit_code == 0
    doc = json.loads(listed.stdout)
    assert [(t["name"], t["status"]) for t in doc["tokens"]] == [
        ("a", "revoked"),
        ("b", "active"),
    ]
    assert set(doc["tokens"][0]) == {
        "name",
        "scopes",
        "created_at",
        "expires_at",
        "last_used_at",
        "revoked_at",
        "status",
    }
    text = cli("mcp", "token", "list", "-i", str(root))
    with Store.open(store_path) as store:
        digests = [r.token_sha256.hex() for r in store.mcp_tokens()]
    for out in (listed.stdout, text.stdout):
        assert "hmcp_" not in out
        assert first["token"] not in out
        assert not any(d in out for d in digests)
    assert "2 tokens, 1 active" in text.stdout


def test_list_on_a_new_instance_creates_nothing(root: Path, store_path: Path) -> None:
    result = cli("mcp", "token", "list", "-i", str(root))
    assert result.exit_code == 0
    assert "no MCP token" in result.stdout
    assert not store_path.exists()


def test_revoke_an_unknown_name_is_refused(root: Path) -> None:
    result = cli("mcp", "token", "revoke", "nobody", "-i", str(root), "--json")
    assert result.exit_code == 3
    assert json.loads(result.stdout)["error"]["kind"] == "unknown_token"


# --- horae mcp serve --http -------------------------------------------------------------------


def test_the_http_server_refuses_to_start_without_a_usable_token(
    root: Path, store_path: Path
) -> None:
    args = ("mcp", "serve", "--http", "--port", "0", "-i", str(root))
    result = cli(*args)
    assert result.exit_code == 1
    assert "no usable MCP token" in result.stderr
    add(store_path, "old", expires_at=NOW)  # long expired
    add(store_path, "gone")
    revoke(store_path, "gone")
    result = cli(*args)
    assert result.exit_code == 1
    assert "no usable MCP token" in result.stderr


def test_listen_options_need_http(root: Path) -> None:
    result = cli("mcp", "serve", "--port", "9999", "-i", str(root))
    assert result.exit_code == 2


def test_the_real_command_serves_http_on_loopback_with_a_token(
    root: Path, store_path: Path
) -> None:
    token = add(store_path, "laptop")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    cmd = [sys.executable, "-m", "horae", "mcp", "serve", "--http", "--port", "0", "-i", str(root)]
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, env=env)  # noqa: S603
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        found = re.search(r"listening on (http://127\.0\.0\.1:\d+/mcp)", line)
        assert found, line
        url = found[1]
        assert post(url, {}).status_code == 401
        names, _ = tools_over_http(url, token)
        assert names == TOOLS
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=20)
    assert proc.returncode == 0
    assert token not in err
    assert "hmcp_" not in err


# --- review follow-ups: explicit transport, pipeline.run over HTTP, refusal logging -------------


def test_build_server_needs_its_transport_said(root: Path, store_path: Path) -> None:
    with pytest.raises(TypeError):
        build_server(http_reader(root, store_path))  # type: ignore[call-arg]


def call_probe(url: str, token: str) -> Any:
    async def go() -> Any:
        headers = {"authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=10) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            return await client.call_tool("probe.pipeline_run", {})

    return asyncio.run(asyncio.wait_for(go(), 30))


def test_a_read_only_token_is_refused_what_needs_pipeline_run(root: Path, store_path: Path) -> None:
    """No tool needs `pipeline.run` yet (#55): a probe tool on the HTTP-built server checks
    it with `require_scope`, behind the same `http_app` wrapping as `build_http_app`."""
    server = build_server(http_reader(root, store_path), stdio=False)

    @server.tool(name="probe.pipeline_run")
    def probe(ctx: Context) -> str:
        require_scope(ctx.request_context.request, SCOPE_PIPELINE_RUN, stdio=False)
        return "allowed"

    reader_token = add(store_path, "reader")
    runner_token = add(store_path, "runner", (SCOPE_READ, SCOPE_PIPELINE_RUN))
    with serving(http_app(server, TokenVerifier(store_path))) as s:
        refused = call_probe(s.url, reader_token)
        allowed = call_probe(s.url, runner_token)
    assert refused.is_error
    assert "insufficient_scope" in json.dumps([c.model_dump() for c in refused.content])
    assert not allowed.is_error


def test_refusals_are_logged_as_one_summary_per_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="horae.mcp")
    clock = [1000.0]
    log = RefusalLog(interval=60, clock=lambda: clock[0])
    for _ in range(500):
        log.refused()
    clock[0] += 59
    log.refused()
    assert len(caplog.records) == 1  # the first refusal only
    clock[0] += 1
    log.refused()
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert lines[1].startswith("501 MCP request(s)")


def test_a_flood_of_bad_tokens_logs_one_line(
    store_path: Path, served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    add(store_path, "laptop")
    caplog.set_level(logging.DEBUG, logger="horae.mcp")
    for _ in range(25):
        assert post(served.url, {"authorization": f"Bearer {new_token()}"}).status_code == 401
    refusals = [r for r in caplog.records if "refused" in r.getMessage()]
    assert len(refusals) == 1
