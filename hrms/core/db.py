"""SQLite access layer: one connection per request, explicit transactions."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# Bump when schema.sql / _ADDED_COLUMNS change in a way a restore must know about. Stored in the SQLite
# header (PRAGMA user_version) and written into every backup manifest. Databases created before this
# existed report 0 and are treated as the baseline (1).
SCHEMA_VERSION = 1


_JOURNAL_MODES = {"WAL", "DELETE", "TRUNCATE", "PERSIST"}


def _connect(path):
    conn = sqlite3.connect(path, isolation_level=None, detect_types=0, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    mode = current_app.config.get("SQLITE_JOURNAL_MODE", "WAL")
    conn.execute(f"PRAGMA journal_mode = {mode if mode in _JOURNAL_MODES else 'WAL'}")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def scalar(sql, params=()):
    row = get_db().execute(sql, params).fetchone()
    return row[0] if row else None


def execute(sql, params=()):
    return get_db().execute(sql, params)


@contextmanager
def transaction():
    """BEGIN IMMEDIATE serialises writers, so status checks + updates are race-free."""
    conn = get_db()
    if conn.in_transaction:  # nested use joins the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# Columns added after the first release: (table, column, DDL). Applied to existing databases.
_ADDED_COLUMNS = [
    ("leave_types", "credit_based", "INTEGER NOT NULL DEFAULT 0"),
    ("leave_types", "accrual_per_month", "REAL"),
    ("holidays", "description", "TEXT"),
    ("employees", "date_of_birth", "TEXT"),
    ("employees", "show_birthday", "INTEGER NOT NULL DEFAULT 1"),
    ("employees", "contract_end_date", "TEXT"),
    ("leave_types", "is_paid", "INTEGER NOT NULL DEFAULT 1"),
    ("leave_requests", "half_day", "TEXT"),
    ("timesheet_projects", "code", "TEXT"),
    ("timesheet_projects", "department_id", "INTEGER REFERENCES departments(id)"),
    ("timesheet_projects", "head_employee_id", "INTEGER REFERENCES employees(id)"),
    ("timesheet_projects", "start_date", "TEXT"),
    ("timesheet_projects", "end_date", "TEXT"),
    ("timesheet_projects", "funding", "TEXT"),
    ("timesheet_projects", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
]
# Run once, right after the column named here is first added to an existing database.
_AFTER_ADD = {
    ("timesheet_projects", "status"): "UPDATE timesheet_projects SET status = 'COMPLETED' WHERE is_active = 0",
}


def init_schema(path):
    conn = _connect(path)
    try:
        for table, column, ddl in _ADDED_COLUMNS:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
            if exists and column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if (table, column) in _AFTER_ADD:
                    conn.execute(_AFTER_ADD[(table, column)])
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:  # never downgrade
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def backup_to(src_path, dest_path):
    """Online, consistent copy using SQLite's backup API."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
