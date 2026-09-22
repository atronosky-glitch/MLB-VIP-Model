"""Tests for the arbitrage-opportunity Discord formatting in
src/message_formatter.py -- the matchup/game-identification fallback
added 2026-09-22 (see tests/test_arb_middle_scan.py for the underlying
matchup-resolution fix)."""

from src.message_formatter import format_arbitrage_alert


def _opp(**overrides):
    base = {
        "player_name": "Test Player",
        "market_type": "batting_totalBases_ou",
        "matchup": "Away @ Home",
        "league": "MLB",
        "side_a": "OVER", "side_a_sportsbook": "BookA", "side_a_price": 110,
        "side_b": "UNDER", "side_b_sportsbook": "BookB", "side_b_price": 130,
        "guaranteed_roi_pct": 2.5,
    }
    base.update(overrides)
    return base


def test_shows_the_real_matchup_when_present():
    out = format_arbitrage_alert([_opp(matchup="Atlanta Dream @ New York Liberty")])
    assert "Atlanta Dream @ New York Liberty" in out


def test_missing_matchup_shows_an_explicit_fallback_not_silence():
    """2026-09-22: previously the matchup parenthetical was just
    dropped entirely when unknown, so a Discord message could show no
    game identification at all. Now matches src/customer_view.py's own
    "Matchup unavailable" fallback -- always says SOMETHING."""
    out = format_arbitrage_alert([_opp(matchup=None)])
    assert "Matchup unavailable" in out


def test_league_prefix_still_shown_alongside_matchup():
    out = format_arbitrage_alert([_opp(league="WNBA", matchup="Atlanta Dream @ New York Liberty")])
    assert "[WNBA]" in out
    assert "Atlanta Dream @ New York Liberty" in out
