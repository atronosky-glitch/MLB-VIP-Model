"""Tests for src/line_plausibility.py."""

from src.line_plausibility import (
    consensus_lines, is_plausible_line, MAX_LINE_DEVIATION,
    consensus_prices, is_plausible_price, implied_probability, MAX_PROBABILITY_DEVIATION,
)


def _row(event_id, player_id, market_type, line):
    return {"event_id": event_id, "player_id": player_id, "market_type": market_type, "line": line}


def test_consensus_lines_is_the_median_per_event_player_market():
    rows = [
        _row("E1", "GAME", "game_total_ou", 4.5),
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E1", "GAME", "game_total_ou", 9.0),
    ]
    result = consensus_lines(rows)
    assert result[("E1", "GAME", "game_total_ou")] == 8.5


def test_consensus_lines_scoped_per_event_and_market():
    rows = [
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E2", "GAME", "game_total_ou", 3.5),
        _row("E1", "P1", "batting_totalBases_ou", 1.5),
    ]
    result = consensus_lines(rows)
    assert result[("E1", "GAME", "game_total_ou")] == 8.5
    assert result[("E2", "GAME", "game_total_ou")] == 3.5
    assert result[("E1", "P1", "batting_totalBases_ou")] == 1.5


def test_consensus_lines_ignores_rows_with_no_line():
    rows = [
        {"event_id": "E1", "player_id": "GAME", "market_type": "game_moneyline", "line": None},
    ]
    assert consensus_lines(rows) == {}


def test_is_plausible_line_uses_market_specific_threshold():
    assert MAX_LINE_DEVIATION["game_total_ou"] == 2.0
    assert is_plausible_line("game_total_ou", 8.5, 8.5) is True
    assert is_plausible_line("game_total_ou", 10.5, 8.5) is True  # exactly at the boundary
    assert is_plausible_line("game_total_ou", 4.5, 8.5) is False  # the real bug case
    assert is_plausible_line("game_total_ou", 7.5, 8.5) is True


def test_is_plausible_line_falls_back_to_default_threshold_for_unlisted_markets():
    from src.line_plausibility import DEFAULT_MAX_DEVIATION
    assert DEFAULT_MAX_DEVIATION == 1.5
    assert is_plausible_line("batting_totalBases_ou", 1.5, 1.5) is True
    assert is_plausible_line("batting_totalBases_ou", 3.5, 1.5) is False


def _price_row(event_id, player_id, market_type, line, side, price):
    return {
        "event_id": event_id, "player_id": player_id, "market_type": market_type,
        "line": line, "side": side, "price": price,
    }


class TestImpliedProbability:
    def test_favorite_price(self):
        assert round(implied_probability(-150), 4) == round(150 / 250, 4)

    def test_underdog_price(self):
        assert round(implied_probability(150), 4) == round(100 / 250, 4)

    def test_even_money(self):
        assert round(implied_probability(100), 4) == 0.5
        assert round(implied_probability(-100), 4) == 0.5


class TestConsensusPrices:
    def test_median_implied_probability_per_event_player_market_line_side(self):
        rows = [
            _price_row("E1", "P1", "batting_homeRuns_ou", 0.5, "Under", -150),
            _price_row("E1", "P1", "batting_homeRuns_ou", 0.5, "Under", -160),
            _price_row("E1", "P1", "batting_homeRuns_ou", 0.5, "Under", -155),
        ]
        result = consensus_prices(rows)
        key = ("E1", "P1", "batting_homeRuns_ou", 0.5, "Under")
        assert round(result[key], 4) == round(implied_probability(-155), 4)

    def test_over_and_under_are_scored_independently(self):
        rows = [
            _price_row("E1", "P1", "batting_homeRuns_ou", 0.5, "Over", 130),
            _price_row("E1", "P1", "batting_homeRuns_ou", 0.5, "Under", -150),
        ]
        result = consensus_prices(rows)
        over_key = ("E1", "P1", "batting_homeRuns_ou", 0.5, "Over")
        under_key = ("E1", "P1", "batting_homeRuns_ou", 0.5, "Under")
        assert round(result[over_key], 4) == round(implied_probability(130), 4)
        assert round(result[under_key], 4) == round(implied_probability(-150), 4)

    def test_ignores_rows_with_no_price(self):
        rows = [{"event_id": "E1", "player_id": "P1", "market_type": "m", "line": 0.5, "side": "Over", "price": None}]
        assert consensus_prices(rows) == {}


class TestIsPlausiblePrice:
    def test_real_bug_case_thin_liquidity_exchange_outlier(self):
        """Live 2026-09-10: a Novig 'Under 0.5 home runs' quote at -9900
        (99.0% implied) next to a consensus nowhere near that -- a
        perfectly normal line, but a price no real market would show."""
        consensus = implied_probability(-150)  # ordinary price, ~60% implied
        assert is_plausible_price(-9900, consensus) is False

    def test_normal_book_to_book_variance_is_plausible(self):
        consensus = implied_probability(-150)
        assert is_plausible_price(-140, consensus) is True
        assert is_plausible_price(-165, consensus) is True

    def test_boundary_is_inclusive(self):
        consensus = 0.50
        # -300 implies exactly 0.75 -- exactly MAX_PROBABILITY_DEVIATION (0.25) away.
        assert round(implied_probability(-300), 6) == 0.75
        assert abs(implied_probability(-300) - consensus) == MAX_PROBABILITY_DEVIATION
        assert is_plausible_price(-300, consensus) is True
