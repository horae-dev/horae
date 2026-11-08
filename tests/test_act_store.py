# SPDX-License-Identifier: Apache-2.0
"""The store side of `/act` (#38, migration 8): used nonces, the `_act` runs that have no
record, cleanup."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from horae.contracts import Usage
from horae.store import (
    ACT_PIPELINE,
    ACT_RETENTION_S,
    SCHEMA_VERSION,
    ActTokenUsed,
    Store,
    StoreError,
    TaskEntry,
    UnknownTask,
    migrate,
)
from horae.store._schema import load_migrations

T0 = dt.datetime(2026, 11, 8, 7, 30, tzinfo=dt.UTC)
NONCE = bytes(range(16))


def test_the_migration_adds_the_table_and_keeps_existing_runs(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db", autocommit=True)
    migrate(conn, target=7)
    conn.execute(
        "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, error, "
        "abandoned_at) VALUES ('r1', 'brief', 'failed', 0, 'x', 'y', 'gone', 'y')"
    )
    assert migrate(conn) == SCHEMA_VERSION == len(load_migrations()) == 8
    assert conn.execute("SELECT id, pipeline, status FROM runs").fetchall() == [
        ("r1", "brief", "failed")
    ]
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"act_consumed", "act_consumed_expires", "one_running_per_pipeline"} <= names
    conn.close()


def test_an_act_run_finishes_without_a_record_and_others_still_need_one(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        store.begin_run("act-1", ACT_PIPELINE, T0)
        store.finish_bare_run(
            "act-1", status="ok", at=T0, cost=Usage(tokens_in=0, tokens_out=0, usd=0.0)
        )
        run = store.run_summary("act-1")
        assert run is not None
        assert run.status == "ok"
        assert store.get_run("act-1") is None
        store.begin_run("brief-1", "brief", T0)
        with pytest.raises(StoreError):  # the CHECK: a finished pipeline run has a record
            store.finish_bare_run(
                "brief-1", status="ok", at=T0, cost=Usage(tokens_in=0, tokens_out=0, usd=0.0)
            )


def test_a_nonce_is_consumed_once(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        assert store.act_outcome(NONCE) is None
        store.act_consume(NONCE, kid=b"abcd", expires_at=100, at=T0, run_id=None, outcome="pending")
        with pytest.raises(ActTokenUsed):
            store.act_consume(
                NONCE, kid=b"abcd", expires_at=100, at=T0, run_id=None, outcome="applied"
            )
        assert store.act_outcome(NONCE) == "pending"
        store.act_set_outcome(NONCE, "applied")
        assert store.act_outcome(NONCE) == "applied"


def test_consuming_rolls_back_with_its_transaction(tmp_path: Path) -> None:
    def consume_then_fail(store: Store) -> None:
        with store.transaction():
            store.act_consume(
                NONCE, kid=b"abcd", expires_at=100, at=T0, run_id="act-1", outcome="pending"
            )
            store.journal_append(
                "act-1",
                intent=0,
                intent_type="task.update_status",
                target="t",
                undoable=False,
                at=T0,
            )
            raise RuntimeError("the journal entry failed")

    with Store.open(tmp_path / "s.db") as store:
        store.begin_run("act-1", ACT_PIPELINE, T0)
        with pytest.raises(RuntimeError, match="journal entry failed"):
            consume_then_fail(store)
        assert store.act_outcome(NONCE) is None
        assert store.journal("act-1") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"nonce": b"short"},
        {"kid": b"abc"},
        {"expires_at": -1},
        {"outcome": "done"},
    ],
)
def test_consume_refuses_bad_arguments(tmp_path: Path, kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "nonce": NONCE,
        "kid": b"abcd",
        "expires_at": 1,
        "at": T0,
        "run_id": None,
        "outcome": "pending",
        **kwargs,
    }
    nonce = arguments.pop("nonce")
    with (
        Store.open(tmp_path / "s.db") as store,
        pytest.raises(ValueError, match=r"nonce|key id|expiry|outcome"),
    ):
        store.act_consume(nonce, **arguments)  # type: ignore[arg-type]


def test_purge_and_prune_drop_long_expired_nonces_only(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        for i, exp in enumerate((100, 200, 300)):
            store.act_consume(
                bytes([i]) * 16, kid=b"abcd", expires_at=exp, at=T0, run_id=None, outcome="applied"
            )
        assert store.act_purge(200) == 1
        assert store.act_outcome(bytes([0]) * 16) is None
        assert store.act_outcome(bytes([1]) * 16) == "applied"
        # `prune_runs` keeps a row `ACT_RETENTION_S` (24 h 05 min) past its token's expiry.
        store.prune_runs(dt.datetime.fromtimestamp(250 + ACT_RETENTION_S, dt.UTC))
        assert store.act_outcome(bytes([1]) * 16) is None
        assert store.act_outcome(bytes([2]) * 16) == "applied"
        assert ACT_RETENTION_S >= 24 * 3600


def test_a_pruned_act_run_leaves_its_nonce_used(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        store.begin_run("act-1", ACT_PIPELINE, T0)
        store.act_consume(
            NONCE, kid=b"abcd", expires_at=2**40, at=T0, run_id="act-1", outcome="applied"
        )
        store.finish_bare_run(
            "act-1", status="ok", at=T0, cost=Usage(tokens_in=0, tokens_out=0, usd=0.0)
        )
        store.prune_runs(T0 + dt.timedelta(days=1))
        assert store.run_summary("act-1") is None
        assert store.act_outcome(NONCE) == "applied"  # still refused until it expires


def _schema(conn: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall())


_COUNTS = {
    "runs": "SELECT count(*) FROM runs",
    "journal": "SELECT count(*) FROM journal",
    "tasks": "SELECT count(*) FROM tasks",
    "blobs": "SELECT count(*) FROM blobs",
}


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: conn.execute(query).fetchone()[0] for table, query in _COUNTS.items()}


def test_upgrading_a_v7_store_with_data_keeps_every_row_and_key(tmp_path: Path) -> None:
    # The reviewer's upgrade test: runs, journal and tasks rows written at v7, then v8.
    conn = sqlite3.connect(tmp_path / "s.db", autocommit=True)
    migrate(conn, target=7)
    conn.executescript(
        """
        INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, error,
                          abandoned_at)
            VALUES ('r1', 'brief', 'failed', 0, 'a', 'b', 'gone', 'b');
        INSERT INTO runs (id, pipeline, status, dry_run, started_at)
            VALUES ('r2', 'other', 'running', 0, 'c');
        INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at)
            VALUES ('eval-1', 'eval:x', 'ok', 0, 'd', 'e');
        INSERT INTO blobs (sha256, data, size) VALUES (hex(zeroblob(32)), x'00', 1);
        INSERT INTO journal (run_id, seq, intent, intent_type, target, before_sha256,
                             undoable, created_at)
            VALUES ('r1', 0, 0, 'note.create', 'Daily.md', hex(zeroblob(32)), 1, 'a');
        INSERT INTO journal (run_id, seq, intent, intent_type, target, undoable, created_at)
            VALUES ('r2', 0, 0, 'notify.push', 'ntfy', 0, 'c');
        INSERT INTO tasks (fingerprint, title, status, run_id, created_at, updated_at)
            VALUES ('fp-1', 'Renew', 'todo', 'r1', 'a', 'a');
        INSERT INTO tasks (fingerprint, title, status, run_id, created_at, updated_at)
            VALUES ('fp-2', 'Call', 'done', 'r2', 'a', 'a');
        """
    )
    counts = _counts(conn)
    assert migrate(conn) == 8
    assert _counts(conn) == counts
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert conn.execute("SELECT fingerprint, recurring FROM tasks ORDER BY 1").fetchall() == [
        ("fp-1", 0),
        ("fp-2", 0),
    ]
    fresh = sqlite3.connect(tmp_path / "fresh.db", autocommit=True)
    migrate(fresh)
    assert _schema(conn) == _schema(fresh)
    fresh.close()
    # The journal's cascade and the tasks' SET NULL still reach the rebuilt runs table.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM runs WHERE id = 'r1'")
    assert conn.execute("SELECT run_id FROM journal").fetchall() == [("r2",)]
    assert conn.execute("SELECT fingerprint, run_id FROM tasks ORDER BY 1").fetchall() == [
        ("fp-1", None),
        ("fp-2", "r2"),
    ]
    conn.close()


def test_a_task_can_be_flagged_recurring(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        store.upsert_task(TaskEntry(fingerprint="fp", title="Water", status="todo"), at=T0)
        store.set_task_recurring("fp", True, at=T0)
        task = store.get_task("fp")
        assert task is not None
        assert task.recurring is True
        assert [t.recurring for t in store.list_tasks()] == [True]
        with pytest.raises(UnknownTask):
            store.set_task_recurring("nope", True, at=T0)
        with pytest.raises(ValueError, match="bool"):
            store.set_task_recurring("fp", 1, at=T0)  # type: ignore[arg-type]


def test_a_not_changed_outcome_is_recorded(tmp_path: Path) -> None:
    with Store.open(tmp_path / "s.db") as store:
        store.act_consume(NONCE, kid=b"abcd", expires_at=10, at=T0, run_id=None, outcome="pending")
        store.act_set_outcome(NONCE, "not_changed")
        assert store.act_outcome(NONCE) == "not_changed"
