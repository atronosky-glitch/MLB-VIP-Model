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
        CREATE TABLE IF NOT EXISTS event_results (
            event_id TEXT PRIMARY KEY, final_status TEXT DEFAULT 'UNRESOLVED',
            away_score INTEGER, home_score INTEGER, result_source TEXT,
            source_observed_at TEXT, result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO historical_recommendations (
            recommendation_id, fingerprint, event_id, player_id, player_name, market_type,
            market_form, line, side, sportsbook, offered_american_odds,
            offered_decimal_odds, offered_implied_prob, rec_status, scan_timestamp,
            freshness_status, recommendation_tier, qualification_passed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rec_id, f"fp-{rec_id}", "E-AUTO", "P-AUTO", "Player", "pitching_strikeouts",
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


def _real_conn(tmp_path, name="clv_test.db"):
    """A real file-backed connection through the full init_db() schema —
    unlike the db_conn fixture (an intentionally minimal hand-rolled
    schema copy, missing several columns closing_prices needs; see
    CHANGELOG's "known fragility of that pattern" note), this guarantees
    closing_prices matches production exactly."""
    from database.db_manager import init_db, get_connection
    db_path = tmp_path / name
    init_db(str(db_path))
    return get_connection(str(db_path))


def _seed_via_save_recommendation(conn, rec_id, event_id="E-AUTO", player_id="P-AUTO"):
    """Insert a real historical_recommendations row through the actual
    production save path (unlike _seed()'s raw INSERT, which is written
    against db_conn's intentionally minimal fixture schema and doesn't
    supply every NOT NULL column the real init_db() schema requires,
    e.g. fingerprint/period) — needed for tests running against a real
    schema (_real_conn) rather than the db_conn fixture."""
    from database.db_manager import save_recommendation
    rec = {
        "event_id": event_id, "player_id": player_id, "player_name": "Player",
        "market_type": "pitching_strikeouts", "market_form": "ou", "period": "game",
        "line": 5.5, "side": "OVER", "sportsbook": "DraftKings",
        "offered_american_odds": -110, "offered_decimal_odds": 1.909,
        "offered_implied_prob": 0.524, "rec_status": "QUALIFIED",
        "scan_timestamp": datetime.now(timezone.utc).isoformat(),
        "freshness_status": "FRESH", "recommendation_tier": "OFFICIAL_TRACKED",
        "qualification_passed": 1, "league": "MLB", "sport": "baseball",
    }
    rec["recommendation_id"] = save_recommendation(conn, rec)
    conn.execute(
        "INSERT INTO official_picks (recommendation_id, tier, outcome) VALUES (?, ?, ?)",
        (rec["recommendation_id"], "OFFICIAL_TRACKED", "pending"),
    )
    conn.commit()
    return rec["recommendation_id"]


def test_grading_captures_the_canonical_closing_price(tmp_path):
    """Regression test (2026-09-06 fix): capture_closing_prices() only
    populates the canonical closing_prices table (the one CLV reporting
    actually reads from) when called with snapshot_kind="final" — no
    production call site ever did that before this fix, so closing_prices
    had zero rows in production despite 9,168 CLOSING_SNAPSHOT lifecycle
    events already being recorded correctly by the morning/pregame
    snapshots. Grading is the first point a recommendation is both
    settled and its game is confirmed over, so it's the correct "final"
    snapshot trigger."""
    db_conn = _real_conn(tmp_path)
    rec_id = _seed_via_save_recommendation(db_conn, "auto-clv-1")
    db_conn.execute(
        """INSERT INTO player_prop_odds
           (event_id, odd_id, sportsbook, player_id, player_name, market_type,
            market_group_key, side, line, price, decimal_odds, is_alt_line,
            available, validation_status, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("E-AUTO", "closing-odd-1", "DraftKings", "P-AUTO", "Player",
         "pitching_strikeouts", "grp-1", "OVER", 5.5, -125, 1.8, 0, 1,
         "VALID", datetime.now(timezone.utc).isoformat()),
    )
    db_conn.commit()
    save_player_stat_result(
        db_conn, "E-AUTO", "P-AUTO", "pitching_strikeouts",
        final_stat_value=7,
        result_source="verified-test", result_status="FINAL",
    )

    result = grade_available_recommendations(db_conn)
    assert result["graded"] == 1

    closing = db_conn.execute(
        "SELECT closing_american, closing_sportsbook FROM closing_prices "
        "WHERE recommendation_id = ?",
        (rec_id,),
    ).fetchone()
    assert closing is not None, "grading must populate the canonical closing_prices row"
    assert closing["closing_american"] == -125
    assert closing["closing_sportsbook"] == "DraftKings"


def test_grading_closing_price_capture_is_idempotent(tmp_path):
    db_conn = _real_conn(tmp_path)
    rec_id = _seed_via_save_recommendation(db_conn, "auto-clv-2")
    db_conn.execute(
        """INSERT INTO player_prop_odds
           (event_id, odd_id, sportsbook, player_id, player_name, market_type,
            market_group_key, side, line, price, decimal_odds, is_alt_line,
            available, validation_status, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("E-AUTO", "closing-odd-2", "DraftKings", "P-AUTO", "Player",
         "pitching_strikeouts", "grp-2", "OVER", 5.5, -125, 1.8, 0, 1,
         "VALID", datetime.now(timezone.utc).isoformat()),
    )
    db_conn.commit()
    save_player_stat_result(
        db_conn, "E-AUTO", "P-AUTO", "pitching_strikeouts",
        final_stat_value=7,
        result_source="verified-test", result_status="FINAL",
    )

    assert grade_available_recommendations(db_conn)["graded"] == 1
    assert grade_available_recommendations(db_conn)["graded"] == 0  # already settled

    count = db_conn.execute(
        "SELECT COUNT(*) AS c FROM closing_prices WHERE recommendation_id = ?",
        (rec_id,),
    ).fetchone()["c"]
    assert count == 1, "a re-run must not create a second closing_prices row"


def test_closing_price_capture_failure_does_not_block_grading(db_conn, monkeypatch):
    """A closing-price lookup problem for one recommendation must never
    prevent that recommendation from being settled."""
    import src.automatic_grading as ag

    _seed(db_conn, "auto-clv-3")
    save_player_stat_result(
        db_conn, "E-AUTO", "P-AUTO", "pitching_strikeouts",
        final_stat_value=7,
        result_source="verified-test", result_status="FINAL",
    )

    def _boom(conn, recs, snapshot_kind="final"):
        raise RuntimeError("simulated closing-price capture failure")

    monkeypatch.setattr(ag, "capture_closing_prices", _boom)

    result = grade_available_recommendations(db_conn)
    assert result["graded"] == 1
    assert result["errors"] == 0
    settlement = db_conn.execute(
        "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
        ("auto-clv-3",),
    ).fetchone()
    assert settlement["settlement_status"] == "WIN"


def test_voided_event_settles_player_prop_without_a_stat_fact(db_conn):
    """A postponed/cancelled game never produces a player_stat_results row
    — without checking event_results first, this recommendation would
    stay UNRESOLVED forever. It must void immediately instead."""
    _seed(db_conn, "auto-void-1")
    db_conn.execute(
        "INSERT INTO event_results (event_id, final_status) VALUES (?, ?)",
        ("E-AUTO", "POSTPONED"),
    )
    db_conn.commit()

    result = grade_available_recommendations(db_conn)
    assert result["graded"] == 1
    settlement = db_conn.execute(
        "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
        ("auto-void-1",),
    ).fetchone()
    assert settlement["settlement_status"] == "VOID"
