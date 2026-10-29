# SPDX-License-Identifier: Apache-2.0
"""The HTTP API (#37, schemas decision 51): `/healthz`, the read-only `/runs`, no secrets in
any response, the security headers. Driven in process (`TestClient`); the real server bind
and the shutdown with the scheduler are in `test_api_server.py`."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from horae import __version__
from horae.api import (
    MAX_PAGE,
    SECURITY_HEADERS,
    ApiContext,
    allowed_host,
    create_app,
    encode_cursor,
    host_of,
)
from horae.cli import app as cli_app
from horae.config import load_instance
from horae.scheduler import PipelineState, SchedulerState
from horae.secrets import Secret
from horae.secrets.redact import encodings, remember
from horae.store import SCHEMA_VERSION, Store
from runner_fixtures import write_instance

T0 = datetime(2026, 3, 1, 6, 0, tzinfo=UTC)
LOCAL = "http://localhost"  # the default TestClient host, `testserver`, is not allowed


class State:
    """A scheduler state the test can change."""

    def __init__(self, state: SchedulerState) -> None:
        self.state = state

    def __call__(self) -> SchedulerState:
        return self.state


ACTIVE = SchedulerState(
    "builtin",
    True,
    T0,
    1,
    (PipelineState("collect", "30 5 * * *", "Europe/Paris", next_fire=T0 + timedelta(days=1)),),
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return write_instance(tmp_path / "instance")


@pytest.fixture
def store_path(root: Path) -> Path:
    path = load_instance(root).store_path
    Store.open(path).close()
    return path


@pytest.fixture
def state() -> State:
    return State(ACTIVE)


@pytest.fixture
def client(root: Path, store_path: Path, state: State) -> Iterator[TestClient]:
    context = ApiContext(store_path, store_path.parent, state)
    app = create_app(context)
    with TestClient(app, base_url=LOCAL, raise_server_exceptions=False) as test_client:
        yield test_client


def _finished(store: Store, run_id: str, pipeline: str, at: datetime, error: str = "gone") -> None:
    """A finished run with no record (an abandoned one): `failed`, `error` set."""
    store.begin_run(run_id, pipeline, at, pid=1, host="h")
    store.abandon_run(run_id, at=at + timedelta(minutes=1), error=error)


def _seed(store_path: Path, count: int) -> list[str]:
    """`count` runs, one minute apart (and two sharing a start time); their ids newest first."""
    ids = []
    with Store.open(store_path) as store:
        for i in range(count):
            at = T0 + timedelta(minutes=i if i < count - 1 else count - 2)
            run_id = f"run-{i:02d}"
            _finished(store, run_id, "collect" if i % 2 else "brief", at)
            ids.append(run_id)
    # Newest first: by start time, then by id (the last two share a start time).
    return sorted(ids, key=lambda r: (min(int(r[4:]), count - 2), r), reverse=True)


# --- /healthz -------------------------------------------------------------------------------


def test_healthz_reports_the_store_and_the_scheduler(client: TestClient, state: State) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "status": "ok",
        "version": __version__,
        "store": {"status": "ok", "reason": None, "schema_version": SCHEMA_VERSION},
        "scheduler": ACTIVE.to_dict(),
    }


def test_healthz_is_503_when_the_builtin_scheduler_is_not_running(
    client: TestClient, state: State
) -> None:
    state.state = SchedulerState("builtin", False, T0)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["scheduler"]["active"] is False
    assert response.json()["store"]["status"] == "ok"


def test_healthz_is_ok_in_external_mode(client: TestClient, state: State) -> None:
    state.state = SchedulerState("external", False)
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("missing", "missing"),
        ("too_new", "schema_too_new"),
        ("too_old", "schema_too_old"),
        ("garbage", "unreadable"),
    ],
)
def test_healthz_is_503_when_the_store_cannot_be_read(
    client: TestClient, store_path: Path, damage: str, reason: str
) -> None:
    if damage == "missing":
        store_path.unlink()
    elif damage == "garbage":
        store_path.write_bytes(b"not a database" * 100)
    else:
        with sqlite3.connect(store_path) as conn:
            conn.execute(
                "PRAGMA user_version = 999" if damage == "too_new" else "PRAGMA user_version = 1"
            )
    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["store"] == {"status": "error", "reason": reason, "schema_version": None}
    # No path, no exception text.
    assert str(store_path.parent) not in response.text
    assert "horae.db" not in response.text


# --- /runs ----------------------------------------------------------------------------------


def test_runs_pages_newest_first_with_a_cursor(client: TestClient, store_path: Path) -> None:
    expected = _seed(store_path, 7)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/runs", params=params).json()
        assert len(body["runs"]) <= 3
        seen += [run["id"] for run in body["runs"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == expected
    assert pages == 3


def test_a_full_last_page_has_no_next_cursor(client: TestClient, store_path: Path) -> None:
    _seed(store_path, 4)
    first = client.get("/runs", params={"limit": 2}).json()
    second = client.get("/runs", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["runs"]) == 2
    assert second["next_cursor"] is None


def test_runs_are_shown_as_horae_runs_json_shows_them(
    client: TestClient, root: Path, store_path: Path
) -> None:
    _seed(store_path, 3)
    with Store.open(store_path) as store:
        store.begin_run("run-live", "collect", T0 + timedelta(hours=1))  # stale: no lock held
    cli = CliRunner().invoke(cli_app, ["runs", "-i", str(root), "--json"])
    assert cli.exit_code == 0, cli.output
    api = client.get("/runs").json()
    assert api["runs"] == json.loads(cli.stdout)["runs"]
    assert api["runs"][0]["id"] == "run-live"
    assert api["runs"][0]["stale"] is True
    one = client.get("/runs/run-live").json()
    assert one == {"schema_version": 1, "run": api["runs"][0]}


def test_runs_filter_by_pipeline_and_status(client: TestClient, store_path: Path) -> None:
    _seed(store_path, 4)
    with Store.open(store_path) as store:
        store.begin_run("run-live", "collect", T0 + timedelta(hours=1))
    collect = client.get("/runs", params={"pipeline": "collect"}).json()["runs"]
    assert {run["pipeline"] for run in collect} == {"collect"}
    assert len(collect) == 3
    running = client.get("/runs", params={"status": "running"}).json()["runs"]
    assert [run["id"] for run in running] == ["run-live"]
    both = client.get("/runs", params={"pipeline": "brief", "status": "running"}).json()
    assert both["runs"] == []


def test_live_skill_evals_are_listed_only_when_asked(client: TestClient, store_path: Path) -> None:
    with Store.open(store_path) as store:
        _finished(store, "run-a", "collect", T0)
        _finished(store, "run-eval", "eval:rank", T0 + timedelta(minutes=1))
    assert [r["id"] for r in client.get("/runs").json()["runs"]] == ["run-a"]
    everything = client.get("/runs", params={"all": "true"}).json()["runs"]
    assert [r["id"] for r in everything] == ["run-eval", "run-a"]
    only = client.get("/runs", params={"pipeline": "eval:rank"}).json()["runs"]
    assert [r["id"] for r in only] == ["run-eval"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": MAX_PAGE + 1},
        {"limit": 0},
        {"limit": "many"},
        {"status": "lost"},
        {"cursor": "not a cursor"},
        {"cursor": encode_cursor(T0, "x")[:-3] + "!!!"},
        {"cursor": "W10"},  # base64url of "[]"
        {"cursor": "x" * 600},
        {"pipeline": "p" * 65},
    ],
)
def test_bad_parameters_are_refused_without_echoing_them(
    client: TestClient, params: dict[str, Any]
) -> None:
    response = client.get("/runs", params=params)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "invalid_request"
    assert error["message"].startswith("invalid parameter: ")
    for value in params.values():
        assert str(value) not in response.text


def test_the_page_size_is_capped(client: TestClient, store_path: Path) -> None:
    _seed(store_path, 3)
    assert client.get("/runs", params={"limit": MAX_PAGE}).status_code == 200


def test_an_unknown_run_is_404(client: TestClient) -> None:
    response = client.get("/runs/nope")
    assert response.status_code == 404
    assert response.json() == {
        "schema_version": 1,
        "error": {"kind": "not_found", "message": "no such run"},
    }


def test_no_store_yet_lists_no_runs(client: TestClient, store_path: Path) -> None:
    store_path.unlink()
    assert client.get("/runs").json() == {"schema_version": 1, "runs": [], "next_cursor": None}
    assert client.get("/runs/run-00").status_code == 404


def test_an_unreadable_store_is_503(client: TestClient, store_path: Path) -> None:
    store_path.write_bytes(b"not a database" * 100)
    for path in ("/runs", "/runs/run-00"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["kind"] == "store_error"
        assert str(store_path.parent) not in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/runs", "/runs/run-00", "/healthz"])
def test_the_api_is_read_only(client: TestClient, store_path: Path, method: str, path: str) -> None:
    _seed(store_path, 2)
    before = sorted(p.name for p in store_path.parent.iterdir())
    digest = hashlib.sha256(store_path.read_bytes()).hexdigest()
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.json()["error"]["kind"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]
    for read in ("/runs", "/runs/run-00", "/healthz"):
        client.get(read)
    # Reading created nothing (no -wal / -shm) and changed nothing.
    assert sorted(p.name for p in store_path.parent.iterdir()) == before
    assert hashlib.sha256(store_path.read_bytes()).hexdigest() == digest


def test_reading_does_not_wait_for_a_writer(client: TestClient, store_path: Path) -> None:
    _seed(store_path, 2)
    with Store.open(store_path) as writer, writer.transaction():
        _finished(writer, "run-uncommitted", "collect", T0 + timedelta(hours=2))
        # WAL: the reader is not blocked by the open write transaction and does not see it.
        response = client.get("/runs")
        assert response.status_code == 200
        assert "run-uncommitted" not in response.text
        assert client.get("/healthz").status_code == 200
    assert client.get("/runs").json()["runs"][0]["id"] == "run-uncommitted"


# --- no secrets, headers ---------------------------------------------------------------------

SECRET = "planted-secret-value-0123456789"  # noqa: S105 (synthetic)


def test_no_response_ever_holds_a_secret(
    client: TestClient, store_path: Path, state: State
) -> None:
    remember(Secret("API_PLANTED", SECRET))
    with Store.open(store_path) as store:
        _finished(store, "run-clean", "collect", T0)
        _finished(store, f"run-{SECRET}", "collect", T0 + timedelta(minutes=1))
        _finished(store, "run-named", f"p-{SECRET}", T0 + timedelta(minutes=2))
    # Planted behind the store's back (the store itself redacts what it writes): a run's
    # error and a journal entry hold the raw value.
    with sqlite3.connect(store_path) as conn:
        conn.execute(
            "UPDATE runs SET error = ? WHERE id = ?", (f"boom: token={SECRET}", "run-clean")
        )
        conn.execute(
            "INSERT INTO journal (run_id, seq, intent, intent_type, target, undoable, "
            "created_at) VALUES (?, 0, 0, 'notify.push', ?, 0, ?)",
            ("run-clean", f"ntfy:{SECRET}", "2026-03-01T06:00:00.000000Z"),
        )
        planted = conn.execute("SELECT count(*) FROM runs WHERE instr(error, ?) > 0", (SECRET,))
        assert planted.fetchone() == (1,)
    state.state = SchedulerState(
        "builtin",
        True,
        T0,
        1,
        (
            PipelineState(
                f"p-{SECRET}", "30 5 * * *", "UTC", last_run_id=f"run-{SECRET}", last_outcome="ok"
            ),
        ),
    )
    first = client.get("/runs", params={"limit": 1})
    responses = [
        client.get("/healthz"),
        client.get("/runs"),
        first,
        client.get("/runs", params={"limit": 1, "cursor": first.json()["next_cursor"]}),
        client.get("/runs/run-clean"),
        client.get(f"/runs/run-{SECRET}"),
        client.get("/runs", params={"pipeline": f"p-{SECRET}"}),
        client.get("/runs", params={"pipeline": SECRET * 3}),
        client.get("/runs", params={"limit": SECRET}),
        client.get("/runs", params={"cursor": SECRET}),
        client.get(f"/{SECRET}"),
        client.post(f"/runs/{SECRET}"),
    ]
    assert {r.status_code for r in responses} >= {200, 400, 404, 405}
    redacted = 0
    for response in responses:
        text = response.text + json.dumps(dict(response.headers))
        for form in encodings(SECRET):
            assert form not in text, (response.request.url.path, response.text)
        redacted += response.text.count("[REDACTED:API_PLANTED]")
    assert redacted >= 6  # the error, the ids, the pipeline names, the scheduler state


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/healthz"),
        ("GET", "/runs"),
        ("GET", "/runs/nope"),
        ("GET", "/runs?limit=0"),
        ("GET", "/nope"),
        ("POST", "/runs"),
    ],
)
def test_every_response_has_the_security_headers(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path, headers={"Origin": "https://evil.example"})
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["content-type"] == "application/json"
    assert "access-control-allow-origin" not in response.headers


def test_a_store_error_response_has_the_security_headers(
    client: TestClient, store_path: Path
) -> None:
    store_path.write_bytes(b"x" * 1000)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_no_cors_preflight_and_no_docs(client: TestClient) -> None:
    preflight = client.options(
        "/runs",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.status_code == 405
    assert "access-control-allow-origin" not in preflight.headers
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404


def test_an_internal_error_shows_only_its_type(root: Path, store_path: Path) -> None:
    def broken() -> SchedulerState:
        raise RuntimeError(f"leaky {SECRET}")

    context = ApiContext(store_path, store_path.parent, broken)
    app = create_app(context)
    with TestClient(app, base_url=LOCAL, raise_server_exceptions=False) as test_client:
        response = test_client.get("/healthz")
    assert response.status_code == 500
    assert response.json()["error"] == {
        "kind": "internal",
        "message": "internal error (RuntimeError)",
    }
    assert SECRET not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


# --- Host check (DNS rebinding) ---------------------------------------------------------------

REBOUND = "rebind.attacker.example"  # synthetic: a name its owner points at 127.0.0.1


@pytest.mark.parametrize(
    "host",
    [
        REBOUND,
        f"{REBOUND}:8080",
        "testserver",
        "localhost.attacker.example",
        "127.0.0.1.attacker.example",
        "horae.example.org.attacker.example",
        "sub.horae.example.org",
        "",
        "localhost:http",
        "localhost:8080:1",
        "[::1",
        "local host",
    ],
)
@pytest.mark.parametrize("path", ["/healthz", "/runs", "/runs/run-00", "/nope"])
def test_a_request_for_another_host_is_refused(
    root: Path, store_path: Path, state: State, host: str, path: str
) -> None:
    context = ApiContext(store_path, store_path.parent, state, frozenset({"horae.example.org"}))
    with TestClient(create_app(context), base_url=LOCAL) as test_client:
        response = test_client.get(path, headers={"Host": host})
    assert response.status_code == 400
    assert response.json() == {
        "schema_version": 1,
        "error": {"kind": "invalid_host", "message": "invalid host"},
    }
    if host:
        assert host not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8080",
        "127.0.0.1",
        "127.0.0.1:8080",
        "[::1]:8080",
        "[::1]",
        "LOCALHOST",
        "horae.example.org",
        "Horae.Example.Org.",
        "horae.example.org:443",
    ],
)
def test_loopback_and_configured_hosts_are_accepted(
    root: Path, store_path: Path, state: State, host: str
) -> None:
    context = ApiContext(store_path, store_path.parent, state, frozenset({"HORAE.example.org."}))
    with TestClient(create_app(context), base_url=LOCAL) as test_client:
        assert test_client.get("/healthz", headers={"Host": host}).status_code == 200


def test_several_host_headers_are_refused(client: TestClient) -> None:
    # Two Host headers: ambiguous, refused whatever they say.
    response = client.get("/healthz", headers=[("Host", "localhost"), ("Host", REBOUND)])
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("value", "normalised"),
    [
        ("Horae.Example.Org.", "horae.example.org"),
        ("[::1]", "::1"),
        ("10.0.0.5", "10.0.0.5"),
        ("horae", "horae"),
    ],
)
def test_allowed_hosts_are_normalised(value: str, normalised: str) -> None:
    assert allowed_host(value) == normalised


@pytest.mark.parametrize(
    "value", ["", "*.example.org", "*", "a.example.org:8080", "a/b", "a b", "-a.org", "é.org"]
)
def test_an_allowed_host_must_be_a_name_or_an_address(value: str) -> None:
    with pytest.raises(ValueError, match="DNS name or an IP address"):
        allowed_host(value)


def test_the_host_of_a_header() -> None:
    assert host_of("[::1]:8080") == "::1"
    assert host_of("Horae.Example.Org.:443") == "horae.example.org"
    assert host_of(None) is None
    assert host_of("localhost:99999999") is None
