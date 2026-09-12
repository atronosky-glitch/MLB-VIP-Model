"""Tests for src/execution/orderbook_math.py. Pure -- no DB, no network."""

from decimal import Decimal

import pytest

from src.execution.base import OrderLevel
from src.execution.orderbook_math import max_quantity_for_budget, walk_book


def _levels(*pairs) -> list[OrderLevel]:
    return [OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in pairs]


class TestWalkBook:
    def test_full_fill_single_level(self):
        result = walk_book(_levels((0.54, 10)), Decimal("10"))
        assert result.quantity_filled == Decimal("10")
        assert result.quantity_unfilled == Decimal("0")
        assert result.vwap == Decimal("0.54")
        assert result.gross_cost == Decimal("5.40")

    def test_partial_fill_across_multiple_levels(self):
        """User's own worked example: asks 10@.54, 20@.55, 50@.58;
        requested 30 -> fills 10@.54 + 20@.55, VWAP = (10*.54+20*.55)/30."""
        levels = _levels((0.54, 10), (0.55, 20), (0.58, 50))
        result = walk_book(levels, Decimal("30"))
        assert result.quantity_filled == Decimal("30")
        assert result.quantity_unfilled == Decimal("0")
        expected_cost = Decimal("10") * Decimal("0.54") + Decimal("20") * Decimal("0.55")
        assert result.gross_cost == expected_cost
        assert result.vwap == expected_cost / Decimal("30")

    def test_zero_liquidity_is_fully_unfilled_not_an_error(self):
        result = walk_book([], Decimal("10"))
        assert result.quantity_filled == Decimal("0")
        assert result.quantity_unfilled == Decimal("10")
        assert result.best_price is None
        assert result.vwap is None
        assert result.gross_cost == Decimal("0")

    def test_single_level_partial_fill(self):
        result = walk_book(_levels((0.50, 5)), Decimal("10"))
        assert result.quantity_filled == Decimal("5")
        assert result.quantity_unfilled == Decimal("5")

    def test_huge_requested_quantity_leaves_remainder_unfilled(self):
        levels = _levels((0.54, 10), (0.55, 20))
        result = walk_book(levels, Decimal("1000000"))
        assert result.quantity_filled == Decimal("30")
        assert result.quantity_unfilled == Decimal("999970")

    def test_best_price_is_the_first_levels_price_even_if_unfilled(self):
        result = walk_book(_levels((0.54, 1)), Decimal("100"))
        assert result.best_price == Decimal("0.54")

    def test_price_impact_and_slippage_pct(self):
        levels = _levels((0.50, 10), (0.60, 10))
        result = walk_book(levels, Decimal("20"))
        expected_vwap = (Decimal("10") * Decimal("0.50") + Decimal("10") * Decimal("0.60")) / Decimal("20")
        assert result.vwap == expected_vwap
        assert result.price_impact == expected_vwap - Decimal("0.50")
        assert result.slippage_pct == (expected_vwap - Decimal("0.50")) / Decimal("0.50")

    def test_no_slippage_when_fully_filled_at_best_price(self):
        result = walk_book(_levels((0.50, 100)), Decimal("10"))
        assert result.price_impact == Decimal("0")
        assert result.slippage_pct == Decimal("0")

    def test_decimal_precision_no_binary_float_drift(self):
        levels = _levels((0.1, 10), (0.2, 10))
        result = walk_book(levels, Decimal("20"))
        # If this were float math, 0.1+0.2 style drift could appear.
        assert result.gross_cost == Decimal("1") + Decimal("2")
        assert isinstance(result.gross_cost, Decimal)

    def test_rejects_non_positive_requested_quantity(self):
        with pytest.raises(ValueError):
            walk_book(_levels((0.5, 10)), Decimal("0"))
        with pytest.raises(ValueError):
            walk_book(_levels((0.5, 10)), Decimal("-5"))


class TestMaxQuantityForBudget:
    def test_budget_covers_multiple_levels_exactly(self):
        levels = _levels((0.50, 10), (0.60, 10))
        # 10*0.50 + 10*0.60 = 11.00
        qty = max_quantity_for_budget(levels, Decimal("11.00"))
        assert qty == Decimal("20")

    def test_budget_partially_covers_a_level(self):
        levels = _levels((0.50, 100))
        qty = max_quantity_for_budget(levels, Decimal("10"))
        assert qty == Decimal("20")  # $10 / $0.50 = 20 contracts

    def test_zero_budget_returns_zero(self):
        assert max_quantity_for_budget(_levels((0.5, 10)), Decimal("0")) == Decimal("0")

    def test_empty_book_returns_zero(self):
        assert max_quantity_for_budget([], Decimal("10")) == Decimal("0")

    def test_budget_exceeds_entire_book(self):
        levels = _levels((0.50, 10))
        qty = max_quantity_for_budget(levels, Decimal("1000"))
        assert qty == Decimal("10")  # can't buy more than exists
