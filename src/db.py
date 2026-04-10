"""
db.py — SQLite connection and initialization.

All other modules call get_conn() to obtain a connection.
Schema is applied from db/schema.sql on first connect.
Migrations in db/migrations/ are run after schema initialization.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "wheel.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"

_initialized = False


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Execute all migration files in db/migrations/ in numeric order."""
    if not MIGRATIONS_DIR.exists():
        return

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for mig_file in migration_files:
        try:
            migration_sql = mig_file.read_text()
            conn.executescript(migration_sql)
        except sqlite3.OperationalError as e:
            # Idempotent errors — column already exists, etc.
            if any(x in str(e).lower() for x in ["already exists", "duplicate column"]):
                continue
            raise


def _init(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)
    _run_migrations(conn)


@contextmanager
def get_conn():
    """Yield a transactional SQLite connection. Commits on exit, rolls back on error."""
    global _initialized
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if not _initialized:
        schema = SCHEMA_PATH.read_text()
        conn.executescript(schema)
        _run_migrations(conn)
        _initialized = True
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
