# SPDX-License-Identifier: Apache-2.0
"""Forward-only, numbered SQL migrations, tracked with `PRAGMA user_version`.

Migrations are package data: `horae/store/migrations/NNNN_<name>.sql`. Each one runs in its own
`BEGIN IMMEDIATE` transaction together with the `user_version` bump (a literal
`PRAGMA user_version = NNNN;` at the end of the file, checked after it runs), so a failed migration
leaves the database at the previous version, and two processes opening a fresh database at the
same time apply each migration once.

Foreign keys are switched off while a migration runs (`PRAGMA foreign_keys` is a no-op inside a
transaction, so this happens before `BEGIN`), so the documented table-rebuild procedure (create,
copy, drop, rename) does not cascade-delete child rows. `PRAGMA foreign_key_check` runs before
`COMMIT`; a migration that leaves dangling references is rolled back. The previous setting is
restored afterwards.

A migration file must not manage transactions or connection settings itself: top-level `BEGIN`,
`COMMIT`, `END`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `VACUUM`, `ATTACH`, `DETACH`,
`PRAGMA journal_mode` and `PRAGMA foreign_keys` are rejected before anything runs.
"""

import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files

from horae.store._errors import MigrationError, SchemaTooNewError, is_busy, translate

_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LEADING_NOISE = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)
_FORBIDDEN_STATEMENT = re.compile(
    r"(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|VACUUM|ATTACH|DETACH)\b", re.IGNORECASE
)
_PRAGMA = re.compile(
    r"PRAGMA\s+(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
_FORBIDDEN_PRAGMAS = frozenset({"journal_mode", "foreign_keys"})


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


SCHEMA_VERSION = len(load_migrations())
"""The schema version this Horae creates and understands."""


def _check_version(version: object) -> int:
    """A schema version: a non-negative `int` (not a `bool`)."""
    if type(version) is not int or version < 0:
        raise MigrationError(f"invalid migration version {version!r}: expected an int >= 0")
    return version


def statements(sql: str) -> Iterator[str]:
    """The top-level statements of a script, split where SQLite would split them."""
    start = 0
    for index, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : index + 1]):
            yield sql[start : index + 1]
            start = index + 1
    if _LEADING_NOISE.sub("", sql[start:]):
        yield sql[start:]


def check_migration(migration: Migration) -> None:
    """Reject statements that would break the transaction and settings `migrate` relies on."""
    for statement in statements(migration.sql):
        text = _LEADING_NOISE.sub("", statement)
        bad = _FORBIDDEN_STATEMENT.match(text)
        pragma = _PRAGMA.match(text)
        if pragma and pragma[1].lower() in _FORBIDDEN_PRAGMAS:
            bad = pragma
        if bad:
            raise MigrationError(
                f"migration {migration.version} ({migration.name}) must not contain "
                f"{bad[0]!r}: migrate() manages transactions and connection settings"
            )


def user_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration in its own transaction, with foreign keys off (see module doc)."""
    (foreign_keys,) = conn.execute("PRAGMA foreign_keys").fetchone()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have applied it while we waited for the lock.
            if user_version(conn) < migration.version:
                # Each migration file ends with a literal `PRAGMA user_version = N;`
                # (PRAGMA values cannot be bound as parameters, and no SQL is formatted here).
                conn.executescript(migration.sql)
                if user_version(conn) != migration.version:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.name}) must end with a "
                        f"literal user_version pragma setting version {migration.version}"
                    )
                dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
                if dangling:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.name}) leaves dangling "
                        f"foreign keys (table, rowid, parent, fk): {dangling[:5]}"
                    )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")


def migrate(
    conn: sqlite3.Connection,
    *,
    target: int | None = None,
    migrations: list[Migration] | None = None,
) -> int:
    """Bring the database up to `target` (default: the latest) and return its version.

    `conn` must be opened with `autocommit=True`, so the transactions here are explicit.
    Raises `MigrationError` (a malformed or failing migration), `SchemaTooNewError`, or another
    `StoreError` (e.g. `StoreBusy`); `ValueError` for a `target` out of range."""
    if conn.autocommit is not True:
        raise MigrationError("migrate() needs a connection opened with autocommit=True")
    migrations = load_migrations() if migrations is None else migrations
    for m in migrations:
        _check_version(m.version)
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError("migrations must be numbered 1..n without gaps")
    for m in migrations:
        check_migration(m)
    latest = len(migrations)
    target = latest if target is None else target
    if not 0 <= target <= latest:
        raise ValueError(f"target {target} is not between 0 and {latest}")

    try:
        current = user_version(conn)
        if current > latest:
            raise SchemaTooNewError(
                f"database schema version {current} is newer than this Horae ({latest})"
            )
        for migration in migrations[current:target]:
            try:
                _apply(conn, migration)
            except sqlite3.Error as exc:
                if is_busy(exc):
                    raise translate(exc) from exc
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) failed: {exc}"
                ) from exc
        return user_version(conn)
    except sqlite3.Error as exc:
        raise translate(exc) from exc
