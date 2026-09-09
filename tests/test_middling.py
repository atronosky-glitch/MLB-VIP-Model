"""Tests for middle detection (src/middling.py)."""

from src.middling import find_middle_between_lines, find_middle_opportunities


def test_finds_a_real_middle_with_a_small_guaranteed_worst_case():
    over_price = {"sportsbook": "BookA", "price": -110, "decimal_odds": 1.909}
    under_price = {"sportsbook": "BookB", "price": -110, "decimal_odds": 1.909}
    result = find_middle_between_lines(8.5, over_price, 9.5, under_price)
    assert result is not None
    assert result["window_width"] == 1.0
    assert result["over_line"] == 8.5
    assert result["under_line"] == 9.5
    # Near-fair -110/-110 prices: small guaranteed worst-case loss, real
    # upside if the number lands in the window.
    assert -10 < result["worst_case_roi_pct"] < 0
    assert result["best_case_roi_pct"] > 0
    assert result["best_case_roi_pct"] > result["worst_case_roi_pct"]


def test_same_or_inverted_lines_is_not_a_middle():
    price = {"sportsbook": "BookA", "price": -110, "decimal_odds": 1.909}
    assert find_middle_between_lines(8.5, price, 8.5, price) is None
    assert find_middle_between_lines(9.5, price, 8.5, price) is None  # over above under: inverted


def test_missing_price_returns_none():
    price = {"sportsbook": "BookA", "price": -110, "decimal_odds": 1.909}
    assert find_middle_between_lines(8.5, None, 9.5, price) is None
    assert find_middle_between_lines(8.5, price, 9.5, {}) is None


def test_stakes_sized_so_either_single_leg_win_returns_the_same_amount():
    over_price = {"sportsbook": "BookA", "price": 100, "decimal_odds": 2.00}
    under_price = {"sportsbook": "BookB", "price": -150, "decimal_odds": 1.667}
    result = find_middle_between_lines(8.5, over_price, 9.5, under_price)
    stake_over = result["over_stake_pct"]
    stake_under = result["under_stake_pct"]
    payout_over = stake_over * over_price["decimal_odds"]
    payout_under = stake_under * under_price["decimal_odds"]
    assert abs(payout_over - payout_under) < 1e-6


def _row(event_id, player_id, market_type, side, sportsbook, price, decimal_odds, line):
    return {
        "event_id": event_id, "player_id": player_id, "player_name": "Test Player",
        "market_type": market_type, "side": side, "sportsbook": sportsbook,
        "price": price, "decimal_odds": decimal_odds, "line": line,
    }


def test_find_middle_opportunities_across_two_lines():
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -110, 1.909, 2.5),
        # Same line both sides too (no window on its own, shouldn't crash)
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookC", -105, 1.952, 2.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookD", -105, 1.952, 1.5),
    ]
    results = find_middle_opportunities(rows)
    assert len(results) >= 1
    best = results[0]
    assert best["over_line"] == 1.5
    assert best["under_line"] == 2.5
    assert best["market_type"] == "batting_totalBases_ou"
    assert best["player_name"] == "Test Player"


def test_find_middle_opportunities_ignores_non_over_under_sides():
    rows = [
        _row("E1", "P1", "game_moneyline", "HOME", "BookA", -110, 1.909, None),
        _row("E1", "P1", "game_moneyline", "AWAY", "BookB", -110, 1.909, None),
    ]
    assert find_middle_opportunities(rows) == []


def test_find_middle_opportunities_respects_worst_case_loss_filter():
    # Wide spread of bad prices on both legs -> guaranteed loss too big
    # to call "little to no risk".
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -400, 1.25, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -400, 1.25, 2.5),
    ]
    assert find_middle_opportunities(rows, max_worst_case_loss_pct=5.0) == []
    # But allowed through with a looser cap.
    assert find_middle_opportunities(rows, max_worst_case_loss_pct=100.0) != []


def test_find_middle_opportunities_needs_at_least_two_distinct_lines():
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -110, 1.909, 1.5),
    ]
    assert find_middle_opportunities(rows) == []
