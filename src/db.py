"""
db.py — SQLite connection and initialization.

All other modules call get_conn() to obtain a connection.
Schema is applied from db/schema.sql on first connect.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "wheel.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

_initialized = False


def _init(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)


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
        _initialized = True
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
