"""Phase 16 Comprehensive Tests.

Tests for: Official Pick Qualification V1, Immutable Freeze, Tracker,
Observations, Automation, DB schema, Pipeline integration.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from src.official_picks import (
    OfficialPickConfig,
    QualificationResult,
    classify_recommendation,
    rank_and_select_official_picks,
    TIER_OFFICIAL,
    TIER_DISCOVERY,
    TIER_RESEARCH,
    RULES_VERSION,
)
from src.tracker import (
    compute_variable_stake,
    compute_pick_units,
    get_official_picks,
    update_pick_outcome,
    compute_performance,
    breakdown_by_field,
    grade_pending_picks,
    PerformanceMetrics,
)
from src.observations import (
    record_observation,
    get_observations,
    has_observation,
    compute_movement,
)
from src.automation import (
    create_job,
    update_job_status,
    get_pending_jobs,
    get_failed_jobs,
    get_recent_jobs,
    retry_failed_jobs,
    schedule_pregame_checks,
    schedule_grading,
    trigger_morning_run,
    trigger_grading,
    get_automation_status,
)
from database.db_manager import freeze_official_pick, get_official_picks_today


# ── Helpers ─────────────────────────────────────────────────────────


def _base_rec(**overrides) -> dict:
    """Create a base qualifying recommendation."""
    rec = {
        "recommendation_id": str(uuid.uuid4()),
        "event_id": "ev_001",
        "event_start_time": "2026-07-26T19:00:00Z",
        "player_id": "player_1",
        "player_name": "Test Player",
        "market_type": "strikeouts",
        "market_form": "ou",
        "period": "game",
        "line": 6.5,
        "side": "over",
        "sportsbook": "DraftKings",
        "offered_american_odds": -115,
        "offered_decimal_odds": 1.87,
        "offered_implied_prob": 0.535,
        "fair_prob": 0.56,
        "fair_american_odds": -127,
        "ev_pct": 3.5,
        "yn_reference_prob": None,
        "yn_reference_odds": None,
        "yn_implied_prob_adv": None,
        "yn_decimal_odds_adv": None,
        "n_consensus_books": 5,
        "market_quality": "PRICED",
        "rec_status": "QUALIFIED",
        "rec_eligible": 1,
        "pinnacle_approved": True,
        "is_official": True,
        "data_source": "test",
        "observation_timestamp": datetime.now(timezone.utc).isoformat(),
        "scan_timestamp": datetime.now(timezone.utc).isoformat(),
        "freshness_status": "FRESH",
        "model_version": "v1",
        "matchup": "NYY @ BOS",
        "event_status": "scheduled",
        "model_score": 8.5,
        "score_version": "model_score_v1",
        "score_components": "{}",
        "score_cap": 9.8,
        "score_explanation": "Test",
        "recommendation_tier": "OFFICIAL_TRACKED",
        "qualification_passed": 1,
        "qualification_reasons": "",
        "disqualification_reasons": "",
        "contributing_book_count": 5,
        "contributing_books": "DK,FD,BM,ES,BV",
        "applicable_edge_metric": "ev_pct",
        "applicable_edge_threshold": 3.0,
        "model_score_threshold": 7.0,
        "qualification_rules_version": "",
    }
    rec.update(overrides)
    return rec


# ═══════════════════════════════════════════════════════════════════
# Part 1: Official Pick Qualification
# ═══════════════════════════════════════════════════════════════════


class TestQualification:
    """Tests for classify_recommendation."""

    def test_qualifies_ou_rec(self):
        rec = _base_rec()
        result = classify_recommendation(rec)
        assert result.tier == TIER_OFFICIAL
        assert result.passed is True
        assert "Qualified" in result.reasons[0]

    def test_disqualifies_low_model_score(self):
        """Score 6.0 with 5 books → DISCOVERY (not official, but above discovery threshold)."""
        rec = _base_rec(model_score=6.0)
        result = classify_recommendation(rec)
        assert result.tier == TIER_DISCOVERY
        assert result.passed is False

    def test_disqualifies_low_ev(self):
        """EV 2.0% with 5 books → DISCOVERY (below official 3.0% but above discovery)."""
        rec = _base_rec(ev_pct=2.0)
        result = classify_recommendation(rec)
        assert result.tier == TIER_DISCOVERY
        assert result.passed is False

    def test_disqualifies_few_books(self):
        """1 book → RESEARCH (below official 2 and discovery 2)."""
        rec = _base_rec(n_consensus_books=1)
        result = classify_recommendation(rec)
        assert result.tier == TIER_RESEARCH
        assert result.passed is False

    def test_qualifies_yn_rec(self):
        rec = _base_rec(
            market_form="yn",
            yn_implied_prob_adv=4.0,
            yn_reference_odds=-110,
            ev_pct=None,
        )
        result = classify_recommendation(rec)
        assert result.tier == TIER_OFFICIAL
        assert result.passed is True

    def test_disqualifies_low_yn_price_adv(self):
        """YN 2.0pp with 5 books → DISCOVERY (below official 3.0pp but above discovery)."""
        rec = _base_rec(
            market_form="yn",
            yn_implied_prob_adv=2.0,
            yn_reference_odds=-110,
            ev_pct=None,
        )
        result = classify_recommendation(rec)
        assert result.tier == TIER_DISCOVERY
        assert result.passed is False

    def test_disqualifies_live_game(self):
        rec = _base_rec(event_status="live")
        result = classify_recommendation(rec)
        assert result.tier == TIER_RESEARCH
        assert any("started" in d.lower() or "live" in d.lower() for d in result.disqualification_reasons)

    def test_disqualifies_completed_game(self):
        rec = _base_rec(event_status="final")
        result = classify_recommendation(rec)
        assert result.tier == TIER_RESEARCH

    def test_rules_version_set(self):
        rec = _base_rec()
        result = classify_recommendation(rec)
        assert result.rules_version == RULES_VERSION

    def test_contributing_books_populated(self):
        rec = _base_rec(contributing_books="DK,FD,BM", contributing_book_count=3, n_consensus_books=3)
        result = classify_recommendation(rec)
        assert result.contributing_book_count == 3

    def test_qualification_timestamp_set(self):
        rec = _base_rec()
        result = classify_recommendation(rec)
        d = result.to_dict()
        assert d.get("qualification_timestamp") is not None


# ═══════════════════════════════════════════════════════════════════
# Part 1: Ranking and Daily Selection
# ═══════════════════════════════════════════════════════════════════


class TestRanking:
    """Tests for rank_and_select_official_picks."""

    def test_returns_empty_for_no_quals(self):
        result = rank_and_select_official_picks([])
        assert result == []

    def test_selects_top_by_model_score(self):
        recs = [
            _base_rec(model_score=7.5, ev_pct=3.5, recommendation_id="r1"),
            _base_rec(model_score=8.5, ev_pct=4.0, recommendation_id="r2"),
            _base_rec(model_score=7.0, ev_pct=3.0, recommendation_id="r3"),
        ]
        selected = rank_and_select_official_picks(recs)
        assert len(selected) > 0
        assert selected[0]["model_score"] >= selected[-1]["model_score"]

    def test_respects_daily_max(self):
        recs = [
            _base_rec(model_score=9.0, event_id=f"ev_{i}", recommendation_id=f"r{i}")
            for i in range(10)
        ]
        selected = rank_and_select_official_picks(recs)
        assert len(selected) <= 3

    def test_per_game_limit(self):
        recs = [
            _base_rec(
                model_score=8.0 + i * 0.1,
                event_id="ev_001",
                player_id=f"player_{i}",
                recommendation_id=f"r{i}",
            )
            for i in range(5)
        ]
        selected = rank_and_select_official_picks(recs)
        ev_001_count = sum(1 for s in selected if s["event_id"] == "ev_001")
        assert ev_001_count <= 1

    def test_dedup_same_player_market_side_line(self):
        rec1 = _base_rec(
            model_score=8.5, player_id="p1", market_type="strikeouts",
            side="over", line=6.5, recommendation_id="r1",
        )
        rec2 = _base_rec(
            model_score=8.5, player_id="p1", market_type="strikeouts",
            side="over", line=6.5, recommendation_id="r2",
        )
        selected = rank_and_select_official_picks([rec1, rec2])
        assert len(selected) == 1

    def test_official_rank_assigned(self):
        recs = [_base_rec(model_score=9.0, recommendation_id="r1")]
        selected = rank_and_select_official_picks(recs)
        assert selected[0]["official_rank"] == 1


# ═══════════════════════════════════════════════════════════════════
# Part 2: Immutable Freeze
# ═══════════════════════════════════════════════════════════════════


class TestImmutableFreeze:
    """Tests for official_picks table (frozen snapshots)."""

    def test_freeze_creates_record(self, db_conn):
        db_conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "rec_001", "ev_001", "p1", "Test Player",
            "strikeouts", "ou", "over", "DraftKings", -115,
            1.87, 0.535, 3.5,
            5, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            8.5, "OFFICIAL_TRACKED", 1,
        ))
        result = freeze_official_pick(db_conn, "rec_001", tier="OFFICIAL_TRACKED", official_rank=1)
        rows = db_conn.execute("SELECT * FROM official_picks WHERE recommendation_id = 'rec_001'").fetchall()
        assert len(rows) == 1
        assert rows[0]["tier"] == "OFFICIAL_TRACKED"
        assert rows[0]["official_rank"] == 1
        assert rows[0]["outcome"] == "pending"

    def test_freeze_is_idempotent(self, db_conn):
        db_conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "rec_002", "ev_002", "p2", "Player 2",
            "strikeouts", "ou", "over", "FanDuel", -120,
            1.83, 0.549, 4.0,
            4, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            7.5, "OFFICIAL_TRACKED", 1,
        ))
        freeze_official_pick(db_conn, "rec_002", tier="OFFICIAL_TRACKED")
        freeze_official_pick(db_conn, "rec_002", tier="OFFICIAL_TRACKED")
        rows = db_conn.execute("SELECT * FROM official_picks WHERE recommendation_id = 'rec_002'").fetchall()
        assert len(rows) == 1

    def test_frozen_pick_never_overwritten(self, db_conn):
        db_conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "rec_003", "ev_003", "p3", "Player 3",
            "strikeouts", "ou", "over", "Bovada", -105,
            1.95, 0.513, 5.0,
            6, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            9.0, "OFFICIAL_TRACKED", 1,
        ))
        freeze_official_pick(db_conn, "rec_003", tier="OFFICIAL_TRACKED", official_rank=1)
        # Try to insert again with different rank — should not overwrite
        freeze_official_pick(db_conn, "rec_003", tier="OFFICIAL_TRACKED", official_rank=99)
        row = db_conn.execute("SELECT official_rank FROM official_picks WHERE recommendation_id = 'rec_003'").fetchone()
        assert row["official_rank"] == 1


# ═══════════════════════════════════════════════════════════════════
# Part 3: Tracker (Flat Staking)
# ═══════════════════════════════════════════════════════════════════


class TestTracker:
    """Tests for flat 1.0 unit staking."""

    def test_positive_odds_profit(self):
        profit = compute_pick_units(150, "win")
        assert profit == 1.5

    def test_negative_odds_profit(self):
        profit = compute_pick_units(-110, "win")
        assert round(profit, 4) == round(100 / 110, 4)

    def test_loss_profit(self):
        profit = compute_pick_units(-110, "loss")
        assert profit == -1.0

    def test_push_profit(self):
        profit = compute_pick_units(-110, "push")
        assert profit == 0.0

    def test_void_profit(self):
        profit = compute_pick_units(-110, "void")
        assert profit == 0.0

    def test_cancelled_profit(self):
        profit = compute_pick_units(-110, "cancelled")
        assert profit == 0.0

    def test_even_odds_profit(self):
        profit = compute_pick_units(100, "win")
        assert profit == 1.0

    def test_heavy_fav_profit(self):
        profit = compute_pick_units(-300, "win")
        assert round(profit, 4) == round(100 / 300, 4)


# ═══════════════════════════════════════════════════════════════════
# Part 3a: Tracker (Variable Staking)
# ═══════════════════════════════════════════════════════════════════


class TestVariableStaking:
    """Tests for compute_variable_stake (25% fractional Kelly)."""

    def test_default_on_none(self):
        assert compute_variable_stake(None, None, None) == 0.5

    def test_default_on_zero_ev(self):
        assert compute_variable_stake(0.0, 2.0, 7.0) == 0.5

    def test_default_on_negative_ev(self):
        assert compute_variable_stake(-2.0, 2.0, 7.0) == 0.5

    def test_default_on_no_odds(self):
        assert compute_variable_stake(5.0, 1.0, 7.0) == 0.5

    def test_basic_kelly_calculation(self):
        # EV 5%, decimal 2.0 (+100), score 7.0
        # kelly = 0.05/1.0 = 5%, 25% = 1.25%, raw_units = 1.25
        # score mult = 1.0
        stake = compute_variable_stake(5.0, 2.0, 7.0)
        assert stake == 1.25

    def test_score_multiplier(self):
        # EV 5%, decimal 2.0 (+100), score 9.0
        # kelly = 0.05/1.0 = 5%, 25% = 1.25%, raw_units = 1.25
        # score mult = 1.0 + min(2.0, 2.0) * 0.25 = 1.5
        # final = 1.25 * 1.5 = 1.875 -> round(1.875, 2) = 1.88
        stake = compute_variable_stake(5.0, 2.0, 9.0)
        assert stake == 1.88

    def test_caps_at_max_2(self):
        # EV 20%, decimal 1.5 (-200)
        # kelly = 0.20/0.5 = 40%, 25% = 10%, raw_units = 10.0
        # score mult at 9.0 = 1.5, final = 15.0 -> clamp to 2.0
        stake = compute_variable_stake(20.0, 1.5, 9.0)
        assert stake == 2.0

    def test_caps_at_min_025(self):
        # EV 1%, decimal 10.0 (+900)
        # kelly = 0.01/9.0 = 0.111%, 25% = 0.0278%, raw_units = 0.0278
        # score mult at 7.0 = 1.0, final = 0.0278 -> clamp to 0.25
        stake = compute_variable_stake(1.0, 10.0, 7.0)
        assert stake == 0.25

    def test_variable_profit_calculation(self):
        # Risk 1.5 units on +150 odds, win
        profit = compute_pick_units(150, "win", risk_units=1.5)
        assert profit == 2.25

    def test_variable_loss(self):
        profit = compute_pick_units(-110, "loss", risk_units=1.5)
        assert profit == -1.5

    def test_variable_push(self):
        profit = compute_pick_units(-110, "push", risk_units=1.5)
        assert profit == 0.0

    def test_variable_with_default_risk(self):
        # Default risk_units should still be 1.0
        profit = compute_pick_units(-110, "win")
        assert round(profit, 4) == round(100/110, 4)


# ═══════════════════════════════════════════════════════════════════
# Part 3: Tracker (Performance)
# ═══════════════════════════════════════════════════════════════════


class TestPerformance:
    """Tests for compute_performance and breakdown_by_field."""

    def _seed_official_pick(self, conn, rec_id, outcome="pending", profit=None):
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id, "ev_001", "p1", "Test Player",
            "strikeouts", "ou", "over", "DraftKings", -115,
            1.87, 0.535, 3.5,
            5, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            8.5, "OFFICIAL_TRACKED", 1,
        ))
        profit_val = profit if profit is not None else (
            0.87 if outcome == "win" else -1.0 if outcome == "loss" else 0.0
        )
        conn.execute("""
            INSERT INTO official_picks (
                recommendation_id, tier, outcome, profit_units
            ) VALUES (?, 'OFFICIAL_TRACKED', ?, ?)
        """, (rec_id, outcome, profit_val))

    def test_performance_empty(self, db_conn):
        metrics = compute_performance(db_conn)
        assert metrics.total == 0

    def test_performance_with_picks(self, db_conn):
        self._seed_official_pick(db_conn, "r1", outcome="win", profit=0.87)
        self._seed_official_pick(db_conn, "r2", outcome="loss", profit=-1.0)
        self._seed_official_pick(db_conn, "r3", outcome="win", profit=0.87)
        self._seed_official_pick(db_conn, "r4", outcome="push", profit=0.0)
        metrics = compute_performance(db_conn)
        assert metrics.total == 4
        assert metrics.wins == 2
        assert metrics.losses == 1
        assert metrics.pushes == 1
        assert round(metrics.units_won, 2) == round(0.87 - 1.0 + 0.87 + 0.0, 2)

    def test_breakdown_by_market_type(self, db_conn):
        self._seed_official_pick(db_conn, "r1", outcome="win")
        self._seed_official_pick(db_conn, "r2", outcome="loss")
        bd = breakdown_by_field(db_conn, "market_type")
        assert len(bd) > 0

    def test_breakdown_invalid_field(self, db_conn):
        bd = breakdown_by_field(db_conn, "invalid_field")
        assert bd == []


# ═══════════════════════════════════════════════════════════════════
# Part 3: Tracker (Grading)
# ═══════════════════════════════════════════════════════════════════


class TestGrading:
    """Tests for grade_pending_picks."""

    def _seed_pick_with_settlement(self, conn, rec_id, settlement_status):
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id, "ev_001", "p1", "Test Player",
            "strikeouts", "ou", "over", "DraftKings", -115,
            1.87, 0.535, 3.5,
            5, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            8.5, "OFFICIAL_TRACKED", 1,
        ))
        conn.execute("""
            INSERT INTO official_picks (
                recommendation_id, tier, outcome
            ) VALUES (?, 'OFFICIAL_TRACKED', 'pending')
        """, (rec_id,))
        conn.execute("""
            INSERT INTO market_settlements (
                recommendation_id, settlement_status, final_stat_value
            ) VALUES (?, ?, ?)
        """, (rec_id, settlement_status, 7.0))

    def test_grades_pending_win(self, db_conn):
        self._seed_pick_with_settlement(db_conn, "gr1", "win")
        count = grade_pending_picks(db_conn)
        assert count == 1
        row = db_conn.execute("SELECT outcome, profit_units FROM official_picks WHERE recommendation_id = 'gr1'").fetchone()
        assert row["outcome"] == "win"
        assert row["profit_units"] is not None
        assert row["profit_units"] > 0

    def test_grades_pending_loss(self, db_conn):
        self._seed_pick_with_settlement(db_conn, "gr2", "loss")
        count = grade_pending_picks(db_conn)
        assert count == 1
        row = db_conn.execute("SELECT outcome FROM official_picks WHERE recommendation_id = 'gr2'").fetchone()
        assert row["outcome"] == "loss"

    def test_skips_already_graded(self, db_conn):
        self._seed_pick_with_settlement(db_conn, "gr3", "win")
        db_conn.execute(
            "UPDATE official_picks SET outcome = 'win' WHERE recommendation_id = 'gr3'"
        )
        count = grade_pending_picks(db_conn)
        assert count == 0


# ═══════════════════════════════════════════════════════════════════
# Part 4: Observations
# ═══════════════════════════════════════════════════════════════════


class TestObservations:
    """Tests for odds observation recording and movement."""

    def _seed_pick(self, conn, rec_id):
        conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec_id, "ev_001", "p1", "Test Player",
            "strikeouts", "ou", "over", "DraftKings", -115,
            1.87, 0.535, 3.5,
            5, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            8.5, "OFFICIAL_TRACKED", 1,
        ))
        conn.execute("""
            INSERT INTO official_picks (recommendation_id, tier, outcome)
            VALUES (?, 'OFFICIAL_TRACKED', 'pending')
        """, (rec_id,))

    def test_record_observation(self, db_conn):
        self._seed_pick(db_conn, "obs1")
        obs_id = record_observation(
            db_conn, "obs1", "MORNING", "DraftKings",
            -115, 1.87, 0.535, line=6.5,
        )
        assert obs_id is not None
        rows = get_observations(db_conn, "obs1")
        assert len(rows) == 1
        assert rows[0]["observation_type"] == "MORNING"

    def test_has_observation_dedup(self, db_conn):
        self._seed_pick(db_conn, "obs2")
        record_observation(db_conn, "obs2", "MORNING", "DraftKings", -115, 1.87, 0.535)
        assert has_observation(db_conn, "obs2", "MORNING") is True
        assert has_observation(db_conn, "obs2", "PREGAME") is False

    def test_compute_movement(self, db_conn):
        self._seed_pick(db_conn, "obs3")
        record_observation(db_conn, "obs3", "MORNING", "DraftKings", -115, 1.87, 0.535, line=6.5)
        record_observation(db_conn, "obs3", "PREGAME", "DraftKings", -120, 1.83, 0.549, line=6.5)
        movement = compute_movement(db_conn, "obs3")
        assert movement["morning_odds"] == -115
        assert movement["pregame_odds"] == -120
        assert movement["odds_movement_morning_to_pregame"] == -5

    def test_get_observations_ordered(self, db_conn):
        self._seed_pick(db_conn, "obs4")
        record_observation(db_conn, "obs4", "CLOSING", "DraftKings", -110, 1.91, 0.524, line=6.5)
        record_observation(db_conn, "obs4", "MORNING", "DraftKings", -115, 1.87, 0.535, line=6.5)
        record_observation(db_conn, "obs4", "PREGAME", "DraftKings", -118, 1.85, 0.541, line=6.5)
        rows = get_observations(db_conn, "obs4")
        assert len(rows) == 3
        types = sorted(r["observation_type"] for r in rows)
        assert types == ["CLOSING", "MORNING", "PREGAME"]


# ═══════════════════════════════════════════════════════════════════
# Part 6: Automation
# ═══════════════════════════════════════════════════════════════════


class TestAutomation:
    """Tests for automation job management."""

    def test_create_job(self, db_conn):
        job_id = create_job(db_conn, "morning")
        assert job_id is not None
        pending = get_pending_jobs(db_conn)
        assert any(j["job_id"] == job_id for j in pending)

    def test_update_job_status(self, db_conn):
        job_id = create_job(db_conn, "morning")
        update_job_status(db_conn, job_id, "running")
        row = db_conn.execute("SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row["status"] == "running"

    def test_get_failed_jobs(self, db_conn):
        job_id = create_job(db_conn, "morning")
        update_job_status(db_conn, job_id, "failed", error_message="test error")
        failed = get_failed_jobs(db_conn)
        assert len(failed) >= 1
        assert failed[0]["error_message"] == "test error"

    def test_retry_failed_jobs(self, db_conn):
        j1 = create_job(db_conn, "morning")
        j2 = create_job(db_conn, "pregame")
        update_job_status(db_conn, j1, "failed")
        update_job_status(db_conn, j2, "failed")
        count = retry_failed_jobs(db_conn)
        assert count == 2
        pending = get_pending_jobs(db_conn)
        assert len(pending) >= 2

    def test_schedule_pregame_checks(self, db_conn):
        # SQLite's date('now') is the REAL clock (not mockable via
        # patch("src.automation.datetime")). Anchor the game to today's
        # real UTC date so the WHERE date(start_time) = date('now') clause
        # always matches, and keep the Python-side target-time checks
        # deterministic via the mocked datetime.
        real_today = datetime.now(timezone.utc).date()
        start_time = datetime(
            real_today.year, real_today.month, real_today.day, 15, 0,
            tzinfo=timezone.utc,
        )
        fixed_now = start_time - timedelta(hours=3)  # 12:00 UTC same day
        with patch("src.automation.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            db_conn.execute("""
                INSERT INTO games (event_id, away_team, home_team, start_time, status)
                VALUES ('ev_pg1', 'NYY', 'BOS', ?, 'scheduled')
            """, (start_time.isoformat(),))
            db_conn.commit()
            count = schedule_pregame_checks(db_conn)
            assert count == 1

    def test_schedule_grading(self, db_conn):
        db_conn.execute("""
            INSERT INTO games (event_id, away_team, home_team, start_time, status)
            VALUES ('ev_gr1', 'NYY', 'BOS', '2026-07-26T18:00:00Z', 'final')
        """)
        count = schedule_grading(db_conn)
        assert count >= 1

    def test_trigger_morning_run(self, db_conn):
        job_id = trigger_morning_run(db_conn)
        assert job_id is not None

    def test_automation_status(self, db_conn):
        status = get_automation_status(db_conn)
        assert "scheduler_enabled" in status
        assert status["scheduler_enabled"] is True
        assert "pending_jobs" in status
        assert "failed_jobs" in status

    def test_recent_jobs(self, db_conn):
        create_job(db_conn, "morning")
        create_job(db_conn, "pregame")
        recent = get_recent_jobs(db_conn, limit=5)
        assert len(recent) == 2


# ═══════════════════════════════════════════════════════════════════
# Part 7: Database Schema
# ═══════════════════════════════════════════════════════════════════


class TestDBSchema:
    """Tests for new database tables."""

    def test_official_picks_table_exists(self, db_conn):
        tables = [r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "official_picks" in tables

    def test_pick_observations_table_exists(self, db_conn):
        tables = [r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "pick_observations" in tables

    def test_scheduled_jobs_table_exists(self, db_conn):
        tables = [r[0] for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "scheduled_jobs" in tables

    def test_historical_recs_has_new_columns(self, db_conn):
        cols = [r[1] for r in db_conn.execute("PRAGMA table_info(historical_recommendations)").fetchall()]
        assert "qualification_timestamp" in cols
        assert "official_rank" in cols


# ═══════════════════════════════════════════════════════════════════
# Part 8: Pipeline Integration (mock-level)
# ═══════════════════════════════════════════════════════════════════


class TestPipelineIntegration:
    """Tests for pipeline integration points."""

    def test_freeze_official_pick_and_retrieve(self, db_conn):
        db_conn.execute("""
            INSERT INTO historical_recommendations (
                recommendation_id, event_id, player_id, player_name,
                market_type, market_form, side, sportsbook, offered_american_odds,
                offered_decimal_odds, offered_implied_prob, ev_pct,
                n_consensus_books, rec_status, scan_timestamp, freshness_status,
                model_score, recommendation_tier, qualification_passed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "pip_001", "ev_p1", "p1", "Pipeline Player",
            "strikeouts", "ou", "over", "DraftKings", -115,
            1.87, 0.535, 3.5,
            5, "QUALIFIED", datetime.now(timezone.utc).isoformat(), "FRESH",
            8.5, "OFFICIAL_TRACKED", 1,
        ))
        freeze_official_pick(db_conn, "pip_001", tier="OFFICIAL_TRACKED", official_rank=1)
        today = get_official_picks_today(db_conn)
        assert len(today) == 1
        assert today[0]["player_name"] == "Pipeline Player"

    def test_automation_status_after_jobs(self, db_conn):
        trigger_morning_run(db_conn)
        schedule_grading(db_conn)
        status = get_automation_status(db_conn)
        assert status["pending_jobs"] >= 1


# ═══════════════════════════════════════════════════════════════════
# Part 9: Export Validation Data
# ═══════════════════════════════════════════════════════════════════


class TestExportValidation:
    """Tests for export validation data (Part 9)."""

    def test_seed_validation_data(self, db_conn):
        """Seed 20 recs with scores 5.5-6.3, RESEARCH/PRICE_OUTLIER."""
        import random
        random.seed(42)
        sportsbooks = ["BetMGM", "DraftKings", "Bovada", "ESPN BET", "FanDuel"]
        for i in range(20):
            rec_id = f"val_{i:03d}"
            score = round(5.5 + random.random() * 0.8, 2)
            db_conn.execute("""
                INSERT INTO historical_recommendations (
                    recommendation_id, event_id, player_id, player_name,
                    market_type, market_form, side, sportsbook, offered_american_odds,
                    offered_decimal_odds, offered_implied_prob, ev_pct,
                    n_consensus_books, rec_status, scan_timestamp, freshness_status,
                    model_score, recommendation_tier, qualification_passed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec_id, f"ev_{i}", f"p_{i}", f"Player {i}",
                "strikeouts", "ou", "over", sportsbooks[i % 5], -115,
                1.87, 0.535, 1.0 + random.random() * 2,
                3, "RESEARCH", datetime.now(timezone.utc).isoformat(), "FRESH",
                score, "RESEARCH_ONLY", 0,
            ))

        rows = db_conn.execute(
            "SELECT COUNT(*) FROM historical_recommendations WHERE recommendation_id LIKE 'val_%'"
        ).fetchone()[0]
        assert rows == 20

        scores = db_conn.execute(
            "SELECT model_score FROM historical_recommendations WHERE recommendation_id LIKE 'val_%'"
        ).fetchall()
        for row in scores:
            assert 5.5 <= row["model_score"] <= 6.3

        tiers = db_conn.execute(
            "SELECT DISTINCT recommendation_tier FROM historical_recommendations WHERE recommendation_id LIKE 'val_%'"
        ).fetchall()
        assert all(t["recommendation_tier"] == "RESEARCH_ONLY" for t in tiers)


# ═══════════════════════════════════════════════════════════════════
# Part 10: Config
# ═══════════════════════════════════════════════════════════════════


class TestConfig:
    """Tests for OfficialPickConfig defaults."""

    def test_min_model_score(self):
        cfg = OfficialPickConfig()
        assert cfg.official_min_model_score == 7.0

    def test_daily_max(self):
        cfg = OfficialPickConfig()
        assert cfg.official_daily_max_picks == 3

    def test_max_per_game(self):
        cfg = OfficialPickConfig()
        assert cfg.official_max_per_game == 1

    def test_allowed_statuses(self):
        cfg = OfficialPickConfig()
        assert "QUALIFIED" in cfg.official_allowed_statuses

    def test_rules_version(self):
        cfg = OfficialPickConfig()
        assert cfg.official_rules_version == RULES_VERSION
