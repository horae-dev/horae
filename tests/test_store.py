# SPDX-License-Identifier: Apache-2.0
"""The SQLite store: schema and migrations, connection settings, and the typed API."""

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from horae.contracts import RunRecord, StepRecord, Usage
from horae.store import (
    SCHEMA_VERSION,
    Migration,
    SchemaTooNewError,
    Store,
    StoreError,
    TaskEntry,
    load_migrations,
    migrate,
)

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "schemas" / "examples" / "run-record" / "valid" / "ok.json"
T0 = datetime(2026, 10, 27, 5, 30, tzinfo=UTC)
TABLES = {"runs", "steps", "journal", "llm_cache", "tasks", "notifications"}


def _record(run_id: str = "run-1", **update: object) -> RunRecord:
    data = json.loads(EXAMPLE.read_text())
    data.update(id=run_id, **update)
    return RunRecord.model_validate(data)


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


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


def test_each_migration_ends_by_setting_its_own_version() -> None:
    for m in load_migrations():
        last = [line for line in m.sql.splitlines() if line.strip()][-1]
        assert re.fullmatch(r"PRAGMA user_version = (\d+);", last), m.name
        assert int(last.split("=")[1].rstrip(";")) == m.version, m.name


def test_a_migration_that_does_not_bump_the_version_is_rolled_back(db: Path) -> None:
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(StoreError, match="must end with"):
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
    conn = sqlite3.connect(db, autocommit=True)
    try:
        assert migrate(conn, target=1) == 1
        assert "llm_cache" not in _tables(conn)
        conn.execute(
            "INSERT INTO runs (id, pipeline, status, dry_run, started_at) "
            "VALUES ('old-run', 'daily-brief', 'running', 0, '2026-10-27T05:30:00.000000Z')"
        )
    finally:
        conn.close()

    with Store.open(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        summary = s.run_summary("old-run")
        assert summary is not None
        assert summary.status == "running"
        s.upsert_task(TaskEntry("t:1", "Buy milk", "todo", run_id="old-run"), at=T0)
    conn = sqlite3.connect(db)
    try:
        assert _tables(conn) >= TABLES
    finally:
        conn.close()


def test_failed_migration_rolls_back(db: Path) -> None:
    db.parent.mkdir(parents=True)
    broken = [*load_migrations()[:1], Migration(2, "broken", "CREATE TABLE x (a INTEGER); nope;")]
    conn = sqlite3.connect(db, autocommit=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, migrations=broken)
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert "x" not in _tables(conn)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_a_newer_database_is_refused(db: Path) -> None:
    Store.open(db).close()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
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
        with pytest.raises(StoreError, match="numbered"):
            migrate(conn, migrations=gap)
        for bad in (True, -1, 1.0, "1"):
            with pytest.raises(StoreError, match="invalid migration version"):
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


# --- connection settings ---------------------------------------------------------------------


def test_wal_busy_timeout_and_foreign_keys(db: Path) -> None:
    with Store.open(db, busy_timeout_ms=1234) as s:
        assert s.pragma("journal_mode") == "wal"
        assert s.pragma("busy_timeout") == 1234
        assert s.pragma("foreign_keys") == 1
        assert s.pragma("synchronous") == 1  # NORMAL, safe with WAL
        with pytest.raises(ValueError, match="pragma"):
            s.pragma("user_version; DROP TABLE runs")


def test_foreign_keys_are_enforced(store: Store) -> None:
    with pytest.raises(StoreError, match="unknown run"):
        store.journal_append(
            "no-such-run", intent=0, intent_type="note.create", target="a.md", after=b"x", at=T0
        )


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
    assert store.get_run("run-1") is None
    with pytest.raises(StoreError, match="already exists"):
        store.begin_run("run-1", "daily-brief", T0)
    store.save_run(_record(status="degraded", error="model timed out"))
    record = store.get_run("run-1")
    assert record is not None
    assert record.status == "degraded"
    with pytest.raises(StoreError, match="pipeline"):
        store.save_run(_record(pipeline="remind"))


def test_save_run_overwrites_for_undo(store: Store) -> None:
    record = _record()
    store.save_run(record)
    store.save_run(record.model_copy(update={"status": "undone"}))
    assert [r.status for r in store.list_runs()] == ["undone"]


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


def test_timestamps_keep_the_instant_across_offsets(store: Store) -> None:
    paris = timezone(timedelta(hours=1))
    store.begin_run("run-1", "daily-brief", datetime(2026, 10, 27, 6, 30, tzinfo=paris))
    summary = store.run_summary("run-1")
    assert summary is not None
    assert summary.started_at == T0
    with pytest.raises(ValueError, match="naive"):
        store.begin_run("run-2", "daily-brief", datetime(2026, 10, 27, 5, 30))


def test_steps_are_recorded_incrementally_and_from_the_record(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    step = StepRecord(kind="collect", status="ok", started_at=T0, duration_ms=5)
    store.record_step("run-1", 0, step)
    store.record_step("run-1", 0, step.model_copy(update={"status": "failed"}))
    assert [s.status for s in store.steps("run-1")] == ["failed"]
    with pytest.raises(StoreError, match="unknown run"):
        store.record_step("missing", 0, step)
    record = _record()
    store.save_run(record)
    assert store.steps("run-1") == record.steps


# --- journal ---------------------------------------------------------------------------------


def test_journal_keeps_before_and_after_bytes_with_digests(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    created = store.journal_append(
        "run-1", intent=0, intent_type="note.create", target="a.md", after=b"new\n", at=T0
    )
    edited = store.journal_append(
        "run-1",
        intent=1,
        intent_type="note.append_section",
        target="b.md",
        before=b"\x00old",
        after=b"\x00old\nmore",
        at=T0,
    )
    pushed = store.journal_append(
        "run-1", intent=2, intent_type="notify.push", target="ntfy", undoable=False, at=T0
    )
    assert [e.seq for e in (created, edited, pushed)] == [0, 1, 2]
    assert created.before is None
    assert created.before_sha256 is None
    assert created.after_sha256 == hashlib.sha256(b"new\n").hexdigest()
    assert edited.before == b"\x00old"
    assert edited.before_sha256 == hashlib.sha256(b"\x00old").hexdigest()
    assert pushed.undoable is False
    assert not any(e.applied for e in store.journal("run-1"))

    store.journal_mark_applied(created.id)
    entries = store.journal("run-1")
    assert [e.target for e in entries] == ["a.md", "b.md", "ntfy"]
    assert [e.applied for e in entries] == [True, False, False]
    assert entries[1] == edited
    assert entries[0].created_at == T0
    with pytest.raises(StoreError, match="journal entry"):
        store.journal_mark_applied(9999)
    assert store.journal("missing") == []


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


# --- task index and notification state -------------------------------------------------------


def test_task_index_keyed_by_fingerprint(store: Store) -> None:
    store.begin_run("run-1", "daily-brief", T0)
    task = TaskEntry(
        "calendar:3b1f0c",
        "Prepare slides",
        "todo",
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
        "calendar:3b1f0c", "Prepare slides", "snoozed", snoozed_until=T0 + timedelta(days=1)
    )
    store.upsert_task(snoozed, at=T0 + timedelta(hours=1))
    assert store.get_task("calendar:3b1f0c") == snoozed
    store.upsert_task(TaskEntry("mail:1", "Reply", "todo", snoozed_until=None), at=T0)
    store.upsert_task(
        TaskEntry("mail:2", "Sign form", "snoozed", snoozed_until=date(2026, 11, 1)), at=T0
    )
    assert [t.fingerprint for t in store.list_tasks()] == ["calendar:3b1f0c", "mail:1", "mail:2"]
    assert [t.fingerprint for t in store.list_tasks(status="snoozed")] == [
        "calendar:3b1f0c",
        "mail:2",
    ]
    with pytest.raises(StoreError, match="task"):
        store.upsert_task(TaskEntry("x:1", "Bad", "snoozed"), at=T0)


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


# --- concurrency -----------------------------------------------------------------------------


def test_concurrent_threads_write_different_runs(db: Path) -> None:
    Store.open(db).close()
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            with Store.open(db) as s:
                for i in range(15):
                    run_id = f"t{n}-{i}"
                    s.begin_run(run_id, "remind", T0)
                    s.journal_append(
                        run_id, intent=0, intent_type="note.create", target="a.md", at=T0
                    )
                    s.save_run(_record(run_id, pipeline="remind"))
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
        for n in range(4)
    ]
    stderr = [p.communicate(timeout=60)[1] for p in procs]
    assert [p.returncode for p in procs] == [0] * 4, stderr
    with Store.open(db) as s:
        assert len(s.list_runs(limit=1000)) == 4 * 20


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


def test_busy_timeout_expiry_is_a_store_error(db: Path) -> None:
    Store.open(db).close()
    holder = sqlite3.connect(db, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with Store.open(db, busy_timeout_ms=50) as s, pytest.raises(StoreError, match="locked"):
            s.save_run(_record())
    finally:
        holder.execute("ROLLBACK")
        holder.close()
