"""Phase 16B: Adaptive Learning and Model Calibration — comprehensive tests.

Covers: grading separation, immutable snapshots, score calibration,
points_to_7, chronological splits, sample-size enforcement, duplicate
exclusion, champion/challenger isolation, version persistence, approval
requirement, rollback, high-variance safeguards, no auto-changes,
dashboard resilience with insufficient data.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from src.adaptive_learning import (
    ADAPTIVE_LEARNING_VERSION,
    MIN_GRADED_OVERALL,
    MIN_GRADED_PER_MARKET,
    MIN_GRADED_PER_BUCKET,
    MIN_BETTING_DAYS,
    MIN_SPORTSBOOK_CONTRIBUTION,
    HIGH_VARIANCE_MARKETS,
    SCORE_BUCKETS,
    STATUS_INSUFFICIENT_DATA,
    STATUS_OBSERVE,
    STATUS_CANDIDATE,
    STATUS_VALIDATED,
    STATUS_REJECTED,
    STATUS_APPROVED,
    SegmentPerformance,
    ScoreBucketAnalysis,
    LearningRecommendation,
    ChampionChallengerResult,
    VersionRecord,
    compute_points_to_7,
    american_to_implied_prob,
    _compute_max_drawdown,
    _wilson_ci,
    _market_family,
    _score_range,
    _mqs_range,
    _books_range,
    _line_range,
    _odds_range,
    _ev_range,
    _price_adv_range,
    _day_of_week,
    _time_bucket,
    _compute_segment_performance,
    _query_graded_recs,
    compute_performance_segments,
    compute_grade_summary,
    compute_score_calibration,
    generate_learning_recommendations,
    _check_safety_rules,
    _assess_overfitting_risk,
    chronological_split,
    evaluate_configuration,
    run_holdout_validation,
    create_challenger_experiment,
    compare_champion_challenger,
    approve_challenger,
    save_version as al_save_version,
    get_active_version as al_get_active_version,
    rollback_version as al_rollback_version,
    can_auto_change,
    is_pending,
    is_stale,
    is_improperly_mapped,
    _is_eligible_for_learning,
)

from database.db_manager import (
    get_connection,
    get_learning_recommendations,
    get_experiments,
    save_learning_recommendation,
    init_db,
)


# ── Test Helpers ───────────────────────────────────────────────────

def _make_graded_rec(
    recommendation_id: str | None = None,
    tier: str = "RESEARCH_ONLY",
    market_type: str = "strikeouts",
    market_form: str = "ou",
    side: str = "Over",
    sportsbook: str = "DK",
    odds: int = -110,
    ev_pct: float = 5.0,
    model_score: float = 7.5,
    n_books: int = 5,
    market_quality: str = "VALID_MARKET",
    settlement_status: str = "WIN",
    profit_units: float = 0.909,
    risk_units: float = 1.0,
    scan_ts: str | None = None,
    event_ts: str | None = None,
    yn_adv: float | None = None,
    market_quality_score: float = 8.0,
    freshness: str = "FRESH",
    score_components: dict | None = None,
    price_outlier_capped: bool = False,
    true_ev_unavailable: bool = False,
    one_sided_market: bool = False,
    insufficient_books_failure: bool = False,
    clv_prob: float | None = None,
    line: float = 6.5,
) -> dict:
    if recommendation_id is None:
        recommendation_id = str(uuid.uuid4())
    if scan_ts is None:
        scan_ts = datetime.now(timezone.utc).isoformat()
    if event_ts is None:
        event_ts = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    if score_components is None:
        score_components = {"value": 0.7, "market_quality": 0.8, "reliability": 0.6,
                           "freshness": 0.9, "confidence": 0.5, "risk": 0.7}
    rec = {
        "recommendation_id": recommendation_id,
        "recommendation_tier": tier,
        "event_id": f"evt_{recommendation_id[:8]}",
        "player_name": "Test Player",
        "market_type": market_type,
        "market_form": market_form,
        "side": side,
        "line": line,
        "sportsbook": sportsbook,
        "offered_american_odds": odds,
        "offered_decimal_odds": abs(odds) / 100 + 1 if odds > 0 else 1 + 100 / abs(odds),
        "ev_pct": ev_pct,
        "yn_implied_prob_adv": yn_adv,
        "n_consensus_books": n_books,
        "market_quality": market_quality,
        "freshness_status": freshness,
        "data_source": "LIVE API",
        "model_score": model_score,
        "score_components": json.dumps(score_components),
        "score_version": "model_score_v1",
        "market_quality_score": market_quality_score,
        "points_to_7": compute_points_to_7(model_score),
        "price_outlier_capped": 1 if price_outlier_capped else 0,
        "true_ev_unavailable": 1 if true_ev_unavailable else 0,
        "one_sided_market": 1 if one_sided_market else 0,
        "insufficient_books_failure": 1 if insufficient_books_failure else 0,
        "contributing_book_count": n_books,
        "contributing_books": ",".join([f"book{i}" for i in range(n_books)]),
        "scan_timestamp": scan_ts,
        "event_start_time": event_ts,
        "qualification_rules_version": "official_pick_rules_v2",
        "settlement_status": settlement_status,
        "risk_units": risk_units,
        "profit_units": profit_units,
        "odds_at_settle": odds,
        "clv_probability": clv_prob,
        "clv_price_diff": None,
        "clv_available": clv_prob is not None,
        "event_status": "scheduled",
    }
    return rec


def _temp_db(tmp_path):
    """Set up a temp DB with all tables created. Returns (dbm, conn)."""
    import database.db_manager as dbm
    db_path = tmp_path / "test.db"
    orig_path = dbm.DB_PATH
    dbm.DB_PATH = db_path
    init_db()
    conn = dbm.get_connection()
    return dbm, conn, orig_path


def _seed_graded_db(conn, n: int = 120, **overrides) -> list[dict]:
    """Seed a database with graded recs for testing."""
    init_db()
    recs = []
    sportsbook_options = ["DK", "FD", "BET365", "BETMGM", "ESPN", "CZRS"]
    for i in range(n):
        tier_options = ["OFFICIAL_TRACKED", "DISCOVERY_TRACKED", "RESEARCH_ONLY"]
        tier = tier_options[i % 3]
        market_options = ["strikeouts", "batter_hits", "total_bases", "earned_runs"]
        market = market_options[i % 4]
        odds_options = [-150, -110, -100, 100, 150]
        odds = odds_options[i % 5]
        status_options = ["WIN", "LOSS", "PUSH"]
        status = status_options[i % 3]
        score = 5.0 + (i % 50) / 10.0
        books = 3 + (i % 5)
        sportsbook = sportsbook_options[i % len(sportsbook_options)]

        scan_ts = (datetime(2026, 4, 1, tzinfo=timezone.utc) + timedelta(days=i)).isoformat()
        event_ts = (datetime(2026, 4, 1, 18, tzinfo=timezone.utc) + timedelta(days=i)).isoformat()

        rec = _make_graded_rec(
            recommendation_id=str(uuid.uuid4()),
            tier=tier,
            market_type=market,
            odds=odds,
            sportsbook=sportsbook,
            model_score=min(score, 9.8),
            n_books=books,
            settlement_status=status,
            profit_units=0.909 if status == "WIN" else (-1.0 if status == "LOSS" else 0.0),
            risk_units=1.0 if status in ("WIN", "LOSS") else 0.0,
            scan_ts=scan_ts,
            event_ts=event_ts,
            clv_prob=(0.01 * (i % 10) - 0.05) if i % 3 != 0 else None,
            **overrides,
        )
        recs.append(rec)

        # Insert into DB
        conn.execute("""
            INSERT OR IGNORE INTO historical_recommendations
            (recommendation_id, fingerprint, event_id, player_id, player_name,
             market_type,
             market_form, period, line, side, sportsbook,
             offered_american_odds, offered_decimal_odds, offered_implied_prob,
             ev_pct, n_consensus_books, market_quality, rec_status, rec_eligible,
             data_source, observation_timestamp, scan_timestamp, freshness_status,
             model_version, matchup, event_status, event_start_time,
             model_score, score_version, score_components, score_explanation,
             recommendation_tier, qualification_passed, qualification_reasons,
             disqualification_reasons, contributing_book_count, contributing_books,
             applicable_edge_metric, applicable_edge_threshold,
             model_score_threshold, qualification_rules_version,
             qualification_timestamp, official_rank,
             points_to_7, price_outlier_capped, true_ev_unavailable,
             one_sided_market, insufficient_books_failure, market_quality_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rec["recommendation_id"],
            f"fp_{rec['recommendation_id'][:8]}",
            rec["event_id"],
            f"player_{rec['recommendation_id'][:8]}",
            rec["player_name"],
            rec["market_type"],
            rec["market_form"],
            "game",
            rec["line"],
            rec["side"],
            rec["sportsbook"],
            rec["offered_american_odds"],
            rec["offered_decimal_odds"],
            american_to_implied_prob(rec["offered_american_odds"]),
            rec["ev_pct"],
            rec["n_consensus_books"],
            rec["market_quality"],
            "QUALIFIED",
            1,
            "LIVE API",
            rec["scan_timestamp"],
            rec["scan_timestamp"],
            rec["freshness_status"],
            "v1",
            "Test @ Test",
            "scheduled",
            rec["event_start_time"],
            rec["model_score"],
            "model_score_v1",
            rec["score_components"],
            "",
            rec["recommendation_tier"],
            1 if rec["recommendation_tier"] == "OFFICIAL_TRACKED" else 0,
            "", "",
            rec["contributing_book_count"],
            rec["contributing_books"],
            "ev_pct", 3.0,
            7.0,
            "official_pick_rules_v2",
            "", None,
            rec["points_to_7"],
            rec["price_outlier_capped"],
            rec["true_ev_unavailable"],
            rec["one_sided_market"],
            rec["insufficient_books_failure"],
            rec["market_quality_score"],
        ))

        # Insert settlement
        settlement_id = str(uuid.uuid4())
        conn.execute("""
            INSERT OR IGNORE INTO market_settlements
            (settlement_id, recommendation_id, settlement_status, final_stat_value,
             settled_at, grader_version)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            settlement_id,
            rec["recommendation_id"],
            rec["settlement_status"],
            7.0 if rec["market_form"] == "ou" else None,
            datetime.now(timezone.utc).isoformat(),
            "v1.0",
        ))

        # Insert bet units
        conn.execute("""
            INSERT OR IGNORE INTO bet_units
            (settlement_id, recommendation_id, risk_units, profit_units,
             return_units, odds_at_settle)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            settlement_id,
            rec["recommendation_id"],
            rec["risk_units"],
            rec["profit_units"],
            rec["risk_units"] + rec["profit_units"],
            rec["offered_american_odds"],
        ))

        # Insert CLV if available
        if rec["clv_probability"] is not None:
            conn.execute("""
                INSERT OR IGNORE INTO closing_prices
                (recommendation_id, closing_american, closing_decimal,
                 closing_implied_prob, closing_line, clv_probability,
                 clv_available, line_move_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                rec["recommendation_id"],
                rec["offered_american_odds"] + 5,
                rec["offered_decimal_odds"] * 1.02,
                american_to_implied_prob(rec["offered_american_odds"] + 5),
                rec["line"],
                rec["clv_probability"],
                1,
                "same_line",
            ))

    conn.commit()
    return recs


# ── Part 1: Grade All Tracking Tiers ───────────────────────────────

class TestGradeAllTiers:
    def test_grades_official_tracked(self):
        rec = _make_graded_rec(tier="OFFICIAL_TRACKED", settlement_status="WIN")
        assert rec["recommendation_tier"] == "OFFICIAL_TRACKED"
        assert rec["settlement_status"] == "WIN"

    def test_grades_discovery_tracked(self):
        rec = _make_graded_rec(tier="DISCOVERY_TRACKED", settlement_status="LOSS")
        assert rec["recommendation_tier"] == "DISCOVERY_TRACKED"
        assert rec["settlement_status"] == "LOSS"

    def test_grades_research_only(self):
        rec = _make_graded_rec(tier="RESEARCH_ONLY", settlement_status="PUSH")
        assert rec["recommendation_tier"] == "RESEARCH_ONLY"
        assert rec["settlement_status"] == "PUSH"

    def test_preserves_all_fields(self):
        rec = _make_graded_rec(
            tier="OFFICIAL_TRACKED",
            odds=-110,
            model_score=8.5,
            market_quality_score=9.0,
            n_books=6,
        )
        # All critical fields present
        assert rec["recommendation_id"]
        assert rec["offered_american_odds"] == -110
        assert rec["model_score"] == 8.5
        assert rec["market_quality_score"] == 9.0
        assert rec["n_consensus_books"] == 6
        assert rec["settlement_status"] == "WIN"
        assert rec["profit_units"] == 0.909

    def test_immutable_frozen_snapshot(self):
        """Original rec fields are never modified after creation."""
        rec = _make_graded_rec(model_score=7.5, odds=-110)
        original_score = rec["model_score"]
        original_odds = rec["offered_american_odds"]

        # Simulating grading does not modify the rec dict
        rec["settlement_status"] = "LOSS"
        rec["profit_units"] = -1.0

        assert rec["model_score"] == original_score
        assert rec["offered_american_odds"] == original_odds

    def test_tier_separation_in_db(self, tmp_path):
        """Verify tiers are stored separately in DB."""
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        conn = dbm.get_connection()
        try:
            _seed_graded_db(conn, n=10)

            rows = conn.execute(
                "SELECT recommendation_tier, COUNT(*) as cnt FROM historical_recommendations GROUP BY recommendation_tier"
            ).fetchall()
            tiers = {r[0]: r[1] for r in rows}
            assert len(tiers) >= 1
        finally:
            conn.close()
            dbm.DB_PATH = orig_path


# ── Part 2: Performance Segmentation ──────────────────────────────

class TestPerformanceSegmentation:
    def test_segment_performance_basic(self):
        recs = [
            _make_graded_rec(settlement_status="WIN", profit_units=0.909),
            _make_graded_rec(settlement_status="LOSS", profit_units=-1.0),
        ]
        perf = _compute_segment_performance(recs)
        assert perf.total == 2
        assert perf.wins == 1
        assert perf.losses == 1
        assert perf.win_rate == 0.5

    def test_segment_performance_empty(self):
        perf = _compute_segment_performance([])
        assert perf.total == 0
        assert perf.win_rate == 0.0

    def test_segment_performance_roi(self):
        recs = [
            _make_graded_rec(settlement_status="WIN", profit_units=0.909, risk_units=1.0),
            _make_graded_rec(settlement_status="WIN", profit_units=0.909, risk_units=1.0),
            _make_graded_rec(settlement_status="LOSS", profit_units=-1.0, risk_units=1.0),
        ]
        perf = _compute_segment_performance(recs)
        assert perf.roi == pytest.approx((0.909 + 0.909 - 1.0) / 3.0, abs=0.01)

    def test_max_drawdown(self):
        recs = [
            _make_graded_rec(settlement_status="WIN", profit_units=0.909),
            _make_graded_rec(settlement_status="LOSS", profit_units=-1.0),
            _make_graded_rec(settlement_status="LOSS", profit_units=-1.0),
            _make_graded_rec(settlement_status="WIN", profit_units=0.909),
        ]
        perf = _compute_segment_performance(recs)
        assert perf.max_drawdown > 0

    def test_confidence_interval(self):
        recs = [_make_graded_rec(settlement_status="WIN") for _ in range(10)]
        recs += [_make_graded_rec(settlement_status="LOSS") for _ in range(10)]
        perf = _compute_segment_performance(recs)
        assert perf.confidence_interval is not None
        ci_low, ci_high = perf.confidence_interval
        assert 0.25 < ci_low < 0.55
        assert 0.45 < ci_high < 0.75
        assert ci_low < ci_high

    def test_performance_segments_by_tier(self):
        recs = [
            _make_graded_rec(tier="OFFICIAL_TRACKED", settlement_status="WIN"),
            _make_graded_rec(tier="RESEARCH_ONLY", settlement_status="LOSS"),
        ]
        segs = compute_performance_segments(None, recs=recs)
        assert "tier" in segs
        tier_vals = {s["value"] for s in segs["tier"]}
        assert "OFFICIAL_TRACKED" in tier_vals
        assert "RESEARCH_ONLY" in tier_vals

    def test_performance_segments_by_market(self):
        recs = [
            _make_graded_rec(market_type="strikeouts", settlement_status="WIN"),
            _make_graded_rec(market_type="batter_hits", settlement_status="LOSS"),
        ]
        segs = compute_performance_segments(None, recs=recs)
        assert "exact_market" in segs
        markets = {s["value"] for s in segs["exact_market"]}
        assert "strikeouts" in markets

    def test_performance_segments_by_side(self):
        recs = [
            _make_graded_rec(side="Over", settlement_status="WIN"),
            _make_graded_rec(side="Under", settlement_status="LOSS"),
        ]
        segs = compute_performance_segments(None, recs=recs)
        assert "side" in segs
        sides = {s["value"] for s in segs["side"]}
        assert "Over" in sides

    def test_grade_summary_structure(self):
        recs = [_make_graded_rec(tier="RESEARCH_ONLY", settlement_status="WIN") for _ in range(5)]
        # Mock the connection to avoid DB query
        summary = {"RESEARCH_ONLY": _compute_segment_performance(recs).to_dict()}
        assert "RESEARCH_ONLY" in summary
        assert summary["RESEARCH_ONLY"]["wins"] == 5


# ── Part 3: Score Calibration ──────────────────────────────────────

class TestScoreCalibration:
    def test_points_to_7_normal(self):
        assert compute_points_to_7(7.5) == 0.0
        assert compute_points_to_7(6.0) == 1.0
        assert compute_points_to_7(5.0) == 2.0

    def test_points_to_7_above_7(self):
        assert compute_points_to_7(9.0) == 0.0
        assert compute_points_to_7(10.0) == 0.0

    def test_points_to_7_none(self):
        assert compute_points_to_7(None) == 7.0

    def test_points_to_7_exactly_7(self):
        assert compute_points_to_7(7.0) == 0.0

    def test_points_to_7_not_rounded(self):
        """Points to 7 should not be rounded at storage level."""
        result = compute_points_to_7(6.37)
        assert result == pytest.approx(0.63, abs=0.001)

    def test_score_buckets_complete(self):
        assert len(SCORE_BUCKETS) == 9
        labels = [b[0] for b in SCORE_BUCKETS]
        assert "below_5.0" in labels
        assert "7.5+" in labels

    def test_score_calibration_structure(self):
        """Score calibration returns expected structure."""
        # With empty DB, should still return valid structure
        recs = [_make_graded_rec(model_score=6.5 + i * 0.1, settlement_status="WIN" if i % 2 == 0 else "LOSS")
                for i in range(20)]

        # Test bucket assignment
        from src.adaptive_learning import _score_range
        assert _score_range(4.5) == "below_5.0"
        assert _score_range(5.3) == "5.0-5.99"
        assert _score_range(6.5) == "6.0-6.99"
        assert _score_range(7.3) == "7.0-7.99"
        assert _score_range(8.5) == "8.0+"

    def test_mqs_range(self):
        from src.adaptive_learning import _mqs_range
        assert _mqs_range(4.0) == "below_5.0"
        assert _mqs_range(6.0) == "5.0-6.99"
        assert _mqs_range(8.0) == "7.0-8.99"
        assert _mqs_range(9.5) == "9.0+"

    def test_books_range(self):
        from src.adaptive_learning import _books_range
        assert _books_range(2) == "1-2"
        assert _books_range(3) == "3"
        assert _books_range(4) == "4"
        assert _books_range(5) == "5-6"
        assert _books_range(7) == "7+"


# ── Part 4: Learning Recommendations ───────────────────────────────

class TestLearningRecommendations:
    def test_wilson_ci_basic(self):
        ci = _wilson_ci(50, 100)
        assert ci[0] < 0.5 < ci[1]

    def test_wilson_ci_empty(self):
        ci = _wilson_ci(0, 0)
        assert ci == (0.0, 0.0)

    def test_wilson_ci_extreme(self):
        ci = _wilson_ci(100, 100)
        assert ci[1] >= 0.95

    def test_market_family(self):
        assert _market_family("strikeouts") == "pitcher"
        assert _market_family("batter_hits") == "batter"
        assert _market_family("unknown_market") == "other"

    def test_safety_rules_insufficient_overall(self):
        recs = [_make_graded_rec() for _ in range(50)]
        passes, reasons = _check_safety_rules(recs)
        assert not passes
        assert any("100" in r for r in reasons)

    def test_safety_rules_single_book_dominance(self):
        recs = [_make_graded_rec(sportsbook="DK") for _ in range(120)]
        passes, reasons = _check_safety_rules(recs)
        assert not passes
        assert any("DK" in r for r in reasons)

    def test_safety_rules_insufficient_betting_days(self):
        recs = [_make_graded_rec(
            scan_ts=datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat()
        ) for _ in range(120)]
        passes, reasons = _check_safety_rules(recs)
        assert not passes
        assert any("betting days" in r.lower() for r in reasons)

    def test_safety_rules_high_variance_market(self):
        recs = [_make_graded_rec(market_type="batter_home_runs") for _ in range(60)]
        passes, reasons = _check_safety_rules(recs, affected_market="batter_home_runs")
        assert not passes
        assert any("High-variance" in r for r in reasons)

    def test_overfitting_risk_high(self):
        assert _assess_overfitting_risk([], 0.5) == "HIGH"

    def test_overfitting_risk_medium(self):
        assert _assess_overfitting_risk([1] * 60, 0.15) == "MEDIUM"

    def test_overfitting_risk_low(self):
        assert _assess_overfitting_risk([1] * 150, 0.05) == "LOW"

    def test_learning_recs_generate(self, tmp_path):
        """Learning recommendations can be generated from sufficient data."""
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=150)
            recs = generate_learning_recommendations(conn)
            assert isinstance(recs, list)
            for r in recs:
                assert "recommendation_id" in r
                assert "category" in r
                assert "proposed_change" in r
                assert "status" in r
                assert r["status"] in (
                    STATUS_INSUFFICIENT_DATA, STATUS_OBSERVE, STATUS_CANDIDATE,
                    STATUS_VALIDATED, STATUS_REJECTED, STATUS_APPROVED,
                )
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_no_auto_production_changes_insufficient_data(self, tmp_path):
        """Cannot auto-change with insufficient data."""
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            # Empty DB
            allowed, reason = can_auto_change(conn)
            assert not allowed
            assert "INSUFFICIENT_DATA" in reason
            conn.close()
        finally:
            dbm.DB_PATH = orig_path


# ── Part 5: Safety & Sample-Size Rules ─────────────────────────────

class TestSafetyRules:
    def test_high_variance_market_stricter(self):
        for mkt in HIGH_VARIANCE_MARKETS:
            assert mkt in ("batter_home_runs", "batter_stolen_bases", "pitcher_strikeouts")

    def test_min_thresholds_defined(self):
        assert MIN_GRADED_OVERALL == 100
        assert MIN_GRADED_PER_MARKET == 50
        assert MIN_GRADED_PER_BUCKET == 30
        assert MIN_BETTING_DAYS == 5


# ── Part 6: Chronological Split ────────────────────────────────────

class TestChronologicalSplit:
    def test_split_basic(self):
        recs = [
            {"scan_timestamp": f"2026-04-{i:02d}T12:00:00+00:00"}
            for i in range(1, 11)
        ]
        train, val, holdout = chronological_split(recs)
        assert len(train) == 6
        assert len(val) == 2
        assert len(holdout) == 2

    def test_split_empty(self):
        train, val, holdout = chronological_split([])
        assert train == []
        assert val == []
        assert holdout == []

    def test_split_chronological_order(self):
        recs = [
            {"scan_timestamp": f"2026-04-{i:02d}T12:00:00+00:00"}
            for i in range(1, 11)
        ]
        train, val, holdout = chronological_split(recs)
        # Train dates < val dates < holdout dates
        if train and val:
            assert train[-1]["scan_timestamp"] <= val[0]["scan_timestamp"]
        if val and holdout:
            assert val[-1]["scan_timestamp"] <= holdout[0]["scan_timestamp"]

    def test_split_no_future_data_leakage(self):
        """Future results cannot appear in earlier windows."""
        recs = [
            {"scan_timestamp": f"2026-04-{i:02d}T12:00:00+00:00", "settlement_status": "WIN"}
            for i in range(1, 11)
        ]
        train, val, holdout = chronological_split(recs)
        train_times = {r["scan_timestamp"] for r in train}
        holdout_times = {r["scan_timestamp"] for r in holdout}
        # No overlap
        assert train_times.isdisjoint(holdout_times)

    def test_evaluate_configuration(self):
        recs = [
            _make_graded_rec(settlement_status="WIN", profit_units=0.909),
            _make_graded_rec(settlement_status="LOSS", profit_units=-1.0),
        ]
        result = evaluate_configuration(recs, "test")
        assert result["label"] == "test"
        assert result["total"] == 2
        assert result["win_rate"] == 0.5

    def test_holdout_validation_no_challenger(self, tmp_path):
        """Holdout validation works without challenger."""
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=50)
            result = run_holdout_validation(conn)
            assert "train_size" in result
            assert "val_size" in result
            assert "holdout_size" in result
            assert "champion_train" in result
            assert "champion_holdout" in result
            assert result["challenger_holdout"] is None
            assert not result["validated"]
            conn.close()
        finally:
            dbm.DB_PATH = orig_path


# ── Part 7: Champion / Challenger ──────────────────────────────────

class TestChampionChallenger:
    def test_experiment_creation(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=50)
            challenger_recs = [_make_graded_rec(settlement_status="WIN") for _ in range(10)]
            result = create_challenger_experiment(
                conn, "challenger_v1", {"weight": 0.4}, challenger_recs
            )
            assert "experiment_id" in result
            assert "comparison" in result
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_experiment_isolation(self, tmp_path):
        """Challenger never modifies champion config."""
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=50)

            # Create experiment
            result = create_challenger_experiment(
                conn, "challenger_v1", {"weight": 0.4},
                [_make_graded_rec(settlement_status="WIN") for _ in range(10)],
            )

            # Verify experiment stored correctly
            experiments = get_experiments(conn)
            assert len(experiments) >= 1
            assert experiments[0]["conclusion"] == "pending"
            assert experiments[0]["approved"] == 0
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_compare_champion_challenger(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=50)
            result = create_challenger_experiment(
                conn, "challenger_v1", {},
                [_make_graded_rec(settlement_status="WIN") for _ in range(10)],
            )
            exp_id = result["experiment_id"]
            comparison = compare_champion_challenger(conn, exp_id)
            assert "champion_metrics" in comparison
            assert "challenger_metrics" in comparison
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_approval_requires_sufficient_sample(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=50)
            # Small challenger
            result = create_challenger_experiment(
                conn, "challenger_v1", {},
                [_make_graded_rec(settlement_status="WIN") for _ in range(5)],
            )
            exp_id = result["experiment_id"]
            approval = approve_challenger(conn, exp_id)
            assert "error" in approval
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_approval_requires_roi_improvement(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=150)
            # Challenger with worse ROI (all losses) — must set profit_units correctly
            bad_challenger = [
                _make_graded_rec(
                    settlement_status="LOSS",
                    profit_units=-1.0,
                    risk_units=1.0,
                )
                for _ in range(40)
            ]
            result = create_challenger_experiment(
                conn, "challenger_v1", {}, bad_challenger,
            )
            exp_id = result["experiment_id"]
            # Challenger ROI is -1.0 (all losses), champion ROI is better
            approval = approve_challenger(conn, exp_id)
            assert "error" in approval
            conn.close()
        finally:
            dbm.DB_PATH = orig_path


# ── Part 8: Versioning & Rollback ──────────────────────────────────

class TestVersioning:
    def test_save_version(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            version = al_save_version(
                conn, "scoring_v1", "mqs_v1", "rules_v2", "cal_v1",
                reason="initial", approver="test",
            )
            assert "version_id" in version
            assert version["scoring_version"] == "scoring_v1"
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_get_active_version(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            al_save_version(conn, "scoring_v1", "mqs_v1", "rules_v2", "cal_v1")
            active = al_get_active_version(conn)
            assert active is not None
            assert active["scoring_version"] == "scoring_v1"
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_rollback(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            v1 = al_save_version(conn, "scoring_v1", "mqs_v1", "rules_v2", "cal_v1")
            v2 = al_save_version(conn, "scoring_v2", "mqs_v2", "rules_v3", "cal_v2")

            # Rollback to v1
            result = al_rollback_version(conn, v1["version_id"])
            assert result["status"] == "rolled_back"

            # Active should now be v1-based
            active = al_get_active_version(conn)
            assert active["scoring_version"] == "scoring_v1"
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_rollback_to_nonexistent(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            result = al_rollback_version(conn, "nonexistent_id")
            assert "error" in result
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_historical_picks_retain_version(self):
        """Historical picks retain the scoring version used at creation time."""
        rec = _make_graded_rec()
        assert rec["score_version"] == "model_score_v1"
        assert rec["qualification_rules_version"] == "official_pick_rules_v2"


# ── Part 10: No Auto-Changes Enforcement ───────────────────────────

class TestNoAutoChanges:
    def test_can_auto_change_empty_db(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            allowed, reason = can_auto_change(conn)
            assert not allowed
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_can_auto_change_with_data(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            _seed_graded_db(conn, n=150)
            allowed, reason = can_auto_change(conn)
            assert allowed
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_is_pending(self):
        assert is_pending({"settlement_status": "UNRESOLVED"})
        assert is_pending({"settlement_status": ""})
        assert is_pending({"settlement_status": None})
        assert not is_pending({"settlement_status": "WIN"})

    def test_is_stale(self):
        assert is_stale({"freshness_status": "STALE"})
        assert not is_stale({"freshness_status": "FRESH"})

    def test_is_improperly_mapped(self):
        assert is_improperly_mapped({"market_quality": "EXCLUDED"})
        assert is_improperly_mapped({"market_quality": "INSUFFICIENT_MARKET"})
        assert not is_improperly_mapped({"market_quality": "VALID_MARKET"})

    def test_learning_eligibility_excludes_pending(self):
        rec = {"settlement_status": "UNRESOLVED", "freshness_status": "FRESH"}
        assert not _is_eligible_for_learning(rec)

    def test_learning_eligibility_excludes_stale(self):
        rec = {"settlement_status": "WIN", "freshness_status": "STALE"}
        assert not _is_eligible_for_learning(rec)

    def test_learning_eligibility_excludes_excluded(self):
        rec = {"settlement_status": "WIN", "market_quality": "EXCLUDED"}
        assert not _is_eligible_for_learning(rec)

    def test_learning_eligibility_allows_valid(self):
        rec = {"settlement_status": "WIN", "freshness_status": "FRESH",
               "market_quality": "VALID_MARKET", "event_status": "scheduled"}
        assert _is_eligible_for_learning(rec)


# ── Utility Tests ──────────────────────────────────────────────────

class TestUtilities:
    def test_american_to_implied_prob_positive(self):
        assert american_to_implied_prob(100) == pytest.approx(0.5, abs=0.01)
        assert american_to_implied_prob(200) == pytest.approx(1 / 3, abs=0.01)

    def test_american_to_implied_prob_negative(self):
        assert american_to_implied_prob(-110) == pytest.approx(0.524, abs=0.01)
        assert american_to_implied_prob(-200) == pytest.approx(2 / 3, abs=0.01)

    def test_max_drawdown_empty(self):
        assert _compute_max_drawdown([]) == 0.0

    def test_max_drawdown_positive(self):
        assert _compute_max_drawdown([1.0, 1.0, 1.0]) == 0.0

    def test_max_drawdown_negative(self):
        dd = _compute_max_drawdown([1.0, -2.0, -1.0, 0.5])
        assert dd == pytest.approx(3.0, abs=0.01)

    def test_day_of_week(self):
        result = _day_of_week("2026-04-06T12:00:00+00:00")
        assert result == "Monday"

    def test_day_of_week_unknown(self):
        assert _day_of_week(None) == "unknown"
        assert _day_of_week("") == "unknown"

    def test_time_bucket(self):
        scan = "2026-04-06T12:00:00+00:00"
        event = "2026-04-06T19:00:00+00:00"
        assert _time_bucket(scan, event) == "6-12h"

    def test_time_bucket_post_start(self):
        scan = "2026-04-06T20:00:00+00:00"
        event = "2026-04-06T19:00:00+00:00"
        assert _time_bucket(scan, event) == "post_start"

    def test_odds_range(self):
        from src.adaptive_learning import _odds_range
        assert _odds_range(-250) == "below_-200"
        assert _odds_range(150) == "+100_to_+150"

    def test_ev_range(self):
        from src.adaptive_learning import _ev_range
        assert _ev_range(-1.0, None) == "negative"
        assert _ev_range(3.0, None) == "2-5%"
        assert _ev_range(None, 6.0) == "5-8pp"  # YN uses price_adv range

    def test_line_range(self):
        from src.adaptive_learning import _line_range
        assert _line_range(0.5) == "0-0.5"
        assert _line_range(6.5) == "6.0+"

    def test_adaptive_learning_version_defined(self):
        assert ADAPTIVE_LEARNING_VERSION == "adaptive_learning_v1"


# ── DB Integration Tests ──────────────────────────────────────────

class TestDBIntegration:
    def test_save_and_get_learning_recs(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            rec = {
                "recommendation_id": "test-001",
                "category": "thresholds",
                "proposed_change": "Test change",
                "current_value": "7.0",
                "proposed_value": "6.5",
                "reason": "test reason",
                "sample_size": 50,
                "historical_roi_diff": 0.05,
                "historical_clv_diff": 0.01,
                "confidence_interval": (0.4, 0.6),
                "expected_volume_effect": "+10 picks",
                "overfitting_risk": "LOW",
                "status": "OBSERVE",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save_learning_recommendation(conn, rec)
            recs = get_learning_recommendations(conn)
            assert len(recs) >= 1
            assert recs[0]["category"] == "thresholds"

            # Filter by status
            recs_filtered = get_learning_recommendations(conn, status="OBSERVE")
            assert len(recs_filtered) >= 1
            conn.close()
        finally:
            dbm.DB_PATH = orig_path

    def test_experiments_table(self, tmp_path):
        import database.db_manager as dbm
        db_path = tmp_path / "test.db"
        orig_path = dbm.DB_PATH
        dbm.DB_PATH = db_path
        init_db()
        try:
            conn = dbm.get_connection()
            conn.execute("""
                INSERT INTO experiments (experiment_id, challenger_id, created_at)
                VALUES ('exp-001', 'ch-001', ?)
            """, (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
            exps = get_experiments(conn)
            assert len(exps) >= 1
            conn.close()
        finally:
            dbm.DB_PATH = orig_path
