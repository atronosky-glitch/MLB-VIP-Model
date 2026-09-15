"""Tests for the middle-opportunity Discord formatting in
src/message_formatter.py -- specifically the hit-probability/true-EV/
suggested-stake line added 2026-09-14 (see src/middling.py)."""

from src.message_formatter import format_middle_alert


def _opp(**overrides):
    base = {
        "player_name": "Test Player",
        "market_type": "batting_totalBases_ou",
        "matchup": "Away @ Home",
        "over_line": 1.5, "over_sportsbook": "BookA", "over_price": -110,
        "under_line": 2.5, "under_sportsbook": "BookB", "under_price": -110,
        "best_case_roi_pct": 10.0, "worst_case_roi_pct": -5.0,
        "hit_probability": None, "true_ev_pct": None, "recommended_stake_units": None,
    }
    base.update(overrides)
    return base


def test_shows_not_estimable_when_hit_probability_unavailable():
    out = format_middle_alert([_opp()])
    assert "not estimable" in out.lower()


def test_shows_hit_chance_true_ev_and_stake_when_available():
    out = format_middle_alert([_opp(
        hit_probability=0.15, true_ev_pct=2.35, recommended_stake_units=0.75,
    )])
    assert "15.0%" in out
    assert "+2.35%" in out
    assert "0.75u" in out


def test_zero_stake_reads_as_skip_not_a_bare_zero():
    out = format_middle_alert([_opp(
        hit_probability=0.02, true_ev_pct=-3.0, recommended_stake_units=0.0,
    )])
    assert "skip" in out.lower()


def test_empty_opportunities_returns_empty_string():
    assert format_middle_alert([]) == ""
