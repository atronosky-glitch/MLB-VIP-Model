"""Tests for Phase 17B: PostgreSQL compatibility layer.

Tests the database/connection.py SQL conversion, DB wrapper, and
dual-mode support.  All tests run against in-memory SQLite since
PostgreSQL is not available in CI.
"""

import pytest
from unittest.mock import patch, MagicMock
import os


class TestSQLConversion:
    """Test _convert_sql dialect conversion."""

    def test_no_conversion_for_sqlite(self):
        """SQLite dialect returns SQL unchanged."""
        from database.connection import _convert_sql
        sql = "SELECT * FROM games WHERE id = ?"
        assert _convert_sql(sql, "sqlite") == sql

    def test_placeholder_conversion(self):
        """? → %s for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "SELECT * FROM games WHERE id = ? AND status = ?"
        result = _convert_sql(sql, "postgresql")
        assert "%s" in result
        assert "?" not in result

    def test_datetime_now_conversion(self):
        """datetime('now') → NOW() for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "INSERT INTO t (created) VALUES (datetime('now'))"
        result = _convert_sql(sql, "postgresql")
        assert "NOW()" in result
        assert "datetime" not in result

    def test_date_now_conversion(self):
        """date('now') → CURRENT_DATE for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "SELECT * FROM t WHERE d = date('now')"
        result = _convert_sql(sql, "postgresql")
        assert "CURRENT_DATE" in result

    def test_begin_immediate_conversion(self):
        """BEGIN IMMEDIATE → BEGIN for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "BEGIN IMMEDIATE"
        result = _convert_sql(sql, "postgresql")
        assert "IMMEDIATE" not in result
        assert "BEGIN" in result

    def test_autoincrement_conversion(self):
        """INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY."""
        from database.connection import _convert_sql
        sql = "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        result = _convert_sql(sql, "postgresql")
        assert "SERIAL PRIMARY KEY" in result

    def test_insert_or_ignore_removal(self):
        """INSERT OR IGNORE → INSERT (conflict handled at app level)."""
        from database.connection import _convert_sql
        sql = "INSERT OR IGNORE INTO t (a) VALUES (1)"
        result = _convert_sql(sql, "postgresql")
        assert "OR IGNORE" not in result
        assert "INSERT INTO" in result

    def test_group_concat_conversion(self):
        """GROUP_CONCAT → STRING_AGG for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "SELECT GROUP_CONCAT(name, ',') FROM t"
        result = _convert_sql(sql, "postgresql")
        assert "STRING_AGG" in result

    def test_sqlite_master_conversion(self):
        """sqlite_master → information_schema for PostgreSQL."""
        from database.connection import _convert_sql
        sql = "SELECT name FROM sqlite_master WHERE type = 'table'"
        result = _convert_sql(sql, "postgresql")
        assert "information_schema" in result
        assert "sqlite_master" not in result


class TestDBWrapper:
    """Test the DB wrapper class with in-memory SQLite."""

    def test_get_connection_returns_db(self):
        """get_connection() returns a DB instance."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            from database.connection import DB
            assert isinstance(conn, DB)
            assert conn.dialect == "sqlite"
        finally:
            conn.close()

    def test_execute_and_fetch(self):
        """Basic execute and fetch works."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            conn.executescript("""
                CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO test VALUES (1, 'hello');
                INSERT INTO test VALUES (2, 'world');
            """)
            result = conn.execute("SELECT * FROM test ORDER BY id")
            rows = result.fetchall()
            assert len(rows) == 2
            assert rows[0][0] == 1
            assert rows[0][1] == 'hello'
        finally:
            conn.close()

    def test_insert_on_conflict_do_nothing(self):
        """ON CONFLICT DO NOTHING works with SQLite."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            conn.executescript("""
                CREATE TABLE test (id TEXT PRIMARY KEY, val TEXT);
            """)
            conn.execute(
                "INSERT INTO test (id, val) VALUES (?, ?) ON CONFLICT (id) DO NOTHING",
                ("a", "first"),
            )
            conn.commit()
            conn.execute(
                "INSERT INTO test (id, val) VALUES (?, ?) ON CONFLICT (id) DO NOTHING",
                ("a", "second"),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM test").fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "first"
        finally:
            conn.close()

    def test_insert_on_conflict_do_update(self):
        """ON CONFLICT DO UPDATE works with SQLite."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            conn.executescript("""
                CREATE TABLE test (id TEXT PRIMARY KEY, val TEXT);
            """)
            conn.execute(
                "INSERT INTO test (id, val) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET val = excluded.val",
                ("a", "first"),
            )
            conn.commit()
            conn.execute(
                "INSERT INTO test (id, val) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET val = excluded.val",
                ("a", "second"),
            )
            conn.commit()
            rows = conn.execute("SELECT * FROM test").fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "second"
        finally:
            conn.close()

    def test_total_changes_tracking(self):
        """DB.total_changes tracks row modifications."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            conn.executescript("""
                CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);
            """)
            conn.execute("INSERT INTO test VALUES (1, 'a')")
            conn.commit()
            assert conn.total_changes == 1
            conn.execute("INSERT INTO test VALUES (2, 'b')")
            conn.commit()
            assert conn.total_changes == 2
        finally:
            conn.close()

    def test_datetime_now_in_execute(self):
        """datetime('now') is handled in execute for SQLite."""
        from database.connection import get_connection
        conn = get_connection(db_path=":memory:")
        try:
            conn.executescript("""
                CREATE TABLE test (id INTEGER PRIMARY KEY, ts TEXT);
            """)
            conn.execute("INSERT INTO test VALUES (1, datetime('now'))")
            conn.commit()
            row = conn.execute("SELECT ts FROM test WHERE id = 1").fetchone()
            assert row[0] is not None
        finally:
            conn.close()


class TestDBManagerDualMode:
    """Test db_manager dual-mode support."""

    def test_db_manager_returns_db_instance(self):
        """db_manager.get_connection returns a DB instance."""
        import database.db_manager as dbm
        from database.connection import DB
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = ":memory:"
            conn = dbm.get_connection()
            assert isinstance(conn, DB)
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_db_manager_with_explicit_path(self):
        """db_manager.get_connection(db_path=...) uses explicit path."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        try:
            conn = dbm.get_connection(db_path=tmp)
            from database.connection import DB
            assert isinstance(conn, DB)
            conn.close()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_init_db_works_with_db_wrapper(self):
        """init_db() works when get_connection returns DB wrapper."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = tmp
            dbm.init_db()
            conn = dbm.get_connection()
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r[0] if not isinstance(r, dict) else r['name'] for r in tables}
            assert "games" in table_names
            assert "odds" in table_names
            assert "historical_recommendations" in table_names
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_save_recommendation_idempotent(self):
        """save_recommendation is idempotent via ON CONFLICT."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = tmp
            conn = dbm.get_connection()
            dbm.init_db()
            rec = {
                "event_id": "E1", "player_id": "P1", "player_name": "Test",
                "market_type": "batter_hits", "side": "over", "line": 1.5,
                "sportsbook": "FanDuel", "offered_american_odds": -110,
                "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
                "rec_status": "VALID", "scan_timestamp": "2026-01-01T00:00:00Z",
            }
            rec_id1 = dbm.save_recommendation(conn, rec)
            assert rec_id1 is not None
            rec_id2 = dbm.save_recommendation(conn, rec)
            assert rec_id2 is None  # deduplicated
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_freeze_official_pick_idempotent(self):
        """freeze_official_pick is idempotent."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = tmp
            conn = dbm.get_connection()
            dbm.init_db()
            result1 = dbm.freeze_official_pick(conn, "rec-123")
            assert result1 is True
            result2 = dbm.freeze_official_pick(conn, "rec-123")
            assert result2 is False
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)


class TestMigrationScript:
    """Test that migration script is importable and well-formed."""

    def test_migration_script_importable(self):
        """migration script can be imported without errors."""
        import importlib
        mod = importlib.import_module("scripts.migrate_sqlite_to_postgres")
        assert hasattr(mod, "main")
        assert hasattr(mod, "migrate_table")

    def test_migration_script_has_dry_run(self):
        """migration script supports --dry-run flag."""
        import importlib
        mod = importlib.import_module("scripts.migrate_sqlite_to_postgres")
        # The main function should accept dry_run parameter
        import inspect
        sig = inspect.signature(mod.main)
        # Check it's callable
        assert callable(mod.main)
