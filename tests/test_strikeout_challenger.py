"""Tests for the independent strikeout challenger baseline."""

from src.strikeout_challenger import evaluate_challenger, project_strikeouts


def test_projection_uses_only_verified_stat_inputs():
    projection = project_strikeouts(
        strikeouts=122, batters_faced=398, games_started=17,
        line=6.5, side="OVER",
    )
    assert projection is not None
    assert projection.expected_strikeouts > 0
    assert 0 < projection.over_probability < 1
    assert projection.version == "strikeout_challenger_v1"


def test_invalid_inputs_are_unavailable():
    assert project_strikeouts(
        strikeouts=10, batters_faced=0, games_started=1, line=5.5, side="OVER"
    ) is None


def test_challenger_evaluation_is_sample_gated():
    result = evaluate_challenger([
        {"timestamp": "2026-08-01", "challenger_fair_probability": 0.6, "outcome": "WIN"},
        {"timestamp": "2026-08-02", "challenger_fair_probability": 0.4, "outcome": "LOSS"},
    ], min_sample=30)
    assert result["sample_size"] == 2
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["chronological"] is True
