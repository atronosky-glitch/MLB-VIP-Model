"""Tests for the middle-opportunity Discord formatting in
src/message_formatter.py -- the hit-probability/true-EV/suggested-stake
line and the WORTH IT / NOT WORTH IT / UNKNOWN verdict label added
2026-09-14/15 (see src/middling.py)."""

from src.message_formatter import format_middle_alert


def _opp(**overrides):
    base = {
        "player_name": "Test Player",
        "market_type": "batting_totalBases_ou",
        "matchup": "Away @ Home",
        "over_line": 1.5, "over_sportsbook": "BookA", "over_price": -110,
        "under_line": 2.5, "under_sportsbook": "BookB", "under_price": -110,
        "best_case_roi_pct": 10.0, "worst_case_roi_pct": -5.0,
        "hit_probability": None, "true_ev_pct": None,
        "verdict": "UNKNOWN", "recommended_stake_units": None,
    }
    base.update(overrides)
    return base


def test_shows_not_estimable_when_hit_probability_unavailable():
    out = format_middle_alert([_opp()])
    assert "not estimable" in out.lower()


def test_unknown_verdict_shows_unknown_label():
    out = format_middle_alert([_opp(verdict="UNKNOWN")])
    assert "UNKNOWN" in out


def test_worth_it_shows_label_hit_chance_true_ev_and_stake():
    out = format_middle_alert([_opp(
        hit_probability=0.15, true_ev_pct=2.35, verdict="WORTH_IT", recommended_stake_units=0.75,
    )])
    assert "WORTH IT" in out
    assert "NOT WORTH IT" not in out
    assert "15.0%" in out
    assert "+2.35%" in out
    assert "0.75u" in out


def test_not_worth_it_shows_label_and_no_stake_number():
    out = format_middle_alert([_opp(
        hit_probability=0.02, true_ev_pct=-3.0, verdict="NOT_WORTH_IT", recommended_stake_units=None,
    )])
    assert "NOT WORTH IT" in out
    assert "not worth betting" in out.lower()
    # No stake unit figure anywhere on this line -- never a bare 0u either.
    assert "0.00u" not in out
    assert "0u" not in out


def test_empty_opportunities_returns_empty_string():
    assert format_middle_alert([]) == ""
