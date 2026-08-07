"""Tests for attaching later scan prices to stable official-pick identities."""

from datetime import datetime, timezone

from src.observations import get_observations, record_pipeline_observations


def _seed(conn):
    rec_id = "stable-rec"
    conn.execute("""
        INSERT INTO historical_recommendations (
            recommendation_id, event_id, player_id, player_name, market_type,
            market_form, side, sportsbook, line, offered_american_odds,
            offered_decimal_odds, offered_implied_prob, rec_status, scan_timestamp,
            freshness_status, recommendation_tier, qualification_passed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (rec_id, "E1", "P1", "Player", "strikeouts", "ou", "OVER",
           "DraftKings", 6.5, -110, 1.909, 0.524, "QUALIFIED",
           "2026-08-06T10:00:00+00:00", "FRESH", "OFFICIAL_TRACKED", 1))
    conn.execute(
        "INSERT INTO official_picks (recommendation_id, tier, outcome) VALUES (?, ?, ?)",
        (rec_id, "OFFICIAL_TRACKED", "pending"),
    )
    conn.commit()
    return rec_id


def test_pipeline_observation_matches_stable_identity(db_conn):
    rec_id = _seed(db_conn)
    opp = {
        "event_id": "E1", "player_id": "P1", "market_type": "strikeouts",
        "line": 6.5, "side": "OVER", "sportsbook": "DraftKings",
        "american_odds": -120, "decimal_odds": 1.833, "fair_prob": 0.55,
        "n_consensus_books": 5, "observation_time": "2026-08-06T11:00:00+00:00",
        "freshness_status": "FRESH", "market_quality": "VALID_MARKET",
    }
    assert record_pipeline_observations(db_conn, [opp], "PREGAME", "run-1") == 1
    rows = get_observations(db_conn, rec_id, "PREGAME")
    assert rows[0]["american_odds"] == -120
    assert rows[0]["source_run_id"] == "run-1"


def test_pipeline_observation_is_idempotent_per_phase(db_conn):
    _seed(db_conn)
    opp = {
        "event_id": "E1", "player_id": "P1", "market_type": "strikeouts",
        "line": 6.5, "side": "OVER", "sportsbook": "DraftKings",
        "american_odds": -120, "decimal_odds": 1.833,
        "observation_time": datetime.now(timezone.utc).isoformat(),
    }
    assert record_pipeline_observations(db_conn, [opp], "MORNING") == 1
    assert record_pipeline_observations(db_conn, [opp], "MORNING") == 0
