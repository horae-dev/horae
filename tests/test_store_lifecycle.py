# SPDX-License-Identifier: Apache-2.0
"""Store support for the run lifecycle (#19): guarded abandon, short heartbeats, read-only."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from horae.store import (
    HEARTBEAT_BUSY_TIMEOUT_MS,
    RunNotRunning,
    RunNotStale,
    SchemaTooOldError,
    Store,
    StoreBusy,
    StoreError,
    UnknownRun,
)

T0 = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "horae.db"


@pytest.fixture
def store(db: Path) -> Iterator[Store]:
    with Store.open(db) as opened:
        yield opened


def test_abandon_is_a_compare_and_set_on_the_last_heartbeat(store: Store) -> None:
    store.begin_run("r", "p", T0)
    beat = T0 + timedelta(seconds=10)
    store.heartbeat("r", beat)
    # Read before the heartbeat: the row changed, it is left alone.
    with pytest.raises(RunNotStale, match="'r'"):
        store.abandon_run("r", at=beat, error="dead", heartbeat_before=T0)
    summary = store.run_summary("r")
    assert summary is not None
    assert summary.status == "running"
    store.abandon_run("r", at=beat, error="dead", heartbeat_before=beat)
    summary = store.run_summary("r")
    assert summary is not None
    assert (summary.status, summary.error) == ("failed", "dead")
    with pytest.raises(RunNotRunning):
        store.abandon_run("r", at=beat, error="again", heartbeat_before=beat)
    with pytest.raises(UnknownRun):
        store.abandon_run("nope", at=beat, error="x", heartbeat_before=beat)


def test_abandon_guard_uses_the_start_without_a_heartbeat(store: Store) -> None:
    store.begin_run("r", "p", T0)
    store.abandon_run("r", at=T0, error="dead", heartbeat_before=T0)
    summary = store.run_summary("r")
    assert summary is not None
    assert summary.status == "failed"


def test_heartbeat_does_not_wait_long_for_a_busy_store(db: Path, store: Store) -> None:
    store.begin_run("r", "p", T0)
    other = sqlite3.connect(db, autocommit=True)
    try:
        other.execute("BEGIN IMMEDIATE")
        begin = time.monotonic()
        with pytest.raises(StoreBusy):
            store.heartbeat("r", T0 + timedelta(seconds=1))
        assert time.monotonic() - begin < 1.0
        assert HEARTBEAT_BUSY_TIMEOUT_MS <= 200
        other.execute("ROLLBACK")
    finally:
        other.close()
    store.heartbeat("r", T0 + timedelta(seconds=2))  # the beat connection recovers
    summary = store.run_summary("r")
    assert summary is not None
    assert summary.heartbeat_at == T0 + timedelta(seconds=2)


def test_heartbeat_errors_on_the_beat_connection(store: Store) -> None:
    with pytest.raises(UnknownRun):
        store.heartbeat("nope", T0)
    store.begin_run("r", "p", T0)
    store.abandon_run("r", at=T0, error="gone")
    with pytest.raises(RunNotRunning, match="failed"):
        store.heartbeat("r", T0)


def test_heartbeat_inside_a_transaction_uses_the_main_connection(store: Store) -> None:
    store.begin_run("r", "p", T0)
    with store.transaction():
        store.heartbeat("r", T0 + timedelta(seconds=1))
    summary = store.run_summary("r")
    assert summary is not None
    assert summary.heartbeat_at == T0 + timedelta(seconds=1)


def test_read_only_store_reads_and_refuses_writes(db: Path, store: Store) -> None:
    store.begin_run("r", "p", T0)
    with Store.open(db, read_only=True) as reader:
        assert reader.read_only is True
        assert [run.id for run in reader.list_runs()] == ["r"]
        with pytest.raises(StoreError):
            reader.begin_run("s", "q", T0)
        with pytest.raises(StoreError):
            reader.heartbeat("r", T0)
    assert store.read_only is False


def test_read_only_needs_an_existing_database(db: Path) -> None:
    with pytest.raises(StoreError, match="no database"):
        Store.open(db, read_only=True)
    assert not db.parent.exists()


def test_read_only_never_migrates(db: Path) -> None:
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()  # an empty database: schema version 0
    with pytest.raises(SchemaTooOldError, match="horae run"):
        Store.open(db, read_only=True)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
    finally:
        conn.close()


def test_read_only_of_something_that_is_not_a_database(db: Path) -> None:
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a database" * 100)
    with pytest.raises(StoreError):
        Store.open(db, read_only=True)


def _files(db: Path) -> dict[str, int]:
    names = {db.name, db.name + "-wal", db.name + "-shm"}
    return {p.name: p.stat().st_size for p in db.parent.iterdir() if p.name in names}


def test_a_plain_read_only_open_may_leave_empty_wal_files_the_next_writer_removes(
    db: Path,
) -> None:
    # Decision 51 E: the API reads with `immutable=False` (plain `mode=ro`).
    with Store.open(db) as writer:
        writer.begin_run("r", "p", T0)
    assert set(_files(db)) == {"horae.db"}  # the writer's close checkpointed and cleaned up
    with Store.open(db, read_only=True, immutable=False) as reader:
        assert [run.id for run in reader.list_runs()] == ["r"]
    left = _files(db)
    # What plain `mode=ro` may leave: an empty `-wal` and a `-shm` (nothing of the data).
    assert set(left) <= {"horae.db", "horae.db-wal", "horae.db-shm"}
    assert left.get("horae.db-wal", 0) == 0
    with Store.open(db, read_only=True) as reader:  # the CLI's open still works with them
        assert [run.id for run in reader.list_runs()] == ["r"]
    with Store.open(db) as writer:
        writer.begin_run("s", "q", T0 + timedelta(minutes=1))
    assert set(_files(db)) == {"horae.db"}


def test_plain_read_only_reads_are_correct_while_a_writer_checkpoints_on_close(
    db: Path,
) -> None:
    # In one process, as in `horae serve`: a writer thread adds runs, each through its own
    # store, closed (and so checkpointed into the main file) after every write, while this
    # thread reads. Every read sees a consistent prefix of the committed runs, never fewer
    # than the last read, and never fails.
    Store.open(db).close()
    total = 60
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for i in range(total):
                with Store.open(db) as writer:
                    writer.begin_run(f"r{i:03d}", f"p{i}", T0 + timedelta(seconds=i))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    seen = 0
    reads = 0
    while thread.is_alive() or reads == 0:
        with Store.open(db, read_only=True, immutable=False) as reader:
            ids = [run.id for run in reader.list_runs(limit=total)]
        assert ids == [f"r{i:03d}" for i in reversed(range(len(ids)))]
        assert len(ids) >= seen
        seen = len(ids)
        reads += 1
    thread.join()
    assert errors == []
    with Store.open(db, read_only=True, immutable=False) as reader:
        assert len(reader.list_runs(limit=total)) == total
