"""Tests for src/execution/sizing.py. Pure -- no I/O."""

from decimal import Decimal

import pytest

from src.execution.sizing import (
    SizingMode, kelly_fraction, size_ev_tiered, size_flat, size_kelly, units_to_usd, usd_to_units,
)


class TestUnitConversions:
    def test_units_to_usd(self):
        assert units_to_usd(Decimal("1.5"), Decimal("10")) == Decimal("15.0")

    def test_usd_to_units(self):
        assert usd_to_units(Decimal("25"), Decimal("10")) == Decimal("2.5")

    def test_round_trip(self):
        usd = units_to_usd(Decimal("0.25"), Decimal("10"))
        assert usd_to_units(usd, Decimal("10")) == Decimal("0.25")

    def test_usd_to_units_rejects_non_positive_unit_size(self):
        with pytest.raises(ValueError):
            usd_to_units(Decimal("10"), Decimal("0"))

    def test_decimal_precision_preserved(self):
        result = units_to_usd(Decimal("0.1"), Decimal("10"))
        assert result == Decimal("1.0")
        assert isinstance(result, Decimal)


class TestFlatSizing:
    def test_default_units_and_unit_size(self):
        result = size_flat(Decimal("1"), Decimal("10"))
        assert result.recommended_units == Decimal("1")
        assert result.recommended_stake_usd == Decimal("10")
        assert result.mode == SizingMode.FLAT

    def test_fractional_units(self):
        result = size_flat(Decimal("1.5"), Decimal("10"))
        assert result.recommended_stake_usd == Decimal("15.0")

    def test_is_deterministic_regardless_of_ev(self):
        a = size_flat(Decimal("1"), Decimal("10"))
        b = size_flat(Decimal("1"), Decimal("10"))
        assert a.recommended_stake_usd == b.recommended_stake_usd


class TestEvTieredSizing:
    TIERS = ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))

    def test_below_lowest_tier_still_uses_lowest_tier(self):
        """A qualified opportunity's net_ev_pct can be below the tier
        table's lowest threshold (e.g. Stage 2B's own min_net_ev_pct
        default of 1.0% is below this table's 3% floor) -- sizing must
        still recommend something, not zero out a qualified opportunity."""
        result = size_ev_tiered(Decimal("1.5"), self.TIERS, Decimal("10"))
        assert result.recommended_units == Decimal("0.5")

    def test_exact_boundary_gets_the_higher_tier(self):
        result = size_ev_tiered(Decimal("5.0"), self.TIERS, Decimal("10"))
        assert result.recommended_units == Decimal("1.0")

    def test_just_below_boundary_gets_the_lower_tier(self):
        result = size_ev_tiered(Decimal("4.99"), self.TIERS, Decimal("10"))
        assert result.recommended_units == Decimal("0.5")

    def test_middle_of_a_tier(self):
        result = size_ev_tiered(Decimal("6.5"), self.TIERS, Decimal("10"))
        assert result.recommended_units == Decimal("1.0")

    def test_above_highest_tier_uses_highest(self):
        result = size_ev_tiered(Decimal("50"), self.TIERS, Decimal("10"))
        assert result.recommended_units == Decimal("2.0")

    def test_stake_matches_units_times_unit_size(self):
        result = size_ev_tiered(Decimal("8.0"), self.TIERS, Decimal("10"))
        assert result.recommended_stake_usd == Decimal("15.0")

    def test_rejects_empty_tier_table(self):
        with pytest.raises(ValueError):
            size_ev_tiered(Decimal("5.0"), (), Decimal("10"))

    def test_unsorted_input_tiers_still_work(self):
        shuffled = ((12.0, 2.0), (3.0, 0.5), (8.0, 1.5), (5.0, 1.0))
        result = size_ev_tiered(Decimal("6.0"), shuffled, Decimal("10"))
        assert result.recommended_units == Decimal("1.0")


class TestKellyFraction:
    """f* = (q - c) / (1 - c). Sanity-checked against the textbook
    even-money case: c=0.5 reduces to f*=2q-1."""

    def test_even_money_case_matches_textbook_formula(self):
        q = Decimal("0.6")
        c = Decimal("0.5")
        f = kelly_fraction(q, c)
        assert f == 2 * q - 1

    def test_full_kelly_worked_example(self):
        # q=0.72, c=0.68 (matches the Stage 3 plan's worked example
        # effective cost) -> f* = (0.72-0.68)/(1-0.68) = 0.04/0.32 = 0.125
        f = kelly_fraction(Decimal("0.72"), Decimal("0.68"))
        assert f == Decimal("0.04") / Decimal("0.32")

    def test_no_edge_returns_zero(self):
        assert kelly_fraction(Decimal("0.5"), Decimal("0.5")) == Decimal("0")

    def test_negative_edge_returns_zero_not_negative(self):
        f = kelly_fraction(Decimal("0.3"), Decimal("0.6"))
        assert f == Decimal("0")

    def test_degenerate_cost_of_zero_returns_zero(self):
        assert kelly_fraction(Decimal("0.9"), Decimal("0")) == Decimal("0")

    def test_degenerate_cost_of_one_returns_zero(self):
        assert kelly_fraction(Decimal("0.9"), Decimal("1")) == Decimal("0")


class TestSizeKelly:
    def test_quarter_kelly_is_one_quarter_of_full_kelly_stake(self):
        full = kelly_fraction(Decimal("0.72"), Decimal("0.68"))
        result = size_kelly(
            Decimal("0.72"), Decimal("0.68"), Decimal("0.25"), Decimal("10"),
            Decimal("100"), Decimal("1000"),
        )
        expected_stake = full * Decimal("0.25") * Decimal("1000")
        assert result.recommended_stake_usd == expected_stake

    def test_negative_kelly_produces_zero_stake(self):
        result = size_kelly(
            Decimal("0.3"), Decimal("0.6"), Decimal("0.25"), Decimal("10"),
            Decimal("100"), Decimal("1000"),
        )
        assert result.recommended_stake_usd == Decimal("0")
        assert result.recommended_units == Decimal("0")

    def test_clamped_to_max_units_per_bet(self):
        # A huge edge would otherwise recommend far more than 2 units.
        result = size_kelly(
            Decimal("0.95"), Decimal("0.10"), Decimal("1.0"), Decimal("10"),
            Decimal("2"), Decimal("100000"),
        )
        assert result.recommended_stake_usd == Decimal("20")  # 2u * $10

    def test_clamped_to_available_bankroll(self):
        # full_kelly=(0.95-0.10)/(1-0.10)=0.9444; multiplier=2.0 (forces
        # the pre-clamp stake, 0.9444*2.0*$10=$18.89, above the $10
        # available bankroll) while max_units_per_bet stays high enough
        # not to be the binding constraint itself.
        result = size_kelly(
            Decimal("0.95"), Decimal("0.10"), Decimal("2.0"), Decimal("10"),
            Decimal("1000"), Decimal("10"),
        )
        assert result.recommended_stake_usd == Decimal("10")

    def test_decimal_precision_throughout(self):
        result = size_kelly(
            Decimal("0.6"), Decimal("0.5"), Decimal("0.25"), Decimal("10"),
            Decimal("2"), Decimal("1000"),
        )
        assert isinstance(result.recommended_stake_usd, Decimal)
        assert isinstance(result.recommended_units, Decimal)
