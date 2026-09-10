"""Tests for cross-book arbitrage detection (src/arbitrage.py)."""

from src.arbitrage import find_arbitrage_in_group, find_arbitrage_opportunities


def test_finds_a_real_cross_book_arbitrage():
    # Over @ +110 (2.10, implied 47.6%) at BookA, Under @ +130 (2.30,
    # implied 43.5%) at BookB. Combined 91.1% < 100% -> guaranteed profit.
    over = {"BookA": {"price": 110, "decimal_odds": 2.10}}
    under = {"BookB": {"price": 130, "decimal_odds": 2.30}}
    result = find_arbitrage_in_group("g1", over, under)
    assert result is not None
    assert result["side_a_book"] == "BookA"
    assert result["side_b_book"] == "BookB"
    assert result["guaranteed_roi_pct"] > 0
    # Stakes should sum to 100% of bankroll.
    assert abs(result["side_a_stake_pct"] + result["side_b_stake_pct"] - 1.0) < 1e-9


def test_no_arbitrage_when_combined_prob_over_100_percent():
    over = {"BookA": {"price": -110, "decimal_odds": 1.909}}
    under = {"BookB": {"price": -110, "decimal_odds": 1.909}}
    assert find_arbitrage_in_group("g1", over, under) is None


def test_no_arbitrage_when_same_book_is_best_on_both_sides():
    # Even if this summed under 100% it must not be trusted -- a real
    # book's own market never actually does this.
    over = {"BookA": {"price": 200, "decimal_odds": 3.00}, "BookB": {"price": -200, "decimal_odds": 1.50}}
    under = {"BookA": {"price": 200, "decimal_odds": 3.00}}
    result = find_arbitrage_in_group("g1", over, under)
    assert result is None


def test_missing_side_returns_none():
    assert find_arbitrage_in_group("g1", {}, {"BookB": {"price": 100, "decimal_odds": 2.0}}) is None
    assert find_arbitrage_in_group("g1", {"BookA": {"price": 100, "decimal_odds": 2.0}}, {}) is None


def test_picks_the_best_price_per_side_across_multiple_books():
    over = {
        "BookA": {"price": 100, "decimal_odds": 2.00},
        "BookB": {"price": 110, "decimal_odds": 2.10},  # best
    }
    under = {
        "BookC": {"price": 120, "decimal_odds": 2.20},
        "BookD": {"price": 130, "decimal_odds": 2.30},  # best
    }
    result = find_arbitrage_in_group("g1", over, under)
    assert result["side_a_book"] == "BookB"
    assert result["side_b_book"] == "BookD"


def _row(event_id, player_id, market_type, group_key, side, sportsbook, price, decimal_odds, line=None):
    return {
        "event_id": event_id, "player_id": player_id, "player_name": "Test Player",
        "market_type": market_type, "market_group_key": group_key, "side": side,
        "sportsbook": sportsbook, "price": price, "decimal_odds": decimal_odds, "line": line,
    }


def test_find_arbitrage_opportunities_scans_a_batch_of_rows():
    rows = [
        # Group 1: real arbitrage
        _row("E1", "P1", "pitching_strikeouts_ou", "E1|P1|k|6.5", "OVER", "BookA", 110, 2.10, 6.5),
        _row("E1", "P1", "pitching_strikeouts_ou", "E1|P1|k|6.5", "UNDER", "BookB", 130, 2.30, 6.5),
        # Group 2: efficient market, no arbitrage
        _row("E2", "P2", "batting_totalBases_ou", "E2|P2|tb|1.5", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E2", "P2", "batting_totalBases_ou", "E2|P2|tb|1.5", "UNDER", "BookB", -110, 1.909, 1.5),
    ]
    results = find_arbitrage_opportunities(rows)
    assert len(results) == 1
    assert results[0]["group_key"] == "E1|P1|k|6.5"
    assert results[0]["market_type"] == "pitching_strikeouts_ou"
    assert results[0]["player_name"] == "Test Player"


def test_find_arbitrage_opportunities_skips_malformed_three_sided_group():
    rows = [
        _row("E1", "P1", "x", "g1", "OVER", "BookA", 200, 3.00),
        _row("E1", "P1", "x", "g1", "UNDER", "BookB", 200, 3.00),
        _row("E1", "P1", "x", "g1", "PUSH", "BookC", 200, 3.00),
    ]
    assert find_arbitrage_opportunities(rows) == []


def test_find_arbitrage_opportunities_sorted_by_roi_descending():
    rows = [
        _row("E1", "P1", "x", "g1", "OVER", "BookA", 100, 2.50),
        _row("E1", "P1", "x", "g1", "UNDER", "BookB", 100, 2.50),  # huge arb
        _row("E2", "P2", "x", "g2", "OVER", "BookA", 105, 2.05),
        _row("E2", "P2", "x", "g2", "UNDER", "BookB", 100, 2.02),  # small arb
    ]
    results = find_arbitrage_opportunities(rows)
    assert len(results) == 2
    assert results[0]["guaranteed_roi_pct"] > results[1]["guaranteed_roi_pct"]


def test_find_arbitrage_opportunities_rejects_an_implausible_far_out_line():
    """Real bug, found live 2026-09-09: a game whose real total sits
    around 8.5-9.0 (many books clustered there, normal vig) also had a
    "4.5" line priced near even money -- unreliable/placeholder data on
    a far-out alternate, not a real cross-book disagreement. A
    mathematically valid-looking arbitrage on that 4.5 line must be
    rejected because it's implausibly far from the game's own
    consensus, even though the two-sided math checks out in isolation."""
    rows = [
        # Establish a tight, sane consensus around 8.5-9.0 -- no arb here.
        _row("E1", "GAME", "game_total_ou", "g_8.5", "OVER", "BookC", -110, 1.9091, line=8.5),
        _row("E1", "GAME", "game_total_ou", "g_8.5", "UNDER", "BookC", -110, 1.9091, line=8.5),
        _row("E1", "GAME", "game_total_ou", "g_9.0", "OVER", "BookD", -115, 1.8696, line=9.0),
        _row("E1", "GAME", "game_total_ou", "g_9.0", "UNDER", "BookD", -105, 1.9524, line=9.0),
        # A far-out 4.5 line with unreliable near-even-money pricing that
        # happens to form a "real" arbitrage mathematically.
        _row("E1", "GAME", "game_total_ou", "g_4.5", "OVER", "BookA", 110, 2.10, line=4.5),
        _row("E1", "GAME", "game_total_ou", "g_4.5", "UNDER", "BookB", 130, 2.30, line=4.5),
    ]
    results = find_arbitrage_opportunities(rows)
    assert all(r["line"] != 4.5 for r in results)


def test_find_arbitrage_opportunities_keeps_a_plausible_nearby_line():
    """A line reasonably close to the consensus (unlike the 4.5 case
    above) must still be found -- this isn't a blanket ban on alt lines,
    only implausibly far ones."""
    rows = [
        _row("E1", "GAME", "game_total_ou", "g_8.5", "OVER", "BookC", -110, 1.9091, line=8.5),
        _row("E1", "GAME", "game_total_ou", "g_8.5", "UNDER", "BookC", -110, 1.9091, line=8.5),
        _row("E1", "GAME", "game_total_ou", "g_9.0", "OVER", "BookD", -115, 1.8696, line=9.0),
        _row("E1", "GAME", "game_total_ou", "g_9.0", "UNDER", "BookD", -105, 1.9524, line=9.0),
        # 7.5 is only ~1 run from the 8.75ish consensus -- plausible.
        _row("E1", "GAME", "game_total_ou", "g_7.5", "OVER", "BookA", 110, 2.10, line=7.5),
        _row("E1", "GAME", "game_total_ou", "g_7.5", "UNDER", "BookB", 130, 2.30, line=7.5),
    ]
    results = find_arbitrage_opportunities(rows)
    assert any(r["line"] == 7.5 for r in results)


def test_find_arbitrage_opportunities_rejects_a_thin_liquidity_outlier_price():
    """Real case, found live 2026-09-10 evaluating exchange venues
    (Kalshi/Novig/Polymarket/ProphetX) for inclusion: a Novig 'Under 0.5
    home runs' quote at -9900 sat on an otherwise completely normal
    line/consensus -- is_plausible_line alone can't catch this since the
    LINE was fine, only the PRICE was an outlier. Several normal-priced
    books establish the real consensus; one exchange row at an absurd
    price must be dropped before it can even be considered, even though
    it would otherwise pair into a huge "guaranteed profit" arbitrage."""
    rows = [
        # Normal consensus pricing for this side across several books.
        _row("E1", "P1", "batting_homeRuns_ou", "g_0.5", "UNDER", "BookA", -150, 1.6667, line=0.5),
        _row("E1", "P1", "batting_homeRuns_ou", "g_0.5", "UNDER", "BookB", -155, 1.6452, line=0.5),
        _row("E1", "P1", "batting_homeRuns_ou", "g_0.5", "UNDER", "BookC", -145, 1.6897, line=0.5),
        _row("E1", "P1", "batting_homeRuns_ou", "g_0.5", "OVER", "BookD", 130, 2.30, line=0.5),
        # Thin-liquidity exchange outlier on the same, otherwise normal, line.
        _row("E1", "P1", "batting_homeRuns_ou", "g_0.5", "UNDER", "novig", -9900, 1.0101, line=0.5),
    ]
    results = find_arbitrage_opportunities(rows)
    assert all(r.get("side_b_book") != "novig" and r.get("side_a_book") != "novig" for r in results)
