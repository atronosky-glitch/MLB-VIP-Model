"""Tests for the read-only Phase 19A production verifier."""

from __future__ import annotations

import sqlite3

from scripts.verify_phase19a_production import main, verify_lifecycle


def _db(with_table: bool = True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE historical_recommendations (recommendation_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO historical_recommendations VALUES ('rec-1')")
    if with_table:
        conn.execute(
            """CREATE TABLE recommendation_lifecycle_events (
                lifecycle_event_id TEXT PRIMARY KEY,
                recommendation_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                snapshot_kind TEXT,
                source_run_id TEXT,
                line_move_type TEXT,
                closing_available INTEGER,
                clv_available INTEGER,
                clv_probability REAL
            )"""
        )
    return conn


def _insert(conn, event_id, event_type, key, *, rec="rec-1", move="same_line", close=1, clv=1, prob=0.01):
    conn.execute(
        "INSERT INTO recommendation_lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, rec, event_type, key, "2026-08-04T12:00:00+00:00", "final", "run-1", move, close, clv, prob),
    )
    conn.commit()


def test_verifier_reports_counts_and_clean_integrity():
    conn = _db()
    _insert(conn, "e1", "RECOMMENDATION_CREATED", "created:1", close=None, clv=None, prob=None)
    _insert(conn, "e2", "CLOSING_SNAPSHOT", "closing:1", close=1, clv=1, prob=0.02)
    report = verify_lifecycle(conn)
    assert report["table_exists"] is True
    assert report["event_counts"]["CLOSING_SNAPSHOT"] == 1
    assert report["closing_available"]["true"] == 1
    assert report["duplicate_event_keys"] == 0
    assert report["orphaned_recommendations"] == 0
    assert report["integrity_failures"] == []


def test_verifier_detects_duplicates_orphans_and_invalid_line_clv():
    conn = _db()
    _insert(conn, "e1", "CLOSING_SNAPSHOT", "closing:1", move="line_changed", close=1, clv=1, prob=0.02)
    # The verifier must detect an orphan even though the lifecycle table has no FK.
    _insert(conn, "e2", "SETTLEMENT", "settlement:orphan", rec="missing", move="no_close", close=1, clv=0, prob=None)
    conn.execute("INSERT INTO recommendation_lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 ("e3", "rec-1", "CLOSING_SNAPSHOT", "closing:1", "2026-08-04T12:01:00+00:00", "final", "run-1", "same_line", 1, 1, 0.03))
    conn.commit()
    report = verify_lifecycle(conn)
    assert report["duplicate_event_keys"] == 1
    assert report["orphaned_recommendations"] == 1
    assert any("Line-changed" in failure for failure in report["integrity_failures"])
    assert any("Duplicate" in failure for failure in report["integrity_failures"])
    assert any("Orphaned" in failure for failure in report["integrity_failures"])


def test_missing_lifecycle_table_is_integrity_failure():
    report = verify_lifecycle(_db(with_table=False))
    assert report["table_exists"] is False
    assert report["integrity_failures"]


def test_main_json_exit_codes_without_database_writes(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "verify.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE historical_recommendations (recommendation_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--db-path", str(db_path), "--json"]) == 1
    output = capsys.readouterr().out
    assert '"table_exists": false' in output
    assert "DATABASE_URL" not in output
    assert db_path.exists()
