# SPDX-License-Identifier: Apache-2.0
"""The store side of `/act` (#38, migration 8): used nonces, the `_act` runs that have no
record, cleanup."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from horae.contracts import Usage
from horae.store import ACT_PIPELINE, SCHEMA_VERSION, ActTokenUsed, Store, StoreError, migrate
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
        store.prune_runs(dt.datetime.fromtimestamp(250, dt.UTC))
        assert store.act_outcome(bytes([1]) * 16) is None
        assert store.act_outcome(bytes([2]) * 16) == "applied"


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
