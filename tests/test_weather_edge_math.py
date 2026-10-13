"""Weather Edge: fees, executable EV, and outcome probabilities."""

from decimal import Decimal as D

import pytest

from src.execution.base import OrderLevel
from src.weather_edge.ev import evaluate_side, paper_pnl, target_quantity
from src.weather_edge.fees import fee_function, kalshi_taker_fee, polymarket_taker_fee
from src.weather_edge.models import Bin
from src.weather_edge.probability import (
    apply_observed_extreme, bin_probability, build_distribution, check_partition, clip_probability,
    point_distribution, probability_interval,
)


def L(price, qty):
    return OrderLevel(D(str(price)), D(str(qty)))


class TestFees:
    def test_kalshi_fee_rounds_up_to_the_cent(self):
        # 0.07 * 100 * 0.5 * 0.5 = 1.75 exactly
        assert kalshi_taker_fee(D(100), D("0.50")) == D("1.75")
        # 0.07 * 3 * 0.30 * 0.70 = 0.0441 -> ceil to 0.05
        assert kalshi_taker_fee(D(3), D("0.30")) == D("0.05")

    def test_kalshi_multiplier_scales_the_fee(self):
        assert kalshi_taker_fee(D(100), D("0.50"), D("0.5")) == D("0.88")  # 0.875 rounds up

    def test_polymarket_fee_uses_five_percent_curve_five_decimals(self):
        assert polymarket_taker_fee(D(100), D("0.50"), D("0.05")) == D("1.25000")
        assert polymarket_taker_fee(D(7), D("0.123"), D("0.05")) == D("0.03775")

    @pytest.mark.parametrize("price", [D("0"), D("1"), D("-0.2"), D("1.5")])
    def test_no_fee_outside_open_price_range(self, price):
        assert kalshi_taker_fee(D(10), price) == 0
        assert polymarket_taker_fee(D(10), price, D("0.05")) == 0

    def test_fee_function_dispatch_and_unknown_model(self):
        assert fee_function("kalshi_quadratic", D(1))(D(100), D("0.5")) == D("1.75")
        assert fee_function("polymarket_curve", D("0.05"))(D(100), D("0.5")) == D("1.25000")
        with pytest.raises(ValueError):
            fee_function("made_up", D(1))


class TestExecutableEV:
    FEE = staticmethod(fee_function("kalshi_quadratic", D(1)))

    def test_hand_computed_yes_example(self):
        # buy 40 YES at 0.50 with p=0.60: fee = ceil(0.07*40*0.25=0.70)=0.70 ; cost = 20.70
        r = evaluate_side(side="YES", asks=[L("0.50", 100)], p_win=0.60, stake_usd=D(20), fee_fn=self.FEE)
        assert r.filled_qty == 40 and r.avg_price == D("0.50") and r.fee == D("0.70")
        assert r.total_cost == D("20.70")
        assert r.gross_ev_per_contract == D("0.10")
        assert r.net_ev_per_contract == D("0.60") - D("20.70") / 40
        assert float(r.net_ev_pct) == pytest.approx(float(r.net_ev_per_contract / (D("20.70") / 40) * 100))
        assert r.break_even_prob == D("20.70") / 40

    def test_no_side_uses_complement_probability_and_no_asks(self):
        # P(YES)=0.30 so a NO buyer wins 70%; NO ask 0.60 is a +EV bet even though YES looks "cheap" at 0.40
        r = evaluate_side(side="NO", asks=[L("0.60", 100)], p_win=1 - 0.30, stake_usd=D(30), fee_fn=self.FEE)
        assert r.p_win == D("0.7") and r.gross_ev_per_contract == D("0.10")

    def test_slippage_walks_the_book_and_lowers_ev(self):
        thin = [L("0.50", 10), L("0.60", 100)]
        r = evaluate_side(side="YES", asks=thin, p_win=0.70, stake_usd=D(50), fee_fn=self.FEE)
        assert r.best_price == D("0.50") and r.avg_price > D("0.50")
        assert r.slippage_per_contract == r.avg_price - r.best_price > 0
        deep = evaluate_side(side="YES", asks=[L("0.50", 1000)], p_win=0.70, stake_usd=D(50), fee_fn=self.FEE)
        assert r.net_ev_per_contract < deep.net_ev_per_contract

    def test_partial_fill_is_reported_not_hidden(self):
        r = evaluate_side(side="YES", asks=[L("0.50", 10)], p_win=0.70, stake_usd=D(100), fee_fn=self.FEE)
        assert r.filled_qty == 10 and r.requested_qty == 200 and r.fill_ratio == D("0.05")

    def test_unsorted_and_invalid_levels_are_handled(self):
        r = evaluate_side(side="YES", asks=[L("0.90", 50), L("0", 10), L("0.40", 50), L("0.5", 0)], p_win=0.6,
                          stake_usd=D(10), fee_fn=self.FEE)
        assert r.best_price == D("0.40")

    def test_nothing_to_buy_returns_none(self):
        assert evaluate_side(side="YES", asks=[], p_win=0.6, stake_usd=D(10), fee_fn=self.FEE) is None
        assert evaluate_side(side="YES", asks=[L("1.0", 5)], p_win=0.6, stake_usd=D(10), fee_fn=self.FEE) is None

    def test_negative_edge_gives_negative_ev(self):
        r = evaluate_side(side="YES", asks=[L("0.70", 100)], p_win=0.60, stake_usd=D(35), fee_fn=self.FEE)
        assert r.net_ev_per_contract < 0 and r.net_ev_pct < 0

    def test_fee_is_included_so_a_thin_gross_edge_can_be_net_negative(self):
        r = evaluate_side(side="YES", asks=[L("0.50", 100)], p_win=0.51, stake_usd=D(50), fee_fn=self.FEE)
        assert r.gross_ev_per_contract > 0 and r.net_ev_per_contract < 0

    def test_target_quantity_floors_and_respects_bounds(self):
        assert target_quantity(D(25), D("0.30"), D(1), D(500)) == 83
        assert target_quantity(D(1), D("0.90"), D(5), D(500)) == 5          # min order lifts it
        assert target_quantity(D(10000), D("0.10"), D(1), D(500)) == 500    # cap
        with pytest.raises(ValueError):
            target_quantity(D(1), D(0), D(1), D(5))

    def test_paper_pnl(self):
        assert paper_pnl(filled_qty=D(40), total_cost=D("20.70"), won=True) == D("19.30")
        assert paper_pnl(filled_qty=D(40), total_cost=D("20.70"), won=False) == D("-20.70")


class TestProbability:
    VALUES = [70 + i * 0.1 for i in range(40)]

    def test_partition_probabilities_sum_to_one(self):
        d = build_distribution(self.VALUES, 0.0, 2.5)
        bins = [Bin(None, 68), Bin(69, 70), Bin(71, 72), Bin(73, 74), Bin(75, None)]
        assert check_partition(bins) == (True, [])
        assert sum(bin_probability(d, b) for b in bins) == pytest.approx(1.0, abs=1e-9)

    def test_whole_degree_edges_use_half_degree_boundaries(self):
        d = point_distribution(72.0, 0.75)
        assert bin_probability(d, Bin(72, 72)) == pytest.approx(0.495, abs=0.01)     # +/- 0.5F around the mean
        assert bin_probability(d, Bin(72, None)) == pytest.approx(0.5 + 0.2475, abs=0.01)

    def test_bias_shifts_probability_mass(self):
        warm = bin_probability(build_distribution(self.VALUES, 2.0, 2.5), Bin(75, None))
        cold = bin_probability(build_distribution(self.VALUES, -2.0, 2.5), Bin(75, None))
        assert warm > cold

    def test_wider_error_spreads_mass_into_the_tails(self):
        tight = bin_probability(build_distribution(self.VALUES, 0, 1.0), Bin(80, None))
        wide = bin_probability(build_distribution(self.VALUES, 0, 4.0), Bin(80, None))
        assert wide > tight

    def test_kernel_never_narrower_than_the_floor(self):
        d = build_distribution([72.0] * 10, 0.0, 0.1)
        assert d.sigma >= 0.75

    def test_validation(self):
        with pytest.raises(ValueError):
            build_distribution([], 0, 2)
        with pytest.raises(ValueError):
            build_distribution([70], 0, 0)
        with pytest.raises(ValueError):
            point_distribution(70, 0)

    def test_clip_never_returns_certainty(self):
        assert clip_probability(0.0) == 0.01 and clip_probability(1.0) == 0.99 and clip_probability(0.4) == 0.4

    def test_interval_brackets_the_point_estimate_and_widens_with_uncertainty(self):
        b = Bin(73, 74)
        point = bin_probability(build_distribution(self.VALUES, 0.5, 2.5), b)
        lo, hi = probability_interval(self.VALUES, 0.5, 2.5, 0.3, b)
        assert lo <= point <= hi
        lo2, hi2 = probability_interval(self.VALUES, 0.5, 2.5, 1.2, b)
        assert (hi2 - lo2) > (hi - lo)

    def test_partition_detects_gaps_overlaps_and_open_ends(self):
        assert check_partition([Bin(None, 68), Bin(70, None)])[0] is False                 # gap at 69
        assert check_partition([Bin(None, 70), Bin(70, None)])[0] is False                 # overlap at 70
        assert check_partition([Bin(69, 70), Bin(71, 72)])[0] is False                     # open ends
        assert check_partition([])[0] is False
        assert Bin(5, 5).label == "5°" and Bin(None, 5).contains(-100)
        with pytest.raises(ValueError):
            Bin(5, 4)
        with pytest.raises(ValueError):
            Bin(None, None)

    def test_observed_extreme_floors_a_high_and_caps_a_low(self):
        assert apply_observed_extreme([68, 72, 75], 73.0, "high") == [73.0, 73.0, 75]
        assert apply_observed_extreme([48, 52, 40], 45.0, "low") == [45.0, 45.0, 40]
        assert apply_observed_extreme([68], None, "high") == [68]
        with pytest.raises(ValueError):
            apply_observed_extreme([1], 1, "rain")

    def test_observed_high_removes_probability_of_lower_outcomes(self):
        vals = apply_observed_extreme([68.0] * 30, 74.0, "high")
        d = build_distribution(vals, 0, 1.0)
        assert bin_probability(d, Bin(None, 70)) < 0.01
