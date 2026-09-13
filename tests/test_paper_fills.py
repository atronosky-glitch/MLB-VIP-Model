"""Tests for src/execution/paper/fills.py. Pure -- hand-built order
book fixtures, no DB/network."""

from decimal import Decimal

from src.execution.base import FeeEstimate, OrderLevel
from src.execution.paper.fills import simulate_paper_fill
from src.execution.paper.models import PaperRejectionReason


def _levels(*pairs) -> list[OrderLevel]:
    return [OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in pairs]


def _zero_fee(side, price, quantity):
    return FeeEstimate(fee=Decimal("0"), fee_estimate=True, detail="test")


def _flat_fee(amount):
    def fee_model(side, price, quantity):
        return FeeEstimate(fee=Decimal(amount), fee_estimate=True, detail="test")
    return fee_model


class TestSuccessfulFill:
    def test_fills_whole_contracts_within_budget(self):
        asks = _levels((0.50, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.99"), _zero_fee, False)
        assert outcome.status == "FILLED"
        assert outcome.fill.quantity_filled == Decimal("20")  # $10 / $0.50
        assert outcome.fill.average_fill_price == Decimal("0.50")

    def test_gross_cost_and_total_cost_include_fees(self):
        asks = _levels((0.50, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.99"), _flat_fee("0.25"), False)
        assert outcome.fill.gross_cost == Decimal("10.00")
        assert outcome.fill.fees == Decimal("0.25")
        assert outcome.fill.total_cost == Decimal("10.25")

    def test_vwap_across_multiple_levels(self):
        asks = _levels((0.50, 5), (0.60, 20))
        outcome = simulate_paper_fill(asks, "YES", Decimal("5.50"), Decimal("0.99"), _zero_fee, False)
        # $5.50 buys 5@0.50 ($2.50) + up to $3.00/$0.60=5 more @0.60 -> 10 contracts, cost=$5.50
        assert outcome.fill.quantity_filled == Decimal("10")
        assert outcome.fill.gross_cost == Decimal("5.50")


class TestPriceCeiling:
    def test_book_liquidity_exists_but_only_above_ceiling(self):
        asks = _levels((0.80, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.60"), _zero_fee, False)
        assert outcome.status == "REJECTED"
        assert outcome.rejection_reason == PaperRejectionReason.PRICE_MOVED_BEYOND_LIMIT

    def test_only_cheaper_levels_within_ceiling_are_used(self):
        # $100 could buy far more than 5 contracts if the 0.90 level
        # were usable, but it's excluded by the 0.60 ceiling -- with
        # partial fills allowed, only the 5 contracts at 0.50 fill.
        asks = _levels((0.50, 5), (0.90, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("100"), Decimal("0.60"), _zero_fee, allow_partial_fills=True)
        assert outcome.status == "PARTIALLY_FILLED"
        assert outcome.fill.quantity_filled == Decimal("5")
        assert outcome.fill.average_fill_price == Decimal("0.50")

    def test_price_ceiling_shortfall_rejected_when_partial_fills_disabled(self):
        asks = _levels((0.50, 5), (0.90, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("100"), Decimal("0.60"), _zero_fee, allow_partial_fills=False)
        assert outcome.status == "REJECTED"
        assert outcome.rejection_reason == PaperRejectionReason.PRICE_MOVED_BEYOND_LIMIT

    def test_full_budget_deployed_across_levels_is_filled_not_partial(self):
        """A fill that spends the ENTIRE budget across multiple price
        levels is a normal FILLED outcome -- not mistaken for a partial
        fill just because it wasn't all at the best price."""
        asks = _levels((0.50, 5), (0.60, 20))
        outcome = simulate_paper_fill(asks, "YES", Decimal("5.50"), Decimal("0.99"), _zero_fee, allow_partial_fills=False)
        assert outcome.status == "FILLED"
        assert outcome.fill.quantity_filled == Decimal("10")
        assert outcome.fill.gross_cost == Decimal("5.50")


class TestInsufficientLiquidity:
    def test_empty_asks_rejected(self):
        outcome = simulate_paper_fill([], "YES", Decimal("10"), Decimal("0.99"), _zero_fee, False)
        assert outcome.status == "REJECTED"
        assert outcome.rejection_reason == PaperRejectionReason.INSUFFICIENT_LIQUIDITY

    def test_budget_cannot_buy_even_one_contract(self):
        asks = _levels((0.99, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("0.50"), Decimal("0.99"), _zero_fee, False)
        assert outcome.status == "REJECTED"
        assert outcome.rejection_reason == PaperRejectionReason.INSUFFICIENT_LIQUIDITY

    def test_book_has_less_liquidity_than_budget_needs_even_uncapped(self):
        asks = _levels((0.50, 3))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.99"), _zero_fee, False)
        # only 3 contracts exist at all, budget could buy 20 -- partial,
        # and since even the UNCAPPED book can't fill more, this is a
        # liquidity shortfall, not a price-ceiling shortfall.
        assert outcome.status == "REJECTED"
        assert outcome.rejection_reason == PaperRejectionReason.INSUFFICIENT_LIQUIDITY


class TestPartialFills:
    def test_disabled_by_default_rejects_instead_of_partial(self):
        asks = _levels((0.50, 3))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.99"), _zero_fee, allow_partial_fills=False)
        assert outcome.status == "REJECTED"

    def test_enabled_returns_partially_filled(self):
        asks = _levels((0.50, 3))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0.99"), _zero_fee, allow_partial_fills=True)
        assert outcome.status == "PARTIALLY_FILLED"
        assert outcome.fill.quantity_filled == Decimal("3")


class TestSideParameterUsedForFees:
    def test_no_side_is_passed_through_to_fee_model(self):
        captured = {}

        def fee_model(side, price, quantity):
            captured["side"] = side
            return FeeEstimate(fee=Decimal("0"), fee_estimate=True, detail="test")

        asks = _levels((0.30, 100))
        simulate_paper_fill(asks, "NO", Decimal("10"), Decimal("0.99"), fee_model, False)
        assert captured["side"] == "NO"


class TestDegenerateInputs:
    def test_non_positive_budget_rejected(self):
        asks = _levels((0.50, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("0"), Decimal("0.99"), _zero_fee, False)
        assert outcome.status == "REJECTED"

    def test_non_positive_ceiling_rejected(self):
        asks = _levels((0.50, 100))
        outcome = simulate_paper_fill(asks, "YES", Decimal("10"), Decimal("0"), _zero_fee, False)
        assert outcome.status == "REJECTED"
