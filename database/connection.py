"""Dialect-aware database connection wrapper.

Provides a uniform interface over sqlite3 (local/tests) and psycopg2
(production PostgreSQL). Handles placeholder conversion, SQL function
translation, and PRAGMA removal transparently.

Usage::

    from database.connection import DB, get_connection

    # SQLite (local/tests)
    db = DB(sqlite3.connect(":memory:"))

    # PostgreSQL (production)
    db = DB(psycopg2.connect(DATABASE_URL))

    # Or use the factory:
    db = get_connection()  # auto-detects from env vars
"""

from __future__ import annotations

import os
import re
import sqlite3
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── SQL conversion ────────────────────────────────────────────────

def _convert_sql(sql: str, dialect: str) -> str:
    """Convert SQLite-flavoured SQL to the target dialect.

    For SQLite, returns the SQL unchanged.
    For PostgreSQL, converts placeholders, functions, and syntax.
    """
    if dialect == "sqlite":
        return sql

    # ── Placeholders: ? → %s ──────────────────────────────────────
    sql = sql.replace("?", "%s")

    # ── Timestamp functions ───────────────────────────────────────
    # datetime('now')  → NOW()
    sql = re.sub(r"datetime\('now'\)", "NOW()", sql)
    # datetime('now', '+N hours') → NOW() + interval 'N hours'
    def _replace_datetime_offset(m):
        inner = m.group(0)
        # Extract the offset part: datetime('now', '+3 hours') → '+3 hours'
        prefix = "datetime('now', "
        offset = inner[len(prefix):-1]  # strip prefix and trailing )
        return f"NOW() + interval {offset}"

    sql = re.sub(
        r"datetime\('now',\s*'[+-]?\d+\s+(second|minute|hour|day|month|year)s?'\)",
        _replace_datetime_offset,
        sql,
    )
    # date('now') → CURRENT_DATE
    sql = re.sub(r"date\('now'\)", "CURRENT_DATE", sql)
    # date(col) → col::date  (only for known column-like patterns)
    sql = re.sub(r"\bdate\((\w+)\)", r"\1::date", sql)

    # ── INSERT OR IGNORE → ON CONFLICT DO NOTHING ─────────────────
    sql = re.sub(
        r"INSERT OR IGNORE INTO\b",
        "INSERT INTO",
        sql,
        flags=re.IGNORECASE,
    )
    # If the original had OR IGNORE, append ON CONFLICT DO NOTHING
    # (We detect this by checking if the converted SQL lacks ON CONFLICT
    #  but the original had OR IGNORE — handled by the caller via a flag)

    # ── INSERT OR REPLACE → standard upsert ───────────────────────
    # Handled explicitly in source code — not auto-converted here.

    # ── PRAGMA → no-op ────────────────────────────────────────────
    sql = re.sub(r"PRAGMA\s+\w+[^;]*;", "", sql, flags=re.IGNORECASE)

    # ── BEGIN IMMEDIATE → BEGIN ───────────────────────────────────
    sql = re.sub(r"BEGIN\s+IMMEDIATE", "BEGIN", sql, flags=re.IGNORECASE)

    # ── AUTOINCREMENT → SERIAL (in CREATE TABLE) ──────────────────
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "SERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )

    # ── sqlite_master → information_schema ────────────────────────
    sql = re.sub(
        r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'",
        "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'public'",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"SELECT\s+COUNT\(\*\)\s+FROM\s+sqlite_master",
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'",
        sql,
        flags=re.IGNORECASE,
    )
    # PRAGMA table_info(table_name) → information_schema.columns
    sql = re.sub(
        r"PRAGMA\s+table_info\((\w+)\)",
        r"SELECT column_name AS name, data_type AS type FROM information_schema.columns WHERE table_name = '\1'",
        sql,
        flags=re.IGNORECASE,
    )

    # ── GROUP_CONCAT → STRING_AGG ─────────────────────────────────
    sql = re.sub(
        r"GROUP_CONCAT\(([^,]+),\s*'([^']+)'\)",
        r"STRING_AGG(\1, '\2')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"GROUP_CONCAT\(([^)]+)\)",
        r"STRING_AGG(\1, ',')",
        sql,
        flags=re.IGNORECASE,
    )

    return sql


def _convert_sql_with_or_ignore(sql: str, dialect: str) -> str:
    """Convert INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING."""
    if dialect == "sqlite":
        return sql

    # First apply standard conversions
    result = _convert_sql(sql, dialect)

    # If original had INSERT OR IGNORE, add ON CONFLICT DO NOTHING
    if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", sql, re.IGNORECASE):
        # Find the VALUES clause and append ON CONFLICT DO NOTHING after it
        # Pattern: INSERT INTO table (...) VALUES (...) [ON CONFLICT ...]
        # We add ON CONFLICT DO NOTHING if not already present
        if "ON CONFLICT" not in result.upper():
            result = result.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING;"

    return result


# ── Result wrapper ────────────────────────────────────────────────


class DBResult:
    """Uniform result set for both SQLite and PostgreSQL cursors."""

    def __init__(self, cursor, dialect: str):
        self._cursor = cursor
        self._dialect = dialect
        self.rowcount = cursor.rowcount

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        if self._dialect == "postgresql":
            # psycopg2 with RealDictCursor returns dicts already
            if isinstance(row, dict):
                return row
            # Fallback: convert tuple to dict using cursor description
            if self._cursor.description:
                cols = [d[0] for d in self._cursor.description]
                return dict(zip(cols, row))
            return row
        return row

    def fetchall(self) -> list[dict]:
        rows = self._cursor.fetchall()
        if self._dialect == "postgresql":
            if rows and isinstance(rows[0], dict):
                return rows
            if self._cursor.description:
                cols = [d[0] for d in self._cursor.description]
                return [dict(zip(cols, r)) for r in rows]
            return rows
        return rows

    @property
    def description(self):
        return self._cursor.description


# ── Main DB wrapper ───────────────────────────────────────────────


class DB:
    """Dialect-aware database connection.

    Wraps either sqlite3.Connection or psycopg2.connection and provides
    a uniform execute/fetch interface. SQLite SQL is auto-converted to
    PostgreSQL syntax on execute.
    """

    def __init__(self, conn, dialect: str = "sqlite"):
        self._conn = conn
        self.dialect = dialect
        self._total_changes = 0

    # ── Core operations ───────────────────────────────────────────

    def execute(self, sql: str, params: tuple | list = ()) -> DBResult:
        """Execute SQL with automatic dialect conversion."""
        if self.dialect == "postgresql":
            converted = _convert_sql(sql, "postgresql")
        else:
            converted = sql

        cursor = self._conn.cursor()
        cursor.execute(converted, params)
        self._total_changes += cursor.rowcount
        return DBResult(cursor, self.dialect)

    def executescript(self, sql_script: str) -> None:
        """Execute a multi-statement SQL script.

        For PostgreSQL, splits into individual statements and executes.
        For SQLite, uses the native executescript.
        """
        if self.dialect == "sqlite":
            self._conn.executescript(sql_script)
        else:
            # Split on semicolons and execute each statement
            for statement in sql_script.split(";"):
                statement = statement.strip()
                if statement:
                    converted = _convert_sql(statement, "postgresql")
                    try:
                        cursor = self._conn.cursor()
                        cursor.execute(converted)
                    except Exception:
                        # Some statements may fail if table already exists
                        pass

    def executemany(self, sql: str, params_list: list[tuple]) -> DBResult:
        """Execute SQL for many parameter sets."""
        if self.dialect == "postgresql":
            converted = _convert_sql(sql, "postgresql")
        else:
            converted = sql

        cursor = self._conn.cursor()
        cursor.executemany(converted, params_list)
        self._total_changes += cursor.rowcount
        return DBResult(cursor, self.dialect)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ── Compatibility properties ──────────────────────────────────

    @property
    def total_changes(self) -> int:
        return self._total_changes

    @property
    def native(self):
        """Return the underlying connection for driver-specific operations."""
        return self._conn


# ── Connection factory ────────────────────────────────────────────


def get_connection(
    url: str | None = None,
    db_path: str | None = None,
) -> DB:
    """Create a database connection.

    Parameters
    ----------
    url:
        PostgreSQL connection string (DATABASE_URL). If provided,
        creates a psycopg2 connection.
    db_path:
        Path to SQLite database file. Used when url is None.

    Returns
    -------
    DB wrapper around the appropriate connection.
    """
    if url:
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(
                url,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
            )
            conn.autocommit = False
            logger.info("Connected to PostgreSQL")
            return DB(conn, dialect="postgresql")
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL. "
                "Install with: pip install psycopg2-binary"
            )
        except Exception as e:
            raise ConnectionError(f"Failed to connect to PostgreSQL: {e}")

    # SQLite mode
    if not db_path:
        db_path = os.environ.get("MLB_DB_PATH", "database/mlb_model.db")

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return DB(conn, dialect="sqlite")


def get_database_url() -> str | None:
    """Return the DATABASE_URL from environment, or None for SQLite mode."""
    return os.environ.get("DATABASE_URL", "")
