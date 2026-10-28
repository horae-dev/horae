# SPDX-License-Identifier: Apache-2.0
"""The SQLite store: schema and migrations, connection settings, and the typed API."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from horae.contracts import RunRecord, StepRecord, Usage
from horae.contracts.intents import INTENT_TYPES
from horae.store import (
    MAX_JOURNAL_BYTES,
    MAX_LIST_RUNS,
    MAX_NESTING,
    SCHEMA_VERSION,
    InvalidRow,
    JournalTooLarge,
    Migration,
    MigrationError,
    PipelineBusy,
    RunExists,
    RunNotRunning,
    SchemaTooNewError,
    Store,
    StoreBusy,
    StoreError,
    TaskEntry,
    UnknownJournalEntry,
    UnknownRun,
    UnknownTask,
    UnsupportedSQLite,
    _store,
    load_migrations,
    migrate,
)
from horae.store._errors import is_busy, translate
from horae.store._schema import check_migration

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "schemas" / "examples" / "run-record" / "valid" / "ok.json"
T0 = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)
TABLES = {"runs", "steps", "blobs", "journal", "llm_cache", "tasks", "notifications"}


def _record(run_id: str = "run-1", **update: object) -> RunRecord:
    data = json.loads(EXAMPLE.read_text())
    data.update(id=run_id, **update)
    return RunRecord.model_validate(data)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


@contextmanager
def _raw(db: Path) -> Iterator[sqlite3.Connection]:
    """A plain connection to the database, with foreign keys on (as the store has them)."""
    conn = sqlite3.connect(db, autocommit=True)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _count(db: Path, table: str) -> int:
    queries = {
        "blobs": "SELECT count(*) FROM blobs",
        "steps": "SELECT count(*) FROM steps",
        "journal": "SELECT count(*) FROM journal",
    }
    with _raw(db) as conn:
        (n,) = conn.execute(queries[table]).fetchone()
    return int(n)


def _busy_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "horae.sqlite"


@pytest.fixture
def store(db: Path) -> Iterator[Store]:
    with Store.open(db) as s:
        yield s


# --- schema and migrations -------------------------------------------------------------------


def test_migrations_are_numbered_from_one_without_gaps() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[-1].version == SCHEMA_VERSION
    assert all(m.sql.strip() for m in migrations)
    for m in migrations:
        check_migration(m)


def test_each_migration_ends_by_setting_its_own_version() -> None:
    for m in load_migrations():
        last = [line for line in m.sql.splitlines() if line.strip()][-1]
        assert re.fullmatch(r"PRAGMA user_version = (\d+);", last), m.name
        assert int(last.split("=")[1].rstrip(";")) == m.version, m.name


def test_a_migration_that_does_not_bump_the_version_is_rolled_back(db: Path) -> None:
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(MigrationError, match="must end with"):
            migrate(conn, migrations=[Migration(1, "lazy", "CREATE TABLE x (a INTEGER);")])
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert "x" not in _tables(conn)
    finally:
        conn.close()


def test_fresh_install_creates_the_schema(db: Path) -> None:
    assert not db.exists()
    with Store.open(db) as s:
        assert s.schema_version == SCHEMA_VERSION
    conn = sqlite3.connect(db)
    try:
        assert _tables(conn) >= TABLES
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    finally:
        conn.close()


def test_reopening_is_a_noop(db: Path) -> None:
    Store.open(db).close()
    with Store.open(db) as s:
        s.save_run(_record())
    with Store.open(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        assert s.get_run("run-1") == _record()


def test_upgrade_from_v1_migrates_in_place(db: Path) -> None:
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        assert migrate(conn, target=1) == 1
        assert "llm_cache" not in _tables(conn)
        conn.execute(
            "INSERT INTO runs (id, pipeline, status, dry_run, started_at) "
            "VALUES ('old-run', 'daily-brief', 'running', 0, '2026-10-27T05:30:00.000000Z')"
        )

    with Store.open(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        summary = s.run_summary("old-run")
        assert summary is not None
        assert summary.status == "running"
        s.upsert_task(
            TaskEntry(fingerprint="t:1", title="Buy milk", status="todo", run_id="old-run"),
            at=T0,
        )
    with _raw(db) as conn:
        assert _tables(conn) >= TABLES


def test_failed_migration_rolls_back(db: Path) -> None:
    db.parent.mkdir(parents=True)
    broken = [*load_migrations()[:1], Migration(2, "broken", "CREATE TABLE x (a INTEGER); nope;")]
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(MigrationError, match="broken"):
            migrate(conn, migrations=broken)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert "x" not in _tables(conn)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_a_newer_database_is_refused(db: Path) -> None:
    Store.open(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 9999")
    conn.close()
    with pytest.raises(SchemaTooNewError, match="newer"):
        Store.open(db)


def test_migrate_rejects_bad_targets_and_gaps(db: Path) -> None:
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(ValueError, match="target"):
            migrate(conn, target=SCHEMA_VERSION + 1)
        gap = [Migration(1, "a", "SELECT 1;"), Migration(3, "c", "SELECT 1;")]
        with pytest.raises(MigrationError, match="numbered"):
            migrate(conn, migrations=gap)
        for bad in (True, -1, 1.0, "1"):
            with pytest.raises(MigrationError, match="invalid migration version"):
                migrate(conn, migrations=[Migration(bad, "a", "SELECT 1;")])  # type: ignore[arg-type]
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    finally:
        conn.close()
    legacy = sqlite3.connect(db)
    try:
        with pytest.raises(StoreError, match="autocommit"):
            migrate(legacy)
    finally:
        legacy.close()


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN; CREATE TABLE x (a INTEGER); COMMIT; PRAGMA user_version = 1;",
        "CREATE TABLE x (a INTEGER);\n  -- done\n  commit;\nPRAGMA user_version = 1;",
        "CREATE TABLE x (a INTEGER); /* close it */ END; PRAGMA user_version = 1;",
        "CREATE TABLE x (a INTEGER); ROLLBACK; PRAGMA user_version = 1;",
        "SAVEPOINT a; RELEASE a; PRAGMA user_version = 1;",
        "VACUUM; PRAGMA user_version = 1;",
        "PRAGMA journal_mode = DELETE; PRAGMA user_version = 1;",
        "PRAGMA main.foreign_keys = ON; PRAGMA user_version = 1;",
        "PRAGMA foreign_keys=OFF; PRAGMA user_version = 1;",
        "PRAGMA user_version = 1; vacuum",
    ],
)
def test_migrations_cannot_manage_transactions_or_settings(db: Path, sql: str) -> None:
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        with pytest.raises(MigrationError, match="must not contain"):
            migrate(conn, migrations=[Migration(1, "sneaky", sql)])
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert "x" not in _tables(conn)


def test_trigger_bodies_and_strings_are_not_top_level_statements(db: Path) -> None:
    sql = (
        "CREATE TABLE x (a TEXT);\n"
        "CREATE TRIGGER x_log AFTER INSERT ON x BEGIN UPDATE x SET a = 'BEGIN; COMMIT;'; END;\n"
        "INSERT INTO x (a) VALUES ('COMMIT;');\n"
        "PRAGMA user_version = 1;\n"
    )
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        assert migrate(conn, migrations=[Migration(1, "trigger", sql)]) == 1
        assert conn.execute("SELECT a FROM x").fetchall() == [("BEGIN; COMMIT;",)]


REBUILD_RUNS = """
CREATE TABLE runs_new (id TEXT PRIMARY KEY, pipeline TEXT NOT NULL, status TEXT NOT NULL) STRICT;
INSERT INTO runs_new (id, pipeline, status) SELECT id, pipeline, status FROM runs;
DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;
PRAGMA user_version = 2;
"""

DANGLING_STEP = """
INSERT INTO steps (run_id, idx, kind, status, record) VALUES ('ghost', 0, 'collect', 'ok', '{}');
PRAGMA user_version = 2;
"""


def _v1_with_a_run(conn: sqlite3.Connection) -> None:
    assert migrate(conn, target=1) == 1
    conn.execute(
        "INSERT INTO runs (id, pipeline, status, dry_run, started_at) "
        "VALUES ('r1', 'daily-brief', 'running', 0, '2026-10-27T05:30:00.000000Z')"
    )
    conn.execute(
        "INSERT INTO steps (run_id, idx, kind, status, record) "
        "VALUES ('r1', 0, 'collect', 'ok', '{}')"
    )
    digest = hashlib.sha256(b"x").hexdigest()
    conn.execute("INSERT INTO blobs (sha256, data, size) VALUES (?, ?, 1)", (digest, b"x"))
    conn.execute(
        "INSERT INTO journal (run_id, seq, intent, intent_type, target, after_sha256, "
        "undoable, created_at) VALUES ('r1', 0, 0, 'note.create', 'a.md', ?, 1, "
        "'2026-10-27T05:30:00.000000Z')",
        (digest,),
    )


def test_a_table_rebuild_migration_keeps_child_rows(db: Path) -> None:
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        _v1_with_a_run(conn)
        rebuild = [load_migrations()[0], Migration(2, "rebuild_runs", REBUILD_RUNS)]
        assert migrate(conn, migrations=rebuild) == 2
        assert conn.execute("SELECT run_id FROM steps").fetchall() == [("r1",)]
        assert conn.execute("SELECT run_id FROM journal").fetchall() == [("r1",)]
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_the_same_rebuild_with_foreign_keys_on_would_lose_them(db: Path) -> None:
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        _v1_with_a_run(conn)
        conn.executescript(REBUILD_RUNS)
        assert conn.execute("SELECT count(*) FROM steps").fetchone() == (0,)


def test_a_migration_that_leaves_dangling_references_is_rolled_back(db: Path) -> None:
    db.parent.mkdir(parents=True)
    with _raw(db) as conn:
        _v1_with_a_run(conn)
        dangling = [load_migrations()[0], Migration(2, "dangling", DANGLING_STEP)]
        with pytest.raises(MigrationError, match="dangling foreign keys"):
            migrate(conn, migrations=dangling)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT run_id FROM steps").fetchall() == [("r1",)]
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    off = sqlite3.connect(db, autocommit=True)
    try:  # foreign keys stay off when they were off
        migrate(off, migrations=[load_migrations()[0], Migration(2, "rb", REBUILD_RUNS)])
        assert off.execute("PRAGMA foreign_keys").fetchone() == (0,)
    finally:
        off.close()


class _Proxy:
    """A connection wrapper (sqlite3.Connection attributes are read-only)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.scripts = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        return self._conn.execute(sql, *args)

    def executescript(self, sql: str) -> sqlite3.Cursor:
        self.scripts += 1
        return self._conn.executescript(sql)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_a_migration_applied_by_another_process_meanwhile_is_skipped(db: Path) -> None:
    db.parent.mkdir(parents=True)

    class Racing(_Proxy):
        raced = False

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE" and not self.raced:
                self.raced = True
                with _raw(db) as other:  # the other process wins the lock first
                    migrate(other)
            return super().execute(sql, *args)

    conn = sqlite3.connect(db, autocommit=True)
    try:
        racing = Racing(conn)
        assert migrate(racing) == SCHEMA_VERSION  # type: ignore[arg-type]
        assert racing.raced
        assert racing.scripts == 0
    finally:
        conn.close()


# --- connection settings ---------------------------------------------------------------------


def test_wal_busy_timeout_and_foreign_keys(db: Path) -> None:
    with Store.open(db, busy_timeout_ms=1234) as s:
        assert s.pragma("journal_mode") == "wal"
        assert s.pragma("busy_timeout") == 1234
        assert s.pragma("foreign_keys") == 1
        assert s.pragma("synchronous") == 1  # NORMAL, safe with WAL
        with pytest.raises(ValueError, match="pragma"):
            s.pragma("user_version; DROP TABLE runs")
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="busy_timeout_ms"):
            Store.open(db, busy_timeout_ms=bad)  # type: ignore[arg-type]


def test_an_old_sqlite_is_refused(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))
    with pytest.raises(UnsupportedSQLite, match=r"3\.37"):
        Store.open(db)
    assert not db.exists()


def test_a_store_is_only_made_by_open(db: Path) -> None:
    Store.open(db).close()
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(TypeError, match=r"Store\.open"):
            Store(conn)
        with pytest.raises(ValueError, match="foreign_keys"):
            Store._from_connection(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        assert Store._from_connection(conn).schema_version == SCHEMA_VERSION
    finally:
        conn.close()
    legacy = sqlite3.connect(db)
    try:
        with pytest.raises(ValueError, match="autocommit"):
            Store._from_connection(legacy)
    finally:
        legacy.close()
    other = db.parent / "rollback.sqlite"
    with _raw(other) as conn:
        with pytest.raises(ValueError, match="WAL"):
            Store._from_connection(conn)
        conn.execute("PRAGMA journal_mode = WAL")
        with pytest.raises(ValueError, match="schema version"):
            Store._from_connection(conn)


def test_sqlite_errors_become_store_errors(db: Path) -> None:
    s = Store.open(db)
    errors: list[BaseException] = []

    def other_thread() -> None:
        try:
            s.list_runs()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert isinstance(errors[0], StoreError)
    s.close()
    with pytest.raises(StoreError, match="closed"):
        s.list_runs()
    assert isinstance(translate(sqlite3.IntegrityError("x")), InvalidRow)
    assert isinstance(translate(_busy_error()), StoreBusy)
    extended = sqlite3.OperationalError("recovering")
    extended.sqlite_errorcode = 261  # SQLITE_BUSY_RECOVERY
    assert is_busy(extended)
    assert not is_busy(sqlite3.OperationalError("no code"))


def test_foreign_keys_are_enforced_and_name_the_referent(store: Store, db: Path) -> None:
    with pytest.raises(UnknownRun, match="no-such-run") as caught:
        store.journal_append(
            "no-such-run", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
        )
    assert caught.value.run_id == "no-such-run"
    with _raw(db) as conn, pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO steps (run_id, idx, kind, status, record) "
            "VALUES ('ghost', 0, 'collect', 'ok', '{}')"
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_state_files_are_private(db: Path) -> None:
    umask = os.umask(0o022)
    try:
        with Store.open(db) as s:
            s.save_run(_record())
            assert os.umask(0o022) == 0o022  # restored after open
            assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
            for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
                assert path.exists(), path
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        os.umask(umask)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wal_switch_backs_off_while_busy(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    real = _store._wal_attempt
    attempts: list[int] = []

    def flaky(conn: sqlite3.Connection) -> str:
        attempts.append(1)
        if len(attempts) <= 3:
            raise _busy_error()
        return real(conn)

    monkeypatch.setattr(_store, "time", clock)
    monkeypatch.setattr(_store, "_wal_attempt", flaky)
    monkeypatch.setattr(_store, "_backoff", min)  # the jitter's upper bound
    with Store.open(db) as s:
        assert s.pragma("journal_mode") == "wal"
    assert clock.sleeps == [0.005, 0.01, 0.02]


def test_backoff_has_full_jitter_bounded_by_the_time_left() -> None:
    sleeps = [_store._backoff(0.1, 1.0) for _ in range(200)]
    assert all(0 <= sleep <= 0.1 for sleep in sleeps)
    assert len(set(sleeps)) > 1  # jittered, so concurrent openers spread out
    assert _store._backoff(0.1, 0.0) == 0.0


def test_wal_switch_waits_at_most_the_busy_timeout(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()

    def always_busy(conn: sqlite3.Connection) -> str:
        raise _busy_error()

    monkeypatch.setattr(_store, "time", clock)
    monkeypatch.setattr(_store, "_wal_attempt", always_busy)
    monkeypatch.setattr(_store, "_backoff", min)
    with pytest.raises(StoreBusy, match="WAL"):
        Store.open(db, busy_timeout_ms=50)
    assert clock.sleeps == pytest.approx([0.005, 0.01, 0.02, 0.015])
    assert sum(clock.sleeps) == pytest.approx(0.05)


def test_wal_switch_does_not_retry_other_errors(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()

    def broken(conn: sqlite3.Connection) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(_store, "time", clock)
    monkeypatch.setattr(_store, "_wal_attempt", broken)
    with pytest.raises(StoreError, match="disk I/O") as caught:
        Store.open(db)
    assert not isinstance(caught.value, StoreBusy)
    assert clock.sleeps == []


# --- transactions ----------------------------------------------------------------------------


def test_a_failing_inner_block_rolls_back_only_itself(store: Store) -> None:
    with store.transaction():
        store.save_run(_record("a"))
        with suppress(RuntimeError), store.transaction():
            store.save_run(_record("b"))
            raise RuntimeError("inner")
        with store.transaction(), store.transaction():
            store.save_run(_record("c"))
    assert [r.id for r in store.list_runs()] == ["c", "a"]


def test_transactions_nest_up_to_the_cap(store: Store) -> None:
    def nest(depth: int) -> None:
        with store.transaction():
            if depth == 0:
                store.save_run(_record())
            else:
                nest(depth - 1)

    # The outer transaction, then MAX_NESTING savepoints (the last one is save_run's own).
    nest(MAX_NESTING - 1)
    assert store.get_run("run-1") is not None
    with pytest.raises(StoreError, match="nested"):
        nest(MAX_NESTING)
    assert store.list_runs() == [store.run_summary("run-1")]
    store.save_run(_record("run-2"))  # the depth was reset


def test_a_failing_commit_rolls_back_and_raises(store: Store) -> None:
    class FailingCommit(_Proxy):
        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    real = store._conn
    store._conn = FailingCommit(real)  # type: ignore[assignment]
    with pytest.raises(StoreError, match="disk I/O"):
        store.save_run(_record())
    store._conn = real
    assert not real.in_transaction
    assert store.get_run("run-1") is None
    store.save_run(_record())
    assert store.get_run("run-1") is not None


# --- runs and steps --------------------------------------------------------------------------


def test_save_and_get_run_round_trips_the_contract(store: Store) -> None:
    record = _record()
    store.save_run(record)
    assert store.get_run("run-1") == record
    assert store.get_run("missing") is None
    summary = store.run_summary("run-1")
    assert summary is not None
    assert summary.pipeline == "daily-brief"
    assert summary.status == "ok"
    assert summary.dry_run is False
    assert summary.started_at == T0
    assert summary.finished_at == T0 + timedelta(seconds=12)
    assert summary.duration == timedelta(seconds=12)
    assert summary.cost == Usage(tokens_in=812, tokens_out=64, usd=0.004)
    assert store.run_summary("missing") is None


def test_begin_then_finish_a_run(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    summary = store.run_summary("run-1")
    assert summary is not None
    assert summary.status == "running"
    assert summary.finished_at is None
    assert summary.duration is None
    assert (summary.pid, summary.heartbeat_at) == (os.getpid(), T0)
    assert summary.host
    assert store.get_run("run-1") is None
    with pytest.raises(RunExists, match="already exists"):
        store.begin_run("run-1", "remind", T0)
    store.save_run(_record(status="degraded", error="model timed out"))
    record = store.get_run("run-1")
    assert record is not None
    assert record.status == "degraded"
    summary = store.run_summary("run-1")
    assert summary is not None
    assert summary.error == "model timed out"


def test_save_run_refuses_to_change_what_identifies_the_run(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    for update in (
        {"pipeline": "remind"},
        {"dry_run": True, "changes": []},
        {"started_at": "2026-10-27T05:29:00Z"},
    ):
        with pytest.raises(InvalidRow, match="exists"):
            store.save_run(_record(**update))
    assert store.get_run("run-1") is None


def test_save_run_overwrites_for_undo(store: Store) -> None:
    record = _record()
    store.save_run(record)
    store.save_run(record.model_copy(update={"status": "undone"}))
    assert [r.status for r in store.list_runs()] == ["undone"]


def test_one_running_run_per_pipeline(store: Store) -> None:
    store.begin_run("a", "daily-brief", T0)
    with pytest.raises(PipelineBusy, match="already running") as caught:
        store.begin_run("b", "daily-brief", T0)
    assert (caught.value.pipeline, caught.value.run_id) == ("daily-brief", "a")
    store.begin_run("c", "remind", T0)
    store.save_run(_record("a"))
    store.begin_run("b", "daily-brief", T0)
    store.abandon_run("b", at=T0, error="killed")
    store.begin_run("d", "daily-brief", T0)


def test_the_pipeline_lock_is_in_the_schema(store: Store, db: Path) -> None:
    store.begin_run("a", "daily-brief", T0)
    with _raw(db) as conn, pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO runs (id, pipeline, status, dry_run, started_at) "
            "VALUES ('b', 'daily-brief', 'running', 0, '2026-10-27T05:30:00.000000Z')"
        )


def test_heartbeat_stale_runs_and_abandon(store: Store) -> None:
    store.begin_run("old", "daily-brief", T0, pid=4242, host="box")
    store.begin_run("live", "remind", T0)
    store.heartbeat("live", T0 + timedelta(minutes=9))
    stale = store.stale_runs(T0 + timedelta(minutes=5))
    assert [(r.id, r.pid, r.host) for r in stale] == [("old", 4242, "box")]
    store.journal_append(
        "old", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
    )

    later = T0 + timedelta(minutes=10)
    store.abandon_run("old", at=later, error="process 4242 is gone")
    summary = store.run_summary("old")
    assert summary is not None
    assert (summary.status, summary.abandoned_at, summary.finished_at) == ("failed", later, later)
    assert summary.error == "process 4242 is gone"
    assert store.get_run("old") is None
    assert len(store.journal("old")) == 1  # kept for undo
    assert [r.id for r in store.stale_runs(T0 + timedelta(minutes=5))] == []

    with pytest.raises(RunNotRunning, match="failed") as caught:
        store.heartbeat("old", later)
    assert caught.value.status == "failed"
    with pytest.raises(RunNotRunning):
        store.abandon_run("old", at=later, error="again")
    with pytest.raises(UnknownRun):
        store.heartbeat("missing", later)
    with pytest.raises(UnknownRun):
        store.abandon_run("missing", at=later, error="x")
    with pytest.raises(InvalidRow, match="abandoned"):
        store.save_run(_record("old"))
    with pytest.raises(InvalidRow):
        store.abandon_run("live", at=later, error="")


def test_run_rows_are_checked_by_the_schema(store: Store, db: Path) -> None:
    bad = [
        # running with a record
        "INSERT INTO runs (id, pipeline, status, dry_run, started_at, record) "
        "VALUES ('x', 'p', 'running', 0, '2026-10-27T05:30:00.000000Z', '{}')",
        # finished without a record and not abandoned
        "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at) "
        "VALUES ('x', 'p', 'ok', 0, '2026-10-27T05:30:00.000000Z', '2026-10-27T05:30:00.000000Z')",
        # abandoned without an error
        "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, abandoned_at) "
        "VALUES ('x', 'p', 'failed', 0, '2026-10-27T05:30:00.000000Z', "
        "'2026-10-27T05:30:00.000000Z', '2026-10-27T05:30:00.000000Z')",
    ]
    with _raw(db) as conn:
        for sql in bad:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                conn.execute(sql)


def test_list_runs_filters_and_orders_newest_first(store: Store) -> None:
    for i in range(5):
        start = T0 + timedelta(hours=i)
        store.save_run(
            _record(
                f"run-{i}",
                pipeline="remind" if i % 2 else "daily-brief",
                started_at=start.isoformat(),
                finished_at=(start + timedelta(seconds=1)).isoformat(),
            )
        )
    store.begin_run("run-5", "remind", T0 + timedelta(hours=5), dry_run=True)
    assert [r.id for r in store.list_runs()] == [f"run-{i}" for i in range(5, -1, -1)]
    assert [r.id for r in store.list_runs(pipeline="remind")] == ["run-5", "run-3", "run-1"]
    assert [r.id for r in store.list_runs(status="running")] == ["run-5"]
    assert [r.id for r in store.list_runs(pipeline="remind", status="ok", limit=1)] == ["run-3"]
    assert store.list_runs(limit=0) == []
    assert len(store.list_runs(limit=MAX_LIST_RUNS)) == 6
    for bad in (-1, MAX_LIST_RUNS + 1, True, 1.0):
        with pytest.raises(ValueError, match="limit"):
            store.list_runs(limit=bad)  # type: ignore[arg-type]


def test_timestamps_keep_the_instant_across_offsets(store: Store) -> None:
    paris = timezone(timedelta(hours=1))
    store.begin_run("run-1", "daily-brief", datetime(2026, 10, 27, 6, 30, tzinfo=paris))
    summary = store.run_summary("run-1")
    assert summary is not None
    assert summary.started_at == T0
    with pytest.raises(ValueError, match="naive"):
        store.begin_run("run-2", "remind", datetime(2026, 10, 27, 5, 30))


def test_steps_are_recorded_incrementally_while_running(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    step = StepRecord(kind="collect", status="ok", started_at=T0, duration_ms=5)
    store.record_step("run-1", 0, step)
    store.record_step("run-1", 0, step.model_copy(update={"status": "failed"}))
    with pytest.raises(InvalidRow, match="next step is 1"):
        store.record_step("run-1", 2, step)
    store.record_step("run-1", 1, step)
    assert [s.status for s in store.steps("run-1")] == ["failed", "ok"]
    with pytest.raises(ValueError, match="index"):
        store.record_step("run-1", -1, step)
    with pytest.raises(UnknownRun):
        store.record_step("missing", 0, step)
    assert store.steps("missing") == []


def test_steps_cannot_drift_from_the_record(store: Store, db: Path) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    store.record_step("run-1", 0, StepRecord(kind="llm", status="failed"))
    record = _record()
    store.save_run(record)
    assert store.steps("run-1") == record.steps
    assert _count(db, "steps") == len(record.steps)  # rewritten from the record
    with pytest.raises(RunNotRunning):
        store.record_step("run-1", 0, StepRecord(kind="llm", status="failed"))
    with _raw(db) as conn:
        conn.execute("DELETE FROM steps")
    assert store.steps("run-1") == record.steps  # read from the record


# --- journal ---------------------------------------------------------------------------------


def test_journal_stores_contents_by_digest(store: Store, db: Path) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    created = store.journal_append(
        "run-1", intent=0, intent_type="note.create", target="a.md", after=b"new\n", at=T0
    )
    edited = store.journal_append(
        "run-1",
        intent=1,
        intent_type="note.append_section",
        target="a.md",
        before=b"new\n",
        after=b"new\nmore\x00",
        at=T0,
    )
    pushed = store.journal_append(
        "run-1", intent=2, intent_type="notify.push", target="ntfy", at=T0
    )
    assert [e.seq for e in (created, edited, pushed)] == [0, 1, 2]
    assert created.before_sha256 is None
    assert created.after_sha256 == hashlib.sha256(b"new\n").hexdigest()
    assert edited.before_sha256 == created.after_sha256
    assert edited.after_sha256 is not None
    assert store.blob(edited.after_sha256) == b"new\nmore\x00"
    assert store.blob(hashlib.sha256(b"never").hexdigest()) is None
    with pytest.raises(ValueError, match="sha256"):
        store.blob("not-a-digest")
    assert _count(db, "blobs") == 2  # b"new\n" once, though it is referenced twice
    assert (created.undoable, pushed.undoable) == (True, False)
    assert not any(e.applied for e in store.journal("run-1"))

    store.journal_mark_applied(created.id)
    entries = store.journal("run-1")
    assert [e.target for e in entries] == ["a.md", "a.md", "ntfy"]
    assert [e.applied for e in entries] == [True, False, False]
    assert entries[1] == edited
    assert entries[0].created_at == T0
    with pytest.raises(UnknownJournalEntry):
        store.journal_mark_applied(9999)
    assert store.journal("missing") == []


def test_journal_entries_are_undone_once(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    entry = store.journal_append(
        "run-1", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
    )
    push = store.journal_append("run-1", intent=1, intent_type="notify.push", target="n", at=T0)
    later = T0 + timedelta(hours=1)
    store.journal_mark_undone(entry.id, at=later)
    assert store.journal("run-1")[0].undone_at == later
    with pytest.raises(InvalidRow, match="already undone"):
        store.journal_mark_undone(entry.id, at=later)
    with pytest.raises(InvalidRow, match="not undoable"):
        store.journal_mark_undone(push.id, at=later)
    with pytest.raises(UnknownJournalEntry):
        store.journal_mark_undone(9999, at=later)


def test_journal_checks_the_intent(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    for intent_type in INTENT_TYPES:
        entry = store.journal_append(
            "run-1", intent=0, intent_type=intent_type, target="t", after=b"x", at=T0
        )
        assert entry.undoable is (intent_type not in {"notify.push", "http.post"})
    with pytest.raises(ValueError, match="unknown intent type"):
        store.journal_append("run-1", intent=0, intent_type="note.delete", target="t", at=T0)  # type: ignore[arg-type]
    for intent_type in ("notify.push", "http.post"):
        with pytest.raises(ValueError, match="cannot be undone"):
            store.journal_append(
                "run-1", intent=0, intent_type=intent_type, target="t", undoable=True, at=T0
            )
    with pytest.raises(ValueError, match="needs `before` or `after`"):
        store.journal_append("run-1", intent=0, intent_type="note.create", target="t", at=T0)
    kept = store.journal_append(
        "run-1", intent=0, intent_type="note.create", target="t", undoable=False, at=T0
    )
    assert kept.undoable is False
    with pytest.raises(JournalTooLarge):
        store.journal_append(
            "run-1",
            intent=0,
            intent_type="note.create",
            target="t",
            before=b"\x00" * (MAX_JOURNAL_BYTES + 1),
            at=T0,
        )


def test_journal_rows_are_checked_by_the_schema(store: Store, db: Path) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    bad = [
        "INSERT INTO journal (run_id, seq, intent, intent_type, target, undoable, created_at) "
        "VALUES ('run-1', 0, 0, 'note.delete', 't', 0, '2026-10-27T05:30:00.000000Z')",
        "INSERT INTO journal (run_id, seq, intent, intent_type, target, undoable, created_at) "
        "VALUES ('run-1', 0, 0, 'notify.push', 't', 1, '2026-10-27T05:30:00.000000Z')",
        "INSERT INTO journal (run_id, seq, intent, intent_type, target, undoable, created_at) "
        "VALUES ('run-1', 0, 0, 'note.create', 't', 1, '2026-10-27T05:30:00.000000Z')",
    ]
    with _raw(db) as conn:
        for sql in bad:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                conn.execute(sql)


def test_journal_needs_a_running_run(store: Store) -> None:
    store.save_run(_record())
    with pytest.raises(RunNotRunning):
        store.journal_append(
            "run-1", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
        )


def test_journal_and_run_commit_together(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)

    def journal_save_then_crash() -> None:
        with store.transaction():
            store.journal_append(
                "run-1", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
            )
            store.save_run(_record())
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        journal_save_then_crash()
    assert store.journal("run-1") == []
    assert store.get_run("run-1") is None
    with store.transaction(), store.transaction():
        store.save_run(_record())
    assert store.get_run("run-1") is not None


# --- retention -------------------------------------------------------------------------------


def test_prune_runs_cascades_and_collects_orphan_blobs(store: Store, db: Path) -> None:
    start = T0 - timedelta(days=30)
    for run_id, pipeline, began, content in (
        ("old", "daily-brief", start, b"old"),
        ("new", "remind", T0, b"new"),
    ):
        store.begin_run(run_id, pipeline, began)
        store.journal_append(
            run_id, intent=0, intent_type="note.create", target="a.md", after=content, at=T0
        )
        store.journal_append(
            run_id, intent=1, intent_type="note.create", target="b.md", after=b"shared", at=T0
        )
    store.begin_run("running", "other", T0 - timedelta(days=30))
    store.upsert_task(
        TaskEntry(fingerprint="t:1", title="Task", status="todo", run_id="old"), at=T0
    )
    store.record_notification("n:1", at=T0, run_id="old")
    store.save_run(_record("old", started_at=start.isoformat(), finished_at=start.isoformat()))
    store.save_run(_record("new", pipeline="remind"))
    assert _count(db, "blobs") == 3

    assert store.prune_runs(T0 - timedelta(days=7)) == 1
    assert store.run_summary("old") is None
    assert store.journal("old") == []
    assert [r.id for r in store.list_runs()] == ["new", "running"]
    assert _count(db, "blobs") == 2  # b"old" collected, b"shared" still referenced
    assert store.blob(hashlib.sha256(b"old").hexdigest()) is None
    task = store.get_task("t:1")
    assert task is not None
    assert task.run_id is None
    notification = store.get_notification("n:1")
    assert notification is not None
    assert notification.run_id is None


# --- LLM cache -------------------------------------------------------------------------------


def test_llm_cache_by_request_hash_with_expiry(store: Store) -> None:
    key = hashlib.sha256(b"request").hexdigest()
    usage = Usage(tokens_in=10, tokens_out=2, usd=0.001)
    assert store.cache_get(key, now=T0) is None
    store.cache_put(
        key,
        skill="daily-brief/rank-and-word",
        skill_version="1",
        model="example-model",
        response={"items": ["Review PR 42"]},
        usage=usage,
        at=T0,
        expires_at=T0 + timedelta(days=1),
    )
    hit = store.cache_get(key, now=T0 + timedelta(hours=1))
    assert hit is not None
    assert hit.response == {"items": ["Review PR 42"]}
    assert hit.usage == usage
    assert (hit.skill, hit.skill_version, hit.model) == (
        "daily-brief/rank-and-word",
        "1",
        "example-model",
    )
    assert hit.created_at == T0
    assert store.cache_get(key, now=T0 + timedelta(days=2)) is None

    forever = hashlib.sha256(b"other").hexdigest()
    store.cache_put(
        forever, skill="s/k", skill_version="2", model="m", response=[1], usage=usage, at=T0
    )
    assert store.cache_purge_expired(T0 + timedelta(days=2)) == 1
    assert store.cache_get(forever, now=T0 + timedelta(days=365)) is not None
    with pytest.raises(ValueError, match="sha256"):
        store.cache_get("not-a-hash", now=T0)


def test_cache_purge(store: Store) -> None:
    usage = Usage(tokens_in=1, tokens_out=1, usd=0)
    keys = [hashlib.sha256(bytes([i])).hexdigest() for i in range(4)]
    for i, key in enumerate(keys):
        store.cache_put(
            key,
            skill="a/x" if i % 2 else "b/y",
            skill_version="1",
            model="m",
            response=i,
            usage=usage,
            at=T0 + timedelta(days=i),
        )
    assert store.cache_purge(skill="a/x", created_before=T0 + timedelta(days=2)) == 1
    assert store.cache_purge(created_before=T0 + timedelta(days=1)) == 1
    assert store.cache_purge(skill="b/y") == 1
    assert store.cache_purge() == 1
    assert all(store.cache_get(key, now=T0) is None for key in keys)


# --- task index and notification state -------------------------------------------------------


def test_task_index_keyed_by_fingerprint(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    task = TaskEntry(
        fingerprint="calendar:3b1f0c",
        title="Prepare slides",
        status="todo",
        due=date(2026, 10, 28),
        time="19:00",
        target="Tasks.assistant.md",
        source="calendar.events",
        run_id="run-1",
    )
    store.upsert_task(task, at=T0)
    assert store.get_task("calendar:3b1f0c") == task
    assert store.get_task("missing") is None
    snoozed = TaskEntry(
        fingerprint="calendar:3b1f0c",
        title="Prepare slides",
        status="snoozed",
        snoozed_until=T0 + timedelta(days=1),
    )
    store.upsert_task(snoozed, at=T0 + timedelta(hours=1))
    assert store.get_task("calendar:3b1f0c") == snoozed
    store.upsert_task(TaskEntry(fingerprint="mail:1", title="Reply", status="todo"), at=T0)
    store.upsert_task(
        TaskEntry(
            fingerprint="mail:2",
            title="Sign form",
            status="snoozed",
            snoozed_until=date(2026, 11, 1),
            snooze_tz="Europe/Paris",
        ),
        at=T0,
    )
    assert [t.fingerprint for t in store.list_tasks()] == ["calendar:3b1f0c", "mail:1", "mail:2"]
    assert [t.fingerprint for t in store.list_tasks(status="snoozed")] == [
        "calendar:3b1f0c",
        "mail:2",
    ]
    with pytest.raises(UnknownRun):
        store.upsert_task(
            TaskEntry(fingerprint="x:1", title="X", status="todo", run_id="missing"), at=T0
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "snoozed"},
        {"status": "todo", "snoozed_until": date(2026, 11, 1), "snooze_tz": "UTC"},
        {"status": "todo", "time": "19:00"},
        {"status": "snoozed", "snoozed_until": date(2026, 11, 1)},
        {"status": "snoozed", "snoozed_until": T0, "snooze_tz": "UTC"},
        {"status": "snoozed", "snoozed_until": date(2026, 11, 1), "snooze_tz": "Mars/Base"},
        {"status": "todo", "fingerprint": "has space"},
        {"status": "todo", "title": "two\nlines"},
        {"status": "todo", "source": "not a plugin ref"},
        {"status": "later"},
    ],
)
def test_task_entries_are_validated(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TaskEntry.model_validate({"fingerprint": "t:1", "title": "Task", **fields})


def test_snooze_wakes_at_the_start_of_the_day_in_its_zone(store: Store) -> None:
    store.upsert_task(TaskEntry(fingerprint="t:1", title="One", status="todo"), at=T0)
    store.upsert_task(TaskEntry(fingerprint="t:2", title="Two", status="todo"), at=T0)
    store.begin_run("run-1", "remind", T0)

    store.set_task_status(
        "t:1", "snoozed", until=date(2026, 11, 1), tz="Europe/Paris", at=T0, run_id="run-1"
    )
    task = store.get_task("t:1")
    assert task is not None
    assert (task.status, task.snoozed_until, task.snooze_tz) == (
        "snoozed",
        date(2026, 11, 1),
        "Europe/Paris",
    )
    assert task.run_id == "run-1"
    paris_midnight = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # CET is UTC+1 in November
    assert task.wake_at == paris_midnight

    store.set_task_status("t:2", "snoozed", until=T0 + timedelta(hours=2), at=T0)
    assert store.due_snoozed(T0 + timedelta(hours=1)) == []
    assert [t.fingerprint for t in store.due_snoozed(T0 + timedelta(hours=2))] == ["t:2"]
    assert [t.fingerprint for t in store.due_snoozed(paris_midnight)] == ["t:2", "t:1"]

    store.set_task_status("t:1", "done", at=T0)
    done = store.get_task("t:1")
    assert done is not None
    assert (done.status, done.snoozed_until, done.wake_at, done.run_id) == (
        "done",
        None,
        None,
        "run-1",
    )
    assert [t.fingerprint for t in store.due_snoozed(paris_midnight)] == ["t:2"]


def test_set_task_status_errors(store: Store) -> None:
    store.upsert_task(TaskEntry(fingerprint="t:1", title="One", status="todo"), at=T0)
    with pytest.raises(UnknownTask, match="missing"):
        store.set_task_status("missing", "done", at=T0)
    with pytest.raises(UnknownRun):
        store.set_task_status("t:1", "done", at=T0, run_id="missing")
    with pytest.raises(ValueError, match="until"):
        store.set_task_status("t:1", "done", until=T0, at=T0)
    with pytest.raises(ValueError, match="until"):
        store.set_task_status("t:1", "snoozed", at=T0)
    with pytest.raises(ValueError, match="time zone"):
        store.set_task_status("t:1", "snoozed", until=date(2026, 11, 1), at=T0)
    with pytest.raises(ValueError, match="time zone"):
        store.set_task_status("t:1", "done", tz="UTC", at=T0)
    with pytest.raises(ValueError, match="status"):
        store.set_task_status("t:1", "later", at=T0)  # type: ignore[arg-type]
    task = store.get_task("t:1")
    assert task is not None
    assert task.status == "todo"


def test_task_rows_are_checked_by_the_schema(store: Store, db: Path) -> None:
    with _raw(db) as conn, pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO tasks (fingerprint, title, status, created_at, updated_at) "
            "VALUES ('x:1', 'Bad', 'snoozed', '2026-10-27T05:30:00.000000Z', "
            "'2026-10-27T05:30:00.000000Z')"
        )


def test_notification_state_counts_sends(store: Store) -> None:
    store.begin_run("run-1", "remind", T0)
    assert store.get_notification("remind:t:1") is None
    first = store.record_notification("remind:t:1", at=T0, run_id="run-1")
    assert (first.count, first.first_sent_at, first.last_sent_at) == (1, T0, T0)
    later = T0 + timedelta(minutes=5)
    again = store.record_notification("remind:t:1", at=later)
    assert (again.count, again.first_sent_at, again.last_sent_at) == (2, T0, later)
    assert again.run_id == "run-1"
    assert store.get_notification("remind:t:1") == again
    with pytest.raises(UnknownRun):
        store.record_notification("remind:t:2", at=T0, run_id="missing")
    with pytest.raises(InvalidRow):
        store.record_notification("", at=T0)


# --- concurrency -----------------------------------------------------------------------------


def test_concurrent_threads_write_different_runs(db: Path) -> None:
    Store.open(db).close()
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            with Store.open(db) as s:
                for i in range(15):
                    run_id = f"t{n}-{i}"
                    s.begin_run(run_id, f"pipe-{n}", T0)
                    s.journal_append(
                        run_id,
                        intent=0,
                        intent_type="note.create",
                        target="a.md",
                        after=b"x",
                        at=T0,
                    )
                    s.save_run(_record(run_id, pipeline=f"pipe-{n}"))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Store.open(db) as s:
        assert len(s.list_runs(limit=1000)) == 8 * 15
    assert _count(db, "blobs") == 1


WRITER = """
import sys
from horae.contracts import RunRecord
from horae.store import Store
path, prefix, record = sys.argv[1], sys.argv[2], sys.argv[3]
template = RunRecord.model_validate_json(record)
with Store.open(path) as s:
    for i in range(20):
        s.save_run(template.model_copy(update={"id": f"{prefix}-{i}"}))
"""


def test_concurrent_processes_write_different_runs(db: Path) -> None:
    db.parent.mkdir(parents=True)
    record = _record().model_dump_json()
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", WRITER, str(db), f"p{n}", record],
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(8)
    ]
    stderr = [p.communicate(timeout=60)[1] for p in procs]
    assert [p.returncode for p in procs] == [0] * 8, stderr
    with Store.open(db) as s:
        assert len(s.list_runs(limit=1000)) == 8 * 20


BEGINNER = """
import sys
from datetime import UTC, datetime
from horae.store import PipelineBusy, Store
path, run_id = sys.argv[1], sys.argv[2]
with Store.open(path) as s:
    try:
        s.begin_run(run_id, "daily-brief", datetime(2026, 10, 27, 5, 30, tzinfo=UTC))
    except PipelineBusy as exc:
        print("busy", exc.run_id)
    else:
        print("ok")
"""


def _begin_concurrently(db: Path, count: int, start: Path | None = None) -> list[str]:
    """Run `count` BEGINNER processes on `db` (each waiting for `start` to exist first, when
    given); every child's stderr is printed (pytest shows it on failure)."""
    script = BEGINNER
    if start is not None:
        wait = "import os, time\nwhile not os.path.exists(sys.argv[3]):\n    time.sleep(0.001)\n"
        script = BEGINNER.replace("with Store.open", wait + "with Store.open", 1)
    extra = [] if start is None else [str(start)]
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", script, str(db), f"p{n}", *extra],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(count)
    ]
    if start is not None:
        start.touch()
    outputs = [p.communicate(timeout=60) for p in procs]
    for n, (_, err) in enumerate(outputs):
        if err:
            print(f"--- p{n} stderr ---\n{err}")
    assert [p.returncode for p in procs] == [0] * count, outputs
    results = {f"p{n}": out.strip() for n, (out, _) in enumerate(outputs)}
    winners = [run_id for run_id, out in results.items() if out == "ok"]
    assert len(winners) == 1, results
    assert sorted(results.values()) == sorted(["ok"] + [f"busy {winners[0]}"] * (count - 1))
    return winners


def test_concurrent_processes_begin_the_same_pipeline_once(db: Path) -> None:
    db.parent.mkdir(parents=True)
    winners = _begin_concurrently(db, 4)
    with Store.open(db) as s:
        assert [r.id for r in s.list_runs()] == winners


def test_concurrent_first_opens_under_stress(tmp_path: Path) -> None:
    """8 processes released at once on a new database, 5 rounds: one initialises it at a
    time (the init lock), exactly one begins the pipeline."""
    for round_ in range(5):
        db = tmp_path / f"round-{round_}" / "state" / "horae.db"
        db.parent.mkdir(parents=True)
        winners = _begin_concurrently(db, 8, start=tmp_path / f"go-{round_}")
        with Store.open(db) as s:
            assert [r.id for r in s.list_runs()] == winners
        assert (db.parent / "horae.db.init.lock").exists()


def test_first_open_waits_for_the_init_lock(db: Path) -> None:
    db.parent.mkdir(parents=True)
    lock = db.with_name(db.name + ".init.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(StoreBusy, match="init lock"):
            Store.open(db, busy_timeout_ms=50)
        released = threading.Timer(0.2, os.close, args=(fd,))
        released.start()
        try:
            Store.open(db, busy_timeout_ms=5000).close()  # waits, then initialises
        finally:
            released.join()
        fd = -1
    finally:
        if fd != -1:
            os.close(fd)
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600
    os.unlink(lock)
    Store.open(db, busy_timeout_ms=50).close()  # initialised: the fast path takes no lock
    assert not lock.exists()


def test_read_only_opens_never_create_the_init_lock(db: Path) -> None:
    Store.open(db).close()
    lock = db.with_name(db.name + ".init.lock")
    os.unlink(lock)
    with Store.open(db, read_only=True):
        pass
    assert not lock.exists()


def test_the_init_lock_is_never_a_symlink(db: Path, tmp_path: Path) -> None:
    db.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    os.symlink(target, db.with_name(db.name + ".init.lock"))
    with pytest.raises(StoreError, match="init lock"):
        Store.open(db)
    assert not target.exists()


def test_a_writer_waits_for_the_lock_instead_of_failing(db: Path) -> None:
    Store.open(db).close()
    holder = sqlite3.connect(db, autocommit=True, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    released = threading.Timer(0.3, holder.execute, args=("COMMIT",))
    released.start()
    try:
        with Store.open(db, busy_timeout_ms=5000) as s:
            s.save_run(_record())
            assert s.get_run("run-1") is not None
    finally:
        released.join()
        holder.close()


def test_busy_timeout_expiry_is_store_busy(db: Path) -> None:
    Store.open(db).close()
    holder = sqlite3.connect(db, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Store.open(db, busy_timeout_ms=50) as s, pytest.raises(StoreBusy, match="locked"):
            s.save_run(_record())
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _rollback_journal_db_with_reader(db: Path) -> sqlite3.Connection:
    """A database still in rollback-journal mode, with a reader holding a shared lock."""
    db.parent.mkdir(parents=True)
    setup = sqlite3.connect(db, autocommit=True)
    setup.execute("CREATE TABLE t (a INTEGER)")
    setup.close()
    reader = sqlite3.connect(db, autocommit=True, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    return reader


def test_switching_to_wal_waits_for_readers(db: Path) -> None:
    reader = _rollback_journal_db_with_reader(db)
    released = threading.Timer(0.2, reader.execute, args=("COMMIT",))
    released.start()
    try:
        with Store.open(db, busy_timeout_ms=5000) as s:
            assert s.pragma("journal_mode") == "wal"
    finally:
        released.join()
        reader.close()


def test_switching_to_wal_gives_up_after_the_busy_timeout(db: Path) -> None:
    reader = _rollback_journal_db_with_reader(db)
    try:
        with pytest.raises(StoreBusy, match="WAL"):
            Store.open(db, busy_timeout_ms=50)
    finally:
        reader.execute("COMMIT")
        reader.close()
