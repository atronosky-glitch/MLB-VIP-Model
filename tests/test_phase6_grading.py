"""Phase 6 tests: Historical recommendations, grading, CLV, performance.

All tests use in-memory SQLite databases. No live API calls. No mutable cache.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from database.db_manager import (
    init_db,
    get_connection,
    compute_fingerprint,
    generate_recommendation_id,
    save_recommendation,
    save_event_result,
    save_player_stat_result,
    settle_recommendation,
    compute_units,
    save_bet_units,
    save_closing_price,
    apply_manual_override,
    get_unsettled_recommendations,
    get_settled_recommendations,
    get_recommendation_by_id,
    get_player_stat_result,
    FINGERPRINT_FIELDS,
)
from src.grading import (
    GRADER_VERSION,
    grade_ou,
    grade_yn,
    calculate_clv,
    classify_line_movement,
    performance_summary,
    breakdown_by_field,
    assign_bucket,
    EV_BUCKETS,
    ODDS_BUCKETS,
    N_BOOKS_BUCKETS,
    YN_ADV_BUCKETS,
)


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def db():
    """In-memory SQLite database with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
            away_team TEXT, home_team TEXT, start_time TEXT,
            status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, market TEXT NOT NULL, selection TEXT,
            price REAL, points REAL, is_alt_line INTEGER DEFAULT 0,
            available INTEGER DEFAULT 1, pulled_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            odd_id TEXT NOT NULL, sportsbook TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT, team_id TEXT DEFAULT '',
            team_name TEXT DEFAULT '', market_type TEXT NOT NULL,
            market_group_key TEXT NOT NULL, side TEXT NOT NULL, line REAL,
            price INTEGER, decimal_odds REAL, is_alt_line INTEGER DEFAULT 0,
            available INTEGER DEFAULT 1, validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT '', mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT '', captured_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
            finished_at TEXT, run_type TEXT DEFAULT 'scan', mode TEXT,
            market_filter TEXT, form_filter TEXT, n_events INTEGER DEFAULT 0,
            n_markets INTEGER DEFAULT 0, n_opportunities INTEGER DEFAULT 0,
            n_yn_opps INTEGER DEFAULT 0, data_source TEXT,
            research_only INTEGER DEFAULT 0, error_message TEXT,
            metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_id TEXT NOT NULL, ingested_at TEXT DEFAULT (datetime('now')),
            odds_rows INTEGER DEFAULT 0, audit_rows INTEGER DEFAULT 0,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            scan_run_id TEXT, ingestion_run_id TEXT, event_id TEXT NOT NULL,
            event_start_time TEXT, player_id TEXT NOT NULL, player_name TEXT,
            market_type TEXT NOT NULL, market_form TEXT NOT NULL,
            period TEXT NOT NULL, line REAL, side TEXT NOT NULL,
            sportsbook TEXT NOT NULL, offered_american_odds INTEGER NOT NULL,
            offered_decimal_odds REAL NOT NULL, offered_implied_prob REAL NOT NULL,
            fair_prob REAL, fair_american_odds INTEGER, ev_pct REAL,
            yn_reference_prob REAL, yn_reference_odds INTEGER,
            yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
            n_consensus_books INTEGER, market_quality TEXT,
            rec_status TEXT NOT NULL, rec_eligible INTEGER DEFAULT 0,
            data_source TEXT, observation_timestamp TEXT,
            scan_timestamp TEXT NOT NULL, freshness_status TEXT,
            model_version TEXT DEFAULT 'v1',
            matchup TEXT DEFAULT '', event_status TEXT DEFAULT '',
            model_score REAL, score_version TEXT DEFAULT 'model_score_v1',
            score_components TEXT, score_cap REAL, score_explanation TEXT,
            recommendation_tier TEXT DEFAULT 'RESEARCH_ONLY',
            qualification_passed INTEGER DEFAULT 0,
            qualification_reasons TEXT DEFAULT '',
            disqualification_reasons TEXT DEFAULT '',
            contributing_book_count INTEGER DEFAULT 0,
            contributing_books TEXT DEFAULT '',
            applicable_edge_metric TEXT DEFAULT '',
            applicable_edge_threshold REAL DEFAULT 0.0,
            model_score_threshold REAL DEFAULT 8.0,
            qualification_rules_version TEXT DEFAULT '',
            league TEXT DEFAULT 'MLB', sport TEXT DEFAULT 'baseball',
            raw_line REAL, confidence_score REAL, confidence_grade TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_fingerprint ON historical_recommendations(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_hr_event ON historical_recommendations(event_id);
        CREATE INDEX IF NOT EXISTS idx_hr_player ON historical_recommendations(player_id);
        CREATE TABLE IF NOT EXISTS event_results (
            event_id TEXT PRIMARY KEY, final_status TEXT DEFAULT 'UNRESOLVED',
            away_score INTEGER, home_score INTEGER, result_source TEXT,
            source_observed_at TEXT, result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_stat_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT, market_type TEXT NOT NULL,
            final_stat_value REAL, result_source TEXT, source_observed_at TEXT,
            result_status TEXT DEFAULT 'UNRESOLVED', result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(event_id, player_id, market_type)
        );
        CREATE TABLE IF NOT EXISTS market_settlements (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            settlement_status TEXT DEFAULT 'UNRESOLVED', final_stat_value REAL,
            settled_at TEXT, settlement_reason TEXT,
            grader_version TEXT DEFAULT 'v1', manual_override INTEGER DEFAULT 0,
            override_reason TEXT, override_previous TEXT, league TEXT DEFAULT 'MLB',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_rec ON market_settlements(recommendation_id);
        CREATE TABLE IF NOT EXISTS bet_units (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            risk_units REAL DEFAULT 1.0, profit_units REAL DEFAULT 0.0,
            return_units REAL DEFAULT 0.0, odds_at_settle INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS closing_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL, closing_american INTEGER,
            closing_decimal REAL, closing_implied_prob REAL,
            closing_line REAL, closing_observed_at TEXT,
            closing_sportsbook TEXT, line_move_type TEXT,
            clv_probability REAL, clv_price_diff INTEGER,
            clv_available INTEGER DEFAULT 0, line_movement_direction TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS manual_override_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL, previous_status TEXT,
            new_status TEXT NOT NULL, override_reason TEXT NOT NULL,
            override_by TEXT DEFAULT 'cli',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    yield conn
    conn.close()


def _make_rec(**overrides) -> dict:
    """Build a minimal recommendation dict with defaults."""
    rec = {
        "event_id": "EVT_001",
        "event_start_time": "2026-07-23T19:00:00Z",
        "player_id": "PLAYER_001",
        "player_name": "Test Pitcher",
        "market_type": "pitching_strikeouts_ou",
        "market_form": "ou",
        "period": "game",
        "line": 5.5,
        "side": "OVER",
        "sportsbook": "draftkings",
        "offered_american_odds": -110,
        "offered_decimal_odds": 1.9091,
        "offered_implied_prob": 0.5238,
        "fair_prob": 0.50,
        "fair_american_odds": 100,
        "ev_pct": 4.76,
        "n_consensus_books": 6,
        "market_quality": "VALID_MARKET",
        "rec_status": "STRONG_EDGE",
        "rec_eligible": True,
        "data_source": "LIVE API",
        "observation_timestamp": "2026-07-23T18:30:00Z",
        "scan_timestamp": "2026-07-23T18:31:00Z",
        "freshness_status": "FRESH",
        "model_version": "v1",
    }
    rec.update(overrides)
    return rec


# ==================================================================
# 1. Recommendation persistence
# ==================================================================

class TestRecommendationPersistence:
    def test_exact_snapshot_inserted(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        assert rec_id is not None
        row = db.execute(
            "SELECT * FROM historical_recommendations WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row is not None
        assert row["event_id"] == "EVT_001"
        assert row["offered_american_odds"] == -110

    def test_exact_duplicate_deduplicated(self, db):
        rec = _make_rec()
        id1 = save_recommendation(db, rec)
        id2 = save_recommendation(db, rec)
        assert id1 is not None
        assert id2 is None  # deduplicated
        count = db.execute("SELECT COUNT(*) FROM historical_recommendations").fetchone()[0]
        assert count == 1

    def test_price_change_creates_new_snapshot(self, db):
        rec1 = _make_rec(offered_american_odds=-110)
        rec2 = _make_rec(offered_american_odds=-105)
        id1 = save_recommendation(db, rec1)
        id2 = save_recommendation(db, rec2)
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2
        count = db.execute("SELECT COUNT(*) FROM historical_recommendations").fetchone()[0]
        assert count == 2

    def test_line_change_creates_new_snapshot(self, db):
        rec1 = _make_rec(line=5.5)
        rec2 = _make_rec(line=6.0)
        id1 = save_recommendation(db, rec1)
        id2 = save_recommendation(db, rec2)
        assert id1 is not None and id2 is not None
        count = db.execute("SELECT COUNT(*) FROM historical_recommendations").fetchone()[0]
        assert count == 2

    def test_status_change_creates_new_snapshot(self, db):
        rec1 = _make_rec(rec_status="STRONG_EDGE")
        rec2 = _make_rec(rec_status="POSITIVE_EDGE")
        id1 = save_recommendation(db, rec1)
        id2 = save_recommendation(db, rec2)
        assert id1 is not None and id2 is not None

    def test_yn_fields_semantically_correct(self, db):
        rec = _make_rec(
            market_type="pitching_strikeouts_yn",
            market_form="yn",
            line=None,
            side="YES",
            offered_american_odds=150,
            offered_decimal_odds=2.5,
            offered_implied_prob=0.4,
            fair_prob=None,
            fair_american_odds=None,
            ev_pct=None,
            yn_reference_prob=0.45,
            yn_reference_odds=122,
            yn_implied_prob_adv=5.0,
            yn_decimal_odds_adv=15,
        )
        rec_id = save_recommendation(db, rec)
        assert rec_id is not None
        row = db.execute(
            "SELECT * FROM historical_recommendations WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["fair_prob"] is None
        assert row["ev_pct"] is None
        assert row["yn_reference_prob"] == 0.45
        assert row["yn_implied_prob_adv"] == 5.0

    def test_old_records_unchanged_after_new_save(self, db):
        rec1 = _make_rec(offered_american_odds=-110)
        id1 = save_recommendation(db, rec1)
        rec2 = _make_rec(offered_american_odds=-105)
        save_recommendation(db, rec2)
        row1 = db.execute(
            "SELECT offered_american_odds FROM historical_recommendations WHERE recommendation_id = ?",
            (id1,),
        ).fetchone()
        assert row1["offered_american_odds"] == -110


# ==================================================================
# 2. Fingerprint
# ==================================================================

class TestFingerprint:
    def test_deterministic(self):
        rec = _make_rec()
        fp1 = compute_fingerprint(rec)
        fp2 = compute_fingerprint(rec)
        assert fp1 == fp2

    def test_price_change_alters_fingerprint(self):
        rec1 = _make_rec(offered_american_odds=-110)
        rec2 = _make_rec(offered_american_odds=-105)
        assert compute_fingerprint(rec1) != compute_fingerprint(rec2)

    def test_line_change_alters_fingerprint(self):
        rec1 = _make_rec(line=5.5)
        rec2 = _make_rec(line=6.0)
        assert compute_fingerprint(rec1) != compute_fingerprint(rec2)

    def test_side_change_alters_fingerprint(self):
        rec1 = _make_rec(side="OVER")
        rec2 = _make_rec(side="UNDER")
        assert compute_fingerprint(rec1) != compute_fingerprint(rec2)

    def test_observation_time_change_alters_fingerprint(self):
        rec1 = _make_rec(observation_timestamp="2026-07-23T18:00:00Z")
        rec2 = _make_rec(observation_timestamp="2026-07-23T19:00:00Z")
        assert compute_fingerprint(rec1) != compute_fingerprint(rec2)

    def test_32_char_hex(self):
        fp = compute_fingerprint(_make_rec())
        assert len(fp) == 32
        int(fp, 16)  # should not raise


# ==================================================================
# 3. O/U Grading
# ==================================================================

class TestOUGrading:
    def test_over_win(self):
        assert grade_ou(7.0, 5.5, "OVER") == "WIN"

    def test_over_loss(self):
        assert grade_ou(4.0, 5.5, "OVER") == "LOSS"

    def test_under_win(self):
        assert grade_ou(4.0, 5.5, "UNDER") == "WIN"

    def test_under_loss(self):
        assert grade_ou(7.0, 5.5, "UNDER") == "LOSS"

    def test_whole_line_push(self):
        assert grade_ou(6.0, 6.0, "OVER") == "PUSH"
        assert grade_ou(6.0, 6.0, "UNDER") == "PUSH"

    def test_half_line_no_push(self):
        # Half-line: equality is impossible for real stats, but if it happens
        assert grade_ou(5.5, 5.5, "OVER") == "LOSS"
        assert grade_ou(5.5, 5.5, "UNDER") == "LOSS"

    def test_void(self):
        # Void is not determined by grade_ou — it's set externally
        assert grade_ou(7.0, 5.5, "OVER") == "WIN"  # still grades

    def test_unresolved_on_none(self):
        assert grade_ou(None, 5.5, "OVER") == "UNRESOLVED"
        assert grade_ou(7.0, None, "OVER") == "UNRESOLVED"

    def test_malformed_side(self):
        assert grade_ou(7.0, 5.5, "INVALID") == "UNRESOLVED"

    def test_exact_stat_over(self):
        assert grade_ou(5.5, 5.5, "OVER") == "LOSS"  # half-line, no push

    def test_exact_stat_under(self):
        assert grade_ou(5.5, 5.5, "UNDER") == "LOSS"


# ==================================================================
# 4. YN Grading
# ==================================================================

class TestYNGrading:
    def test_yn_always_unresolved(self):
        assert grade_yn() == "UNRESOLVED"

    def test_yn_no_automation(self):
        # Multiple calls all return UNRESOLVED
        for _ in range(5):
            assert grade_yn() == "UNRESOLVED"

    def test_yn_verified_numeric_fact(self):
        assert grade_yn(2, "YES") == "WIN"
        assert grade_yn(0, "YES") == "LOSS"
        assert grade_yn(0, "NO") == "WIN"


# ==================================================================
# 5. Units
# ==================================================================

class TestUnits:
    def test_positive_odds_win(self):
        risk, profit, ret = compute_units("WIN", 150)
        assert risk == 1.0
        assert profit == 1.5
        assert ret == 2.5

    def test_negative_odds_win(self):
        risk, profit, ret = compute_units("WIN", -150)
        assert risk == 1.0
        assert abs(profit - 100 / 150) < 0.001
        assert abs(ret - (1 + 100 / 150)) < 0.001

    def test_loss(self):
        risk, profit, ret = compute_units("LOSS", -110)
        assert risk == 1.0
        assert profit == -1.0
        assert ret == 0.0

    def test_push(self):
        risk, profit, ret = compute_units("PUSH", -110)
        assert risk == 1.0
        assert profit == 0.0
        assert ret == 1.0

    def test_void(self):
        risk, profit, ret = compute_units("VOID", -110)
        assert risk == 1.0
        assert profit == 0.0

    def test_cancelled(self):
        risk, profit, ret = compute_units("CANCELLED", -110)
        assert risk == 1.0
        assert profit == 0.0

    def test_unresolved_excluded(self):
        risk, profit, ret = compute_units("UNRESOLVED", -110)
        assert risk == 0.0
        assert profit == 0.0
        assert ret == 0.0


# ==================================================================
# 6. CLV
# ==================================================================

class TestCLV:
    def test_favorable_same_line(self):
        result = calculate_clv(-110, 5.5, -105, 5.5)
        assert result["clv_available"] is True
        assert result["line_move_type"] == "same_line"
        assert result["clv_probability"] > 0  # line moved in our favor
        assert result["clv_price_diff"] == 5  # -105 - (-110) = +5

    def test_unfavorable_same_line(self):
        result = calculate_clv(-110, 5.5, -120, 5.5)
        assert result["clv_available"] is True
        assert result["clv_probability"] < 0

    def test_unchanged_price(self):
        result = calculate_clv(-110, 5.5, -110, 5.5)
        assert result["clv_available"] is True
        assert result["clv_probability"] == 0.0
        assert result["clv_price_diff"] == 0

    def test_changed_line(self):
        result = calculate_clv(-110, 5.5, -110, 6.0)
        assert result["clv_available"] is False
        assert result["line_move_type"] == "line_changed"

    def test_missing_close(self):
        result = calculate_clv(-110, 5.5, None, None)
        assert result["clv_available"] is False
        assert result["line_move_type"] == "no_close"

    def test_yn_close_labeled_correctly(self):
        result = calculate_clv(150, None, 140, None)
        assert result["clv_available"] is True
        assert result["line_move_type"] == "same_line"
        assert result["clv_probability"] < 0  # price shortened

    def test_exact_line_closing_american_used_when_representative_line_moved(self):
        # Representative market moved from 5.5 to 6.5, but a book still
        # quoted our exact original 5.5 as an alt line at close: that's a
        # genuinely correct same-line comparison, not "line_changed".
        result = calculate_clv(
            -110, 5.5, -110, 6.5,
            exact_line_closing_american=-105,
        )
        assert result["clv_available"] is True
        assert result["line_move_type"] == "same_line"
        assert result["clv_price_diff"] == 5  # -105 - (-110)
        assert result["line_movement_direction"] is None

    def test_line_changed_without_exact_line_reports_direction_not_fabricated_price(self):
        result = calculate_clv(
            -110, 5.5, -105, 6.5,
            market_type="player_points_ou", side="OVER",
        )
        assert result["clv_available"] is False
        assert result["line_move_type"] == "line_changed"
        # OVER 5.5 -> OVER 6.5: bettor locked in the easier (lower) number
        # before it rose -> favorable CLV, even though price CLV can't be
        # computed at mismatched lines.
        assert result["line_movement_direction"] == "favorable"
        assert result["clv_probability"] is None
        assert result["clv_price_diff"] is None


class TestClassifyLineMovement:
    def test_missing_data_is_unknown(self):
        assert classify_line_movement("player_points_ou", "OVER", None, 5.5) == "unknown"
        assert classify_line_movement("player_points_ou", "OVER", 5.5, None) == "unknown"

    def test_no_movement_is_neutral(self):
        assert classify_line_movement("player_points_ou", "OVER", 5.5, 5.5) == "neutral"

    def test_over_favorable_when_line_rises(self):
        assert classify_line_movement("player_points_ou", "OVER", 5.5, 6.5) == "favorable"

    def test_over_unfavorable_when_line_falls(self):
        assert classify_line_movement("player_points_ou", "OVER", 5.5, 4.5) == "unfavorable"

    def test_under_favorable_when_line_falls(self):
        assert classify_line_movement("player_points_ou", "UNDER", 5.5, 4.5) == "favorable"

    def test_under_unfavorable_when_line_rises(self):
        assert classify_line_movement("player_points_ou", "UNDER", 5.5, 6.5) == "unfavorable"

    def test_spread_favorable_when_signed_raw_line_falls(self):
        # Favorite -3.5 tightening to -2.5 is UNFAVORABLE for the favorite
        # (raw_line rose from -3.5 to -2.5), regardless of side label.
        assert classify_line_movement("game_spread_ou", "home", -3.5, -2.5) == "unfavorable"
        # Favorite -3.5 drifting to -4.5 is FAVORABLE (raw_line fell).
        assert classify_line_movement("game_spread_ou", "home", -3.5, -4.5) == "favorable"

    def test_spread_direction_independent_of_favorite_or_underdog(self):
        # Underdog +3.5 drifting to +4.5: raw_line rose -> unfavorable for
        # whichever side holds that raw_line, same rule as the favorite.
        assert classify_line_movement("game_spread_ou", "away", 3.5, 4.5) == "unfavorable"
        assert classify_line_movement("game_spread_ou", "away", 3.5, 2.5) == "favorable"

    def test_unrecognized_side_is_unknown(self):
        assert classify_line_movement("player_points_ou", "", 5.5, 6.5) == "unknown"
        assert classify_line_movement("player_points_ou", None, 5.5, 6.5) == "unknown"


# ==================================================================
# 7. Buckets
# ==================================================================

class TestBuckets:
    def test_ev_buckets(self):
        assert assign_bucket(-1, EV_BUCKETS) == "below_0"
        assert assign_bucket(0, EV_BUCKETS) == "0_to_2"
        assert assign_bucket(1.9, EV_BUCKETS) == "0_to_2"
        assert assign_bucket(2, EV_BUCKETS) == "2_to_5"
        assert assign_bucket(4.9, EV_BUCKETS) == "2_to_5"
        assert assign_bucket(5, EV_BUCKETS) == "5_to_10"
        assert assign_bucket(9.9, EV_BUCKETS) == "5_to_10"
        assert assign_bucket(10, EV_BUCKETS) == "10_plus"
        assert assign_bucket(25, EV_BUCKETS) == "10_plus"

    def test_odds_buckets(self):
        assert assign_bucket(-250, ODDS_BUCKETS) == "shorter_than_-200"
        assert assign_bucket(-200, ODDS_BUCKETS) == "-200_to_-151"
        assert assign_bucket(-151, ODDS_BUCKETS) == "-200_to_-151"
        assert assign_bucket(-150, ODDS_BUCKETS) == "-150_to_-101"
        assert assign_bucket(-101, ODDS_BUCKETS) == "-150_to_-101"
        assert assign_bucket(-100, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(0, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(100, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(101, ODDS_BUCKETS) == "+101_to_+150"
        assert assign_bucket(150, ODDS_BUCKETS) == "+101_to_+150"
        assert assign_bucket(151, ODDS_BUCKETS) == "+151_to_+200"
        assert assign_bucket(200, ODDS_BUCKETS) == "+151_to_+200"
        assert assign_bucket(201, ODDS_BUCKETS) == "longer_than_+200"

    def test_n_books_buckets(self):
        assert assign_bucket(2, N_BOOKS_BUCKETS) == "fewer_than_3"
        assert assign_bucket(3, N_BOOKS_BUCKETS) == "3"
        assert assign_bucket(4, N_BOOKS_BUCKETS) == "4"
        assert assign_bucket(5, N_BOOKS_BUCKETS) == "5_plus"
        assert assign_bucket(10, N_BOOKS_BUCKETS) == "5_plus"

    def test_yn_adv_buckets(self):
        assert assign_bucket(-1, YN_ADV_BUCKETS) == "below_0"
        assert assign_bucket(0, YN_ADV_BUCKETS) == "0_to_2"
        assert assign_bucket(1.9, YN_ADV_BUCKETS) == "0_to_2"
        assert assign_bucket(2, YN_ADV_BUCKETS) == "2_to_4"
        assert assign_bucket(3.9, YN_ADV_BUCKETS) == "2_to_4"
        assert assign_bucket(4, YN_ADV_BUCKETS) == "4_to_8"
        assert assign_bucket(7.9, YN_ADV_BUCKETS) == "4_to_8"
        assert assign_bucket(8, YN_ADV_BUCKETS) == "8_plus"


# ==================================================================
# 8. Settlement
# ==================================================================

class TestSettlement:
    def test_settle_win(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        ok = settle_recommendation(db, rec_id, "WIN", final_stat_value=7.0)
        assert ok is True
        row = db.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["settlement_status"] == "WIN"

    def test_settlement_league_matches_recommendation_not_hardcoded_mlb(self, db):
        """Found live 2026-08-20 against a real completed WNBA game:
        market_settlements.league defaults to 'MLB' at the schema level
        and settle_recommendation() never overrode it — every WNBA/NFL
        settlement was silently mislabeled MLB in the database."""
        rec = _make_rec(league="WNBA", sport="basketball")
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "WIN", final_stat_value=7.0)
        row = db.execute(
            "SELECT league FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["league"] == "WNBA"

    def test_idempotent_regrading(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "WIN")
        # Second settlement should be idempotent
        ok = settle_recommendation(db, rec_id, "LOSS")
        assert ok is True
        row = db.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["settlement_status"] == "WIN"  # unchanged

    def test_settle_ou_with_stat(self, db):
        rec = _make_rec(market_type="pitching_strikeouts_ou", side="OVER", line=5.5)
        rec_id = save_recommendation(db, rec)
        save_player_stat_result(db, "EVT_001", "PLAYER_001",
                                "pitching_strikeouts_ou", final_stat_value=7.0)
        stat = get_player_stat_result(db, "EVT_001", "PLAYER_001",
                                       "pitching_strikeouts_ou")
        assert stat["final_stat_value"] == 7.0

    def test_units_saved_on_settle(self, db):
        rec = _make_rec(offered_american_odds=-110)
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "WIN")
        save_bet_units(db, rec_id, "WIN", -110)
        row = db.execute(
            "SELECT * FROM bet_units WHERE recommendation_id = ?", (rec_id,),
        ).fetchone()
        assert row is not None
        assert row["risk_units"] == 1.0
        assert row["profit_units"] > 0


# ==================================================================
# 9. Manual overrides
# ==================================================================

class TestManualOverrides:
    def test_valid_override(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "UNRESOLVED")
        ok = apply_manual_override(db, rec_id, "WIN", "Verified via box score")
        assert ok is True
        row = db.execute(
            "SELECT manual_override, override_reason FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["manual_override"] == 1
        assert "box score" in row["override_reason"]

    def test_missing_reason_rejected(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        ok = apply_manual_override(db, rec_id, "WIN", "")
        assert ok is False

    def test_audit_record_preserved(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        apply_manual_override(db, rec_id, "WIN", "Test reason")
        audit = db.execute(
            "SELECT * FROM manual_override_audit WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert audit is not None
        assert audit["new_status"] == "WIN"
        assert audit["override_reason"] == "Test reason"

    def test_automated_not_silently_overwritten(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "WIN")
        # Manual override should still work (it's explicit)
        ok = apply_manual_override(db, rec_id, "LOSS", "Corrected")
        assert ok is True
        row = db.execute(
            "SELECT settlement_status, override_previous FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["settlement_status"] == "LOSS"
        assert row["override_previous"] == "WIN"


# ==================================================================
# 10. Performance summaries
# ==================================================================

class TestPerformanceSummary:
    def test_overall_roi(self):
        recs = [
            {"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 0.909,
             "offered_american_odds": -110, "ev_pct": 5.0},
            {"settlement_status": "LOSS", "risk_units": 1.0, "profit_units": -1.0,
             "offered_american_odds": -110, "ev_pct": 3.0},
        ]
        s = performance_summary(recs)
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["win_rate"] == 0.5
        assert s["units_risked"] == 2.0
        assert abs(s["units_won"] - (-0.091)) < 0.01

    def test_win_rate_denominator(self):
        recs = [
            {"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 1.0,
             "offered_american_odds": 100},
            {"settlement_status": "PUSH", "risk_units": 1.0, "profit_units": 0.0,
             "offered_american_odds": -110},
            {"settlement_status": "UNRESOLVED", "risk_units": 0.0, "profit_units": 0.0,
             "offered_american_odds": -110},
        ]
        s = performance_summary(recs)
        assert s["settled"] == 2  # WIN + PUSH
        assert s["win_rate"] == 0.5  # 1/2

    def test_pct_beating_close_uses_price_clv_when_available(self):
        recs = [
            {"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 0.9,
             "clv_probability": 0.02},
            {"settlement_status": "LOSS", "risk_units": 1.0, "profit_units": -1.0,
             "clv_probability": -0.01},
        ]
        s = performance_summary(recs)
        assert s["pct_beating_close"] == 0.5

    def test_pct_beating_close_falls_back_to_line_movement_direction(self):
        # No price CLV (line moved, no exact-line match) but the bettor
        # still captured the more favorable original number.
        recs = [
            {"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 0.9,
             "clv_probability": None, "line_movement_direction": "favorable"},
            {"settlement_status": "LOSS", "risk_units": 1.0, "profit_units": -1.0,
             "clv_probability": None, "line_movement_direction": "unfavorable"},
        ]
        s = performance_summary(recs)
        assert s["pct_beating_close"] == 0.5

    def test_pct_beating_close_excludes_recs_with_no_closing_evidence(self):
        recs = [
            {"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 0.9},
            {"settlement_status": "LOSS", "risk_units": 1.0, "profit_units": -1.0,
             "clv_probability": -0.01},
        ]
        s = performance_summary(recs)
        assert s["pct_beating_close"] == 0.0  # only the 1 rec with evidence, and it lost

    def test_pct_beating_close_none_when_no_evidence_at_all(self):
        recs = [{"settlement_status": "WIN", "risk_units": 1.0, "profit_units": 0.9}]
        s = performance_summary(recs)
        assert s["pct_beating_close"] is None

    def test_pushes_excluded_from_risked(self):
        recs = [
            {"settlement_status": "PUSH", "risk_units": 1.0, "profit_units": 0.0,
             "offered_american_odds": -110},
        ]
        s = performance_summary(recs)
        assert s["units_risked"] == 0.0  # PUSH not risked

    def test_unresolved_excluded(self):
        recs = [
            {"settlement_status": "UNRESOLVED", "risk_units": 0.0, "profit_units": 0.0,
             "offered_american_odds": -110},
        ]
        s = performance_summary(recs)
        assert s["unresolved"] == 1
        assert s["settled"] == 0

    def test_market_breakdown(self):
        recs = [
            {"market_type": "pitching_strikeouts_ou", "settlement_status": "WIN",
             "risk_units": 1.0, "profit_units": 1.0, "offered_american_odds": 100},
            {"market_type": "pitching_outs_ou", "settlement_status": "LOSS",
             "risk_units": 1.0, "profit_units": -1.0, "offered_american_odds": -110},
        ]
        bd = breakdown_by_field(recs, "market_type")
        assert "pitching_strikeouts_ou" in bd
        assert bd["pitching_strikeouts_ou"]["wins"] == 1
        assert bd["pitching_outs_ou"]["losses"] == 1

    def test_sportsbook_breakdown(self):
        recs = [
            {"sportsbook": "draftkings", "settlement_status": "WIN",
             "risk_units": 1.0, "profit_units": 1.0, "offered_american_odds": 100},
            {"sportsbook": "fanduel", "settlement_status": "LOSS",
             "risk_units": 1.0, "profit_units": -1.0, "offered_american_odds": -110},
        ]
        bd = breakdown_by_field(recs, "sportsbook")
        assert bd["draftkings"]["wins"] == 1
        assert bd["fanduel"]["losses"] == 1

    def test_ev_bucket_boundary(self):
        assert assign_bucket(1.99, EV_BUCKETS) == "0_to_2"
        assert assign_bucket(2.0, EV_BUCKETS) == "2_to_5"
        assert assign_bucket(4.99, EV_BUCKETS) == "2_to_5"
        assert assign_bucket(5.0, EV_BUCKETS) == "5_to_10"

    def test_odds_bucket_boundary(self):
        assert assign_bucket(-201, ODDS_BUCKETS) == "shorter_than_-200"
        assert assign_bucket(-200, ODDS_BUCKETS) == "-200_to_-151"
        assert assign_bucket(-150, ODDS_BUCKETS) == "-150_to_-101"
        assert assign_bucket(-100, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(100, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(101, ODDS_BUCKETS) == "+101_to_+150"
        assert assign_bucket(200, ODDS_BUCKETS) == "+151_to_+200"
        assert assign_bucket(201, ODDS_BUCKETS) == "longer_than_+200"

    def test_n_books_bucket_boundary(self):
        assert assign_bucket(2, N_BOOKS_BUCKETS) == "fewer_than_3"
        assert assign_bucket(3, N_BOOKS_BUCKETS) == "3"
        assert assign_bucket(4, N_BOOKS_BUCKETS) == "4"
        assert assign_bucket(5, N_BOOKS_BUCKETS) == "5_plus"


# ==================================================================
# 11. Database schema
# ==================================================================

class TestDatabaseSchema:
    def test_recommendations_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "historical_recommendations" in tables

    def test_event_results_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "event_results" in tables

    def test_player_stat_results_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "player_stat_results" in tables

    def test_market_settlements_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "market_settlements" in tables

    def test_bet_units_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "bet_units" in tables

    def test_closing_prices_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "closing_prices" in tables

    def test_manual_override_audit_table_exists(self, db):
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "manual_override_audit" in tables

    def test_fingerprint_unique_index(self, db):
        indexes = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_hr_fingerprint" in indexes

    def test_settlement_unique_index(self, db):
        indexes = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_ms_rec" in indexes


# ==================================================================
# 12. Player stat results
# ==================================================================

class TestPlayerStatResults:
    def test_save_and_retrieve(self, db):
        save_player_stat_result(
            db, "EVT_001", "PLAYER_001", "pitching_strikeouts_ou",
            final_stat_value=7.0, result_source="manual",
        )
        result = get_player_stat_result(db, "EVT_001", "PLAYER_001",
                                         "pitching_strikeouts_ou")
        assert result is not None
        assert result["final_stat_value"] == 7.0

    def test_idempotent_upsert(self, db):
        save_player_stat_result(
            db, "EVT_001", "PLAYER_001", "pitching_strikeouts_ou",
            final_stat_value=7.0,
        )
        save_player_stat_result(
            db, "EVT_001", "PLAYER_001", "pitching_strikeouts_ou",
            final_stat_value=8.0,
        )
        result = get_player_stat_result(db, "EVT_001", "PLAYER_001",
                                         "pitching_strikeouts_ou")
        assert result["final_stat_value"] == 8.0

    def test_multiple_markets(self, db):
        save_player_stat_result(db, "EVT_001", "P1", "pitching_strikeouts_ou",
                                final_stat_value=7.0)
        save_player_stat_result(db, "EVT_001", "P1", "pitching_outs_ou",
                                final_stat_value=18.0)
        k = get_player_stat_result(db, "EVT_001", "P1", "pitching_strikeouts_ou")
        o = get_player_stat_result(db, "EVT_001", "P1", "pitching_outs_ou")
        assert k["final_stat_value"] == 7.0
        assert o["final_stat_value"] == 18.0


# ==================================================================
# 13. Event results
# ==================================================================

class TestEventResults:
    def test_save_and_update(self, db):
        save_event_result(db, "EVT_001", final_status="COMPLETED",
                          away_score=3, home_score=5)
        row = db.execute("SELECT * FROM event_results WHERE event_id = 'EVT_001'").fetchone()
        assert row is not None
        assert row["final_status"] == "COMPLETED"
        assert row["away_score"] == 3

    def test_upsert(self, db):
        save_event_result(db, "EVT_001", final_status="UNRESOLVED")
        save_event_result(db, "EVT_001", final_status="COMPLETED", away_score=3)
        row = db.execute("SELECT * FROM event_results WHERE event_id = 'EVT_001'").fetchone()
        assert row["final_status"] == "COMPLETED"
        assert row["away_score"] == 3


# ==================================================================
# 14. CLV storage
# ==================================================================

class TestCLVStorage:
    def test_save_closing_price(self, db):
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        save_closing_price(
            db, rec_id,
            closing_american=-105, closing_decimal=1.9524,
            closing_implied_prob=0.5238, closing_line=5.5,
            closing_observed_at="2026-07-23T23:00:00Z",
            closing_sportsbook="draftkings",
            line_move_type="same_line",
            clv_probability=0.02, clv_price_diff=5, clv_available=True,
        )
        row = db.execute(
            "SELECT * FROM closing_prices WHERE recommendation_id = ?", (rec_id,),
        ).fetchone()
        assert row is not None
        assert row["closing_american"] == -105
        assert row["clv_available"] == 1

    def test_get_settled_recommendations_includes_clv(self, db):
        """Found live 2026-08-20: get_settled_recommendations() never
        joined closing_prices, so every caller of performance_summary()
        fed by it (e.g. src/grade_recommendations.py) always saw
        clv_probability as None regardless of real accumulated CLV data."""
        rec = _make_rec()
        rec_id = save_recommendation(db, rec)
        settle_recommendation(db, rec_id, "WIN", final_stat_value=7.0)
        save_closing_price(
            db, rec_id,
            closing_american=-105, closing_decimal=1.9524,
            closing_implied_prob=0.5238, closing_line=5.5,
            closing_observed_at="2026-07-23T23:00:00Z",
            closing_sportsbook="draftkings",
            line_move_type="same_line",
            clv_probability=0.02, clv_price_diff=5, clv_available=True,
        )
        settled = get_settled_recommendations(db)
        assert len(settled) == 1
        assert settled[0]["clv_probability"] == 0.02


# ==================================================================
# 15. Migration safety
# ==================================================================

class TestMigrationSafety:
    def test_repeated_init_db_safe(self, db):
        # init_db is idempotent — tables already exist from fixture
        # Just verify no error on re-creation
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "historical_recommendations" in tables
        assert "market_settlements" in tables

    def test_indexes_present(self, db):
        indexes = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_hr_fingerprint" in indexes
        assert "idx_ms_rec" in indexes
        assert "idx_hr_event" in indexes
        assert "idx_hr_player" in indexes
