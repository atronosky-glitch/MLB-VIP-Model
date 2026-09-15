"""Tests for middle detection (src/middling.py)."""

from src.middling import (
    compute_middle_stake_units, estimate_middle_hit_probability,
    find_middle_between_lines, find_middle_opportunities, kelly_fraction_bounded,
    MAX_STAKE_UNITS, MIN_STAKE_UNITS,
)
from src.line_plausibility import consensus_prices


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


def test_find_middle_opportunities_rejects_an_implausible_far_out_line():
    """Real bug, found live 2026-09-09: a "Game Total 4.5" line priced
    near even money right next to that same game's 8.5/9.0 lines (also
    near even money) is physically impossible for MLB combined runs --
    unreliable/placeholder pricing on a far-out alternate, not a real
    two-sided market. Must not produce a "huge window, small guaranteed
    risk" middle card built on that bad data."""
    rows = [
        # Tight, sane consensus around 8.5-9.0.
        _row("E1", "GAME", "game_total_ou", "OVER", "BookC", -110, 1.9091, 8.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookC", -110, 1.9091, 8.5),
        _row("E1", "GAME", "game_total_ou", "OVER", "BookD", -115, 1.8696, 9.0),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookD", -105, 1.9524, 9.0),
        # The real card that triggered this fix: Over 4.5 / Under 7.5,
        # both priced near even money.
        _row("E1", "GAME", "game_total_ou", "OVER", "betrivers", 102, 2.02, 4.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "betmgm", 100, 2.00, 7.5),
    ]
    results = find_middle_opportunities(rows)
    assert all(r["over_line"] != 4.5 for r in results)


def test_find_middle_opportunities_keeps_a_plausible_nearby_pair():
    """Not a blanket ban on alt lines -- a pair reasonably close to the
    consensus (unlike the 4.5/7.5 case above) must still be found."""
    rows = [
        _row("E1", "GAME", "game_total_ou", "OVER", "BookC", -110, 1.9091, 8.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookC", -110, 1.9091, 8.5),
        _row("E1", "GAME", "game_total_ou", "OVER", "BookD", -115, 1.8696, 9.0),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookD", -105, 1.9524, 9.0),
        # 7.5/9.5 are within the game_total_ou plausibility band (2.0).
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", 110, 2.10, 7.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookB", 130, 2.30, 9.5),
    ]
    results = find_middle_opportunities(rows)
    assert any(r["over_line"] == 7.5 and r["under_line"] == 9.5 for r in results)


def test_find_middle_opportunities_rejects_a_thin_liquidity_outlier_price():
    """Real case, found live 2026-09-10 evaluating exchange venues
    (Kalshi/Novig/Polymarket/ProphetX) for inclusion: a normal, plausible
    line can still carry one thin-liquidity outlier PRICE (e.g. a Novig
    quote at -9900) that is_plausible_line alone can't catch. That row
    must never end up as a leg of a reported middle."""
    rows = [
        # Normal consensus pricing on both lines/sides.
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -110, 1.909, 2.5),
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookC", -105, 1.952, 2.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookD", -105, 1.952, 1.5),
        # Thin-liquidity exchange outlier on the same, otherwise normal, line.
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "novig", -9900, 1.0101, 2.5),
    ]
    results = find_middle_opportunities(rows)
    assert all(r["over_sportsbook"] != "novig" and r["under_sportsbook"] != "novig" for r in results)


# ── Hit-probability estimation ────────────────────────────────────────

def test_estimate_middle_hit_probability_needs_two_sided_data_at_both_lines():
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        # No UNDER quoted anywhere at 1.5, and no OVER quoted at 2.5.
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -110, 1.909, 2.5),
    ]
    price_consensus = consensus_prices(rows)
    p_hit, confidence = estimate_middle_hit_probability(
        ("E1", "P1", "batting_totalBases_ou"), 1.5, 2.5, price_consensus,
    )
    assert p_hit is None
    assert confidence == "UNAVAILABLE"


def test_estimate_middle_hit_probability_symmetric_coinflip_lines_gives_zero():
    """Both lines exactly 50/50 no-vig means the market puts zero
    probability mass strictly between them."""
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 2.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookA", -110, 1.909, 2.5),
    ]
    price_consensus = consensus_prices(rows)
    p_hit, confidence = estimate_middle_hit_probability(
        ("E1", "P1", "batting_totalBases_ou"), 1.5, 2.5, price_consensus,
    )
    assert confidence == "DEVIGGED"
    assert abs(p_hit - 0.0) < 1e-6


def test_estimate_middle_hit_probability_real_window():
    # Over 8.5 fair ~55% (market leans Over), Under 9.5 fair ~60% (market
    # leans Under) -- consistent with real mass sitting around 9.
    rows = [
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", -122, 1.8197, 8.5),   # implied ~0.55
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", 122, 2.2200, 8.5),    # implied ~0.45
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", 150, 2.50, 9.5),        # implied ~0.40
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", -150, 1.6667, 9.5),    # implied ~0.60
    ]
    price_consensus = consensus_prices(rows)
    p_hit, confidence = estimate_middle_hit_probability(
        ("E1", "GAME", "game_total_ou"), 8.5, 9.5, price_consensus,
    )
    assert confidence == "DEVIGGED"
    assert 0.10 < p_hit < 0.20  # ~0.55 + 0.60 - 1 = 0.15


def test_estimate_middle_hit_probability_clamped_to_valid_range():
    # Deliberately inconsistent no-vig numbers (real noise) that would
    # otherwise push the raw identity below 0.
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", 400, 5.00, 1.5),   # implied 0.20
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookA", -400, 1.25, 1.5),  # implied 0.80
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -400, 1.25, 2.5),   # implied 0.80
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookA", 400, 5.00, 2.5),   # implied 0.20
    ]
    price_consensus = consensus_prices(rows)
    p_hit, confidence = estimate_middle_hit_probability(
        ("E1", "P1", "batting_totalBases_ou"), 1.5, 2.5, price_consensus,
    )
    assert confidence == "DEVIGGED"
    assert 0.0 <= p_hit <= 1.0


# ── Kelly sizing for a bounded (not lose-everything) two-outcome bet ──

def test_kelly_fraction_matches_textbook_binary_formula_when_miss_loses_everything():
    # r_miss = -1.0 (lose the whole stake) should reduce exactly to the
    # standard f* = p - q/b Kelly formula. p=0.6, even-money (b=1).
    f = kelly_fraction_bounded(p_hit=0.6, r_hit=1.0, r_miss=-1.0)
    assert abs(f - 0.2) < 1e-9


def test_kelly_fraction_zero_when_no_edge():
    assert kelly_fraction_bounded(p_hit=0.3, r_hit=0.10, r_miss=-0.05) == 0.0


def test_kelly_fraction_capped_at_one_when_no_real_downside():
    # r_miss >= 0 means this isn't really a middle (arbitrage territory) --
    # Kelly wants max size, capped externally by MAX_STAKE_UNITS.
    assert kelly_fraction_bounded(p_hit=0.5, r_hit=0.05, r_miss=0.01) == 1.0


def test_kelly_fraction_never_negative():
    assert kelly_fraction_bounded(p_hit=0.05, r_hit=0.05, r_miss=-0.05) >= 0.0


class TestComputeMiddleStakeUnits:
    def test_none_hit_probability_returns_no_recommendation(self):
        assert compute_middle_stake_units(None, 20.0, -5.0) is None

    def test_negative_true_ev_returns_zero_units(self):
        # p_hit too low for the window to be worth it.
        units = compute_middle_stake_units(0.02, best_case_roi_pct=10.0, worst_case_roi_pct=-8.0)
        assert units == 0.0

    def test_positive_true_ev_returns_a_clamped_recommendation(self):
        units = compute_middle_stake_units(0.30, best_case_roi_pct=20.0, worst_case_roi_pct=-4.0)
        assert units is not None
        assert MIN_STAKE_UNITS <= units <= MAX_STAKE_UNITS

    def test_strong_edge_clamps_at_max_units_not_beyond(self):
        units = compute_middle_stake_units(0.80, best_case_roi_pct=50.0, worst_case_roi_pct=-2.0)
        assert units == MAX_STAKE_UNITS


# ── Wiring into find_middle_between_lines / find_middle_opportunities ─

def test_find_middle_between_lines_without_hit_probability_leaves_new_fields_none():
    over_price = {"sportsbook": "BookA", "price": -110, "decimal_odds": 1.909}
    under_price = {"sportsbook": "BookB", "price": -110, "decimal_odds": 1.909}
    result = find_middle_between_lines(8.5, over_price, 9.5, under_price)
    assert result["hit_probability"] is None
    assert result["hit_probability_confidence"] == "UNAVAILABLE"
    assert result["true_ev_pct"] is None
    assert result["recommended_stake_units"] is None


def test_find_middle_between_lines_with_hit_probability_computes_true_ev_and_stake():
    over_price = {"sportsbook": "BookA", "price": -110, "decimal_odds": 1.909}
    under_price = {"sportsbook": "BookB", "price": -110, "decimal_odds": 1.909}
    result = find_middle_between_lines(
        8.5, over_price, 9.5, under_price,
        hit_probability=0.15, hit_probability_confidence="DEVIGGED",
    )
    assert result["hit_probability"] == 0.15
    assert result["hit_probability_confidence"] == "DEVIGGED"
    expected_ev = 0.15 * result["best_case_roi_pct"] + 0.85 * result["worst_case_roi_pct"]
    assert abs(result["true_ev_pct"] - round(expected_ev, 4)) < 1e-6
    assert result["recommended_stake_units"] is not None


def test_find_middle_opportunities_attaches_hit_probability_when_deviggable():
    rows = [
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", -122, 1.8197, 8.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", 122, 2.2200, 8.5),
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", 150, 2.50, 9.5),
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", -150, 1.6667, 9.5),
    ]
    results = find_middle_opportunities(rows, max_worst_case_loss_pct=100.0)
    assert len(results) >= 1
    best = results[0]
    assert best["hit_probability_confidence"] == "DEVIGGED"
    assert best["hit_probability"] is not None
    assert best["true_ev_pct"] is not None


def test_find_middle_opportunities_min_true_ev_pct_filters_bad_windows():
    # A window whose true EV is clearly negative even though best-case
    # ROI alone might look attractive -- p_hit is tiny (lines far apart,
    # each near-certain on its own side), so most of the probability
    # mass is a miss.
    rows = [
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", -400, 1.25, 6.5),    # implied 0.80 Over 6.5
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", 400, 5.00, 6.5),     # implied 0.20
        _row("E1", "GAME", "game_total_ou", "OVER", "BookA", 400, 5.00, 8.5),      # implied 0.20 Over 8.5
        _row("E1", "GAME", "game_total_ou", "UNDER", "BookA", -400, 1.25, 8.5),    # implied 0.80 Under 8.5
    ]
    all_results = find_middle_opportunities(rows, max_worst_case_loss_pct=100.0)
    assert len(all_results) >= 1
    unfiltered = all_results[0]
    assert unfiltered["true_ev_pct"] is not None

    filtered = find_middle_opportunities(
        rows, max_worst_case_loss_pct=100.0, min_true_ev_pct=unfiltered["true_ev_pct"] + 1.0,
    )
    assert unfiltered not in filtered


def test_find_middle_opportunities_never_filters_unavailable_confidence_by_true_ev():
    """min_true_ev_pct must never silently drop an opportunity whose
    true EV simply couldn't be computed -- only ones with a known,
    below-threshold true EV."""
    rows = [
        _row("E1", "P1", "batting_totalBases_ou", "OVER", "BookA", -110, 1.909, 1.5),
        _row("E1", "P1", "batting_totalBases_ou", "UNDER", "BookB", -110, 1.909, 2.5),
    ]
    results = find_middle_opportunities(rows, max_worst_case_loss_pct=100.0, min_true_ev_pct=9999.0)
    assert len(results) >= 1
    assert results[0]["hit_probability_confidence"] == "UNAVAILABLE"
