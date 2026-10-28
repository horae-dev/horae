# SPDX-License-Identifier: Apache-2.0
"""The store's errors.

Every failure the store reports to its callers is a `StoreError`; `sqlite3.Error` never escapes
the public API (it is translated by `translate`). `ValueError` (and pydantic's
`ValidationError`, a `ValueError`) is kept for programming errors: arguments that are wrong
whatever the database holds, such as a naive datetime, an unknown intent type, a malformed cache
key or an out-of-range limit.
"""

import sqlite3


class StoreError(Exception):
    """The store cannot do what was asked."""


class SchemaTooNewError(StoreError):
    """The database was written by a newer Horae (its schema version is unknown here)."""


class SchemaTooOldError(StoreError):
    """The database has an older schema and was opened read-only, so it cannot be migrated."""


class UnsupportedSQLite(StoreError):
    """The SQLite library is too old (the schema needs 3.37+ for STRICT tables)."""


class MigrationError(StoreError):
    """A migration is malformed or failed; the database stays at the previous version."""


class StoreBusy(StoreError):
    """Another connection held the lock for longer than the busy timeout."""


class InvalidRow(StoreError):
    """The data breaks a constraint of the store (or disagrees with what is stored)."""


class RunExists(StoreError):
    """A run with this id already exists."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} already exists")
        self.run_id = run_id


class UnknownRun(StoreError):
    """No run with this id."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"unknown run {run_id!r}")
        self.run_id = run_id


class RunNotRunning(StoreError):
    """The run exists but is no longer `running` (finished, or abandoned)."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(f"run {run_id!r} is {status}, not running")
        self.run_id = run_id
        self.status = status


class RunNotStale(StoreError):
    """`abandon_run` with `heartbeat_before`: the run is running and heartbeat since then."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} is still running and has heartbeat since it was read")
        self.run_id = run_id


class PipelineBusy(StoreError):
    """Another run of the pipeline is still `running`."""

    def __init__(self, pipeline: str, run_id: str) -> None:
        super().__init__(f"pipeline {pipeline!r} is already running as run {run_id!r}")
        self.pipeline = pipeline
        self.run_id = run_id
        """The run that holds the pipeline."""


class UnknownTask(StoreError):
    """No task with this fingerprint."""

    def __init__(self, fingerprint: str) -> None:
        super().__init__(f"unknown task {fingerprint!r}")
        self.fingerprint = fingerprint


class UnknownJournalEntry(StoreError):
    """No journal entry with this id."""

    def __init__(self, entry_id: int) -> None:
        super().__init__(f"unknown journal entry {entry_id}")
        self.entry_id = entry_id


class SecretRefused(InvalidRow):
    """A write was refused because it holds the value of a resolved secret where the store
    cannot redact it (#53): an LLM cache entry, an identifier (a task fingerprint, a plugin
    ref), or a record that would no longer be valid once redacted. Only the secret's name is
    kept, never its value."""

    def __init__(self, name: str, where: str) -> None:
        super().__init__(f"{where} holds secret {name}: not stored")
        self.name = name
        self.where = where


class JournalTooLarge(StoreError):
    """A journalled content is larger than `MAX_JOURNAL_BYTES`."""


_BUSY_CODES = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def is_busy(exc: sqlite3.Error) -> bool:
    """Whether `exc` is SQLITE_BUSY or SQLITE_LOCKED (including their extended codes)."""
    code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in _BUSY_CODES


def translate(exc: sqlite3.Error) -> StoreError:
    """The `StoreError` to raise (`from exc`) for a `sqlite3.Error`."""
    if is_busy(exc):
        return StoreBusy(f"the database is locked: {exc}")
    if isinstance(exc, sqlite3.IntegrityError):
        return InvalidRow(str(exc))
    return StoreError(f"{type(exc).__name__}: {exc}")
