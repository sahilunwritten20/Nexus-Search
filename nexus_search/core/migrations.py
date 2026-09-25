"""Minimal versioned schema migrations.

`CREATE TABLE IF NOT EXISTS` is fine for a green-field DB and was the whole
story through Phase 4. A real product needs an upgrade path when a column
needs to change on an EXISTING database, so every store now names its schema
version in a `schema_version` table and migrations apply in order, each in
its own transaction.

Design (deliberately tiny, no framework dependency):
- a migration is (version:int, sql:str)
- migrations are append-only; never edit an applied one
- current state is per STORE (so storage.py, vector_store.py, frontier.py
  each version independently instead of sharing one fragile global version)
- every migration's SQL must be idempotent (IF NOT EXISTS / column guards),
  so baselining a pre-migrations DB is just "run them all, they're no-ops
  on an up-to-date file"
"""
import sqlite3

_VERSION_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
)


def apply_migrations(conn: sqlite3.Connection, name: str,
                     migrations: list[tuple[int, str]]) -> int:
    """Apply all pending migrations for store `name`. Returns final version.

    Raises on any SQL error — a store whose schema can't be brought to the
    expected version must NOT come up half-migrated and serve wrong answers."""
    if not migrations:
        raise ValueError("migrations must not be empty")
    conn.execute(_VERSION_DDL)
    conn.commit()
    row = conn.execute("SELECT version FROM schema_version WHERE name = ?",
                       (name,)).fetchone()
    current = row[0] if row else 0
    for version, sql in sorted(migrations, key=lambda m: m[0]):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # NOT executescript(): it issues an implicit COMMIT before running,
            # which would break the atomicity this transaction is for. Migration
            # SQL is therefore plain semicolon-separated DDL, no embedded
            # semicolons in literals.
            for statement in sql.split(";"):
                statement = statement.strip()
                if statement:
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (name, version) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET version = excluded.version",
                (name, version))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        current = version
    return current


def get_version(conn: sqlite3.Connection, name: str) -> int:
    """Current version of store `name`; 0 if never migrated."""
    try:
        row = conn.execute("SELECT version FROM schema_version WHERE name = ?",
                           (name,)).fetchone()
    except sqlite3.OperationalError:  # table doesn't exist at all yet
        return 0
    return row[0] if row else 0
