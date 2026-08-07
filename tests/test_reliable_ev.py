"""Tests for reliable-EV input validation and realized-EV summaries."""

from src.reliable_ev import (
    assess_reliable_ev, summarize_realized_ev, summarize_realized_ev_segments,
)
from src.official_picks import TIER_OFFICIAL, classify_recommendation


def _rec(**overrides):
    rec = {
        "market_form": "ou",
        "fair_prob": 0.55,
        "offered_decimal_odds": 1.9090909,
        "ev_pct": 4.999995,
        "n_consensus_books": 4,
        "market_quality": "VALID_MARKET",
        "freshness_status": "FRESH",
    }
    rec.update(overrides)
    return rec


def test_reliable_ev_requires_consistent_valid_inputs():
    result = assess_reliable_ev(_rec())
    assert result["reliable_ev"] is True
    assert result["reliable_ev_status"] == "RELIABLE"


def test_reliable_ev_rejects_thin_stale_or_excluded_market():
    result = assess_reliable_ev(_rec(n_consensus_books=3, freshness_status="STALE"))
    assert result["reliable_ev"] is False
    assert "insufficient_independent_books" in result["reliable_ev_reasons"]
    assert "stale_quote" in result["reliable_ev_reasons"]


def test_reliable_ev_rejects_arithmetic_mismatch():
    result = assess_reliable_ev(_rec(ev_pct=12.0))
    assert result["reliable_ev"] is False
    assert "ev_arithmetic_mismatch" in result["reliable_ev_reasons"]


def test_reliable_ev_rejects_extreme_outliers():
    result = assess_reliable_ev(_rec(ev_pct=25.0))
    assert result["reliable_ev"] is False
    assert "extreme_ev_outlier" in result["reliable_ev_reasons"]


def test_yn_is_not_relabelled_as_ev():
    result = assess_reliable_ev(_rec(market_form="yn"))
    assert result["reliable_ev"] is False
    assert "not_an_ou_market" in result["reliable_ev_reasons"]


def test_realized_ev_is_advisory_until_sample_gate():
    result = summarize_realized_ev([
        {"ev_pct": 5.0, "profit_units": 1.0, "risk_units": 1.0},
        {"ev_pct": 3.0, "profit_units": -1.0, "risk_units": 1.0},
    ], min_sample=3)
    assert result["realized_roi"] == 0.0
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["sufficient_sample"] is False


def test_failed_reliable_ev_cannot_be_official():
    rec = {
        "event_id": "E1", "player_id": "P1", "side": "Over", "sportsbook": "DK",
        "market_form": "ou", "market_quality": "VALID_MARKET", "freshness_status": "FRESH",
        "event_status": "scheduled", "model_score": 8.5, "rec_status": "QUALIFIED",
        "ev_pct": 5.0, "n_consensus_books": 4, "pinnacle_approved": True,
        "reliable_ev_checked": True, "reliable_ev": False,
        "reliable_ev_reasons": ["ev_arithmetic_mismatch"],
    }
    result = classify_recommendation(rec)
    assert result.tier != TIER_OFFICIAL
    assert "EV reliability gate failed" in result.disqualification_reasons[0]


def test_realized_ev_segments_do_not_pool_markets_or_books():
    result = summarize_realized_ev_segments([
        {"market_type": "strikeouts", "sportsbook": "DK", "ev_bucket": "3-5", "ev_pct": 4, "profit_units": 1},
        {"market_type": "home_runs", "sportsbook": "FD", "ev_bucket": "3-5", "ev_pct": 4, "profit_units": -1},
    ], min_sample=2)
    assert result["segment_count"] == 2
    assert {segment["status"] for segment in result["segments"]} == {"INSUFFICIENT_DATA"}
