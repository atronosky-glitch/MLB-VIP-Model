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


# ── Sportsbook-independent matching (2026-09-07 fix) ─────────────────
#
# Root cause: this matched on event/player/market/side/line AND an exact
# sportsbook. A later scan's best price is very often a different book
# than whichever one was quoting it when the pick was frozen (books move
# independently of each other) — real production data showed 91
# completed pregame scans had produced only 9 PREGAME observations ever.
# The identity that actually defines "the same pick" everywhere else in
# this codebase (compute_bet_slot_key) never includes sportsbook, price,
# or line for exactly this reason.

def test_pregame_observation_matches_a_different_sportsbook_than_freeze_time(db_conn):
    """The real-world case this fix targets: DraftKings had the best
    price this morning (frozen as the pick); by pregame, FanDuel is the
    one showing up in the scan's opportunities. Must still attach."""
    rec_id = _seed(db_conn)
    opp = {
        "event_id": "E1", "player_id": "P1", "market_type": "strikeouts",
        "line": 6.5, "side": "OVER", "sportsbook": "FanDuel",
        "american_odds": -105, "decimal_odds": 1.952, "fair_prob": 0.55,
        "n_consensus_books": 5, "observation_time": "2026-08-06T15:00:00+00:00",
        "freshness_status": "FRESH", "market_quality": "VALID_MARKET",
    }
    assert record_pipeline_observations(db_conn, [opp], "PREGAME", "run-2") == 1
    rows = get_observations(db_conn, rec_id, "PREGAME")
    assert rows[0]["sportsbook"] == "FanDuel"
    assert rows[0]["american_odds"] == -105


def test_does_not_attach_a_new_observation_to_a_superseded_pick(db_conn):
    """A pick that's been superseded (material line/price update replaced
    it with a new official_picks row) must never receive a new
    observation for the OLD, no-longer-active identity — only the
    current ACTIVE pick for that player/market/side/line is a valid
    match."""
    rec_id = _seed(db_conn)
    db_conn.execute(
        "UPDATE official_picks SET pick_status = 'SUPERSEDED' WHERE recommendation_id = ?",
        (rec_id,),
    )
    db_conn.commit()
    opp = {
        "event_id": "E1", "player_id": "P1", "market_type": "strikeouts",
        "line": 6.5, "side": "OVER", "sportsbook": "DraftKings",
        "american_odds": -110, "decimal_odds": 1.909,
        "observation_time": datetime.now(timezone.utc).isoformat(),
    }
    assert record_pipeline_observations(db_conn, [opp], "PREGAME") == 0
    assert get_observations(db_conn, rec_id, "PREGAME") == []


def test_still_requires_the_same_line(db_conn):
    """A different line is a materially different bet (it would have
    triggered its own new official pick via classify_pick_update) — not
    something this matching should paper over."""
    _seed(db_conn)
    opp = {
        "event_id": "E1", "player_id": "P1", "market_type": "strikeouts",
        "line": 7.5, "side": "OVER", "sportsbook": "DraftKings",
        "american_odds": -110, "decimal_odds": 1.909,
        "observation_time": datetime.now(timezone.utc).isoformat(),
    }
    assert record_pipeline_observations(db_conn, [opp], "PREGAME") == 0
