"""Tests for shadow challenger comparison metrics."""

from src.challenger_evaluation import evaluate_shadow_records


def test_challenger_evaluation_is_sample_gated_and_real_data_based():
    result = evaluate_shadow_records([
        {
            "challenger_fair_probability": 0.60,
            "settlement_status": "WIN",
            "offered_decimal_odds": 1.9,
            "profit_units": 0.9,
            "risk_units": 1.0,
            "clv_probability": 0.02,
        },
        {
            "challenger_fair_probability": 0.40,
            "settlement_status": "LOSS",
            "offered_decimal_odds": 2.0,
            "profit_units": -1.0,
            "risk_units": 1.0,
            "clv_probability": -0.01,
        },
    ], min_sample=30)
    assert result["sample_size"] == 2
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["realized_profit_units"] == -0.1
    assert result["clv_sample_size"] == 2
