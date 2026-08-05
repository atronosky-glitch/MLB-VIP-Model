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

    def fetchall(self):
        return []

    def fetchone(self):
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
