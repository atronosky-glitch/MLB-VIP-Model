"""Independent property/boundary checks of arbitrage and middle math (launch
audit). Expected values are derived here from first principles, not by
re-using the module's own formulas."""

from __future__ import annotations

import itertools

import pytest

from src.arbitrage import find_arbitrage_in_group
from src.middling import find_middle_between_lines


def _px(book, dec):
    return {book: {"price": 0, "decimal_odds": dec}}


class TestArbitrage:
    @pytest.mark.parametrize("dec_a,dec_b", [(2.10, 2.05), (1.95, 2.20), (3.0, 1.60), (1.60, 3.0), (2.5, 1.75)])
    def test_real_arbitrage_locks_the_same_payout_on_either_outcome(self, dec_a, dec_b):
        combined = 1 / dec_a + 1 / dec_b
        assert combined < 1
        arb = find_arbitrage_in_group("g", _px("BookA", dec_a), _px("BookB", dec_b))
        assert arb is not None
        stake_a, stake_b = arb["side_a_stake_pct"], arb["side_b_stake_pct"]
        assert stake_a + stake_b == pytest.approx(1.0, abs=1e-5)
        assert stake_a * dec_a == pytest.approx(stake_b * dec_b, rel=1e-4)             # equal payout either way
        assert stake_a * dec_a - 1 == pytest.approx(1 / combined - 1, rel=1e-3)         # guaranteed profit per unit
        assert arb["guaranteed_roi_pct"] == pytest.approx((1 / combined - 1) * 100, rel=1e-3)
        assert arb["guaranteed_roi_pct"] > 0

    @pytest.mark.parametrize("dec_a,dec_b", [(2.0, 2.0), (1.9, 2.1), (1.5, 3.0), (1.909, 1.909), (2.2, 1.8)])
    def test_break_even_or_negative_books_are_never_called_arbitrage(self, dec_a, dec_b):
        assert 1 / dec_a + 1 / dec_b >= 1 - 1e-9
        assert find_arbitrage_in_group("g", _px("A", dec_a), _px("B", dec_b)) is None

    def test_exactly_one_hundred_percent_is_not_arbitrage(self):
        assert find_arbitrage_in_group("g", _px("A", 2.0), _px("B", 2.0)) is None

    def test_best_price_per_side_is_used_and_same_book_both_sides_is_rejected(self):
        a = {"A": {"price": 0, "decimal_odds": 2.05}, "C": {"price": 0, "decimal_odds": 1.90}}
        b = {"B": {"price": 0, "decimal_odds": 2.10}, "D": {"price": 0, "decimal_odds": 1.80}}
        arb = find_arbitrage_in_group("g", a, b)
        assert arb["side_a_book"] == "A" and arb["side_b_book"] == "B"
        same = {"Solo": {"price": 0, "decimal_odds": 2.5}}
        assert find_arbitrage_in_group("g", same, {"Solo": {"price": 0, "decimal_odds": 2.5}}) is None

    def test_invalid_inputs_are_rejected(self):
        assert find_arbitrage_in_group("g", {}, _px("B", 2.1)) is None
        assert find_arbitrage_in_group("g", _px("A", 2.1), {}) is None
        assert find_arbitrage_in_group("g", _px("A", 1.0), _px("B", 5.0)) is None
        assert find_arbitrage_in_group("g", _px("A", 0.0), _px("B", 5.0)) is None
        assert find_arbitrage_in_group("g", _px("A", -2.0), _px("B", 5.0)) is None


def _leg(book, dec, price=0):
    return {"sportsbook": book, "price": price, "decimal_odds": dec}


class TestMiddleMath:
    def test_over_at_a_lower_line_and_under_at_a_higher_line_forms_the_window(self):
        m = find_middle_between_lines(5.5, _leg("A", 1.91), 7.5, _leg("B", 1.91))
        assert m["window_width"] == 2.0 and m["over_line"] == 5.5 and m["under_line"] == 7.5
        assert m["over_sportsbook"] == "A" and m["under_sportsbook"] == "B"

    @pytest.mark.parametrize("over_line,under_line", [(6.5, 6.5), (7.5, 5.5), (None, 5.5), (5.5, None)])
    def test_no_window_means_no_middle(self, over_line, under_line):
        assert find_middle_between_lines(over_line, _leg("A", 1.91), under_line, _leg("B", 1.91)) is None

    def test_missing_or_invalid_prices_are_rejected(self):
        assert find_middle_between_lines(5.5, None, 7.5, _leg("B", 1.9)) is None
        assert find_middle_between_lines(5.5, _leg("A", 1.0), 7.5, _leg("B", 1.9)) is None
        assert find_middle_between_lines(5.5, _leg("A", 1.9), 7.5, {"sportsbook": "B"}) is None

    def test_worst_and_best_case_returns_from_first_principles(self):
        over_dec, under_dec = 1.95, 1.87
        m = find_middle_between_lines(5.5, _leg("A", over_dec), 7.5, _leg("B", under_dec), stake_total=100.0)
        p_o, p_u = 1 / over_dec, 1 / under_dec
        s_o, s_u = 100 * p_o / (p_o + p_u), 100 * p_u / (p_o + p_u)
        assert m["over_stake_pct"] + m["under_stake_pct"] == pytest.approx(1.0, abs=1e-5)
        worst = min(s_o * over_dec, s_u * under_dec) - 100
        best = s_o * over_dec + s_u * under_dec - 100
        assert m["worst_case_roi_pct"] == pytest.approx(worst, abs=1e-3)
        assert m["best_case_roi_pct"] == pytest.approx(best, abs=1e-3)
        assert m["best_case_roi_pct"] > m["worst_case_roi_pct"]

    def test_verdict_boundary_is_strictly_positive_ev(self):
        legs = (5.5, _leg("A", 1.91), 7.5, _leg("B", 1.91))
        base = find_middle_between_lines(*legs)
        worst, best = base["worst_case_roi_pct"], base["best_case_roi_pct"]
        p_break_even = -worst / (best - worst)
        at_zero = find_middle_between_lines(*legs, hit_probability=p_break_even)
        assert abs(at_zero["true_ev_pct"]) < 0.01
        assert find_middle_between_lines(*legs, hit_probability=p_break_even - 0.02)["verdict"] == "NOT_WORTH_IT"
        worth = find_middle_between_lines(*legs, hit_probability=min(0.99, p_break_even + 0.05))
        assert worth["verdict"] == "WORTH_IT" and worth["true_ev_pct"] > 0

    def test_only_worth_it_gets_a_stake_and_unknown_has_no_verdict_or_stake(self):
        legs = (5.5, _leg("A", 1.91), 7.5, _leg("B", 1.91))
        assert find_middle_between_lines(*legs)["verdict"] == "UNKNOWN"
        assert find_middle_between_lines(*legs)["recommended_stake_units"] is None
        bad = find_middle_between_lines(*legs, hit_probability=0.01)
        assert bad["verdict"] == "NOT_WORTH_IT" and bad["recommended_stake_units"] is None      # None, never 0
        good = find_middle_between_lines(*legs, hit_probability=0.6)
        assert good["verdict"] == "WORTH_IT" and good["recommended_stake_units"] > 0

    def test_a_wider_window_never_lowers_the_best_case(self):
        prev = None
        for width in (1.0, 2.0, 3.0):
            m = find_middle_between_lines(5.5, _leg("A", 1.91), 5.5 + width, _leg("B", 1.91))
            assert prev is None or m["window_width"] > prev
            prev = m["window_width"]
