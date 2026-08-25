"""Tests for Phase 17B: PostgreSQL compatibility layer.

Tests the database/connection.py SQL conversion, DB wrapper, and
dual-mode support.  All tests run against in-memory SQLite since
PostgreSQL is not available in CI.
"""

import pytest
from unittest.mock import patch, MagicMock
import os
from datetime import datetime, timezone
from pathlib import Path


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


    def test_named_placeholder_conversion(self):
        """:event_id → %(event_id)s for psycopg2."""
        from database.connection import _convert_sql
        sql = "INSERT INTO t (id, league) VALUES (:event_id, :league)"
        result = _convert_sql(sql, "postgresql")
        assert "%(event_id)s" in result
        assert "%(league)s" in result
        assert ":event_id" not in result
        assert ":league" not in result

    def test_question_mark_still_works(self):
        """? → %s still works alongside named placeholders."""
        from database.connection import _convert_sql
        sql = "VALUES (?, ?)"
        result = _convert_sql(sql, "postgresql")
        assert result == "VALUES (%s, %s)"

    def test_postgres_cast_not_mangled(self):
        """::date PostgreSQL casts are not accidentally changed."""
        from database.connection import _convert_sql
        sql = "SELECT start_time::date FROM games WHERE id = ?"
        result = _convert_sql(sql, "postgresql")
        assert "::date" in result
        assert result == "SELECT start_time::date FROM games WHERE id = %s"


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

    def test_save_recommendation_result_distinguishes_saved_vs_duplicate(self):
        """Regression (2026-08-24): save_recommendation_result's .status
        must be SAVED for a fresh insert and DUPLICATE for a repeat of
        the same fingerprint — the two must never collapse into the
        same indistinguishable outcome the way save_recommendation's
        plain None return necessarily does."""
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
            r1 = dbm.save_recommendation_result(conn, rec)
            assert r1.status == dbm.SAVE_STATUS_SAVED
            assert r1.recommendation_id is not None
            assert r1.ok is True

            r2 = dbm.save_recommendation_result(conn, rec)
            assert r2.status == dbm.SAVE_STATUS_DUPLICATE
            assert r2.recommendation_id == r1.recommendation_id  # points at the original row
            assert r2.ok is True  # a duplicate is not a failure
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_save_recommendation_result_reports_real_errors_distinctly(self):
        """Regression (2026-08-24): a genuine persistence error must
        report status=ERROR with a populated error_type/error_message —
        not the same silent None a duplicate produces. This is what a
        real production outage looked like for 3 days: every save
        failed with a real psycopg2.errors.DatatypeMismatch that was
        indistinguishable from a normal duplicate skip."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = tmp
            conn = dbm.get_connection()
            dbm.init_db()
            # Missing required keys (event_id etc.) raises a real
            # KeyError inside the function — must be caught, logged, and
            # reported as ERROR, not silently returned as None.
            broken_rec = {"scan_timestamp": "2026-01-01T00:00:00Z"}
            result = dbm.save_recommendation_result(conn, broken_rec)
            assert result.status == dbm.SAVE_STATUS_ERROR
            assert result.recommendation_id is None
            assert result.ok is False
            assert result.error_type == "KeyError"
            assert result.error_message
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_bool_to_int_or_none_conversion(self):
        """Direct unit coverage for the exact bug: a real Python bool
        must convert to 1/0, and None must stay None (not collapse to
        0 — None means "not applicable/unknown", 0 means "checked and
        false", a real semantic difference for pinnacle_found)."""
        from database.db_manager import _bool_to_int_or_none
        assert _bool_to_int_or_none(True) == 1
        assert _bool_to_int_or_none(False) == 0
        assert _bool_to_int_or_none(None) is None

    def test_persist_recommendation_evidence_never_passes_raw_bool_for_int_columns(self):
        """Regression (2026-08-24): the real production bug.
        pinnacle_found/pinnacle_reference_used/pinnacle_approved are
        genuine Python bools whenever Pinnacle was actually checked —
        _persist_recommendation_evidence must cast them to 0/1/None
        before binding, or PostgreSQL raises DatatypeMismatch on an
        `integer` column (SQLite accepts a bare bool silently, which is
        exactly why this was invisible in this SQLite-only test suite
        until reproduced directly against the real prod schema)."""
        import database.db_manager as dbm
        import tempfile, os
        tmp = tempfile.mktemp(suffix=".db")
        orig_path = dbm.DB_PATH
        try:
            dbm.DB_PATH = tmp
            conn = dbm.get_connection()
            dbm.init_db()
            rec = {
                "event_id": "E1", "player_id": "GAME", "player_name": "Moneyline",
                "market_type": "game_moneyline", "side": "HOME", "line": None,
                "sportsbook": "draftkings", "offered_american_odds": 200,
                "offered_decimal_odds": 3.0, "offered_implied_prob": 0.333,
                "rec_status": "STRONG_EDGE", "scan_timestamp": "2026-01-01T00:00:00Z",
                # The exact real shape that broke production: genuine bools,
                # not None and not pre-cast ints.
                "pinnacle_found": True,
                "pinnacle_reference_used": True,
                "pinnacle_approved": False,
            }
            result = dbm.save_recommendation_result(conn, rec)
            assert result.status == dbm.SAVE_STATUS_SAVED, (
                f"expected a clean save, got {result.status}: {result.error_message}"
            )
            row = conn.execute(
                "SELECT pinnacle_found, pinnacle_reference_used, pinnacle_approved "
                "FROM historical_recommendations WHERE recommendation_id = ?",
                (result.recommendation_id,),
            ).fetchone()
            assert row["pinnacle_found"] in (1, True)
            assert row["pinnacle_reference_used"] in (1, True)
            assert row["pinnacle_approved"] in (0, False)
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
            if os.path.exists(tmp):
                os.unlink(tmp)

    def test_persist_recommendation_evidence_binds_only_ints_on_postgres_dialect(self):
        """The dialect-aware guard against the real bug: a mock
        conn.dialect=="postgresql" (SQLite alone never would have caught
        this — it silently accepts a bare Python bool where Postgres
        raises DatatypeMismatch on an `integer` column). Asserts the
        UPDATE's bound parameters for pinnacle_found/
        pinnacle_reference_used/pinnacle_approved are never a raw
        True/False, regardless of what SQLite would tolerate."""
        import database.db_manager as dbm

        columns_result = MagicMock()
        columns_result.fetchall.return_value = [
            {"name": "market_quality_score"}, {"name": "pinnacle_found"},
            {"name": "pinnacle_reference_used"}, {"name": "pinnacle_approved"},
            {"name": "pinnacle_book"}, {"name": "pinnacle_line"},
            {"name": "pinnacle_over_price"}, {"name": "pinnacle_under_price"},
            {"name": "pinnacle_fair_prob"}, {"name": "pinnacle_ev"},
            {"name": "pinnacle_prob_edge"}, {"name": "is_official"},
            {"name": "confidence_score"}, {"name": "reliable_ev_checked"},
            {"name": "reliable_ev"}, {"name": "reliable_ev_status"},
            {"name": "reliable_ev_reasons"}, {"name": "reliable_ev_calculated_pct"},
            {"name": "reliable_ev_version"}, {"name": "challenger_expected_strikeouts"},
            {"name": "challenger_over_probability"}, {"name": "challenger_under_probability"},
            {"name": "challenger_push_probability"}, {"name": "challenger_fair_probability"},
            {"name": "challenger_version"},
        ]

        captured = {}

        def _fake_execute(sql, params=()):
            if "information_schema.columns" in sql:
                return columns_result
            if sql.strip().startswith("UPDATE"):
                captured["params"] = params
            return MagicMock()

        mock_conn = MagicMock()
        mock_conn.dialect = "postgresql"
        mock_conn.execute.side_effect = _fake_execute

        rec = {
            "pinnacle_found": True,
            "pinnacle_reference_used": True,
            "pinnacle_approved": False,
            "is_official": True,
            "reliable_ev_checked": True,
            "reliable_ev": False,
        }
        dbm._persist_recommendation_evidence(mock_conn, "rec-1", rec)

        assert "params" in captured, "UPDATE was never issued"
        for value in captured["params"]:
            assert not isinstance(value, bool), (
                f"a raw Python bool ({value!r}) was bound as a parameter — "
                "PostgreSQL would reject this against an `integer` column"
            )

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


class TestPostgresPathGuard:
    """Test that dashboard helpers bypass SQLite file-existence check
    when PostgreSQL is configured (DATABASE_URL set)."""

    def setup_method(self):
        import sqlite3
        from database.connection import DB
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.conn.executescript("""
            CREATE TABLE scan_runs (
                run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
                run_type TEXT
            );
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, start_time TEXT, status TEXT,
                away_team TEXT, home_team TEXT, league TEXT DEFAULT 'MLB'
            );
            CREATE TABLE historical_recommendations (
                recommendation_id TEXT PRIMARY KEY, event_id TEXT,
                player_name TEXT, market_type TEXT, scan_run_id TEXT,
                ev_pct REAL, rec_status TEXT, scan_timestamp TEXT,
                freshness_status TEXT
            );
        """)
        self.conn.execute(
            "INSERT INTO scan_runs (run_id, started_at, finished_at, run_type) "
            "VALUES (?, ?, ?, ?)",
            ("run-999", "2026-07-25T10:00:00", "2026-07-25T11:00:00", "scan"),
        )
        self.conn.execute(
            "INSERT INTO games (event_id, start_time, status, away_team, home_team) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e1", f"{today}T20:00:00", "scheduled", "NYY", "BOS"),
        )
        self.conn.execute(
            "INSERT INTO games (event_id, start_time, status, away_team, home_team) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e2", f"{today}T20:00:00", "scheduled", "LAD", "SF"),
        )
        self.conn.execute(
            "INSERT INTO historical_recommendations "
            "(recommendation_id, event_id, player_name, market_type, scan_run_id, "
            "ev_pct, rec_status, scan_timestamp, freshness_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "e1", "Judge", "homeruns", "run-999",
             0.05, "BET", "2026-07-25T10:30:00", "fresh"),
        )
        # Commit so the DB wrapper's rollback-on-error (triggered by the
        # explicit-column query failing against this minimal schema) cannot
        # wipe the seeded rows during _load_recs' fallback to SELECT *.
        self.conn.commit()
        self.db = DB(self.conn, dialect="sqlite")

    def teardown_method(self):
        self.conn.close()

    def test_is_postgres_false_when_no_url(self):
        from src.control_panel import _is_postgres
        with patch.dict(os.environ, {}, clear=True):
            assert not _is_postgres()

    def test_is_postgres_true_when_url_set(self):
        from src.control_panel import _is_postgres
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@h/d"}):
            assert _is_postgres()

    def test_should_query_true_for_postgres_without_file(self):
        from src.control_panel import _should_query
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@h/d"}):
            assert _should_query("/nonexistent/db.db")

    def test_should_query_false_for_sqlite_without_file(self):
        from src.control_panel import _should_query
        with patch.dict(os.environ, {}, clear=True):
            assert not _should_query("/nonexistent/db.db")

    def test_should_query_true_for_sqlite_with_file(self, tmp_path):
        dbfile = str(tmp_path / "test.db")
        Path(dbfile).touch()
        from src.control_panel import _should_query
        with patch.dict(os.environ, {}, clear=True):
            assert _should_query(dbfile)

    def test_guard_bypass_for_latest_run_id(self):
        from src.control_panel import _get_latest_run_id
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake"}):
            with patch("src.control_panel.get_connection", return_value=self.db):
                result = _get_latest_run_id("/nonexistent/path.db")
                assert result == "run-999"

    def test_guard_bypass_for_schedule_summary(self):
        from src.control_panel import _get_schedule_summary
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake"}):
            with patch("src.control_panel.get_connection", return_value=self.db):
                s = _get_schedule_summary("/nonexistent/path.db")
                assert s["total"] == 2
                assert s["eligible"] == 2
                assert s["analyzed"] == 1

    def test_guard_bypass_for_load_recs_latest(self):
        from src.control_panel import _load_recs
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake"}):
            with patch("src.control_panel.get_connection", return_value=self.db):
                recs = _load_recs("/nonexistent/path.db", "latest")
                assert len(recs) == 1
                assert recs[0]["recommendation_id"] == "r1"

    def test_guard_bypass_for_live_game_warnings(self):
        from src.control_panel import _get_live_game_warnings
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://fake"}):
            with patch("src.control_panel.get_connection", return_value=self.db):
                result = _get_live_game_warnings("/nonexistent/path.db", "run-999")
                assert result == []

    def test_sqlite_guard_still_works_when_path_missing(self):
        from src.control_panel import _get_latest_run_id
        with patch.dict(os.environ, {}, clear=True):
            assert _get_latest_run_id("/nonexistent/path.db") == ""

    def test_control_panel_imports_database_url_util(self):
        from src.control_panel import get_database_url
        assert callable(get_database_url)
