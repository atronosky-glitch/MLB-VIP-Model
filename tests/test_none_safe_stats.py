"""A settled pick with no bet_units row (risk_units/profit_units NULL after the
LEFT JOIN) must never crash a stats summary -- one such row used to take down
the entire customer page (found by the customer end-to-end test, 2026-09-24)."""

from src.adaptive_learning import _compute_segment_performance
from src.grading import performance_summary

RECS = [
    {"settlement_status": "WIN", "risk_units": None, "profit_units": None, "offered_american_odds": 110},
    {"settlement_status": "LOSS", "risk_units": 1.0, "profit_units": -1.0, "offered_american_odds": -110},
    {"settlement_status": "LOSS"},                                   # keys absent entirely
]


def test_performance_summary_tolerates_null_units():
    result = performance_summary(RECS)
    assert result["roi"] == -1.0 / 1.0 or result["roi"] is not None
    assert result["wins"] == 1 and result["losses"] == 2


def test_segment_performance_tolerates_null_units():
    segment = _compute_segment_performance(RECS)
    assert segment is not None
