# SPDX-License-Identifier: Apache-2.0
"""The SQLite store: runs, step records, journal, LLM cache, task index, notification state.

See ADR 0006. The schema is created and upgraded by numbered SQL migrations shipped in
`horae/store/migrations/`; `PRAGMA user_version` is the schema version. Every failure is a
`StoreError`; `ValueError` is kept for programming errors.
"""

from horae.store._errors import (
    InvalidRow,
    JournalTooLarge,
    MigrationError,
    PipelineBusy,
    RunExists,
    RunNotRunning,
    SchemaTooNewError,
    StoreBusy,
    StoreError,
    UnknownJournalEntry,
    UnknownRun,
    UnknownTask,
    UnsupportedSQLite,
)
from horae.store._schema import SCHEMA_VERSION, Migration, load_migrations, migrate
from horae.store._store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    MAX_JOURNAL_BYTES,
    MAX_LIST_RUNS,
    MAX_NESTING,
    CacheEntry,
    JournalEntry,
    NotificationState,
    RunSummary,
    Store,
    StoreRunStatus,
    TaskEntry,
    TaskStatus,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MAX_JOURNAL_BYTES",
    "MAX_LIST_RUNS",
    "MAX_NESTING",
    "SCHEMA_VERSION",
    "CacheEntry",
    "InvalidRow",
    "JournalEntry",
    "JournalTooLarge",
    "Migration",
    "MigrationError",
    "NotificationState",
    "PipelineBusy",
    "RunExists",
    "RunNotRunning",
    "RunSummary",
    "SchemaTooNewError",
    "Store",
    "StoreBusy",
    "StoreError",
    "StoreRunStatus",
    "TaskEntry",
    "TaskStatus",
    "UnknownJournalEntry",
    "UnknownRun",
    "UnknownTask",
    "UnsupportedSQLite",
    "load_migrations",
    "migrate",
]
