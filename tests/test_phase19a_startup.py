"""Phase 19A full-schema startup regression tests."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch


class _FakeCursor:
    rowcount = 0

    def __init__(self, statements):
        self.statements = statements
        self.description = None

    def execute(self, sql, params=()):
        self.statements.append(sql)
        self.last_sql = sql

    def fetchall(self):
        if "information_schema.tables" in getattr(self, "last_sql", ""):
            return [{"name": name} for name in (
                "recommendation_lifecycle_events", "scan_runs", "games", "odds",
                "historical_recommendations", "closing_prices", "market_settlements",
            )]
        return []

    def fetchone(self):
        if "current_database()" in getattr(self, "last_sql", ""):
            return {"database_name": "test_db", "schema_name": "public"}
        if "to_regclass" in getattr(self, "last_sql", ""):
            return {"table_name": "recommendation_lifecycle_events"}
        if "information_schema.tables" in getattr(self, "last_sql", ""):
            return {"name": "recommendation_lifecycle_events"}
        return None


class _FakePostgresRaw:
    def __init__(self):
        self.statements = []

    def cursor(self):
        return _FakeCursor(self.statements)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_init_db_creates_complete_schema_and_preserves_sqlite_data(tmp_path):
    import database.db_manager as dbm

    db_path = tmp_path / "startup.db"
    dbm.init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO games (event_id, league) VALUES ('keep-me', 'MLB')")
    conn.commit()
    conn.close()

    # Repeated startup is idempotent and must preserve existing rows.
    dbm.init_db(str(db_path))
    conn = sqlite3.connect(str(db_path))
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "recommendation_lifecycle_events" in tables
    assert {row[0] for row in conn.execute("SELECT event_id FROM games")} == {"keep-me"}
    conn.close()


def test_init_db_calls_required_schema_verification(tmp_path):
    import database.db_manager as dbm

    with patch.object(dbm, "verify_required_schema", wraps=dbm.verify_required_schema) as verify:
        dbm.init_db(str(tmp_path / "verified.db"))
    verify.assert_called_once()


def test_init_db_calls_lifecycle_creation_helper_exactly_once(tmp_path):
    import database.db_manager as dbm

    with patch.object(
        dbm,
        "create_recommendation_lifecycle_table",
        wraps=dbm.create_recommendation_lifecycle_table,
    ) as create_lifecycle:
        diagnostic = dbm.init_db(str(tmp_path / "lifecycle.db"))
    create_lifecycle.assert_called_once()
    assert diagnostic["lifecycle_helper_ran"] is True

def test_init_db_generates_postgresql_compatible_full_schema():
    import database.db_manager as dbm
    from database.connection import DB

    raw = _FakePostgresRaw()
    with patch.object(dbm, "get_connection", return_value=DB(raw, dialect="postgresql")):
        dbm.init_db("ignored-local-path.db")

    statements = "\n".join(raw.statements)
    assert "CREATE TABLE IF NOT EXISTS recommendation_lifecycle_events" in statements
    assert "CREATE TABLE IF NOT EXISTS games" in statements
    assert "DROP TABLE" not in statements.upper()
    assert "AUTOINCREMENT" not in statements.upper()


def test_postgresql_script_failure_rolls_back_and_stops():
    from database.connection import DB

    class FailingRaw(_FakePostgresRaw):
        def __init__(self):
            super().__init__()
            self.rollback_count = 0

        def cursor(self):
            raw = self

            class Cursor(_FakeCursor):
                def execute(self, sql, params=()):
                    self.statements.append(sql)
                    if "FAIL_DDL" in sql:
                        raise RuntimeError("ddl failed")

            return Cursor(raw.statements)

        def rollback(self):
            self.rollback_count += 1

    raw = FailingRaw()
    conn = DB(raw, dialect="postgresql")
    try:
        conn.executescript("CREATE TABLE ok (id INTEGER); CREATE TABLE FAIL_DDL (id INTEGER); CREATE TABLE never (id INTEGER);")
    except RuntimeError as exc:
        assert "statement 2" in str(exc)
    else:
        raise AssertionError("Expected schema DDL failure")
    assert raw.rollback_count == 1
    assert not any("never" in statement for statement in raw.statements)


def test_required_schema_verification_names_missing_tables(tmp_path):
    import database.db_manager as dbm

    conn = sqlite3.connect(tmp_path / "partial.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE games (event_id TEXT)")
    conn.commit()
    try:
        try:
            dbm.verify_required_schema(conn)
        except RuntimeError as exc:
            assert "recommendation_lifecycle_events" in str(exc)
            assert "market_settlements" in str(exc)
        else:
            raise AssertionError("Expected missing-schema failure")
    finally:
        conn.close()


def test_init_and_verify_schema_script_supports_sqlite(tmp_path, capsys):
    from scripts.init_and_verify_schema import main

    db_path = tmp_path / "script.db"
    assert main(["--db-path", str(db_path)]) == 0
    assert "SCHEMA VERIFIED dialect=sqlite" in capsys.readouterr().out


def test_init_and_verify_schema_script_fails_when_verification_fails(tmp_path, monkeypatch):
    from scripts import init_and_verify_schema

    monkeypatch.setattr(init_and_verify_schema, "init_db", lambda db_path: None)
    monkeypatch.setattr(
        init_and_verify_schema,
        "verify_required_schema",
        lambda conn: (_ for _ in ()).throw(RuntimeError("missing required tables")),
    )
    assert init_and_verify_schema.main(["--db-path", str(tmp_path / "missing.db")]) == 1


def test_debug_lifecycle_script_creates_and_verifies_sqlite(tmp_path, capsys):
    from scripts.debug_lifecycle_table_creation import main

    assert main(["--db-path", str(tmp_path / "lifecycle.db")]) == 0
    output = capsys.readouterr().out
    assert "LIFECYCLE DDL BEFORE COMMIT" in output
    assert "LIFECYCLE DDL AFTER COMMIT" in output
    assert "to_regclass=recommendation_lifecycle_events" in output


def test_worker_schema_startup_uses_full_initializer():
    import src.worker as worker

    config = SimpleNamespace(database_path="/data/mlb.db")
    with patch("database.db_manager.init_db") as init_db:
        worker._initialize_worker_schema(config)
    init_db.assert_called_once_with("/data/mlb.db")


def test_startup_source_paths_call_schema_initializer():
    from pathlib import Path

    worker_source = Path("src/worker.py").read_text(encoding="utf-8")
    dashboard_source = Path("src/control_panel.py").read_text(encoding="utf-8")
    pipeline_source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
    assert "_initialize_worker_schema(config)" in worker_source
    assert "init_db(db_path)" in dashboard_source
    assert "init_db()" in pipeline_source
