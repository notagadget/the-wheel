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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Execute all migration files in db/migrations/ in numeric order, statement by statement."""
    if not MIGRATIONS_DIR.exists():
        return

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for mig_file in migration_files:
        migration_sql = mig_file.read_text()
        statements = [s.strip() for s in migration_sql.split(";") if s.strip()]
        for stmt in statements:
            # Strip leading comment lines
            lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
            stmt_clean = " ".join(lines).strip()
            if not stmt_clean:
                continue
            upper = stmt_clean.upper()
            # Guard ALTER TABLE ... ADD COLUMN against duplicate columns
            if "ALTER TABLE" in upper and "ADD COLUMN" in upper:
                parts = stmt_clean.split()
                uppers = [p.upper() for p in parts]
                try:
                    tbl = parts[uppers.index("TABLE") + 1]
                    col = parts[uppers.index("COLUMN") + 1]
                    if _column_exists(conn, tbl, col):
                        continue
                except (ValueError, IndexError):
                    pass
            # Guard ALTER TABLE ... DROP COLUMN against already-removed columns
            if "ALTER TABLE" in upper and "DROP COLUMN" in upper:
                parts = stmt_clean.split()
                uppers = [p.upper() for p in parts]
                try:
                    tbl = parts[uppers.index("TABLE") + 1]
                    col = parts[uppers.index("COLUMN") + 1].rstrip(";")
                    if not _column_exists(conn, tbl, col):
                        continue
                except (ValueError, IndexError):
                    pass
            try:
                conn.execute(stmt_clean)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if any(x in msg for x in ["already exists", "duplicate column", "no such column"]):
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
