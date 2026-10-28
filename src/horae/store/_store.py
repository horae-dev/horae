# SPDX-License-Identifier: Apache-2.0
"""The store: one SQLite database in the instance state directory (ADR 0006).

One `Store` wraps one connection, opened with `Store.open`, and is used by one thread (the
connection keeps `check_same_thread=True`, so another thread gets a `StoreError`); open one per
thread or process. Writes take the write lock up front (`BEGIN IMMEDIATE`) and wait up to the
busy timeout for it, so concurrent runs serialize instead of failing with "database is locked";
past the timeout the store raises `StoreBusy`.

Errors: every failure is a `StoreError` subclass (see `horae.store._errors`); `ValueError` is
raised only for programming errors, i.e. arguments that are wrong whatever the database holds.

**The secret gate** (#53, schemas decision 43 B and C): the store is the last line of
defence against a producer that forgot to redact. Every value it writes passes the redactor of
every secret this process resolved (`horae.secrets.redact.current()`) just before the
`INSERT` / `UPDATE`, in every table, and is treated by what it is:

- **free text: redacted and flagged.** The free-text fields of the run record and of the step
  rows (the allowlist of `horae.store._gate`: step output, parameters, `error.message`,
  stderr, input data, intent payload fields, `context.vars` values, LLM requests, responses,
  repair and schema errors, violation details, the run's `error`), a task's `title` and an
  abandoned run's `error`. A match becomes `[REDACTED:<NAME>]`; inside JSON, string leaves and
  object keys are redacted (a key that becomes equal to another key of its object is renamed
  `[REDACTED:<NAME>]#2`, and that is flagged too: nothing is lost silently);
- **identifiers and numbers: exempt and flagged** (kept as written, a rewrite would make the
  row invalid or break undo, replay or the run's own id): run ids, pipeline names,
  timestamps, statuses, kinds, plugin names and refs, tokens, digests, the pipeline
  definition, the journal's `target`, `applied_by` and `sink_identity`, a task's
  `fingerprint`, `source` and `target`, notification keys, the usage ledger's `alias`,
  `backend` and `model`, a run's `host`, and a JSON number whose text is a secret's value
  (its type never changes);
- **refused** (`SecretRefused`, nothing written): an LLM cache entry holding a secret, in any
  string, key or number (a cache hit would replay it into later runs; callers treat the
  refusal as "do not cache": `cache_put_safe`), and, as a residual, a record or step that would
  no longer validate once its free text is redacted (a token longer than a text field allows);
- **the journalled `after` bytes and a sink's `actual_after` bytes: exempt and flagged when
  new** (maintainer decision, schemas decision 43): the note as Horae wrote it, kept byte for
  byte (apply's mismatch check and undo depend on it). A secret there is flagged only when it
  is **new compared to the journalled `before`** (it occurs more often than in `before`): a
  secret the user already had in their note is not flagged;
- **exempt**: the journalled `before` bytes and the bytes a forced undo `overwrote` (the
  user's own note as it was in the vault: undo restores it byte for byte).

Every finding is logged (`horae.store`, names only) and, for a run, written to the
`secret_flags` table in the same transaction as the row that held it; `save_run` turns the
run's findings (and those in the record itself) into `secret_in_output` violations of the
record (when the record's violations would pass `MAX_ITEMS`, the rest is aggregated into one
violation), then deletes them; `abandon_run` deletes them (the run gets no record). So a
record saved by another `Store` or process still gets them.

Every writable connection sets `PRAGMA secure_delete = FAST`, so a deleted or overwritten row
(a pruned run, a replaced record) is zeroed in the database pages it leaves rather than kept
as free-page garbage. A database written before #53 can still hold unredacted bytes in its
free pages: see docs/operations/retention-and-backups.md for the one-off `VACUUM`.
"""

import fcntl
import functools
import hashlib
import json
import logging
import os
import random
import re
import socket
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, ValidationError, model_validator

from horae.contracts import RunRecord, StepRecord, Usage
from horae.contracts.intents import INTENT_TYPES, ClockTime, IntentType
from horae.contracts.run import RunId, Target
from horae.contracts.types import (
    MAX_ITEMS,
    Contract,
    Fingerprint,
    IanaTimezone,
    IsoDate,
    PluginRef,
    Timestamp,
    Title,
)
from horae.engine.ports import PreviousLlm
from horae.secrets.redact import current as current_redactor
from horae.store._errors import (
    InvalidRow,
    JournalTooLarge,
    PipelineBusy,
    RunExists,
    RunNotRunning,
    RunNotStale,
    SchemaTooNewError,
    SchemaTooOldError,
    SecretRefused,
    StoreBusy,
    StoreError,
    UnknownJournalEntry,
    UnknownRun,
    UnknownTask,
    UnsupportedSQLite,
    is_busy,
    translate,
)
from horae.store._gate import Findings, scrub_record, scrub_step
from horae.store._schema import SCHEMA_VERSION, migrate, user_version

_log = logging.getLogger("horae.store")

StoreRunStatus = Literal["running", "ok", "noop", "degraded", "failed", "undone"]
"""A run record's status, plus `running` while the run has no record yet."""
TaskStatus = Literal["todo", "done", "cancelled", "snoozed"]

DEFAULT_BUSY_TIMEOUT_MS = 5000
HEARTBEAT_BUSY_TIMEOUT_MS = 100
"""How long a heartbeat waits for the write lock: a heartbeat never blocks the event loop for
long, and a busy store only skips a beat."""
MIN_SQLITE = (3, 37, 0)
"""STRICT tables need SQLite 3.37."""
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
"""Upper bound for one journalled content (`before` or `after`): 16 MiB."""
MAX_LIST_RUNS = 1000
MAX_NESTING = 16
"""How deep `transaction()` blocks may nest inside the outermost one."""

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NOT_UNDOABLE: frozenset[str] = frozenset({"notify.push", "http.post"})
_TASK_STATUSES = frozenset({"todo", "done", "cancelled", "snoozed"})
_PRIVATE = object()
# Static SQL only: PRAGMA names cannot be bound as parameters.
_PRAGMAS = {
    "journal_mode": "PRAGMA journal_mode",
    "busy_timeout": "PRAGMA busy_timeout",
    "foreign_keys": "PRAGMA foreign_keys",
    "synchronous": "PRAGMA synchronous",
    "user_version": "PRAGMA user_version",
}
# Savepoint names cannot be bound either: one literal statement per nesting level.
_SAVEPOINT = (
    "SAVEPOINT horae_1",
    "SAVEPOINT horae_2",
    "SAVEPOINT horae_3",
    "SAVEPOINT horae_4",
    "SAVEPOINT horae_5",
    "SAVEPOINT horae_6",
    "SAVEPOINT horae_7",
    "SAVEPOINT horae_8",
    "SAVEPOINT horae_9",
    "SAVEPOINT horae_10",
    "SAVEPOINT horae_11",
    "SAVEPOINT horae_12",
    "SAVEPOINT horae_13",
    "SAVEPOINT horae_14",
    "SAVEPOINT horae_15",
    "SAVEPOINT horae_16",
)
_RELEASE = (
    "RELEASE horae_1",
    "RELEASE horae_2",
    "RELEASE horae_3",
    "RELEASE horae_4",
    "RELEASE horae_5",
    "RELEASE horae_6",
    "RELEASE horae_7",
    "RELEASE horae_8",
    "RELEASE horae_9",
    "RELEASE horae_10",
    "RELEASE horae_11",
    "RELEASE horae_12",
    "RELEASE horae_13",
    "RELEASE horae_14",
    "RELEASE horae_15",
    "RELEASE horae_16",
)
_ROLLBACK_TO = (
    "ROLLBACK TO horae_1",
    "ROLLBACK TO horae_2",
    "ROLLBACK TO horae_3",
    "ROLLBACK TO horae_4",
    "ROLLBACK TO horae_5",
    "ROLLBACK TO horae_6",
    "ROLLBACK TO horae_7",
    "ROLLBACK TO horae_8",
    "ROLLBACK TO horae_9",
    "ROLLBACK TO horae_10",
    "ROLLBACK TO horae_11",
    "ROLLBACK TO horae_12",
    "ROLLBACK TO horae_13",
    "ROLLBACK TO horae_14",
    "ROLLBACK TO horae_15",
    "ROLLBACK TO horae_16",
)


def _api[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Translate `sqlite3.Error` into `StoreError` at the public API boundary."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            raise translate(exc) from exc

    return wrapper


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


def _sha(value: str) -> str:
    if not _SHA256.match(value):
        raise ValueError("expected a sha256 hex digest")
    return value


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA time zone {tz!r}") from exc


def _snooze(
    status: str, until: date | datetime | None, tz: str | None
) -> tuple[str | None, str | None, str | None]:
    """`(snoozed_until, snooze_tz, wake_at)` as stored, from a status and its `until`.

    A date needs the IANA zone it is in: the task wakes at the start of that day there."""
    if (status == "snoozed") != (until is not None):
        raise ValueError("`until` is required for `snoozed` and only allowed there")
    if until is None or isinstance(until, datetime):
        if tz is not None:
            raise ValueError("a time zone is only used with a snooze date")
        stamp = _opt_ts(until)
        return stamp, None, stamp
    if tz is None:
        raise ValueError("snoozing until a date needs the IANA time zone of that date")
    wake = datetime.combine(until, clock(), tzinfo=_zone(tz))
    return until.isoformat(), tz, _ts(wake)


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
    pid: int | None = None
    """The process that began the run (`begin_run`)."""
    host: str | None = None
    heartbeat_at: datetime | None = None
    abandoned_at: datetime | None = None
    """Set when the run was abandoned (`abandon_run`): it is `failed` with no record."""
    error: str | None = None

    @property
    def duration(self) -> timedelta | None:
        return None if self.finished_at is None else self.finished_at - self.started_at


@dataclass(frozen=True)
class DaySpend:
    """What the usage ledger records for one day: settled calls at their cost, calls in
    flight (or whose process died) at their reservation."""

    usd: float = 0.0
    tokens_out: int = 0


_USD_TOLERANCE = 1e-12
"""Relative (and absolute, in dollars) slack when comparing a settled cost with its
reservation: float sums of the same prices may differ in their last bits."""


@dataclass(frozen=True)
class Overrun:
    """A call that cost more than its reservation (`Store.settle_usage`)."""

    reserved_usd: float
    usd: float
    reserved_tokens_out: int
    tokens_out: int


@dataclass(frozen=True)
class UsageGroup:
    """LLM calls of one day, pipeline, model alias, backend and model (`Store.usage_report`)."""

    day: str
    """The local day (`YYYY-MM-DD`, instance time zone)."""
    pipeline: str
    alias: str
    backend: str | None
    model: str | None
    calls: int
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    usd: float
    unpriced: int
    """Calls of an alias without `price` (their `usd` is `0`)."""
    in_flight: int
    """Calls still `reserved`, counted at their reservation (an upper bound): in flight, or
    their process died and the run was not recovered yet."""
    estimated: int
    """Settled calls counted at their reservation: the provider reported no usage, or their
    run was abandoned (`abandon_run`)."""
    over_reservation: int
    """Settled calls that cost more than they reserved (logged when settled)."""


@dataclass(frozen=True)
class JournalEntry:
    """One change, recorded before it is applied, with enough to undo it.

    The contents are stored once, by digest; read them with `Store.blob`."""

    id: int
    run_id: str
    seq: int
    intent: int
    """Index into the run record's `intents`."""
    intent_type: IntentType
    target: str
    before_sha256: str | None
    """Digest of the content before the change; `None` when the target did not exist."""
    after_sha256: str | None
    """Digest of the content after; `None` when there is none (e.g. a notification)."""
    undoable: bool
    applied: bool
    """`False` means "maybe applied": the process may have died between applying the change
    and marking it, so compare the target's current digest with `before_sha256` and
    `after_sha256` before acting on it."""
    undone_at: datetime | None
    created_at: datetime
    applied_by: str | None = None
    """The sink that applies the change (`ProposedIntent.applied_by`), when recorded."""
    sink_identity: str | None = None
    """Where that sink writes (`horae.apply.sink_identity`), when it says."""
    actual_after_sha256: str | None = None
    """Digest of the bytes the sink really wrote, when they differ from `after_sha256`
    (`apply_mismatch`)."""
    undo_overwrote_sha256: str | None = None
    """Digest of the bytes a forced undo overwrote (kept as a blob)."""


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


class TaskEntry(Contract):
    """A task in the index, keyed by its fingerprint."""

    model_config = ConfigDict(frozen=True)

    fingerprint: Fingerprint
    title: Title
    status: TaskStatus
    due: IsoDate | None = None
    time: ClockTime | None = None
    """`HH:MM`, requires `due`."""
    snoozed_until: IsoDate | Timestamp | None = None
    """Required for (and only for) `snoozed`."""
    snooze_tz: IanaTimezone | None = None
    """The IANA zone of a `snoozed_until` date (required then, and only then)."""
    target: Target | None = None
    """Where the task lives, e.g. a note path."""
    source: PluginRef | None = None
    """The plugin call that proposed it, e.g. `calendar.events`."""
    run_id: RunId | None = None
    """The run that last wrote it."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.time is not None and self.due is None:
            raise ValueError("`time` requires `due`")
        _snooze(self.status, self.snoozed_until, self.snooze_tz)
        return self

    @property
    def wake_at(self) -> datetime | None:
        """When a snoozed task comes back, in UTC."""
        return _opt_dt(_snooze(self.status, self.snoozed_until, self.snooze_tz)[2])


@dataclass(frozen=True)
class NotificationState:
    """What was sent for one dedupe key."""

    key: str
    run_id: str | None
    first_sent_at: datetime
    last_sent_at: datetime
    count: int


def _prepare_files(path: Path) -> None:
    """Create the state directory (0700) and the database file (0600) if they are missing."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)


INIT_LOCK_SUFFIX = ".init.lock"
"""`<db>.init.lock`: the advisory lock that serialises first-open initialisation."""
_HEADER = b"SQLite format 3\x00"


def _initialised(path: Path) -> bool:
    """Whether the main file's header already says WAL and the current schema version.

    Both only move forward, so a stale header (the version still in a `-wal` file) only
    makes `open` take the init lock when it did not need to, never the other way round."""
    try:
        with path.open("rb") as handle:
            header = handle.read(100)
    except OSError:
        return False
    if len(header) < 100 or not header.startswith(_HEADER):
        return False
    wal = header[18] == 2 and header[19] == 2
    return wal and int.from_bytes(header[60:64], "big") == SCHEMA_VERSION


def _backoff(delay: float, remaining: float) -> float:
    """A full-jitter sleep (uniform in `[0, delay]`), never past `remaining`."""
    return min(random.uniform(0, delay), remaining)  # noqa: S311 - jitter, not a secret


@contextmanager
def _init_lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold the POSIX advisory lock `<db>.init.lock` (0600, created if missing, never through
    a symbolic link): `flock(LOCK_EX | LOCK_NB)` retried with jittered backoff for at most
    `timeout`, then `StoreBusy`. Closing the descriptor releases it (also if the process
    dies); the file is left in place (removing it would race with the next opener)."""
    lock = path.with_name(path.name + INIT_LOCK_SUFFIX)
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise StoreError(f"cannot open the init lock {lock}: {exc.strerror}") from exc
    try:
        deadline = time.monotonic() + timeout
        delay = 0.005
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreBusy(
                        "another process is initialising the database (init lock held)"
                    ) from None
                time.sleep(_backoff(delay, remaining))
                delay = min(delay * 2, 0.1)
        yield
    finally:
        os.close(fd)


def _wal_attempt(conn: sqlite3.Connection) -> str:
    (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
    if mode != "wal":
        (mode,) = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    return str(mode)


def _enable_wal(path: Path, timeout: float) -> None:
    """Switch the database to WAL (persistent in the file).

    The switch needs an exclusive lock and SQLite does not always wait for it with the busy
    handler, so concurrent first opens of a new database retry here, on a connection that does
    not wait itself, with a full-jitter backoff that never sleeps past `timeout` in total.
    Only SQLITE_BUSY and SQLITE_LOCKED are retried. (`open` also runs it under the init lock,
    so this only waits for connections that are not initialising.)"""
    conn = sqlite3.connect(path, timeout=0, autocommit=True)
    try:
        deadline = time.monotonic() + timeout
        delay = 0.005
        while True:
            try:
                mode = _wal_attempt(conn)
                break
            except sqlite3.OperationalError as exc:
                if not is_busy(exc):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreBusy(f"cannot enable WAL mode: {exc}") from exc
                time.sleep(_backoff(delay, remaining))
                delay = min(delay * 2, 0.1)
    finally:
        conn.close()
    if mode != "wal":  # pragma: no cover - e.g. a filesystem without shared memory
        raise StoreError(f"cannot enable WAL mode (got {mode!r})")


class Store:
    """Typed access to the Horae state database. Create it with `Store.open`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        _token: object = None,
        _path: Path | None = None,
        _read_only: bool = False,
    ) -> None:
        if _token is not _PRIVATE:
            raise TypeError("use Store.open(path) to create a Store")
        self._conn = conn
        self._depth = 0
        self._path = _path
        self._read_only = _read_only
        self._beat: sqlite3.Connection | None = None

    @classmethod
    def _from_connection(cls, conn: sqlite3.Connection, *, _path: Path | None = None) -> Self:
        """Wrap a connection configured and migrated as `open` does (checked)."""
        if conn.autocommit is not True:
            raise ValueError("the store needs a connection opened with autocommit=True")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise ValueError("the store needs PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise ValueError("the store needs WAL mode")
        if user_version(conn) != SCHEMA_VERSION:
            raise ValueError(f"the store needs schema version {SCHEMA_VERSION}")
        return cls(conn, _token=_PRIVATE, _path=_path)

    @property
    def read_only(self) -> bool:
        """Whether the store was opened with `open(read_only=True)`."""
        return self._read_only

    @classmethod
    def _open_read_only(cls, path: Path, timeout: float) -> Self:
        """See `open(read_only=True)`."""
        if not path.is_file():
            raise StoreError(f"no database at {path}")
        # With no `-wal` file, every committed change is in the main file: open it immutable,
        # so SQLite creates no `-wal` / `-shm` (a read-only connection would otherwise create
        # them, with the database file's permissions, and could not remove them). A writer
        # that starts meanwhile appends to its own WAL, so this one-shot read sees the last
        # checkpoint. With a `-wal`, a writer is (or was) active: open it read-only, sharing
        # the existing `-shm`.
        wal = path.with_name(path.name + "-wal")
        query = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
        try:
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?{query}", uri=True, timeout=timeout, autocommit=True
            )
        except sqlite3.Error as exc:
            raise translate(exc) from exc
        try:
            conn.execute("PRAGMA query_only = ON")
            version = user_version(conn)
            if version > SCHEMA_VERSION:
                raise SchemaTooNewError(
                    f"database schema version {version} is newer than this Horae ({SCHEMA_VERSION})"
                )
            if version < SCHEMA_VERSION:
                raise SchemaTooOldError(
                    f"database schema version {version} is older than this Horae "
                    f"({SCHEMA_VERSION}); run `horae run` once to migrate it"
                )
            return cls(conn, _token=_PRIVATE, _path=path, _read_only=True)
        except sqlite3.Error as exc:
            conn.close()
            raise translate(exc) from exc
        except BaseException:
            conn.close()
            raise

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        read_only: bool = False,
    ) -> Self:
        """Open (creating it if needed) and migrate the database at `path`.

        **First open.** Creating the file, switching it to WAL and migrating it run under a
        POSIX advisory lock, `<db>.init.lock` (0600, `flock`, waited for at most the busy
        timeout, then `StoreBusy`), so concurrent first opens (several processes starting on
        a new instance) initialise it once, one after the other. An open whose database
        header already says WAL and the current schema version skips the lock (and never
        creates the lock file).

        A new state directory is created 0700 and a new database file 0600; the umask is
        0077 while the connection opens, so `-wal` and `-shm` are private too (the umask is
        process-wide: open stores before starting threads that create files).

        `read_only=True` (for listings such as `horae runs`) creates nothing, switches
        nothing and never migrates: the database must exist (else `StoreError`), and a schema
        older than this Horae raises `SchemaTooOldError` (newer: `SchemaTooNewError`). Writes
        then fail with a `StoreError`. When no writer is active, no `-wal` / `-shm` file is
        created either."""
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise UnsupportedSQLite(
                f"SQLite {sqlite3.sqlite_version} is too old; Horae needs 3.37 or later"
            )
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be an int >= 0")
        path = Path(path)
        timeout = busy_timeout_ms / 1000
        if read_only:
            return cls._open_read_only(path, timeout)
        old_umask = os.umask(0o077)
        try:
            if _initialised(path):
                return cls._open_initialised(path, timeout)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with _init_lock(path, timeout):
                return cls._open_initialised(path, timeout)
        finally:
            os.umask(old_umask)

    @classmethod
    def _open_initialised(cls, path: Path, timeout: float) -> Self:
        """Create, switch to WAL and migrate (each a no-op when done already), then wrap."""
        _prepare_files(path)
        try:
            _enable_wal(path, timeout)
            conn = sqlite3.connect(path, timeout=timeout, autocommit=True)
        except sqlite3.Error as exc:
            raise translate(exc) from exc
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA secure_delete = FAST")
            migrate(conn)
            return cls._from_connection(conn, _path=path)
        except sqlite3.Error as exc:
            conn.close()
            raise translate(exc) from exc
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        if self._beat is not None:
            self._beat.close()
            self._beat = None
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
    @_api
    def schema_version(self) -> int:
        return user_version(self._conn)

    @_api
    def pragma(self, name: str) -> object:
        """Read one of a fixed set of PRAGMAs (for checks and diagnostics)."""
        sql = _PRAGMAS.get(name)
        if sql is None:
            raise ValueError(f"unsupported pragma {name!r}")
        value: object = self._conn.execute(sql).fetchone()[0]
        return value

    # --- transactions ----------------------------------------------------------------------

    def _execute(self, sql: str) -> None:
        try:
            self._conn.execute(sql)
        except sqlite3.Error as exc:
            raise translate(exc) from exc

    def _commit(self) -> None:
        try:
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                with suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
            raise translate(exc) from exc

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes atomically.

        The outermost block is a `BEGIN IMMEDIATE` transaction; nested blocks (up to
        `MAX_NESTING` deep) are savepoints, so an exception rolls back only its own block and
        the outer one can catch it and go on. Raises `StoreBusy` past the busy timeout, and
        `StoreError` if the commit fails (the transaction is then rolled back)."""
        if self._depth == 0:
            self._execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                if self._conn.in_transaction:
                    with suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._commit()
            return
        level = self._depth - 1
        if level >= MAX_NESTING:
            raise StoreError(f"transactions nested deeper than {MAX_NESTING}")
        self._execute(_SAVEPOINT[level])
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            self._execute(_ROLLBACK_TO[level])
            self._execute(_RELEASE[level])
            raise
        self._depth -= 1
        self._execute(_RELEASE[level])

    # --- runs ------------------------------------------------------------------------------

    def _run_status(self, run_id: str) -> str:
        row = self._conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRun(run_id)
        return str(row[0])

    def _require_running(self, run_id: str) -> None:
        status = self._run_status(run_id)
        if status != "running":
            raise RunNotRunning(run_id, status)

    # --- the secret gate (#53) ------------------------------------------------------------

    @staticmethod
    def _finding(names: set[str] | frozenset[str], where: str, how: str) -> list[str]:
        """Log (names only) one finding of the gate and return its violation detail (empty
        when `names` is). `how`: `redacted`, `kept`, `new` or `collided`."""
        if not names:
            return []
        redactor = current_redactor()
        shown = ", ".join(sorted(names))
        where = redactor.redact_text(where)[0]  # a target, say, can hold one too
        if how == "redacted":
            detail = f"the store redacted secret {shown} in {where} (a producer did not redact it)"
        elif how == "kept":
            detail = (
                f"the store found secret {shown} in {where} (kept as written: identifiers "
                "and numbers are exempt)"
            )
        elif how == "new":
            detail = (
                f"the store found secret {shown} in {where}, not in the note before (kept "
                "byte for byte: the journalled content is exempt)"
            )
        else:
            detail = (
                f"redacting secret {shown} made two object keys equal in {where}: the later "
                "one was renamed with a #2 (or next) suffix"
            )
        _log.warning("%s", detail)
        return [detail]

    def _findings(self, found: Findings, where: str) -> list[str]:
        return [
            *self._finding(found.redacted, where, "redacted"),
            *self._finding(found.flagged, where, "kept"),
            *self._finding(found.collisions, where, "collided"),
        ]

    def _write_flags(self, run_id: str | None, details: list[str]) -> None:
        """Keep `details` for `run_id`'s record (inside the caller's transaction)."""
        if run_id is None or not details:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO secret_flags (run_id, detail) VALUES (?, ?)",
            [(run_id, detail) for detail in details],
        )

    def _text(self, value: str, where: str) -> tuple[str, list[str]]:
        """Free text: redacted."""
        new, names = current_redactor().redact_text(value)
        return new, self._finding(names, where, "redacted")

    def _ident(self, value: str | None, where: str) -> list[str]:
        """An identifier: kept as written, flagged."""
        if value is None:
            return []
        return self._finding(current_redactor().names_in(value), where, "kept")

    def _new_in(self, data: bytes | None, before: bytes | None, where: str) -> list[str]:
        """Exempt content Horae wrote: flagged for the secrets it holds more often than
        `before` (a secret the user already had in their note is not flagged)."""
        if data is None:
            return []
        redactor = current_redactor()
        had = redactor.counts(before or b"")
        new = {name for name, count in redactor.counts(data).items() if count > had.get(name, 0)}
        return self._finding(new, where, "new")

    def _clean_record(self, record: RunRecord) -> tuple[RunRecord, list[int]]:
        """`record` with its free text redacted, and every finding of the gate for it (in
        it, and those kept in `secret_flags`) recorded as `secret_in_output` violations; also
        the ids of the `secret_flags` rows used (deleted when the record is saved)."""
        redactor = current_redactor()
        rows = self._conn.execute(
            "SELECT id, detail FROM secret_flags WHERE run_id = ? ORDER BY id", (record.id,)
        ).fetchall()
        if not redactor and not rows:
            return record, []
        data = record.model_dump(mode="json")
        found = scrub_record(data, redactor)
        details = [detail for _id, detail in rows] + self._findings(found, "the run record")
        if not details:
            return record, []
        violations = data["violations"]
        have = {v.get("detail") for v in violations if isinstance(v, dict)}
        new = list(dict.fromkeys(d for d in details if d not in have))
        room = MAX_ITEMS - len(violations)
        if len(new) > room > 0:
            rest = len(new) - (room - 1)
            new = [
                *new[: room - 1],
                f"the store's secret gate made {rest} more findings for this run (names "
                "only, in the horae.store log)",
            ]
        for detail in new[: max(room, 0)]:
            violations.append({"kind": "secret_in_output", "detail": detail})
        try:
            cleaned = RunRecord.model_validate_json(json.dumps(data))
        except ValidationError:
            name = ", ".join(sorted(found.redacted)) or "?"
            raise SecretRefused(name, f"run record {record.id!r}") from None
        return cleaned, [flag_id for flag_id, _detail in rows]

    def _step_json(self, run_id: str, index: int, step: StepRecord) -> tuple[str, list[str]]:
        """The step row's JSON, its free text redacted, and the gate's findings."""
        redactor = current_redactor()
        if not redactor:
            return step.model_dump_json(), []
        data = step.model_dump(mode="json")
        found = scrub_step(data, redactor)
        details = self._findings(found, f"step {index}")
        if not found.redacted and not found.collisions:
            return step.model_dump_json(), details
        try:
            return StepRecord.model_validate_json(json.dumps(data)).model_dump_json(), details
        except ValidationError:
            name = ", ".join(sorted(found.redacted))
            raise SecretRefused(name, f"step {index} of run {run_id!r}") from None

    @_api
    def begin_run(
        self,
        run_id: str,
        pipeline: str,
        started_at: datetime,
        *,
        dry_run: bool = False,
        pid: int | None = None,
        host: str | None = None,
    ) -> None:
        """Record that a run started, before it has a run record (the journal needs the row).

        Takes the pipeline's lock: raises `PipelineBusy` (naming the holder) while another run
        of `pipeline` is `running`, and `RunExists` for a duplicate id. `pid` and `host`
        default to this process; `heartbeat` keeps the run alive."""
        started = _ts(started_at)
        owner_pid = os.getpid() if pid is None else pid
        owner_host = (socket.gethostname() or None) if host is None else host
        with self.transaction():
            if self._conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
                raise RunExists(run_id)
            holder = self._conn.execute(
                "SELECT id FROM runs WHERE pipeline = ? AND status = 'running'", (pipeline,)
            ).fetchone()
            if holder is not None:
                raise PipelineBusy(pipeline, holder[0])
            self._conn.execute(
                "INSERT INTO runs (id, pipeline, status, dry_run, started_at, pid, host, "
                "heartbeat_at) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (run_id, pipeline, int(dry_run), started, owner_pid, owner_host, started),
            )
            self._write_flags(run_id, self._ident(owner_host, "the run's host"))

    @_api
    def heartbeat(self, run_id: str, at: datetime) -> None:
        """Record that a running run's process is alive (`RunNotRunning` once it is not).

        It waits at most `HEARTBEAT_BUSY_TIMEOUT_MS` for the write lock (on a second
        connection with that busy timeout), then raises `StoreBusy`: a heartbeat must not
        block the event loop for long. Heartbeats are for observability (`horae runs`): the
        runner's pipeline lock, not the heartbeat, decides whether a run is alive."""
        stamp = _ts(at)
        if self._path is None or self._read_only or self._depth:
            with self.transaction():
                cur = self._conn.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                    (stamp, run_id),
                )
                if cur.rowcount != 1:
                    self._require_running(run_id)
            return
        if self._beat is None:
            self._beat = sqlite3.connect(
                self._path, timeout=HEARTBEAT_BUSY_TIMEOUT_MS / 1000, autocommit=True
            )
            self._beat.execute("PRAGMA secure_delete = FAST")
        beat = self._beat
        beat.execute("BEGIN IMMEDIATE")
        try:
            cur = beat.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                (stamp, run_id),
            )
            row = None
            if cur.rowcount != 1:
                row = beat.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            beat.execute("COMMIT")
        except BaseException:
            if beat.in_transaction:
                with suppress(sqlite3.Error):
                    beat.execute("ROLLBACK")
            raise
        if cur.rowcount != 1:
            if row is None:
                raise UnknownRun(run_id)
            raise RunNotRunning(run_id, str(row[0]))

    @_api
    def stale_runs(self, older_than: datetime) -> list[RunSummary]:
        """Running runs with no heartbeat since `older_than`, oldest first (for recovery)."""
        rows = self._conn.execute(
            "SELECT id, pipeline, status, dry_run, started_at, finished_at, tokens_in, "
            "tokens_out, cost_usd, pid, host, heartbeat_at, abandoned_at, error FROM runs "
            "WHERE status = 'running' AND coalesce(heartbeat_at, started_at) < ? "
            "ORDER BY started_at, id",
            (_ts(older_than),),
        ).fetchall()
        return [self._summary(row) for row in rows]

    @_api
    def abandon_run(
        self,
        run_id: str,
        *,
        at: datetime,
        error: str,
        heartbeat_before: datetime | None = None,
    ) -> None:
        """Mark a running run whose process died as `failed`, without a run record.

        This releases the store's pipeline guard. Its journal stays, for undo. Its usage
        ledger rows still `reserved` (calls its process never settled) become `settled` at
        their reservation, marked `estimated` (schemas decision 41 D). It is a
        compare-and-set: it changes run `run_id` only while it is still `running` (else
        `RunNotRunning` / `UnknownRun`) and, with `heartbeat_before`, only if its last
        heartbeat (or its start) is not later than that, i.e. the row has not changed since
        it was read (else `RunNotStale`)."""
        stamp = _ts(at)
        before = None if heartbeat_before is None else _ts(heartbeat_before)
        error, _found = self._text(error, f"the error of abandoned run {run_id}")
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE runs SET status = 'failed', finished_at = ?, abandoned_at = ?, "
                "error = ? WHERE id = ? AND status = 'running' "
                "AND (? IS NULL OR coalesce(heartbeat_at, started_at) <= ?)",
                (stamp, stamp, error, run_id, before, before),
            )
            if cur.rowcount != 1:
                self._require_running(run_id)
                raise RunNotStale(run_id)
            self._conn.execute(
                "UPDATE llm_usage SET state = 'settled', estimated = 1, settled_at = ? "
                "WHERE run_id = ? AND state = 'reserved'",
                (stamp, run_id),
            )
            # The run gets no record: its findings were logged, nothing to record them on.
            self._conn.execute("DELETE FROM secret_flags WHERE run_id = ?", (run_id,))

    @_api
    def save_run(self, record: RunRecord) -> None:
        """Insert or replace a run's record (also its step rows).

        Replacing keeps the pipeline, `dry_run` and `started_at` (a mismatch raises
        `InvalidRow`), e.g. to mark a run `undone`. An abandoned run cannot get a record.
        Secrets are redacted and flagged first (the secret gate, see the module docstring)."""
        record, flag_ids = self._clean_record(record)
        started = _ts(record.started_at)
        values = (
            record.status,
            _ts(record.finished_at),
            record.cost.tokens_in,
            record.cost.tokens_out,
            record.cost.usd,
            record.error,
            record.model_dump_json(),
            record.id,
        )
        with self.transaction():
            row = self._conn.execute(
                "SELECT pipeline, dry_run, started_at, abandoned_at FROM runs WHERE id = ?",
                (record.id,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO runs (status, finished_at, tokens_in, tokens_out, cost_usd, "
                    "error, record, id, pipeline, dry_run, started_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, record.pipeline, int(record.dry_run), started),
                )
            else:
                pipeline, dry_run, stored_start, abandoned_at = row
                if pipeline != record.pipeline:
                    raise InvalidRow(f"run {record.id!r} exists for pipeline {pipeline!r}")
                if bool(dry_run) != record.dry_run:
                    raise InvalidRow(f"run {record.id!r} exists with dry_run={bool(dry_run)}")
                if stored_start != started:
                    raise InvalidRow(f"run {record.id!r} exists with started_at {stored_start}")
                if abandoned_at is not None:
                    raise InvalidRow(f"run {record.id!r} was abandoned at {abandoned_at}")
                self._conn.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, tokens_in = ?, "
                    "tokens_out = ?, cost_usd = ?, error = ?, record = ? WHERE id = ?",
                    values,
                )
            self._conn.execute("DELETE FROM steps WHERE run_id = ?", (record.id,))
            for index, step in enumerate(record.steps):
                self._insert_step(record.id, index, step.model_dump_json(), step)
            self._conn.executemany(
                "DELETE FROM secret_flags WHERE id = ?", [(flag_id,) for flag_id in flag_ids]
            )

    @_api
    def get_run(self, run_id: str) -> RunRecord | None:
        """The run record, or `None` if the run is unknown, still running or abandoned."""
        row = self._conn.execute("SELECT record FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None or row[0] is None:
            return None
        return RunRecord.model_validate_json(row[0])

    @_api
    def last_accepted_llm(self, pipeline: str) -> PreviousLlm | None:
        """The accepted LLM output of `pipeline`'s last run, for the unchanged-input path.

        Only the most recent finished, non-dry-run run counts, and only if it is `ok` or
        `noop`: after a `degraded`, `failed` or `undone` run (or with no run, or no accepted
        LLM output in it) this is `None`, so the next run calls the LLM again. Read from the
        stored run record: its `input_hash`, and the last schema-valid `LlmCall` of its `llm`
        step if that step is `ok`. A stored record that no longer validates, or a call without
        recorded intents (`intents` is `None`), is not reused either."""
        row = self._conn.execute(
            "SELECT status, record FROM runs WHERE pipeline = ? AND dry_run = 0 "
            "AND status != 'running' ORDER BY started_at DESC, id DESC LIMIT 1",
            (pipeline,),
        ).fetchone()
        if row is None or row[0] not in ("ok", "noop") or row[1] is None:
            return None
        try:
            record = RunRecord.model_validate_json(row[1])
        except ValidationError:
            return None
        if record.input_hash is None:
            return None
        steps = [i for i, step in enumerate(record.steps) if step.kind == "llm"]
        if not steps or record.steps[steps[0]].status != "ok":
            return None
        calls = [c for c in record.llm if c.step == steps[0] and c.schema_valid is not False]
        if not calls:
            return None
        call = calls[-1]
        if call.intents is None:
            return None
        return PreviousLlm(
            input_hash=record.input_hash,
            output=call.response,
            intents=list(call.intents),
            skill_version=call.skill_version,
            model=call.model,
            backend=call.backend,
        )

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
            pid=row[9],
            host=row[10],
            heartbeat_at=_opt_dt(row[11]),
            abandoned_at=_opt_dt(row[12]),
            error=row[13],
        )

    @_api
    def run_summary(self, run_id: str) -> RunSummary | None:
        row = self._conn.execute(
            "SELECT id, pipeline, status, dry_run, started_at, finished_at, tokens_in, "
            "tokens_out, cost_usd, pid, host, heartbeat_at, abandoned_at, error FROM runs "
            "WHERE id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else self._summary(row)

    @_api
    def list_runs(
        self,
        *,
        pipeline: str | None = None,
        status: StoreRunStatus | None = None,
        limit: int = 50,
    ) -> list[RunSummary]:
        """Runs, newest first, optionally filtered; `0 <= limit <= MAX_LIST_RUNS`."""
        if type(limit) is not int or not 0 <= limit <= MAX_LIST_RUNS:
            raise ValueError(f"limit must be an int between 0 and {MAX_LIST_RUNS}")
        rows = self._conn.execute(
            "SELECT id, pipeline, status, dry_run, started_at, finished_at, tokens_in, "
            "tokens_out, cost_usd, pid, host, heartbeat_at, abandoned_at, error FROM runs "
            "WHERE (:pipeline IS NULL OR pipeline = :pipeline) "
            "AND (:status IS NULL OR status = :status) "
            "ORDER BY started_at DESC, id DESC LIMIT :limit",
            {"pipeline": pipeline, "status": status, "limit": limit},
        ).fetchall()
        return [self._summary(row) for row in rows]

    def _insert_step(self, run_id: str, index: int, record: str, step: StepRecord) -> None:
        self._conn.execute(
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
                record,
            ),
        )

    @_api
    def record_step(self, run_id: str, index: int, step: StepRecord) -> None:
        """Record one step of a running run as it finishes.

        `index` is the next step (the number recorded so far) or an existing one to replace;
        anything else raises `InvalidRow`. A run that is not `running` raises
        `RunNotRunning`: once a run has its record, `save_run` owns its steps."""
        if type(index) is not int or index < 0:
            raise ValueError("step index must be an int >= 0")
        record, details = self._step_json(run_id, index, step)
        with self.transaction():
            self._require_running(run_id)
            (count,) = self._conn.execute(
                "SELECT count(*) FROM steps WHERE run_id = ?", (run_id,)
            ).fetchone()
            if index > count:
                raise InvalidRow(f"step {index} of run {run_id!r}: the next step is {count}")
            self._insert_step(run_id, index, record, step)
            self._write_flags(run_id, details)

    @_api
    def steps(self, run_id: str) -> list[StepRecord]:
        """A run's steps: from its record once it has one, else as recorded so far."""
        row = self._conn.execute("SELECT record FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return []
        if row[0] is not None:
            return RunRecord.model_validate_json(row[0]).steps
        rows = self._conn.execute(
            "SELECT record FROM steps WHERE run_id = ? ORDER BY idx", (run_id,)
        ).fetchall()
        return [StepRecord.model_validate_json(record) for (record,) in rows]

    @_api
    def prune_runs(self, older_than: datetime) -> int:
        """Delete finished runs that finished before `older_than`; return how many.

        Their steps and journal go with them (tasks, notifications and usage ledger rows keep
        their rows, without the run: a day's ledger totals never change; the ledger has its
        own retention, `prune_usage`), and journal contents no longer referenced are
        deleted."""
        with self.transaction():
            cur = self._conn.execute(
                "DELETE FROM runs WHERE status <> 'running' AND finished_at < ?",
                (_ts(older_than),),
            )
            pruned = cur.rowcount
            self._conn.execute(
                "DELETE FROM blobs WHERE NOT EXISTS "
                "(SELECT 1 FROM journal WHERE journal.before_sha256 = blobs.sha256) "
                "AND NOT EXISTS (SELECT 1 FROM journal WHERE journal.after_sha256 = blobs.sha256) "
                "AND NOT EXISTS "
                "(SELECT 1 FROM journal WHERE journal.actual_after_sha256 = blobs.sha256) "
                "AND NOT EXISTS "
                "(SELECT 1 FROM journal WHERE journal.undo_overwrote_sha256 = blobs.sha256)"
            )
        return pruned

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
            before_sha256=row[6],
            after_sha256=row[7],
            undoable=bool(row[8]),
            applied=bool(row[9]),
            undone_at=_opt_dt(row[10]),
            created_at=_dt(row[11]),
            applied_by=row[12],
            sink_identity=row[13],
            actual_after_sha256=row[14],
            undo_overwrote_sha256=row[15],
        )

    @staticmethod
    def _check_size(content: bytes | None) -> None:
        if content is not None and len(content) > MAX_JOURNAL_BYTES:
            raise JournalTooLarge(
                f"journalled content of {len(content)} bytes exceeds {MAX_JOURNAL_BYTES}"
            )

    def _put_blob(self, data: bytes | None) -> str | None:
        if data is None:
            return None
        digest = hashlib.sha256(data).hexdigest()
        self._conn.execute(
            "INSERT INTO blobs (sha256, data, size) VALUES (?, ?, ?) "
            "ON CONFLICT (sha256) DO NOTHING",
            (digest, data, len(data)),
        )
        return digest

    @_api
    def journal_append(
        self,
        run_id: str,
        *,
        intent: int,
        intent_type: IntentType,
        target: str,
        before: bytes | None = None,
        after: bytes | None = None,
        undoable: bool | None = None,
        applied_by: str | None = None,
        sink_identity: str | None = None,
        at: datetime,
    ) -> JournalEntry:
        """Record a change of a running run before applying it; `journal_mark_applied` after.

        `undoable` defaults to what the intent type allows: `notify.push` and `http.post`
        cannot be undone (and `True` is refused for them); an undoable change needs a `before`
        or an `after`. Each content is at most `MAX_JOURNAL_BYTES` (`JournalTooLarge`).
        `applied_by` names the sink applying it (so undo can find it again) and
        `sink_identity` where it writes (so undo can tell another vault from this one)."""
        if intent_type not in INTENT_TYPES:
            raise ValueError(f"unknown intent type {intent_type!r}")
        if undoable is None:
            undoable = intent_type not in _NOT_UNDOABLE
        elif undoable and intent_type in _NOT_UNDOABLE:
            raise ValueError(f"{intent_type} cannot be undone")
        if undoable and before is None and after is None:
            raise ValueError("an undoable change needs `before` or `after`")
        for content in (before, after):
            self._check_size(content)
        # The secret gate: `before` is exempt (the user's note, restored byte for byte by
        # undo); `after` is exempt, flagged for a secret not already in `before`; the other
        # columns are identifiers (exempt, flagged).
        details = [
            *self._ident(target, "a journal target"),
            *self._ident(applied_by, "a journal entry's sink"),
            *self._ident(sink_identity, "a journal entry's sink identity"),
            *self._new_in(after, before, f"the content Horae wrote to {target}"),
        ]
        with self.transaction():
            self._require_running(run_id)
            before_sha = self._put_blob(before)
            after_sha = self._put_blob(after)
            (row,) = self._conn.execute(
                "INSERT INTO journal (run_id, seq, intent, intent_type, target, before_sha256, "
                "after_sha256, undoable, created_at, applied_by, sink_identity) VALUES (:run_id, "
                "(SELECT coalesce(max(seq) + 1, 0) FROM journal WHERE run_id = :run_id), "
                ":intent, :intent_type, :target, :before, :after, :undoable, :at, :applied_by, "
                ":sink_identity) RETURNING id, run_id, seq, intent, intent_type, target, "
                "before_sha256, after_sha256, undoable, applied, undone_at, created_at, "
                "applied_by, sink_identity, actual_after_sha256, undo_overwrote_sha256",
                {
                    "run_id": run_id,
                    "intent": intent,
                    "intent_type": intent_type,
                    "target": target,
                    "before": before_sha,
                    "after": after_sha,
                    "undoable": int(undoable),
                    "at": _ts(at),
                    "applied_by": applied_by,
                    "sink_identity": sink_identity,
                },
            ).fetchall()
            self._write_flags(run_id, details)
        return self._entry(row)

    @_api
    def journal_mark_applied(self, entry_id: int, *, actual_after: bytes | None = None) -> None:
        """Record that a change was applied; `actual_after`: the bytes the sink really wrote,
        when they are not the journalled `after` (kept as a blob, for undo)."""
        self._check_size(actual_after)
        with self.transaction():
            actual = self._put_blob(actual_after)
            cur = self._conn.execute(
                "UPDATE journal SET applied = 1, actual_after_sha256 = ? WHERE id = ?",
                (actual, entry_id),
            )
            if cur.rowcount != 1:
                raise UnknownJournalEntry(entry_id)
            if actual_after is not None and current_redactor().spans(actual_after):
                run_id, target, before_sha = self._conn.execute(
                    "SELECT run_id, target, before_sha256 FROM journal WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                before = None if before_sha is None else self.blob(before_sha)
                where = f"the content a sink wrote to {target}"
                self._write_flags(run_id, self._new_in(actual_after, before, where))

    @_api
    def journal_mark_undone(
        self, entry_id: int, *, at: datetime, overwrote: bytes | None = None
    ) -> None:
        """Record that an undoable change was reverted (once); `overwrote`: the bytes a forced
        undo replaced (kept as a blob, so they can be recovered)."""
        self._check_size(overwrote)
        with self.transaction():
            overwrote_sha = self._put_blob(overwrote)
            cur = self._conn.execute(
                "UPDATE journal SET undone_at = ?, undo_overwrote_sha256 = ? "
                "WHERE id = ? AND undoable = 1 AND undone_at IS NULL",
                (_ts(at), overwrote_sha, entry_id),
            )
            if cur.rowcount == 1:
                return
            row = self._conn.execute(
                "SELECT undoable FROM journal WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise UnknownJournalEntry(entry_id)
            if not row[0]:
                raise InvalidRow(f"journal entry {entry_id} is not undoable")
            raise InvalidRow(f"journal entry {entry_id} is already undone")

    @_api
    def journal(self, run_id: str) -> list[JournalEntry]:
        """A run's journal, in the order the changes were recorded."""
        rows = self._conn.execute(
            "SELECT id, run_id, seq, intent, intent_type, target, before_sha256, after_sha256, "
            "undoable, applied, undone_at, created_at, applied_by, sink_identity, "
            "actual_after_sha256, undo_overwrote_sha256 FROM journal "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        return [self._entry(row) for row in rows.fetchall()]

    @_api
    def journal_later(self, run_id: str, target: str, after_id: int) -> list[JournalEntry]:
        """Undoable entries of *other* runs for `target` recorded after entry `after_id`, not
        undone, in the order they were recorded (undo is last-in, first-out per note)."""
        rows = self._conn.execute(
            "SELECT id, run_id, seq, intent, intent_type, target, before_sha256, after_sha256, "
            "undoable, applied, undone_at, created_at, applied_by, sink_identity, "
            "actual_after_sha256, undo_overwrote_sha256 FROM journal "
            "WHERE target = ? AND id > ? AND undoable = 1 AND run_id <> ? "
            "AND undone_at IS NULL ORDER BY id",
            (target, after_id, run_id),
        )
        return [self._entry(row) for row in rows.fetchall()]

    @_api
    def blob(self, sha256: str) -> bytes | None:
        """Journalled content by digest, or `None` if it is not stored."""
        row = self._conn.execute(
            "SELECT data FROM blobs WHERE sha256 = ?", (_sha(sha256),)
        ).fetchone()
        return None if row is None else bytes(row[0])

    # --- LLM cache -------------------------------------------------------------------------

    @_api
    def cache_get(self, key: str, *, now: datetime) -> CacheEntry | None:
        """The cached response for a request hash, unless missing or expired at `now`."""
        row = self._conn.execute(
            "SELECT key, skill, skill_version, model, response, tokens_in, tokens_out, cost_usd, "
            "created_at, expires_at FROM llm_cache "
            "WHERE key = ? AND (expires_at IS NULL OR expires_at > ?)",
            (_sha(key), _ts(now)),
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

    @_api
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
        """Insert or replace the response for a request hash. An entry holding a secret (in
        any string, object key or number) is refused (`SecretRefused`, nothing stored): a
        hit would replay it into later runs. Callers treat that as "do not cache"
        (`cache_put_safe`)."""
        names = current_redactor().find([skill, skill_version, model, response])
        if names:
            name = ", ".join(sorted(names))
            _log.warning("secret %s found in an LLM cache entry by the store; refused", name)
            raise SecretRefused(name, "the LLM cache entry")
        with self.transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_cache (key, skill, skill_version, model, response, "
                "tokens_in, tokens_out, cost_usd, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _sha(key),
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

    def cache_put_safe(
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
    ) -> bool:
        """`cache_put`, treating `SecretRefused` as "do not cache": returns whether the entry
        was stored. The cache's writer (#32) uses this, so a response holding a secret is
        simply not cached (the run goes on; the refusal is logged by name)."""
        try:
            self.cache_put(
                key,
                skill=skill,
                skill_version=skill_version,
                model=model,
                response=response,
                usage=usage,
                at=at,
                expires_at=expires_at,
            )
        except SecretRefused:
            return False
        return True

    @_api
    def cache_purge_expired(self, now: datetime) -> int:
        """Delete entries expired at `now`; return how many."""
        with self.transaction():
            cur = self._conn.execute("DELETE FROM llm_cache WHERE expires_at <= ?", (_ts(now),))
        return cur.rowcount

    @_api
    def cache_purge(
        self, *, skill: str | None = None, created_before: datetime | None = None
    ) -> int:
        """Delete cached responses (all of them, or a skill's, or older ones); return how many."""
        with self.transaction():
            cur = self._conn.execute(
                "DELETE FROM llm_cache WHERE (:skill IS NULL OR skill = :skill) "
                "AND (:before IS NULL OR created_at < :before)",
                {"skill": skill, "before": _opt_ts(created_before)},
            )
        return cur.rowcount

    # --- task index ------------------------------------------------------------------------

    def _require_run(self, run_id: str | None) -> None:
        if run_id is not None:
            self._run_status(run_id)

    @_api
    def upsert_task(self, task: TaskEntry, *, at: datetime) -> None:
        """Insert or replace a task by fingerprint (`created_at` is kept)."""
        stamp = _ts(at)
        until, tz, wake = _snooze(task.status, task.snoozed_until, task.snooze_tz)
        title, details = self._text(task.title, "a task title")
        details += [
            *self._ident(task.fingerprint, "a task fingerprint"),
            *self._ident(task.source, "a task source"),
            *self._ident(task.target, "a task target"),
        ]
        with self.transaction():
            self._require_run(task.run_id)
            self._write_flags(task.run_id, details)
            self._conn.execute(
                "INSERT INTO tasks (fingerprint, title, status, due, time, snoozed_until, "
                "snooze_tz, wake_at, target, source, run_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (fingerprint) DO UPDATE SET title = excluded.title, "
                "status = excluded.status, due = excluded.due, time = excluded.time, "
                "snoozed_until = excluded.snoozed_until, snooze_tz = excluded.snooze_tz, "
                "wake_at = excluded.wake_at, target = excluded.target, "
                "source = excluded.source, run_id = excluded.run_id, "
                "updated_at = excluded.updated_at",
                (
                    task.fingerprint,
                    title,
                    task.status,
                    None if task.due is None else task.due.isoformat(),
                    task.time,
                    until,
                    tz,
                    wake,
                    task.target,
                    task.source,
                    task.run_id,
                    stamp,
                    stamp,
                ),
            )

    @_api
    def set_task_status(
        self,
        fingerprint: str,
        status: TaskStatus,
        *,
        until: date | datetime | None = None,
        tz: str | None = None,
        at: datetime,
        run_id: str | None = None,
    ) -> None:
        """Change a task's status (`task.update_status`), in one update.

        `until` is required for `snoozed` and only allowed there; a date needs `tz`, the IANA
        zone it is in. `run_id`, when given, becomes the run that last wrote the task.
        Raises `UnknownTask` and `UnknownRun`."""
        if status not in _TASK_STATUSES:
            raise ValueError(f"unknown task status {status!r}")
        snoozed_until, snooze_tz, wake = _snooze(status, until, tz)
        with self.transaction():
            self._require_run(run_id)
            cur = self._conn.execute(
                "UPDATE tasks SET status = ?, snoozed_until = ?, snooze_tz = ?, wake_at = ?, "
                "run_id = coalesce(?, run_id), updated_at = ? WHERE fingerprint = ?",
                (status, snoozed_until, snooze_tz, wake, run_id, _ts(at), fingerprint),
            )
            if cur.rowcount != 1:
                raise UnknownTask(fingerprint)

    @staticmethod
    def _task(row: tuple[Any, ...]) -> TaskEntry:
        return TaskEntry.model_validate(
            {
                "fingerprint": row[0],
                "title": row[1],
                "status": row[2],
                "due": row[3],
                "time": row[4],
                "snoozed_until": row[5],
                "snooze_tz": row[6],
                "target": row[7],
                "source": row[8],
                "run_id": row[9],
            }
        )

    @_api
    def get_task(self, fingerprint: str) -> TaskEntry | None:
        row = self._conn.execute(
            "SELECT fingerprint, title, status, due, time, snoozed_until, snooze_tz, target, "
            "source, run_id FROM tasks WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return None if row is None else self._task(row)

    @_api
    def list_tasks(self, *, status: TaskStatus | None = None) -> list[TaskEntry]:
        """Tasks by due date (undated last), then fingerprint."""
        rows = self._conn.execute(
            "SELECT fingerprint, title, status, due, time, snoozed_until, snooze_tz, target, "
            "source, run_id FROM tasks WHERE (:status IS NULL OR status = :status) "
            "ORDER BY due IS NULL, due, fingerprint",
            {"status": status},
        ).fetchall()
        return [self._task(row) for row in rows]

    @_api
    def due_snoozed(self, now: datetime) -> list[TaskEntry]:
        """Snoozed tasks whose wake time is at or before `now`, earliest first."""
        rows = self._conn.execute(
            "SELECT fingerprint, title, status, due, time, snoozed_until, snooze_tz, target, "
            "source, run_id FROM tasks WHERE status = 'snoozed' AND wake_at <= ? "
            "ORDER BY wake_at, fingerprint",
            (_ts(now),),
        ).fetchall()
        return [self._task(row) for row in rows]

    # --- notification state ----------------------------------------------------------------

    @_api
    def record_notification(
        self, key: str, *, at: datetime, run_id: str | None = None
    ) -> NotificationState:
        """Record a send for a dedupe key and return the updated state."""
        stamp = _ts(at)
        details = self._ident(key, "a notification key")
        with self.transaction():
            self._require_run(run_id)
            self._write_flags(run_id, details)
            (row,) = self._conn.execute(
                "INSERT INTO notifications (key, run_id, first_sent_at, last_sent_at, count) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT (key) DO UPDATE SET "
                "run_id = coalesce(excluded.run_id, run_id), "
                "last_sent_at = excluded.last_sent_at, count = count + 1 "
                "RETURNING key, run_id, first_sent_at, last_sent_at, count",
                (key, run_id, stamp, stamp),
            ).fetchall()
        return NotificationState(row[0], row[1], _dt(row[2]), _dt(row[3]), row[4])

    @_api
    def get_notification(self, key: str) -> NotificationState | None:
        row = self._conn.execute(
            "SELECT key, run_id, first_sent_at, last_sent_at, count FROM notifications "
            "WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return NotificationState(row[0], row[1], _dt(row[2]), _dt(row[3]), row[4])

    # --- usage ledger (#33) ----------------------------------------------------------------

    @_api
    def day_spend(self, day: date, *, pipeline: str | None = None) -> DaySpend:
        """The ledger's spend on local `day`, of every pipeline or of `pipeline` only.

        Call it inside `transaction()` together with `reserve_usage`, so that no other
        process reserves in between (the daily ceilings). The `day` of a row is the one
        stored when it was reserved."""
        row = self._conn.execute(
            "SELECT coalesce(sum(usd), 0), coalesce(sum(tokens_out), 0) FROM llm_usage "
            "WHERE day = :day AND (:pipeline IS NULL OR pipeline = :pipeline)",
            {"day": day.isoformat(), "pipeline": pipeline},
        ).fetchone()
        return DaySpend(usd=float(row[0]), tokens_out=int(row[1]))

    @_api
    def reserve_usage(
        self,
        run_id: str,
        *,
        step: int,
        attempt: int,
        alias: str,
        at: datetime,
        day: date,
        usd: float,
        tokens_out: int,
        tokens_in: int = 0,
        backend: str | None = None,
        model: str | None = None,
        priced: bool | None = None,
    ) -> int:
        """Record a call about to be made, at the most it can cost; return the row's id.

        The run must be `running` (`RunNotRunning`, `UnknownRun`). `day` is stored as given
        (the local day at reservation time; a later time zone change does not move it).
        `settle_usage` replaces the reservation with what the call cost; `release_usage`
        drops it (no call made)."""
        if type(step) is not int or step < 0 or type(attempt) is not int or attempt < 1:
            raise ValueError("step must be an int >= 0 and attempt an int >= 1")
        if not usd >= 0 or tokens_out < 0 or tokens_in < 0:
            raise ValueError("a reservation is not negative")
        details = [
            *self._ident(alias, "the usage ledger"),
            *self._ident(backend, "the usage ledger"),
            *self._ident(model, "the usage ledger"),
        ]
        with self.transaction():
            self._require_running(run_id)
            self._write_flags(run_id, list(dict.fromkeys(details)))
            (pipeline,) = self._conn.execute(
                "SELECT pipeline FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            cur = self._conn.execute(
                "INSERT INTO llm_usage (run_id, pipeline, step, attempt, alias, backend, model, "
                "state, tokens_in, tokens_out, usd, priced, at, day) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    pipeline,
                    step,
                    attempt,
                    alias,
                    backend,
                    model,
                    tokens_in,
                    tokens_out,
                    usd,
                    None if priced is None else int(priced),
                    _ts(at),
                    day.isoformat(),
                ),
            )
            entry = cur.lastrowid
        if entry is None:  # pragma: no cover - an INSERT always sets it
            raise StoreError("the reservation has no id")
        return entry

    @_api
    def settle_usage(
        self,
        entry_id: int,
        usage: Usage | None,
        *,
        at: datetime,
        backend: str | None = None,
        model: str | None = None,
    ) -> Overrun | None:
        """Replace a reservation with what the call cost; `usage=None` keeps the reserved
        amounts, marked `estimated` (the provider reported nothing: a timeout, say).
        Settling twice, or a row pruned by `prune_usage`, is a no-op.

        Returns an `Overrun` when the call cost more (`usd` or `tokens_out`) than it
        reserved; the row is then marked `over_reservation` (the caller logs it)."""
        stamp = _ts(at)
        details = list(
            dict.fromkeys(
                [*self._ident(backend, "the usage ledger"), *self._ident(model, "the usage ledger")]
            )
        )
        with self.transaction():
            if details:
                found = self._conn.execute(
                    "SELECT run_id FROM llm_usage WHERE id = ?", (entry_id,)
                ).fetchone()
                if found is not None and found[0] is not None:
                    self._write_flags(found[0], details)
            if usage is None:
                self._conn.execute(
                    "UPDATE llm_usage SET state = 'settled', estimated = 1, settled_at = ?, "
                    "backend = coalesce(?, backend), model = coalesce(?, model) "
                    "WHERE id = ? AND state = 'reserved'",
                    (stamp, backend, model, entry_id),
                )
                return None
            row = self._conn.execute(
                "SELECT usd, tokens_out FROM llm_usage WHERE id = ? AND state = 'reserved'",
                (entry_id,),
            ).fetchone()
            if row is None:
                return None
            reserved_usd, reserved_tokens = float(row[0]), int(row[1])
            over = (
                usage.usd > reserved_usd * (1 + _USD_TOLERANCE) + _USD_TOLERANCE
                or usage.tokens_out > reserved_tokens
            )
            self._conn.execute(
                "UPDATE llm_usage SET state = 'settled', settled_at = ?, tokens_in = ?, "
                "tokens_out = ?, cache_read_tokens = ?, cache_write_tokens = ?, usd = ?, "
                "priced = coalesce(?, priced), backend = coalesce(?, backend), "
                "model = coalesce(?, model), over_reservation = ? "
                "WHERE id = ? AND state = 'reserved'",
                (
                    stamp,
                    usage.tokens_in,
                    usage.tokens_out,
                    usage.cache_read_tokens,
                    usage.cache_write_tokens,
                    usage.usd,
                    None if usage.priced is None else int(usage.priced),
                    backend,
                    model,
                    int(over),
                    entry_id,
                ),
            )
        if not over:
            return None
        return Overrun(reserved_usd, usage.usd, reserved_tokens, usage.tokens_out)

    @_api
    def prune_usage(self, older_than: date) -> int:
        """Delete the settled usage ledger rows of local days before `older_than`; return
        how many. The ledger's own retention (decision 41 C): independent of `prune_runs`.
        Rows still `reserved` are kept (they still count)."""
        with self.transaction():
            cur = self._conn.execute(
                "DELETE FROM llm_usage WHERE state = 'settled' AND day < ?",
                (older_than.isoformat(),),
            )
        return cur.rowcount

    @_api
    def release_usage(self, entry_id: int) -> None:
        """Drop a reservation whose call was not made (a no-op once it is settled)."""
        with self.transaction():
            self._conn.execute(
                "DELETE FROM llm_usage WHERE id = ? AND state = 'reserved'", (entry_id,)
            )

    @_api
    def usage_report(
        self,
        *,
        since: date | None = None,
        until: date | None = None,
        pipeline: str | None = None,
    ) -> list[UsageGroup]:
        """The ledger grouped by local day, pipeline, alias, backend and model, oldest day
        first; `since` and `until` are inclusive local days."""
        rows = self._conn.execute(
            "SELECT day, pipeline, alias, backend, model, count(*), sum(tokens_in), "
            "sum(tokens_out), coalesce(sum(cache_read_tokens), 0), "
            "coalesce(sum(cache_write_tokens), 0), sum(usd), "
            "sum(CASE WHEN priced = 0 THEN 1 ELSE 0 END), "
            "sum(CASE WHEN state = 'reserved' THEN 1 ELSE 0 END), "
            "sum(estimated), sum(over_reservation) "
            "FROM llm_usage WHERE (:since IS NULL OR day >= :since) "
            "AND (:until IS NULL OR day <= :until) "
            "AND (:pipeline IS NULL OR pipeline = :pipeline) "
            "GROUP BY day, pipeline, alias, backend, model "
            "ORDER BY day, pipeline, alias, backend, model",
            {
                "since": None if since is None else since.isoformat(),
                "until": None if until is None else until.isoformat(),
                "pipeline": pipeline,
            },
        ).fetchall()
        return [
            UsageGroup(
                day=row[0],
                pipeline=row[1],
                alias=row[2],
                backend=row[3],
                model=row[4],
                calls=int(row[5]),
                tokens_in=int(row[6]),
                tokens_out=int(row[7]),
                cache_read_tokens=int(row[8]),
                cache_write_tokens=int(row[9]),
                usd=float(row[10]),
                unpriced=int(row[11]),
                in_flight=int(row[12]),
                estimated=int(row[13]),
                over_reservation=int(row[14]),
            )
            for row in rows
        ]
