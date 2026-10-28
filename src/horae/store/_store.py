# SPDX-License-Identifier: Apache-2.0
"""The store: one SQLite database in the instance state directory (ADR 0006).

One `Store` wraps one connection and is used by one thread; open one per thread or process.
Writes take the write lock up front (`BEGIN IMMEDIATE`) and wait up to the busy timeout for it,
so concurrent runs serialize instead of failing with "database is locked".
"""

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from horae.contracts import RunRecord, StepRecord, Usage
from horae.contracts.intents import IntentType
from horae.store._schema import StoreError, migrate, user_version

StoreRunStatus = Literal["running", "ok", "noop", "degraded", "failed", "undone"]
"""A run record's status, plus `running` while the run has no record yet."""
TaskStatus = Literal["todo", "done", "cancelled", "snoozed"]

DEFAULT_BUSY_TIMEOUT_MS = 5000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Static SQL only: PRAGMA names cannot be bound as parameters.
_PRAGMAS = {
    "journal_mode": "PRAGMA journal_mode",
    "busy_timeout": "PRAGMA busy_timeout",
    "foreign_keys": "PRAGMA foreign_keys",
    "synchronous": "PRAGMA synchronous",
    "user_version": "PRAGMA user_version",
}


def _ts(value: datetime) -> str:
    """An aware datetime as sortable UTC text."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime: the store needs an aware datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _opt_ts(value: datetime | None) -> str | None:
    return None if value is None else _ts(value)


def _opt_dt(text: str | None) -> datetime | None:
    return None if text is None else _dt(text)


def _sha256(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class RunSummary:
    """The indexed columns of a run, for listings such as `horae runs`."""

    id: str
    pipeline: str
    status: StoreRunStatus
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    cost: Usage

    @property
    def duration(self) -> timedelta | None:
        return None if self.finished_at is None else self.finished_at - self.started_at


@dataclass(frozen=True)
class JournalEntry:
    """One change, recorded before it is applied, with enough to undo it."""

    id: int
    run_id: str
    seq: int
    intent: int
    """Index into the run record's `intents`."""
    intent_type: IntentType
    target: str
    before: bytes | None
    """Content before the change; `None` when the target did not exist."""
    before_sha256: str | None
    after: bytes | None
    """Content after the change; `None` when there is none (e.g. a notification)."""
    after_sha256: str | None
    undoable: bool
    applied: bool
    created_at: datetime


@dataclass(frozen=True)
class CacheEntry:
    """A cached LLM response."""

    key: str
    skill: str
    skill_version: str
    model: str
    response: Any
    usage: Usage
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class TaskEntry:
    """A task in the index, keyed by its fingerprint."""

    fingerprint: str
    title: str
    status: TaskStatus
    due: date | None = None
    time: str | None = None
    """`HH:MM`, requires `due`."""
    snoozed_until: date | datetime | None = None
    """Required for (and only for) `snoozed`."""
    target: str | None = None
    """Where the task lives, e.g. a note path."""
    source: str | None = None
    """The plugin call that proposed it, e.g. `calendar.events`."""
    run_id: str | None = None
    """The run that last wrote it."""


@dataclass(frozen=True)
class NotificationState:
    """What was sent for one dedupe key."""

    key: str
    run_id: str | None
    first_sent_at: datetime
    last_sent_at: datetime
    count: int


def _until_text(value: date | datetime | None) -> str | None:
    if isinstance(value, datetime):
        return _ts(value)
    return None if value is None else value.isoformat()


def _until_value(text: str | None) -> date | datetime | None:
    if text is None:
        return None
    return _dt(text) if "T" in text else date.fromisoformat(text)


class Store:
    """Typed access to the Horae state database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._depth = 0

    @classmethod
    def open(cls, path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> Self:
        """Open (creating it if needed) and migrate the database at `path`."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, autocommit=True)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                (mode,) = conn.execute("PRAGMA journal_mode = WAL").fetchone()
                if mode != "wal":  # pragma: no cover - e.g. a filesystem without shared memory
                    raise StoreError(f"cannot enable WAL mode (got {mode!r})")
            migrate(conn)
        except BaseException:
            conn.close()
            raise
        return cls(conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return user_version(self._conn)

    def pragma(self, name: str) -> object:
        """Read one of a fixed set of PRAGMAs (for checks and diagnostics)."""
        sql = _PRAGMAS.get(name)
        if sql is None:
            raise ValueError(f"unsupported pragma {name!r}")
        value: object = self._conn.execute(sql).fetchone()[0]
        return value

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes atomically. Nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise StoreError(f"cannot start a write: {exc}") from exc
        self._depth = 1
        try:
            yield
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth = 0

    def _write(self, sql: str, params: tuple[Any, ...] | dict[str, Any]) -> sqlite3.Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            if "FOREIGN KEY" in str(exc):
                raise StoreError(f"unknown run: {exc}") from exc
            raise

    # --- runs ------------------------------------------------------------------------------

    def begin_run(
        self, run_id: str, pipeline: str, started_at: datetime, *, dry_run: bool = False
    ) -> None:
        """Record that a run started, before it has a run record (the journal needs the row)."""
        started = _ts(started_at)
        with self.transaction():
            try:
                self._write(
                    "INSERT INTO runs (id, pipeline, status, dry_run, started_at) "
                    "VALUES (?, ?, 'running', ?, ?)",
                    (run_id, pipeline, int(dry_run), started),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"run {run_id!r} already exists") from exc

    def save_run(self, record: RunRecord) -> None:
        """Insert or replace a run's record (also its step rows); the pipeline cannot change."""
        params = {
            "id": record.id,
            "pipeline": record.pipeline,
            "status": record.status,
            "dry_run": int(record.dry_run),
            "started_at": _ts(record.started_at),
            "finished_at": _ts(record.finished_at),
            "tokens_in": record.cost.tokens_in,
            "tokens_out": record.cost.tokens_out,
            "cost_usd": record.cost.usd,
            "record": record.model_dump_json(),
        }
        with self.transaction():
            cur = self._write(
                "INSERT INTO runs (id, pipeline, status, dry_run, started_at, finished_at, "
                "tokens_in, tokens_out, cost_usd, record) VALUES (:id, :pipeline, :status, "
                ":dry_run, :started_at, :finished_at, :tokens_in, :tokens_out, :cost_usd, :record) "
                "ON CONFLICT (id) DO UPDATE SET status = excluded.status, "
                "dry_run = excluded.dry_run, started_at = excluded.started_at, "
                "finished_at = excluded.finished_at, tokens_in = excluded.tokens_in, "
                "tokens_out = excluded.tokens_out, cost_usd = excluded.cost_usd, "
                "record = excluded.record WHERE runs.pipeline = excluded.pipeline",
                params,
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {record.id!r} exists for another pipeline")
            self._write("DELETE FROM steps WHERE run_id = ?", (record.id,))
            for index, step in enumerate(record.steps):
                self.record_step(record.id, index, step)

    def get_run(self, run_id: str) -> RunRecord | None:
        """The run record, or `None` if the run is unknown or still running."""
        row = self._conn.execute("SELECT record FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return RunRecord.model_validate_json(row[0])

    @staticmethod
    def _summary(row: tuple[Any, ...]) -> RunSummary:
        return RunSummary(
            id=row[0],
            pipeline=row[1],
            status=row[2],
            dry_run=bool(row[3]),
            started_at=_dt(row[4]),
            finished_at=_opt_dt(row[5]),
            cost=Usage(tokens_in=row[6], tokens_out=row[7], usd=row[8]),
        )

    def run_summary(self, run_id: str) -> RunSummary | None:
        row = self._conn.execute(
            "SELECT id, pipeline, status, dry_run, started_at, finished_at, tokens_in, "
            "tokens_out, cost_usd FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else self._summary(row)

    def list_runs(
        self,
        *,
        pipeline: str | None = None,
        status: StoreRunStatus | None = None,
        limit: int = 50,
    ) -> list[RunSummary]:
        """Runs, newest first, optionally filtered."""
        rows = self._conn.execute(
            "SELECT id, pipeline, status, dry_run, started_at, finished_at, tokens_in, "
            "tokens_out, cost_usd FROM runs WHERE (:pipeline IS NULL OR pipeline = :pipeline) "
            "AND (:status IS NULL OR status = :status) "
            "ORDER BY started_at DESC, id DESC LIMIT :limit",
            {"pipeline": pipeline, "status": status, "limit": limit},
        ).fetchall()
        return [self._summary(row) for row in rows]

    def record_step(self, run_id: str, index: int, step: StepRecord) -> None:
        """Insert or replace one step's record (e.g. as each step finishes)."""
        with self.transaction():
            self._write(
                "INSERT OR REPLACE INTO steps (run_id, idx, kind, status, started_at, "
                "finished_at, duration_ms, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    index,
                    step.kind,
                    step.status,
                    _opt_ts(step.started_at),
                    _opt_ts(step.finished_at),
                    step.duration_ms,
                    step.model_dump_json(),
                ),
            )

    def steps(self, run_id: str) -> list[StepRecord]:
        rows = self._conn.execute(
            "SELECT record FROM steps WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
        return [StepRecord.model_validate_json(record) for (record,) in rows]

    # --- journal ---------------------------------------------------------------------------

    @staticmethod
    def _entry(row: tuple[Any, ...]) -> JournalEntry:
        return JournalEntry(
            id=row[0],
            run_id=row[1],
            seq=row[2],
            intent=row[3],
            intent_type=row[4],
            target=row[5],
            before=row[6],
            before_sha256=row[7],
            after=row[8],
            after_sha256=row[9],
            undoable=bool(row[10]),
            applied=bool(row[11]),
            created_at=_dt(row[12]),
        )

    def journal_append(
        self,
        run_id: str,
        *,
        intent: int,
        intent_type: IntentType,
        target: str,
        before: bytes | None = None,
        after: bytes | None = None,
        undoable: bool = True,
        at: datetime,
    ) -> JournalEntry:
        """Record a change before applying it; `journal_mark_applied` once it is applied."""
        with self.transaction():
            cur = self._write(
                "INSERT INTO journal (run_id, seq, intent, intent_type, target, before, "
                "before_sha256, after, after_sha256, undoable, created_at) VALUES (?, "
                "(SELECT count(*) FROM journal WHERE run_id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run_id,
                    intent,
                    intent_type,
                    target,
                    before,
                    _sha256(before),
                    after,
                    _sha256(after),
                    int(undoable),
                    _ts(at),
                ),
            )
            row = self._conn.execute(
                "SELECT id, run_id, seq, intent, intent_type, target, before, before_sha256, "
                "after, after_sha256, undoable, applied, created_at FROM journal WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        return self._entry(row)

    def journal_mark_applied(self, entry_id: int) -> None:
        with self.transaction():
            cur = self._write("UPDATE journal SET applied = 1 WHERE id = ?", (entry_id,))
            if cur.rowcount != 1:
                raise StoreError(f"no journal entry {entry_id}")

    def journal(self, run_id: str) -> list[JournalEntry]:
        """A run's journal, in the order the changes were recorded."""
        rows = self._conn.execute(
            "SELECT id, run_id, seq, intent, intent_type, target, before, before_sha256, "
            "after, after_sha256, undoable, applied, created_at FROM journal "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        return [self._entry(row) for row in rows.fetchall()]

    # --- LLM cache -------------------------------------------------------------------------

    @staticmethod
    def _key(key: str) -> str:
        if not _SHA256.match(key):
            raise ValueError("cache key must be a sha256 hex digest")
        return key

    def cache_get(self, key: str, *, now: datetime) -> CacheEntry | None:
        """The cached response for a request hash, unless missing or expired at `now`."""
        row = self._conn.execute(
            "SELECT key, skill, skill_version, model, response, tokens_in, tokens_out, cost_usd, "
            "created_at, expires_at FROM llm_cache "
            "WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)",
            (self._key(key), _ts(now)),
        ).fetchone()
        if row is None:
            return None
        return CacheEntry(
            key=row[0],
            skill=row[1],
            skill_version=row[2],
            model=row[3],
            response=json.loads(row[4]),
            usage=Usage(tokens_in=row[5], tokens_out=row[6], usd=row[7]),
            created_at=_dt(row[8]),
            expires_at=_opt_dt(row[9]),
        )

    def cache_put(
        self,
        key: str,
        *,
        skill: str,
        skill_version: str,
        model: str,
        response: Any,
        usage: Usage,
        at: datetime,
        expires_at: datetime | None = None,
    ) -> None:
        """Insert or replace the response for a request hash."""
        with self.transaction():
            self._write(
                "INSERT OR REPLACE INTO llm_cache (key, skill, skill_version, model, response, "
                "tokens_in, tokens_out, cost_usd, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._key(key),
                    skill,
                    skill_version,
                    model,
                    json.dumps(response, ensure_ascii=False, allow_nan=False),
                    usage.tokens_in,
                    usage.tokens_out,
                    usage.usd,
                    _ts(at),
                    _opt_ts(expires_at),
                ),
            )

    def cache_purge_expired(self, now: datetime) -> int:
        """Delete entries expired at `now`; return how many."""
        with self.transaction():
            cur = self._write("DELETE FROM llm_cache WHERE expires_at <= ?", (_ts(now),))
        return cur.rowcount

    # --- task index ------------------------------------------------------------------------

    def upsert_task(self, task: TaskEntry, *, at: datetime) -> None:
        """Insert or replace a task by fingerprint (`created_at` is kept)."""
        stamp = _ts(at)
        with self.transaction():
            try:
                self._write(
                    "INSERT INTO tasks (fingerprint, title, status, due, time, snoozed_until, "
                    "target, source, run_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (fingerprint) DO UPDATE SET title = excluded.title, "
                    "status = excluded.status, due = excluded.due, time = excluded.time, "
                    "snoozed_until = excluded.snoozed_until, target = excluded.target, "
                    "source = excluded.source, run_id = excluded.run_id, "
                    "updated_at = excluded.updated_at",
                    (
                        task.fingerprint,
                        task.title,
                        task.status,
                        None if task.due is None else task.due.isoformat(),
                        task.time,
                        _until_text(task.snoozed_until),
                        task.target,
                        task.source,
                        task.run_id,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"invalid task {task.fingerprint!r}: {exc}") from exc

    @staticmethod
    def _task(row: tuple[Any, ...]) -> TaskEntry:
        return TaskEntry(
            fingerprint=row[0],
            title=row[1],
            status=row[2],
            due=None if row[3] is None else date.fromisoformat(row[3]),
            time=row[4],
            snoozed_until=_until_value(row[5]),
            target=row[6],
            source=row[7],
            run_id=row[8],
        )

    def get_task(self, fingerprint: str) -> TaskEntry | None:
        row = self._conn.execute(
            "SELECT fingerprint, title, status, due, time, snoozed_until, target, source, "
            "run_id FROM tasks WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return None if row is None else self._task(row)

    def list_tasks(self, *, status: TaskStatus | None = None) -> list[TaskEntry]:
        """Tasks by due date (undated last), then fingerprint."""
        rows = self._conn.execute(
            "SELECT fingerprint, title, status, due, time, snoozed_until, target, source, "
            "run_id FROM tasks WHERE (:status IS NULL OR status = :status) "
            "ORDER BY due IS NULL, due, fingerprint",
            {"status": status},
        ).fetchall()
        return [self._task(row) for row in rows]

    # --- notification state ----------------------------------------------------------------

    def record_notification(
        self, key: str, *, at: datetime, run_id: str | None = None
    ) -> NotificationState:
        """Record a send for a dedupe key and return the updated state."""
        stamp = _ts(at)
        with self.transaction():
            self._write(
                "INSERT INTO notifications (key, run_id, first_sent_at, last_sent_at, count) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT (key) DO UPDATE SET "
                "run_id = coalesce(excluded.run_id, run_id), "
                "last_sent_at = excluded.last_sent_at, count = count + 1",
                (key, run_id, stamp, stamp),
            )
            state = self.get_notification(key)
        if state is None:  # pragma: no cover - written in the same transaction
            raise StoreError(f"notification {key!r} vanished")
        return state

    def get_notification(self, key: str) -> NotificationState | None:
        row = self._conn.execute(
            "SELECT key, run_id, first_sent_at, last_sent_at, count FROM notifications "
            "WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return NotificationState(row[0], row[1], _dt(row[2]), _dt(row[3]), row[4])
