# SPDX-License-Identifier: Apache-2.0
"""`GET` and `POST /act/<token>` (#38, schemas decision 53): single use, GET consumes nothing,
the origin check, the same 404 for every refused token, staleness, the apply lock, the pages
and their headers, the routing under the SSO-exempt prefix, and no token in any log or store.
Driven in process (`TestClient`); the concurrent tests use threads and processes."""

import asyncio
import base64
import datetime as dt
import hashlib
import logging
import multiprocessing
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from act_fixtures import (
    DUE,
    HOST,
    NOW,
    OTHER_KEY_VALUE,
    PUBLIC,
    TASK,
    TITLE,
    TOOL,
    Env,
    make_env,
    post_in_process,
    seed,
    token,
)
from act_fixtures import key as act_key
from horae.api.act import TOKEN_TTL_S, verify
from horae.api.act._page import CONTENT_SECURITY_POLICY, MAX_TITLE, PAGE_HEADERS
from horae.contracts.intents import TaskUpdateStatus
from horae.runner import try_acquire_apply_lock
from horae.store import Store, TaskEntry


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def _rows(store_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _dump(store_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _raw(app: Any, method: str, raw_path: str) -> tuple[int, bytes]:
    """One request straight to the ASGI app, its path as uvicorn passes it (percent-decoded,
    dot segments **not** removed: an HTTP client would normalise `..` before sending)."""
    from urllib.parse import unquote

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", HOST.encode())],
        "client": ("198.51.100.9", 4444),
        "server": (HOST, 443),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


# --- AC1: single use ----------------------------------------------------------------------------


def test_a_token_applies_once_and_is_refused_after(env: Env) -> None:
    value = token()
    with env.client() as client:
        first = client.post(f"/act/{value}")
        again = client.post(f"/act/{value}")
        shown = client.get(f"/act/{value}")
    assert first.status_code == 200
    assert "Marked as done." in first.text
    assert again.status_code == 404
    assert shown.status_code == 404
    assert len(env.sink.calls) == 1
    with Store.open(env.store_path) as store:
        task = store.get_task(TASK)
        assert task is not None
        assert task.status == "done"
        (row,) = _rows(env.store_path, "SELECT outcome, run_id FROM act_consumed")
        assert row[0] == "applied"
        run = store.run_summary(row[1])
        assert run is not None
        assert run.pipeline == "_act"
        assert run.status == "ok"
        (entry,) = store.journal(row[1])
        assert (entry.intent_type, entry.target, entry.applied, entry.undoable) == (
            "task.update_status",
            TASK,
            True,
            False,
        )
        assert entry.applied_by == TOOL


def test_an_expired_token_is_refused(env: Env) -> None:
    value = token()
    env.clock.advance(seconds=TOKEN_TTL_S)
    with env.client() as client:
        assert client.get(f"/act/{value}").status_code == 404
        assert client.post(f"/act/{value}").status_code == 404
    assert env.sink.calls == []


def test_a_token_expiring_too_far_ahead_is_refused(env: Env) -> None:
    value = token(at=NOW + 3600)  # minted by a clock an hour ahead
    with env.client() as client:
        assert client.post(f"/act/{value}").status_code == 404
    assert env.sink.calls == []


def test_two_concurrent_posts_apply_once(env: Env) -> None:
    env.sink.delay = 0.3
    env.service.lock_wait_s = 5.0
    value = token()
    app = env.app()
    statuses: list[int] = []

    def post() -> None:
        from fastapi.testclient import TestClient

        with TestClient(app, base_url=PUBLIC, raise_server_exceptions=False) as client:
            statuses.append(client.post(f"/act/{value}").status_code)

    threads = [threading.Thread(target=post) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200, 404, 404, 404]
    assert len(env.sink.calls) == 1


def test_the_nonce_alone_refuses_a_concurrent_second_use(env: Env) -> None:
    # Below the locks: two connections consuming the same nonce at once, as two processes
    # that did not take the locks would; the primary key lets exactly one through.
    from horae.store import ActTokenUsed

    nonce = bytes(range(16))
    barrier = threading.Barrier(8)
    results: list[str] = []

    def consume() -> None:
        with Store.open(env.store_path) as store:
            barrier.wait()
            try:
                store.act_consume(
                    nonce,
                    kid=b"kid1",
                    expires_at=NOW,
                    at=env.clock(),
                    run_id=None,
                    outcome="pending",
                )
            except ActTokenUsed:
                results.append("used")
            else:
                results.append("consumed")

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["consumed"] + ["used"] * 7
    with Store.open(env.store_path) as store:
        assert store.act_outcome(nonce) == "pending"


def test_two_processes_apply_once(env: Env, tmp_path: Path) -> None:
    marker = tmp_path / "applied.txt"
    value = token()
    context = multiprocessing.get_context("spawn")
    args = (str(env.state_dir), str(env.store_path), str(marker), value, 0.5)
    with context.Pool(3) as pool:
        outcomes = pool.starmap(post_in_process, [args] * 3)
    assert sorted(outcomes) == ["applied", "used", "used"]
    assert marker.read_text().splitlines() == ["task.update_status"]


def test_a_crash_after_consume_never_applies_twice(env: Env) -> None:
    value = token()
    env.sink.boom = KeyboardInterrupt()  # the process dies while the sink applies
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(env.service.act(value))
    (row,) = _rows(env.store_path, "SELECT outcome, run_id FROM act_consumed")
    assert row[0] == "pending"
    with Store.open(env.store_path) as store:
        (entry,) = store.journal(row[1])
        assert entry.applied is False  # "maybe applied"
    env.sink.boom = None
    again = asyncio.run(env.service.act(value))
    assert again.outcome == "used"
    assert len(env.sink.calls) == 1  # the crashed attempt only
    with Store.open(env.store_path) as store:
        crashed = store.run_summary(row[1])
        assert crashed is not None
        assert crashed.status == "failed"
        assert crashed.abandoned_at is not None  # recovered by the next POST


def test_cleanup_keeps_unexpired_nonces(env: Env) -> None:
    used = token()
    with env.client() as client:
        assert client.post(f"/act/{used}").status_code == 200
    # Back to todo, so a second token for the task is not stale.
    with Store.open(env.store_path) as store:
        store.set_task_status(TASK, "todo", at=env.clock())
    later = token(at=NOW + 3 * 3600)
    env.clock.advance(hours=3)
    with env.client() as client:
        assert client.post(f"/act/{later}").status_code == 200
        assert len(_rows(env.store_path, "SELECT nonce FROM act_consumed")) == 2
        assert client.post(f"/act/{used}").status_code == 404  # still refused: used
    # More than an hour past the first token's expiry its row goes; the second's stays.
    env.clock.advance(hours=23)  # NOW + 26 h: the first token expired 2 h ago
    with Store.open(env.store_path) as store:
        store.set_task_status(TASK, "todo", at=env.clock())
    third = token(at=NOW + 26 * 3600)
    with env.client() as client:
        assert client.post(f"/act/{third}").status_code == 200
    exps = sorted(row[0] for row in _rows(env.store_path, "SELECT expires_at FROM act_consumed"))
    assert exps == [NOW + 3 * 3600 + TOKEN_TTL_S, NOW + 26 * 3600 + TOKEN_TTL_S]


# --- AC4: GET shows, POST applies ----------------------------------------------------------------


def test_a_get_leaves_the_token_usable(env: Env) -> None:
    value = token()
    with env.client() as client:
        for _ in range(3):  # a link preview, a prefetch, a scanner
            shown = client.get(f"/act/{value}")
            assert shown.status_code == 200
            assert '<form method="post">' in shown.text
        assert client.head(f"/act/{value}").status_code == 405  # not served, not consumed
        assert client.post(f"/act/{value}").status_code == 200
    assert len(env.sink.calls) == 1


def test_get_writes_nothing_to_the_store(env: Env) -> None:
    value = token()
    before = _dump(env.store_path)
    files = sorted(p.name for p in env.state_dir.rglob("*"))
    with env.client() as client:
        assert client.get(f"/act/{value}").status_code == 200
        assert client.get("/act/" + "A" * 120).status_code == 404
    assert _dump(env.store_path) == before
    after = sorted(
        p.name for p in env.state_dir.rglob("*") if not p.name.endswith(("-wal", "-shm"))
    )
    assert after == [name for name in files if not name.endswith(("-wal", "-shm"))]
    assert not (env.state_dir / "locks").exists()  # no lock taken either


def test_a_cross_origin_post_is_refused(env: Env) -> None:
    value = token()
    refused = [
        {"Origin": "https://evil.example.net"},
        {"Origin": "null"},
        {"Origin": "http://horae.example.org"},
        {"Origin": "https://horae.example.org.evil.example.net"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": PUBLIC, "Sec-Fetch-Site": "cross-site"},
    ]
    with env.client() as client:
        for headers in refused:
            response = client.post(f"/act/{value}", headers=headers)
            assert response.status_code == 404, headers
        assert env.sink.calls == []
        # The token is still usable from the confirmation page (same origin).
        ok = client.post(
            f"/act/{value}", headers={"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"}
        )
    assert ok.status_code == 200
    assert len(env.sink.calls) == 1


def test_a_native_client_post_without_origin_applies(env: Env) -> None:
    # The ntfy app's `http` action sends neither Origin nor Sec-Fetch-Site (decision 1).
    with env.client() as client:
        assert client.post(f"/act/{token()}").status_code == 200
    with env.client() as client:
        seed(env.store_path, fingerprint="other:1")
        response = client.post(f"/act/{token(task='other:1')}", headers={"Sec-Fetch-Site": "none"})
    assert response.status_code == 200


def test_several_origin_headers_are_refused(env: Env) -> None:
    with env.client() as client:
        response = client.post(f"/act/{token()}", headers=[("Origin", PUBLIC), ("Origin", PUBLIC)])
    assert response.status_code == 404
    assert env.sink.calls == []


def test_the_page_has_no_script_or_external_asset_and_strict_csp(env: Env) -> None:
    value = token()
    with env.client() as client:
        pages = [
            client.get(f"/act/{value}"),
            client.post(f"/act/{value}"),
            client.post(f"/act/{value}"),
            client.get("/act/nope"),
        ]
    for page in pages:
        body = page.text.lower()
        assert "<script" not in body
        assert "javascript:" not in body
        assert "http://" not in body
        assert "https://" not in body
        assert "//" not in body
        assert "<link" not in body
        assert "<img" not in body
        assert "@import" not in body
        assert 'property="og:' not in body
        assert "twitter:" not in body
        assert '<meta name="robots" content="noindex,nofollow">' in body
        assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert CONTENT_SECURITY_POLICY.startswith("default-src 'none'; style-src 'sha256-")
        assert "form-action 'self'" in CONTENT_SECURITY_POLICY
        for name, value_ in PAGE_HEADERS.items():
            assert page.headers[name] == value_
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["x-robots-tag"] == "noindex, nofollow"
        assert "set-cookie" not in page.headers
        assert page.headers["content-type"].startswith("text/html")


def test_the_inline_style_matches_its_hash(env: Env) -> None:
    import base64
    import re

    with env.client() as client:
        body = client.get(f"/act/{token()}").text
    (style,) = re.findall(r"<style>(.*?)</style>", body, flags=re.DOTALL)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in CONTENT_SECURITY_POLICY


def test_the_page_shows_the_title_only(env: Env, tmp_path: Path) -> None:
    hostile = '<script>alert(1)</script> & "quotes" ' + "x" * 200
    with Store.open(env.store_path) as store:
        store.upsert_task(
            TaskEntry(
                fingerprint=TASK,
                title=hostile,
                status="todo",
                due=DUE,
                time="08:15",
                target="Private/Medical.md",
                source="vault.tasks",
            ),
            at=env.clock(),
        )
    with env.client() as client:
        body = client.get(f"/act/{token()}").text
    assert "<script>alert" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;quotes&quot;" in body
    shown = hostile[: MAX_TITLE - 1]
    assert ("x" * 200) not in body
    assert "…" in body
    import html

    assert html.escape(shown) + "…" in body
    assert "Due 2026-11-10" in body
    assert "08:15" not in body
    assert TASK not in body
    assert "Medical" not in body
    assert "vault.tasks" not in body
    assert "Mark as done?" in body


@pytest.mark.parametrize(
    ("duration", "label"),
    [
        ("1h", "Snooze for 1 hour"),
        ("3h", "Snooze for 3 hours"),
        ("tomorrow", "Snooze until tomorrow 09:00"),
    ],
)
def test_the_page_names_the_action(env: Env, duration: str, label: str) -> None:
    with env.client() as client:
        body = client.get(f"/act/{token('task.snooze', duration=duration)}").text
    assert f"{label}?" in body


# --- binding, CAS -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"status": "done"}, id="done-meanwhile"),
        pytest.param({"status": "snoozed", "until": dt.date(2026, 11, 20)}, id="snoozed"),
        pytest.param({"due": dt.date(2026, 11, 12)}, id="due-moved"),
    ],
)
def test_a_stale_task_is_refused_and_consumes_the_token(env: Env, change: dict[str, Any]) -> None:
    value = token()
    with Store.open(env.store_path) as store:
        if "due" in change:
            store.upsert_task(
                TaskEntry(fingerprint=TASK, title=TITLE, status="todo", due=change["due"]),
                at=env.clock(),
            )
        else:
            until = change.get("until")
            store.set_task_status(
                TASK,
                change["status"],
                until=until,
                tz="Europe/Paris" if until else None,
                at=env.clock(),
            )
    with env.client() as client:
        preview = client.get(f"/act/{value}")
        assert preview.status_code == 409
        assert "<form" not in preview.text
        stale = client.post(f"/act/{value}")
        again = client.post(f"/act/{value}")
    assert stale.status_code == 409
    assert "changed since the notification" in stale.text
    assert again.status_code == 404
    assert env.sink.calls == []
    (row,) = _rows(env.store_path, "SELECT outcome, run_id FROM act_consumed")
    assert row[0] == "stale"
    with Store.open(env.store_path) as store:
        run = store.run_summary(row[1])
        assert run is not None
        assert run.status == "failed"
        assert run.error is not None
        assert run.error.startswith("target_changed")
        assert store.journal(row[1]) == []


def test_recollection_that_keeps_the_three_fields_does_not_make_the_link_stale(env: Env) -> None:
    value = token()
    with Store.open(env.store_path) as store:  # re-collected: new title, same state
        store.upsert_task(
            TaskEntry(
                fingerprint=TASK, title="Renew the passport (urgent)", status="todo", due=DUE
            ),
            at=env.clock(),
        )
    with env.client() as client:
        assert client.post(f"/act/{value}").status_code == 200


def test_an_unknown_task_is_the_same_404_and_consumes_nothing(env: Env) -> None:
    value = token(task="github:issue:999")
    with env.client() as client:
        assert client.get(f"/act/{value}").status_code == 404
        assert client.post(f"/act/{value}").status_code == 404
    assert _rows(env.store_path, "SELECT * FROM act_consumed") == []
    assert env.sink.calls == []


def test_the_intent_is_built_by_the_core_from_the_kind(env: Env) -> None:
    done, later = token(), token("task.snooze", duration="3h")
    env.clock.advance(minutes=50)  # the snooze is computed when the button is pressed
    with env.client() as client:
        assert client.post(f"/act/{done}").status_code == 200
    with Store.open(env.store_path) as store:
        store.set_task_status(TASK, "todo", at=env.clock())
    with env.client() as client:
        response = client.post(f"/act/{later}")
    assert response.status_code == 200
    first, second = (call.intent for call in env.sink.calls)
    assert first == TaskUpdateStatus(type="task.update_status", task=TASK, status="done")
    assert isinstance(second, TaskUpdateStatus)
    assert second.status == "snoozed"
    assert second.until == env.clock().replace(microsecond=0) + dt.timedelta(hours=3)
    for call in env.sink.calls:
        assert (call.tool, call.params, call.index) == (TOOL, {"mode": "tasks"}, 0)
        assert call.actions == ()
        assert call.dedupe_key is None


def test_snooze_tomorrow_is_0900_in_the_instance_time_zone(env: Env) -> None:
    value = token("task.snooze", duration="tomorrow")
    env.clock.at = dt.datetime(2026, 11, 8, 23, 30, tzinfo=dt.UTC)  # 00:30 on the 9th in Paris
    with env.client() as client:
        response = client.post(f"/act/{value}")
    assert response.status_code == 200
    assert "Snoozed until 2026-11-10 09:00." in response.text
    (call,) = env.sink.calls
    assert isinstance(call.intent, TaskUpdateStatus)
    until = call.intent.until
    assert isinstance(until, dt.datetime)
    assert until == dt.datetime(2026, 11, 10, 8, 0, tzinfo=dt.UTC)  # 09:00 CET
    with Store.open(env.store_path) as store:
        task = store.get_task(TASK)
        assert task is not None
        assert task.status == "snoozed"
        assert task.wake_at == until


def test_a_sink_failure_consumes_the_token(env: Env) -> None:
    from horae.apply import SinkError

    env.sink.boom = SinkError("target_changed", "the task line changed")
    value = token()
    with env.client() as client:
        failed = client.post(f"/act/{value}")
        again = client.post(f"/act/{value}")
    assert failed.status_code == 502
    assert "could not be applied" in failed.text
    assert "task line changed" not in failed.text
    assert again.status_code == 404
    (row,) = _rows(env.store_path, "SELECT outcome, run_id FROM act_consumed")
    assert row[0] == "failed"
    with Store.open(env.store_path) as store:
        run = store.run_summary(row[1])
        assert run is not None
        assert run.status == "failed"
        assert run.error is not None
        assert run.error.startswith("target_changed")
        (entry,) = store.journal(row[1])
        assert entry.applied is False
        task = store.get_task(TASK)
        assert task is not None
        assert task.status == "todo"


def test_a_sink_timeout_consumes_the_token(env: Env) -> None:
    env.sink.delay = 2.0
    env.service.call_timeout_s = 0.1
    with env.client() as client:
        assert client.post(f"/act/{token()}").status_code == 502
    (row,) = _rows(env.store_path, "SELECT outcome FROM act_consumed")
    assert row[0] == "failed"


def test_an_unreachable_sink_is_503_and_consumes_nothing(env: Env) -> None:

    class Broken:
        async def __aenter__(self) -> Any:
            raise OSError("the plugin host did not start")

        async def __aexit__(self, *exc: object) -> None:
            return None

    def broken() -> Any:
        return Broken()

    env.service.sinks = broken
    value = token()
    with env.client() as client:
        assert client.post(f"/act/{value}").status_code == 503
    assert _rows(env.store_path, "SELECT * FROM act_consumed") == []
    assert _rows(env.store_path, "SELECT * FROM runs") == []


def test_a_busy_apply_lock_is_503_and_consumes_nothing(env: Env) -> None:
    value = token()
    lock = try_acquire_apply_lock(env.state_dir)
    assert lock is not None
    try:
        with env.client() as client:
            busy = client.post(f"/act/{value}")
    finally:
        lock.release()
    assert busy.status_code == 503
    assert "busy" in busy.text.lower()
    assert _rows(env.store_path, "SELECT * FROM act_consumed") == []
    with env.client() as client:
        assert client.post(f"/act/{value}").status_code == 200
    assert len(env.sink.calls) == 1


# --- AC5: the key ----------------------------------------------------------------------------


def test_rotating_the_key_refuses_every_outstanding_token(env: Env, tmp_path: Path) -> None:
    outstanding = [token(), token("task.snooze", duration="1h")]
    rotated = make_env(tmp_path / "rotated", value=OTHER_KEY_VALUE)
    rotated.store_path = env.store_path  # the same store, a new key
    rotated.service.store_path = env.store_path
    rotated.service.state_dir = env.state_dir
    with rotated.client() as client:
        for value in outstanding:
            assert client.get(f"/act/{value}").status_code == 404
            assert client.post(f"/act/{value}").status_code == 404
        fresh = token(value=OTHER_KEY_VALUE)
        assert client.post(f"/act/{fresh}").status_code == 200


def test_without_a_key_act_is_503(env: Env) -> None:
    from fastapi.testclient import TestClient

    from act_fixtures import IDLE
    from horae.api import ApiContext, create_app

    app = create_app(ApiContext(env.store_path, env.state_dir, lambda: IDLE, frozenset({HOST})))
    with TestClient(app, base_url=PUBLIC) as client:
        assert client.get(f"/act/{token()}").status_code == 503
        assert client.post(f"/act/{token()}").status_code == 503
        assert client.post("/act/garbage").status_code == 503
    with env.client(public_url=None) as client:
        assert client.post(f"/act/{token()}").status_code == 503


# --- failures and logs --------------------------------------------------------------------------


def test_every_invalid_token_gets_the_same_response(env: Env) -> None:
    valid = token()
    raw = bytearray(base64.urlsafe_b64decode(valid + "=" * (-len(valid) % 4)))
    raw[50] ^= 1
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    used = token("task.snooze", duration="1h")
    with env.client() as client:
        assert client.post(f"/act/{used}").status_code == 200
        with Store.open(env.store_path) as store:
            store.set_task_status(TASK, "todo", at=env.clock())
        cases = {
            "malformed": "/act/not-a-token",
            "truncated": f"/act/{valid[:-1]}",
            "oversized": "/act/" + "A" * 268,
            "non-base64": f"/act/{valid[:30]}.{valid[31:]}",
            "tampered": f"/act/{tampered}",
            "other-key": f"/act/{token(value=OTHER_KEY_VALUE)}",
            "expired": f"/act/{token(at=NOW - TOKEN_TTL_S)}",
            "used": f"/act/{used}",
            "unknown-task": f"/act/{token(task='nobody:1')}",
            "no-token": "/act/",
            "bare-prefix": "/act",
            "two-segments": f"/act/{valid}/x",
            "encoded-slash": f"/act/{valid[:40]}%2F{valid[40:]}",
        }
        responses = {name: client.post(path) for name, path in cases.items()}
        responses |= {f"get-{name}": client.get(path) for name, path in cases.items()}
    statuses = {name: response.status_code for name, response in responses.items()}
    assert set(statuses.values()) == {404}, statuses
    assert len({response.text for response in responses.values()}) == 1
    reference = {k: v for k, v in responses["malformed"].headers.items() if k != "date"}
    for response in responses.values():
        assert {k: v for k, v in response.headers.items() if k != "date"} == reference
    assert len(env.sink.calls) == 1  # the `used` token's own use


def test_only_the_exact_act_route_is_served_without_sso(env: Env) -> None:
    # Nothing under /act reaches /runs or /healthz, whatever the encoding (the proxy exempts
    # the prefix from SSO, PLAN §7). Sent raw: an HTTP client would remove the dot segments.
    value = token()
    app = env.app()
    with env.client() as client:
        not_found = client.get("/act/nope").content
    for path in (
        "/act/../runs",
        "/act/..%2Fruns",
        "/act/%2E%2E/runs",
        "/act/%2e%2e%2fhealthz",
        f"/act/{value}/../../runs",
        f"/act/{value}/",
        f"/act/{value}%2F..%2F..%2Fhealthz",
        "/act/./healthz",
        "/act//healthz",
        "/act/%2F",
        "/act/",
        "/act",
    ):
        for method in ("GET", "POST", "HEAD"):
            status, body = _raw(app, method, path)
            assert status == 404, (method, path)
            assert method == "HEAD" or body == not_found, (method, path)
    for path in ("/act/runs", "/act/healthz"):  # a token-shaped segment: just a bad token
        assert _raw(app, "GET", path) == (404, not_found)
    assert _raw(app, "PUT", f"/act/{value}")[0] == 405
    assert _raw(app, "DELETE", f"/act/{value}")[0] == 405
    assert env.sink.calls == []
    assert _raw(app, "GET", "/runs")[0] == 200  # the raw helper does reach other routes


def test_the_host_check_still_applies_to_act(env: Env) -> None:
    from fastapi.testclient import TestClient

    with TestClient(env.app(), base_url="https://rebinding.example.net") as client:
        response = client.post(f"/act/{token()}")
    assert response.status_code == 400
    assert env.sink.calls == []


def test_act_reads_no_sso_or_forwarded_user_header(env: Env) -> None:
    headers = {
        "X-Forwarded-User": "admin",
        "Remote-User": "admin",
        "X-Auth-Request-User": "admin",
        "Cookie": "session=abc",
        "Authorization": "Bearer x",
    }
    with env.client() as client:
        assert client.get("/act/nope", headers=headers).status_code == 404
        assert (
            client.post(f"/act/{token(value=OTHER_KEY_VALUE)}", headers=headers).status_code == 404
        )
        assert client.post(f"/act/{token()}").status_code == 200


def test_the_token_never_appears_in_logs_or_the_store(
    env: Env, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    from horae.apply import SinkError

    values = [token(), token("task.snooze", duration="1h"), token(task="nobody:1")]
    with env.client() as client:
        client.get(f"/act/{values[0]}")
        client.post(f"/act/{values[0]}")
        client.post(f"/act/{values[0]}")
        with Store.open(env.store_path) as store:
            store.set_task_status(TASK, "todo", at=env.clock())
        env.sink.boom = SinkError("sink_failed", "failed")
        client.post(f"/act/{values[1]}")
        client.post(f"/act/{values[2]}")
        client.post(f"/act/{values[0][:-2]}")
    out, err = capfd.readouterr()
    # The server's records, not the test client's (httpx logs the URL it requests).
    server = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    logged = "\n".join(r.getMessage() for r in server) + out + err
    nonces = []
    for value in values:
        assert value not in logged
        assert value[:40] not in logged
        assert value[-40:] not in logged
        parsed = verify(act_key(), value, NOW)
        assert parsed is not None
        nonces.append(parsed.nonce)
    # The log names a token by its ref only.
    ref = hashlib.sha256(nonces[0]).hexdigest()[:8]
    assert f"act POST task.done {ref} applied" in logged
    assert f"act POST task.done {ref} used" in logged
    assert "act POST invalid len=" in logged
    # The store (database and WAL files) holds the nonce, never the token or its MAC.
    data = b"".join(p.read_bytes() for p in env.state_dir.rglob("horae.db*"))
    for value in values:
        assert value.encode() not in data
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        assert raw not in data
        assert raw[-32:] not in data
    assert _rows(env.store_path, "SELECT COUNT(*) FROM act_consumed") == [(2,)]


# --- AC3: rate limit (in the app) ------------------------------------------------------------


def test_per_ip_limit_returns_429(tmp_path: Path) -> None:
    from horae.api.act._limit import RateLimiter

    clock = [0.0]
    limiter = RateLimiter(clock=lambda: clock[0])
    env = make_env(tmp_path, limiter=limiter)
    with env.client() as client:
        statuses = [client.get(f"/act/{token()}").status_code for _ in range(6)]
        limited = client.get(f"/act/{token()}")
    assert statuses == [200] * 5 + [429]
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.headers["cache-control"] == "no-store"
    clock[0] += 6.0  # 10 a minute: one more after 6 s
    with env.client() as client:
        assert client.get(f"/act/{token()}").status_code == 200
        assert client.post(f"/act/{token()}").status_code == 429


def test_invalid_tokens_cost_more(tmp_path: Path) -> None:
    from horae.api.act._limit import RateLimiter

    clock = [0.0]
    env = make_env(tmp_path, limiter=RateLimiter(clock=lambda: clock[0]))
    with env.client() as client:
        assert client.get("/act/forged").status_code == 404  # 5 - 3 = 2
        assert client.post("/act/forged").status_code == 404  # 2 - 3 = -1
        assert client.get(f"/act/{token()}").status_code == 429  # a valid one now waits
    clock[0] += 12.0  # two tokens back: -1 + 2 = 1
    with env.client() as client:
        assert client.get(f"/act/{token()}").status_code == 200


def test_the_limit_is_per_client_address(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from horae.api.act._limit import RateLimiter

    env = make_env(tmp_path, limiter=RateLimiter(clock=lambda: 0.0))
    app = env.app()
    with TestClient(app, base_url=PUBLIC, client=("198.51.100.7", 1234)) as one:
        assert [one.get(f"/act/{token()}").status_code for _ in range(6)][-1] == 429
    with TestClient(app, base_url=PUBLIC, client=("198.51.100.8", 1234)) as other:
        assert other.get(f"/act/{token()}").status_code == 200
        # An untrusted peer cannot pick its bucket with X-Forwarded-For.
        assert (
            other.get(f"/act/{token()}", headers={"X-Forwarded-For": "203.0.113.1"}).status_code
            == 200
        )


def test_the_limit_comes_before_any_decoding_or_store_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from horae.api.act import _service
    from horae.api.act._limit import RateLimiter

    env = make_env(tmp_path, limiter=RateLimiter(per_ip_burst=1, clock=lambda: 0.0))
    with env.client() as client:
        assert client.get(f"/act/{token()}").status_code == 200

        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("decoded while rate limited")

        monkeypatch.setattr(_service, "verify", boom)
        monkeypatch.setattr(Store, "open", boom)
        assert client.get(f"/act/{token()}").status_code == 429
        assert client.post(f"/act/{token()}").status_code == 429


def test_the_base_url_comes_from_config_not_the_host_header(env: Env) -> None:
    # Pages never build a URL (the form posts to the page itself), links are minted from
    # `http.public_url` (`test_act_mint`), and forwarded host headers are never read.
    forwarded = {"X-Forwarded-Host": "evil.example.net", "Forwarded": "host=evil.example.net"}
    with env.client() as client:
        page = client.get(f"/act/{token()}", headers=forwarded)
        done = client.post(f"/act/{token()}", headers=forwarded)
    assert page.status_code == 200
    assert done.status_code == 200
    for body in (page.text, done.text):
        assert "evil" not in body
        assert HOST not in body
        assert "action=" not in body
