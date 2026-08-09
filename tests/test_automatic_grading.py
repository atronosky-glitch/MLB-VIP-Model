"""Tests for verified-result automatic catch-up grading."""

from datetime import datetime, timezone

from database.db_manager import save_player_stat_result, save_bet_units
from src.automatic_grading import grade_available_recommendations


def _seed(conn, rec_id="auto-1"):
    for ddl in (
        "ALTER TABLE market_settlements ADD COLUMN settlement_id TEXT",
        "ALTER TABLE market_settlements ADD COLUMN settlement_reason TEXT",
        "ALTER TABLE market_settlements ADD COLUMN grader_version TEXT",
        "ALTER TABLE market_settlements ADD COLUMN settled_at TEXT",
    ):
        try:
            conn.execute(ddl)
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bet_units (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT,
            risk_units REAL, profit_units REAL, return_units REAL,
            odds_at_settle INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_stat_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, player_id TEXT NOT NULL,
            player_name TEXT, market_type TEXT NOT NULL,
            final_stat_value REAL, result_source TEXT,
            source_observed_at TEXT, result_status TEXT NOT NULL DEFAULT 'UNRESOLVED',
            result_detail TEXT, created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(event_id, player_id, market_type)
        )
    """)
    conn.execute("""
        INSERT INTO historical_recommendations (
            recommendation_id, event_id, player_id, player_name, market_type,
            market_form, line, side, sportsbook, offered_american_odds,
            offered_decimal_odds, offered_implied_prob, rec_status, scan_timestamp,
            freshness_status, recommendation_tier, qualification_passed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rec_id, "E-AUTO", "P-AUTO", "Player", "pitching_strikeouts",
           "ou", 5.5, "OVER", "DraftKings", -110, 1.909, 0.524,
           "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
           "OFFICIAL_TRACKED", 1))
    conn.execute(
        "INSERT INTO official_picks (recommendation_id, tier, outcome) VALUES (?, ?, ?)",
        (rec_id, "OFFICIAL_TRACKED", "pending"),
    )
    conn.commit()


def test_automatic_grading_settles_and_updates_projection(db_conn):
    _seed(db_conn)
    save_player_stat_result(
        db_conn, "E-AUTO", "P-AUTO", "pitching_strikeouts",
        final_stat_value=7,
        result_source="verified-test", result_status="FINAL",
    )
    result = grade_available_recommendations(db_conn)
    assert result["graded"] == 1
    settlement = db_conn.execute(
        "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
        ("auto-1",),
    ).fetchone()
    official = db_conn.execute(
        "SELECT outcome, profit_units FROM official_picks WHERE recommendation_id = ?",
        ("auto-1",),
    ).fetchone()
    assert settlement[0] == "WIN"
    assert official[0] == "win"
    assert official[1] > 0
    assert official[1] != 1.0


def test_automatic_grading_is_idempotent(db_conn):
    _seed(db_conn, "auto-2")
    save_player_stat_result(
        db_conn, "E-AUTO", "P-AUTO", "pitching_strikeouts",
        final_stat_value=4,
        result_source="verified-test", result_status="FINAL",
    )
    assert grade_available_recommendations(db_conn)["graded"] == 1
    assert grade_available_recommendations(db_conn)["graded"] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM bet_units WHERE recommendation_id = 'auto-2'").fetchone()[0] == 1
