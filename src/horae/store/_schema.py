# SPDX-License-Identifier: Apache-2.0
"""Forward-only, numbered SQL migrations, tracked with `PRAGMA user_version`.

Migrations are package data: `horae/store/migrations/NNNN_<name>.sql`. Each one runs in its own
`BEGIN IMMEDIATE` transaction together with the `user_version` bump (a literal
`PRAGMA user_version = NNNN;` at the end of the file, checked after it runs), so a failed migration
leaves the database at the previous version, and two processes opening a fresh database at the
same time apply each migration once.
"""

import re
import sqlite3
from dataclasses import dataclass
from importlib.resources import files

_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class StoreError(Exception):
    """The store cannot do what was asked."""


class SchemaTooNewError(StoreError):
    """The database was written by a newer Horae (its schema version is unknown here)."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """The migrations shipped with this package, in order."""
    found = []
    for entry in files(__package__ or "horae.store").joinpath("migrations").iterdir():
        match = _NAME.match(entry.name)
        if match:
            found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    return sorted(found, key=lambda m: m.version)


def _check_version(version: object) -> int:
    """A schema version: a non-negative `int` (not a `bool`)."""
    if type(version) is not int or version < 0:
        raise StoreError(f"invalid migration version {version!r}: expected an int >= 0")
    return version


def user_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def migrate(
    conn: sqlite3.Connection,
    *,
    target: int | None = None,
    migrations: list[Migration] | None = None,
) -> int:
    """Bring the database up to `target` (default: the latest) and return its version.

    `conn` must be opened with `autocommit=True`, so the transactions here are explicit."""
    if conn.autocommit is not True:
        raise StoreError("migrate() needs a connection opened with autocommit=True")
    migrations = load_migrations() if migrations is None else migrations
    for m in migrations:
        _check_version(m.version)
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise StoreError("migrations must be numbered 1..n without gaps")
    latest = len(migrations)
    target = latest if target is None else target
    if not 0 <= target <= latest:
        raise ValueError(f"target {target} is not between 0 and {latest}")

    current = user_version(conn)
    if current > latest:
        raise SchemaTooNewError(
            f"database schema version {current} is newer than this Horae ({latest})"
        )
    for migration in migrations[current:target]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have applied it while we waited for the lock.
            if user_version(conn) < migration.version:
                # Each migration file ends with a literal `PRAGMA user_version = N;`
                # (PRAGMA values cannot be bound as parameters, and no SQL is formatted here).
                conn.executescript(migration.sql)
                if user_version(conn) != migration.version:
                    raise StoreError(
                        f"migration {migration.version} ({migration.name}) must end with "
                        f"PRAGMA user_version = {migration.version};"
                    )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return user_version(conn)
