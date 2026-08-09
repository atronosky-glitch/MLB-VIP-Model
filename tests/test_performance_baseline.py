"""Tests for non-destructive customer performance epochs."""

from database.db_manager import get_performance_baseline


def test_performance_baseline_is_stable(db_conn):
    db_conn.execute("""CREATE TABLE IF NOT EXISTS performance_baseline (
        baseline_id INTEGER PRIMARY KEY, baseline_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT 'production_baseline'
    )""")
    db_conn.execute("INSERT INTO performance_baseline VALUES (1, '2026-08-09T12:00:00Z', 'test')")
    db_conn.commit()
    assert get_performance_baseline(db_conn) == "2026-08-09T12:00:00Z"
