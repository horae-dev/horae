# SPDX-License-Identifier: Apache-2.0
"""The SQLite store: runs, step records, journal, LLM cache, task index, notification state.

See ADR 0006. The schema is created and upgraded by numbered SQL migrations shipped in
`horae/store/migrations/`; `PRAGMA user_version` is the schema version.
"""

from horae.store._schema import (
    Migration,
    SchemaTooNewError,
    StoreError,
    load_migrations,
    migrate,
)
from horae.store._store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    CacheEntry,
    JournalEntry,
    NotificationState,
    RunSummary,
    Store,
    StoreRunStatus,
    TaskEntry,
    TaskStatus,
)

SCHEMA_VERSION = len(load_migrations())
"""The schema version this Horae creates and understands."""

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "CacheEntry",
    "JournalEntry",
    "Migration",
    "NotificationState",
    "RunSummary",
    "SchemaTooNewError",
    "Store",
    "StoreError",
    "StoreRunStatus",
    "TaskEntry",
    "TaskStatus",
    "load_migrations",
    "migrate",
]
