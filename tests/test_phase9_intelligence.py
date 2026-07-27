"""Phase 9: Intelligence Layer tests.

Deterministic tests for CLV capture, analytics queries, confidence scoring,
calibration analysis, bookmaker quality scores, and report generation.

All tests use isolated in-memory databases.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """In-memory database with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    # Full schema from db_manager
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY, league TEXT NOT NULL DEFAULT 'MLB',
            away_team TEXT, home_team TEXT, start_time TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            sport_id TEXT, league_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, player_id TEXT NOT NULL,
            player_name TEXT, team_id TEXT DEFAULT '', team_name TEXT DEFAULT '',
            market_type TEXT NOT NULL, market_group_key TEXT NOT NULL,
            side TEXT NOT NULL, line REAL, price INTEGER, decimal_odds REAL,
            is_alt_line INTEGER NOT NULL DEFAULT 0, available INTEGER NOT NULL DEFAULT 1,
            validation_status TEXT NOT NULL DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT '', mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT '', captured_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            scan_run_id TEXT, ingestion_run_id TEXT,
            event_id TEXT NOT NULL, event_start_time TEXT,
            player_id TEXT NOT NULL, player_name TEXT,
            market_type TEXT NOT NULL, market_form TEXT NOT NULL,
            period TEXT NOT NULL, line REAL, side TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            offered_american_odds INTEGER NOT NULL, offered_decimal_odds REAL NOT NULL,
            offered_implied_prob REAL NOT NULL, fair_prob REAL, fair_american_odds INTEGER,
            ev_pct REAL, yn_reference_prob REAL, yn_reference_odds INTEGER,
            yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
            n_consensus_books INTEGER, market_quality TEXT,
            rec_status TEXT NOT NULL, rec_eligible INTEGER NOT NULL DEFAULT 0,
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
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS market_settlements (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            settlement_status TEXT NOT NULL DEFAULT 'UNRESOLVED',
            final_stat_value REAL, settled_at TEXT, settlement_reason TEXT,
            grader_version TEXT DEFAULT 'v1',
            manual_override INTEGER NOT NULL DEFAULT 0,
            override_reason TEXT, override_previous TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bet_units (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            risk_units REAL NOT NULL DEFAULT 1.0, profit_units REAL NOT NULL DEFAULT 0.0,
            return_units REAL NOT NULL DEFAULT 0.0, odds_at_settle INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS closing_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL,
            closing_american INTEGER, closing_decimal REAL,
            closing_implied_prob REAL, closing_line REAL,
            closing_observed_at TEXT, closing_sportsbook TEXT,
            line_move_type TEXT, clv_probability REAL,
            clv_price_diff INTEGER, clv_available INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_cp_rec ON closing_prices(recommendation_id);
    """)

    yield conn
    conn.close()


def _insert_recommendation(conn, rec_id, **overrides):
    """Insert a recommendation with sensible defaults."""
    defaults = {
        "recommendation_id": rec_id,
        "fingerprint": f"fp_{rec_id}",
        "event_id": "EVT001",
        "event_start_time": "2026-07-23T19:10:00+00:00",
        "player_id": "PLAYER_1",
        "player_name": "Test Player",
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
        "n_consensus_books": 5,
        "market_quality": "VALID_MARKET",
        "rec_status": "STRONG_EDGE",
        "rec_eligible": 1,
        "data_source": "CACHE",
        "observation_timestamp": "2026-07-23T18:00:00+00:00",
        "scan_timestamp": "2026-07-23T18:00:00+00:00",
        "freshness_status": "FRESH",
        "model_version": "v1",
    }
    defaults.update(overrides)

    cols = list(defaults.keys())
    placeholders = ", ".join(["?"] * len(cols))
    sql = f"INSERT INTO historical_recommendations ({', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [defaults[c] for c in cols])
    conn.commit()


def _insert_settlement(conn, rec_id, status, risk=1.0, profit=0.0, odds=-110):
    """Insert settlement and units for a recommendation."""
    conn.execute(
        "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status) VALUES (?, ?, ?)",
        (f"settle_{rec_id}", rec_id, status),
    )
    conn.execute(
        "INSERT INTO bet_units (settlement_id, recommendation_id, risk_units, profit_units, odds_at_settle) VALUES (?, ?, ?, ?, ?)",
        (f"settle_{rec_id}", rec_id, risk, profit, odds),
    )
    conn.commit()


def _insert_closing(conn, rec_id, closing_american=-115, clv_prob=0.01, available=True):
    """Insert a closing price record."""
    conn.execute(
        """INSERT INTO closing_prices
           (recommendation_id, closing_american, closing_decimal,
            closing_implied_prob, line_move_type, clv_probability,
            clv_price_diff, clv_available)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (rec_id, closing_american, 1.0 + 100 / abs(closing_american),
         1.0 / (1.0 + 100 / abs(closing_american)),
         "same_line", clv_prob, closing_american - (-110), 1 if available else 0),
    )
    conn.commit()


# ══════════════════════════════════════════════════════════════════
# CLV Tests
# ══════════════════════════════════════════════════════════════════

class TestCLVCapture:
    def test_closing_price_stored(self, db_conn):
        from database.db_manager import save_closing_price
        save_closing_price(
            db_conn, "REC001",
            closing_american=-115, closing_decimal=1.8696,
            closing_implied_prob=0.5349, closing_line=5.5,
            closing_observed_at="2026-07-23T19:00:00",
            closing_sportsbook="draftkings",
            line_move_type="same_line",
            clv_probability=0.012, clv_price_diff=-5, clv_available=True,
        )
        row = db_conn.execute(
            "SELECT * FROM closing_prices WHERE recommendation_id = 'REC001'"
        ).fetchone()
        assert row is not None
        assert row["closing_american"] == -115
        assert row["clv_probability"] == 0.012
        assert row["clv_available"] == 1

    def test_clv_favorable_positive(self):
        from src.grading import calculate_clv
        # Closing at -105 (better than bet at -110) → favorable CLV
        result = calculate_clv(-110, 5.5, -105, 5.5)
        assert result["clv_probability"] > 0
        assert result["line_move_type"] == "same_line"
        assert result["clv_available"] is True

    def test_clv_line_changed_no_available(self):
        from src.grading import calculate_clv
        result = calculate_clv(-110, 5.5, -110, 6.5)
        assert result["clv_available"] is False
        assert result["line_move_type"] == "line_changed"

    def test_clv_no_close(self):
        from src.grading import calculate_clv
        result = calculate_clv(-110, 5.5, None, None)
        assert result["clv_available"] is False
        assert result["line_move_type"] == "no_close"

    def test_capture_closing_prices_from_odds(self, db_conn):
        from database.db_manager import capture_closing_prices
        # Insert odds data
        db_conn.execute(
            """INSERT INTO player_prop_odds
               (event_id, odd_id, sportsbook, player_id, player_name,
                market_type, market_group_key, side, line, price,
                decimal_odds, is_alt_line, available, validation_status,
                captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("EVT001", "pitching_strikeouts-P1-game-ou-over", "draftkings",
             "PLAYER_1", "Test Player", "pitching_strikeouts_ou",
             "EVT001|PLAYER_1|pitching_strikeouts_ou|game",
             "over", 5.5, -115, 1.8696, 0, 1, "VALID", "2026-07-23T19:00:00"),
        )
        db_conn.commit()

        _insert_recommendation(db_conn, "REC001")
        # Pass full rec dict (capture needs event_id, player_id, etc.)
        rec = dict(db_conn.execute(
            "SELECT * FROM historical_recommendations WHERE recommendation_id = 'REC001'"
        ).fetchone())
        captured = capture_closing_prices(db_conn, [rec])
        assert captured == 1
        row = db_conn.execute(
            "SELECT * FROM closing_prices WHERE recommendation_id = 'REC001'"
        ).fetchone()
        assert row is not None
        assert row["closing_american"] == -115

    def test_capture_skips_existing(self, db_conn):
        from database.db_manager import capture_closing_prices
        _insert_recommendation(db_conn, "REC001")
        _insert_closing(db_conn, "REC001")
        captured = capture_closing_prices(db_conn, [{"recommendation_id": "REC001"}])
        assert captured == 0


# ══════════════════════════════════════════════════════════════════
# Analytics Tests
# ══════════════════════════════════════════════════════════════════

class TestAnalytics:
    def _populate_data(self, conn):
        """Insert test data: 3 recommendations with different outcomes."""
        _insert_recommendation(conn, "R1", market_type="pitching_strikeouts_ou",
                               sportsbook="draftkings", offered_american_odds=-110, ev_pct=6.0)
        _insert_settlement(conn, "R1", "WIN", profit=0.9091, odds=-110)

        _insert_recommendation(conn, "R2", market_type="pitching_strikeouts_ou",
                               sportsbook="fanduel", offered_american_odds=-105, ev_pct=3.0)
        _insert_settlement(conn, "R2", "LOSS", profit=-1.0, odds=-105)

        _insert_recommendation(conn, "R3", market_type="batting_hits_ou",
                               sportsbook="draftkings", offered_american_odds=-120, ev_pct=8.0)
        _insert_settlement(conn, "R3", "WIN", profit=0.8333, odds=-120)

    def test_roi_by_market(self, db_conn):
        from src.analytics import roi_by_market
        self._populate_data(db_conn)
        result = roi_by_market(db_conn)
        assert len(result) == 2
        market_types = {r["market_type"] for r in result}
        assert "pitching_strikeouts_ou" in market_types
        assert "batting_hits_ou" in market_types

    def test_roi_by_sportsbook(self, db_conn):
        from src.analytics import roi_by_sportsbook
        self._populate_data(db_conn)
        result = roi_by_sportsbook(db_conn)
        assert len(result) == 2
        # Both should have ROI calculated
        for r in result:
            assert "roi" in r
            assert "win_rate" in r

    def test_roi_by_ev_bucket(self, db_conn):
        from src.analytics import roi_by_ev_bucket
        from src.grading import EV_BUCKETS
        self._populate_data(db_conn)
        result = roi_by_ev_bucket(db_conn, EV_BUCKETS)
        assert len(result) == len(EV_BUCKETS)
        # ev_pct stored as decimal: 0.06=6%, 0.03=3%, 0.08=8%
        # Buckets in percentage points: 0_to_2=0-2%, 2_to_5=2-5%, 5_to_10=5-10%
        # 6% → 5_to_10, 3% → 2_to_5, 8% → 5_to_10
        bucket_5_10 = next(r for r in result if r["bucket"] == "5_to_10")
        assert bucket_5_10["count"] == 2
        bucket_2_5 = next(r for r in result if r["bucket"] == "2_to_5")
        assert bucket_2_5["count"] == 1

    def test_roi_by_day(self, db_conn):
        from src.analytics import roi_by_day
        self._populate_data(db_conn)
        result = roi_by_day(db_conn)
        assert len(result) >= 1
        assert "date" in result[0]

    def test_clv_by_sportsbook(self, db_conn):
        from src.analytics import clv_by_sportsbook
        _insert_recommendation(db_conn, "R1", sportsbook="draftkings")
        _insert_closing(db_conn, "R1", clv_prob=0.02)
        result = clv_by_sportsbook(db_conn)
        assert len(result) == 1
        assert result[0]["sportsbook"] == "draftkings"
        assert result[0]["avg_clv_prob"] == 0.02

    def test_clv_by_market(self, db_conn):
        from src.analytics import clv_by_market
        _insert_recommendation(db_conn, "R1", market_type="pitching_strikeouts_ou")
        _insert_closing(db_conn, "R1", clv_prob=0.015)
        result = clv_by_market(db_conn)
        assert len(result) == 1
        assert result[0]["market_type"] == "pitching_strikeouts_ou"

    def test_overall_summary(self, db_conn):
        from src.analytics import overall_summary
        self._populate_data(db_conn)
        result = overall_summary(db_conn)
        assert result["total"] == 3
        assert result["wins"] == 2
        assert result["losses"] == 1
        assert result["roi"] != 0.0

    def test_roi_by_rec_status(self, db_conn):
        from src.analytics import roi_by_rec_status
        self._populate_data(db_conn)
        result = roi_by_rec_status(db_conn)
        assert len(result) >= 1

    def test_hit_rate_by_market(self, db_conn):
        from src.analytics import hit_rate_by_market
        self._populate_data(db_conn)
        result = hit_rate_by_market(db_conn)
        assert len(result) >= 1
        for r in result:
            assert "win_rate" in r


# ══════════════════════════════════════════════════════════════════
# Confidence Scoring Tests
# ══════════════════════════════════════════════════════════════════

class TestConfidenceScoring:
    def test_high_quality_rec_scores_high(self):
        from src.confidence import compute_confidence
        rec = {
            "n_consensus_books": 6,
            "market_quality": "VALID_MARKET",
            "ev_pct": 0.08,
            "freshness_status": "FRESH",
            "data_source": "LIVE API",
            "mapping_confidence": "HIGH",
        }
        result = compute_confidence(rec)
        assert result["confidence_score"] > 60
        assert result["grade"] in ("A", "B")

    def test_low_quality_rec_scores_low(self):
        from src.confidence import compute_confidence
        rec = {
            "n_consensus_books": 2,
            "market_quality": "INSUFFICIENT_MARKET",
            "ev_pct": 0.01,
            "freshness_status": "STALE",
            "data_source": "CACHE",
            "mapping_confidence": "LOW",
        }
        result = compute_confidence(rec)
        assert result["confidence_score"] < 50
        assert result["grade"] in ("D", "F")

    def test_yn_market_uses_advantage(self):
        from src.confidence import compute_confidence
        rec = {
            "n_consensus_books": 4,
            "market_quality": "VALID_MARKET",
            "ev_pct": None,
            "yn_implied_prob_adv": 0.10,
            "freshness_status": "FRESH",
            "data_source": "CACHE",
            "mapping_confidence": "MEDIUM",
        }
        result = compute_confidence(rec)
        assert result["confidence_score"] > 0
        assert result["components"]["ev_magnitude"] > 0

    def test_grade_boundaries(self):
        from src.confidence import compute_confidence
        # Test grade A
        rec_a = {"n_consensus_books": 8, "market_quality": "VALID_MARKET",
                 "ev_pct": 15.0, "freshness_status": "FRESH", "data_source": "LIVE API",
                 "mapping_confidence": "HIGH"}
        assert compute_confidence(rec_a)["grade"] == "A"

        # Test grade F
        rec_f = {"n_consensus_books": 0, "market_quality": "EXCLUDED",
                 "ev_pct": 0.0, "freshness_status": "STALE", "data_source": "CACHE",
                 "mapping_confidence": "NONE"}
        assert compute_confidence(rec_f)["grade"] == "F"

    def test_components_are_normalized(self):
        from src.confidence import compute_confidence
        rec = {"n_consensus_books": 5, "market_quality": "VALID_MARKET",
               "ev_pct": 0.05, "freshness_status": "FRESH", "data_source": "CACHE"}
        result = compute_confidence(rec)
        for k, v in result["components"].items():
            assert 0.0 <= v <= 1.0, f"Component {k} out of range: {v}"

    def test_custom_weights(self):
        from src.confidence import compute_confidence, ConfidenceWeights
        rec = {"n_consensus_books": 5, "market_quality": "VALID_MARKET",
               "ev_pct": 0.05, "freshness_status": "FRESH", "data_source": "CACHE"}
        default = compute_confidence(rec)
        custom = compute_confidence(rec, ConfidenceWeights(n_books=10.0))
        # Different weights should produce different scores
        assert default["confidence_score"] != custom["confidence_score"]


# ══════════════════════════════════════════════════════════════════
# Calibration Tests
# ══════════════════════════════════════════════════════════════════

class TestCalibration:
    def test_calibration_returns_buckets(self, db_conn):
        from src.calibration import analyze_calibration
        # Insert recs across EV buckets
        for i, (ev, status) in enumerate([
            (0.01, "WIN"), (0.01, "WIN"), (0.01, "LOSS"),  # 0_to_2
            (0.06, "WIN"), (0.06, "WIN"), (0.06, "LOSS"),  # 5_to_10
        ]):
            _insert_recommendation(db_conn, f"RC{i}", ev_pct=ev)
            profit = 0.9 if status == "WIN" else -1.0
            _insert_settlement(db_conn, f"RC{i}", status, profit=profit)

        result = analyze_calibration(db_conn)
        assert "buckets" in result
        assert "recommendations" in result
        assert len(result["buckets"]) > 0

    def test_calibration_empty_data(self, db_conn):
        from src.calibration import analyze_calibration
        result = analyze_calibration(db_conn)
        assert result["buckets"] == [] or all(b["count"] == 0 for b in result["buckets"])
        assert result["recommendations"] == []


# ══════════════════════════════════════════════════════════════════
# Bookmaker Scores Tests
# ══════════════════════════════════════════════════════════════════

class TestBookmakerScores:
    def test_quality_scores(self, db_conn):
        from src.bookmaker_scores import bookmaker_quality_scores
        _insert_recommendation(db_conn, "R1", sportsbook="draftkings")
        _insert_settlement(db_conn, "R1", "WIN", profit=0.9091)
        _insert_closing(db_conn, "R1", clv_prob=0.02)

        _insert_recommendation(db_conn, "R2", sportsbook="fanduel")
        _insert_settlement(db_conn, "R2", "LOSS", profit=-1.0)
        _insert_closing(db_conn, "R2", clv_prob=-0.01)

        result = bookmaker_quality_scores(db_conn)
        assert len(result) == 2
        # draftkings should score higher (positive CLV + win)
        dk = next(r for r in result if r["sportsbook"] == "draftkings")
        fd = next(r for r in result if r["sportsbook"] == "fanduel")
        assert dk["quality_score"] > fd["quality_score"]

    def test_empty_bookmakers(self, db_conn):
        from src.bookmaker_scores import bookmaker_quality_scores
        result = bookmaker_quality_scores(db_conn)
        assert result == []


# ══════════════════════════════════════════════════════════════════
# Report Generation Tests
# ══════════════════════════════════════════════════════════════════

class TestReports:
    def test_performance_report(self, db_conn):
        from src.reports import generate_performance_report
        _insert_recommendation(db_conn, "R1")
        _insert_settlement(db_conn, "R1", "WIN", profit=0.9091)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_performance_report(db_conn, Path(tmpdir))
            assert path.exists()
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert "total" in rows[0]

    def test_sportsbook_report(self, db_conn):
        from src.reports import generate_sportsbook_report
        _insert_recommendation(db_conn, "R1", sportsbook="draftkings")
        _insert_settlement(db_conn, "R1", "WIN", profit=0.9091)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_sportsbook_report(db_conn, Path(tmpdir))
            assert path.exists()

    def test_market_report(self, db_conn):
        from src.reports import generate_market_report
        _insert_recommendation(db_conn, "R1", market_type="pitching_strikeouts_ou")
        _insert_settlement(db_conn, "R1", "WIN", profit=0.9091)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_market_report(db_conn, Path(tmpdir))
            assert path.exists()

    def test_recommendation_report(self, db_conn):
        from src.reports import generate_recommendation_report
        _insert_recommendation(db_conn, "R1")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_recommendation_report(db_conn, Path(tmpdir))
            assert path.exists()
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert "confidence_score" in rows[0]
                assert "confidence_grade" in rows[0]

    def test_confidence_report(self, db_conn):
        from src.reports import generate_confidence_report
        _insert_recommendation(db_conn, "R1")
        _insert_recommendation(db_conn, "R2")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_confidence_report(db_conn, Path(tmpdir))
            assert path.exists()
            with open(path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) >= 6  # A-F + TOTAL + AVG + components

    def test_generate_all_reports(self, db_conn):
        from src.reports import generate_all_reports
        _insert_recommendation(db_conn, "R1")
        _insert_settlement(db_conn, "R1", "WIN", profit=0.9091)

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_reports(db_conn, tmpdir)
            assert len(paths) == 5
            for p in paths:
                assert p.exists()

    def test_empty_data_reports(self, db_conn):
        from src.reports import generate_all_reports
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_reports(db_conn, tmpdir)
            assert len(paths) == 5
            for p in paths:
                assert p.exists()


# ══════════════════════════════════════════════════════════════════
# Bucket Calculation Tests
# ══════════════════════════════════════════════════════════════════

class TestBuckets:
    def test_ev_bucket_assignment(self):
        from src.grading import assign_bucket, EV_BUCKETS
        assert assign_bucket(-0.01, EV_BUCKETS) == "below_0"
        assert assign_bucket(0.01, EV_BUCKETS) == "0_to_2"
        assert assign_bucket(3.0, EV_BUCKETS) == "2_to_5"
        assert assign_bucket(7.0, EV_BUCKETS) == "5_to_10"
        assert assign_bucket(15.0, EV_BUCKETS) == "10_plus"

    def test_odds_bucket_assignment(self):
        from src.grading import assign_bucket, ODDS_BUCKETS
        assert assign_bucket(-250, ODDS_BUCKETS) == "shorter_than_-200"
        assert assign_bucket(-175, ODDS_BUCKETS) == "-200_to_-151"
        assert assign_bucket(-120, ODDS_BUCKETS) == "-150_to_-101"
        assert assign_bucket(0, ODDS_BUCKETS) == "_-100_to_+100"
        assert assign_bucket(125, ODDS_BUCKETS) == "+101_to_+150"
        assert assign_bucket(175, ODDS_BUCKETS) == "+151_to_+200"
        assert assign_bucket(300, ODDS_BUCKETS) == "longer_than_+200"

    def test_n_books_bucket_assignment(self):
        from src.grading import assign_bucket, N_BOOKS_BUCKETS
        assert assign_bucket(2, N_BOOKS_BUCKETS) == "fewer_than_3"
        assert assign_bucket(3, N_BOOKS_BUCKETS) == "3"
        assert assign_bucket(4, N_BOOKS_BUCKETS) == "4"
        assert assign_bucket(6, N_BOOKS_BUCKETS) == "5_plus"


# ══════════════════════════════════════════════════════════════════
# Database Query Helper Tests
# ══════════════════════════════════════════════════════════════════

class TestDBHelpers:
    def test_get_all_recommendations_with_settlement(self, db_conn):
        from database.db_manager import get_all_recommendations_with_settlement
        _insert_recommendation(db_conn, "R1")
        _insert_settlement(db_conn, "R1", "WIN")
        _insert_closing(db_conn, "R1")

        result = get_all_recommendations_with_settlement(db_conn)
        assert len(result) == 1
        assert result[0]["settlement_status"] == "WIN"
        assert result[0]["clv_available"] == 1


# ══════════════════════════════════════════════════════════════════
# Unit / Compute Tests
# ══════════════════════════════════════════════════════════════════

class TestComputeUnits:
    def test_win_positive_odds(self):
        from database.db_manager import compute_units
        risk, profit, ret = compute_units("WIN", 150)
        assert risk == 1.0
        assert profit == 1.5
        assert ret == 2.5

    def test_win_negative_odds(self):
        from database.db_manager import compute_units
        risk, profit, ret = compute_units("WIN", -150)
        assert risk == 1.0
        assert round(profit, 4) == 0.6667
        assert round(ret, 4) == 1.6667

    def test_loss(self):
        from database.db_manager import compute_units
        risk, profit, ret = compute_units("LOSS", -110)
        assert risk == 1.0
        assert profit == -1.0
        assert ret == 0.0

    def test_push(self):
        from database.db_manager import compute_units
        risk, profit, ret = compute_units("PUSH", -110)
        assert risk == 1.0
        assert profit == 0.0
        assert ret == 1.0

    def test_unresolved_excluded(self):
        from database.db_manager import compute_units
        risk, profit, ret = compute_units("UNRESOLVED", -110)
        assert risk == 0.0
        assert profit == 0.0
        assert ret == 0.0
