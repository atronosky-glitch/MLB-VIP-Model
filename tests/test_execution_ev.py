"""Tests for src/execution/ev.py. Pure -- hand-built fixtures, no
provider/mocking needed. net_ev_pct's definition is proven against a
hand-computed worked example (point 9's explicit requirement), not just
asserted structurally.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution.base import FeeEstimate
from src.execution.ev import break_even_probability, compute_ev, max_acceptable_price
from src.execution.quotes import ExecutableQuote


def _quote(
    price="0.56", filled="10", gross_cost="5.60", fees="0.20", total_cost="5.80",
) -> ExecutableQuote:
    return ExecutableQuote(
        provider="kalshi", provider_market_id="M1", side="YES",
        desired_contracts=Decimal(filled),
        best_price=Decimal(price), best_quantity=Decimal(filled),
        expected_fill_price=Decimal(price), expected_fill_quantity=Decimal(filled),
        requested_quantity=Decimal(filled), unfilled_quantity=Decimal("0"),
        gross_cost=Decimal(gross_cost), estimated_fees=Decimal(fees),
        expected_total_cost=Decimal(total_cost),
        spread=Decimal("0.02"), spread_pct=Decimal("0.03"),
        slippage_absolute=Decimal("0"), slippage_pct=Decimal("0"),
        liquidity_available=Decimal("1000"),
        orderbook_timestamp=datetime.now(timezone.utc),
    )


def _flat_fee_model(fee_amount="0.05"):
    def fee_model(side, price, quantity):
        return FeeEstimate(fee=Decimal(fee_amount), fee_estimate=True, detail="test")
    return fee_model


class TestComputeEvYes:
    """Hand-computed worked example: model_probability=0.64 (YES),
    quantity=10, gross_cost=5.60, fees=0.20, total_entry_cost=5.80."""

    def test_raw_ev_matches_the_hand_computed_value(self):
        result = compute_ev(Decimal("0.64"), "YES", _quote(), Decimal("1.0"), _flat_fee_model())
        assert result.raw_ev_dollars == Decimal("0.80")
        assert result.raw_ev_pct == Decimal("0.80") / Decimal("5.60") * 100

    def test_net_ev_matches_the_hand_computed_value(self):
        result = compute_ev(Decimal("0.64"), "YES", _quote(), Decimal("1.0"), _flat_fee_model())
        assert result.net_ev_dollars == Decimal("0.60")
        assert result.net_ev_pct == Decimal("0.60") / Decimal("5.80") * 100

    def test_net_ev_pct_uses_total_entry_cost_not_gross_cost_as_denominator(self):
        """The one formula distinction this whole module exists to get
        right: net_ev_pct's denominator is gross_cost + fees, not just
        gross_cost (that's raw_ev_pct's denominator)."""
        result = compute_ev(Decimal("0.64"), "YES", _quote(), Decimal("1.0"), _flat_fee_model())
        assert result.net_ev_pct != result.raw_ev_pct
        assert result.total_entry_cost == Decimal("5.80")

    def test_break_even_probability(self):
        result = compute_ev(Decimal("0.64"), "YES", _quote(), Decimal("1.0"), _flat_fee_model())
        assert result.break_even_probability == Decimal("5.80") / Decimal("10")

    def test_expected_payout_and_profit(self):
        result = compute_ev(Decimal("0.64"), "YES", _quote(), Decimal("1.0"), _flat_fee_model())
        assert result.expected_payout == Decimal("0.64") * Decimal("10")
        assert result.expected_profit == result.expected_payout - Decimal("5.80")


class TestComputeEvNo:
    def test_no_side_transforms_model_probability(self):
        """model_probability is always the YES probability; NO side
        uses (1 - model_probability) as the win probability."""
        quote = _quote(price="0.40", gross_cost="4.00", fees="0.15", total_cost="4.15")
        result = compute_ev(Decimal("0.64"), "NO", quote, Decimal("1.0"), _flat_fee_model())
        q_no = Decimal("1") - Decimal("0.64")
        assert result.expected_payout == q_no * Decimal("10")

    def test_no_side_can_have_negative_ev(self):
        quote = _quote(price="0.40", gross_cost="4.00", fees="0.15", total_cost="4.15")
        result = compute_ev(Decimal("0.64"), "NO", quote, Decimal("1.0"), _flat_fee_model())
        assert result.net_ev_pct < 0


class TestComputeEvSpecWorkedExamples:
    """Item 6's exact named examples: model YES probability 0.65 priced
    at 0.55 on the YES side, and the mirror case (model YES probability
    0.35, i.e. NO win probability 0.65) priced at 0.55 on the NO side --
    both hand-computed independently of the module under test, at
    quantity=1 with zero fees so the arithmetic is checked directly
    against compute_ev's raw formula, not against another fixture."""

    def _zero_fee_model(self, side, price, quantity):
        return FeeEstimate(fee=Decimal("0"), fee_estimate=True, detail="test")

    def _one_contract_quote(self, price: str) -> ExecutableQuote:
        return _quote(price=price, filled="1", gross_cost=price, fees="0", total_cost=price)

    def test_yes_side_model_065_priced_at_055(self):
        # expected_payout = 0.65 * 1 = 0.65; gross_cost = 0.55; fees = 0
        # raw_ev_dollars = 0.65 - 0.55 = 0.10; raw_ev_pct = 0.10/0.55*100 ~= 18.18%
        result = compute_ev(
            Decimal("0.65"), "YES", self._one_contract_quote("0.55"), Decimal("1.0"), self._zero_fee_model,
        )
        assert result.expected_payout == Decimal("0.65")
        assert result.raw_ev_dollars == Decimal("0.10")
        assert result.raw_ev_pct == Decimal("0.10") / Decimal("0.55") * 100
        assert result.net_ev_pct == result.raw_ev_pct  # zero fees -> net equals raw

    def test_no_side_model_yes_035_priced_at_055(self):
        # NO win probability = 1 - 0.35 = 0.65 -- symmetric to the YES
        # case above by construction, proving the side transform is
        # applied correctly rather than accidentally reusing model_probability.
        result = compute_ev(
            Decimal("0.35"), "NO", self._one_contract_quote("0.55"), Decimal("1.0"), self._zero_fee_model,
        )
        assert result.expected_payout == Decimal("0.65")
        assert result.raw_ev_dollars == Decimal("0.10")
        assert result.raw_ev_pct == Decimal("0.10") / Decimal("0.55") * 100

    def test_kalshi_fee_reduces_net_ev_below_raw_ev_on_yes_side(self):
        fee_model = TestMaxAcceptablePrice()._kalshi_like_fee_model
        fee = fee_model("YES", Decimal("0.55"), Decimal("1")).fee
        quote = _quote(
            price="0.55", filled="1", gross_cost="0.55", fees=str(fee), total_cost=str(Decimal("0.55") + fee),
        )
        result = compute_ev(Decimal("0.65"), "YES", quote, Decimal("1.0"), fee_model)
        assert result.net_ev_pct < result.raw_ev_pct

    def test_kalshi_fee_reduces_net_ev_below_raw_ev_on_no_side(self):
        fee_model = TestMaxAcceptablePrice()._kalshi_like_fee_model
        fee = fee_model("NO", Decimal("0.55"), Decimal("1")).fee
        quote = _quote(
            price="0.55", filled="1", gross_cost="0.55", fees=str(fee), total_cost=str(Decimal("0.55") + fee),
        )
        result = compute_ev(Decimal("0.35"), "NO", quote, Decimal("1.0"), fee_model)
        assert result.net_ev_pct < result.raw_ev_pct


class TestComputeEvValidation:
    def test_rejects_invalid_side(self):
        with pytest.raises(ValueError):
            compute_ev(Decimal("0.5"), "MAYBE", _quote(), Decimal("1.0"), _flat_fee_model())

    def test_rejects_zero_filled_quantity(self):
        quote = _quote(filled="0", gross_cost="0", fees="0", total_cost="0")
        with pytest.raises(ValueError):
            compute_ev(Decimal("0.5"), "YES", quote, Decimal("1.0"), _flat_fee_model())


class TestBreakEvenProbability:
    def test_matches_total_entry_cost_over_quantity(self):
        assert break_even_probability(Decimal("5.80"), Decimal("10")) == Decimal("0.58")

    def test_rejects_non_positive_quantity(self):
        with pytest.raises(ValueError):
            break_even_probability(Decimal("5"), Decimal("0"))


class TestMaxAcceptablePrice:
    """Verified independently (see Stage 2B plan): model probability
    64%, min net EV 5%, Kalshi's 7% fee coefficient converges to ~59
    cents -- below the 64% fair probability, and below the no-fee
    approximation of ~61 cents since fees tighten the ceiling."""

    def _kalshi_like_fee_model(self, side, price, quantity):
        fee = Decimal("0.07") * quantity * price * (Decimal("1") - price)
        return FeeEstimate(fee=fee, fee_estimate=True, detail="test")

    def test_converges_near_the_independently_verified_value(self):
        result = max_acceptable_price(
            Decimal("0.64"), Decimal("5.0"), self._kalshi_like_fee_model, Decimal("1"), "YES",
        )
        assert Decimal("0.58") <= result <= Decimal("0.60")

    def test_price_just_below_result_still_qualifies(self):
        max_price = max_acceptable_price(
            Decimal("0.64"), Decimal("5.0"), self._kalshi_like_fee_model, Decimal("1"), "YES",
        )
        fee = self._kalshi_like_fee_model("YES", max_price, Decimal("1")).fee
        cost = max_price * Decimal("1") + fee
        profit = Decimal("0.64") * Decimal("1") - cost
        net_ev_pct = profit / cost * 100
        assert net_ev_pct >= Decimal("5.0")

    def test_price_one_cent_above_result_rejects(self):
        max_price = max_acceptable_price(
            Decimal("0.64"), Decimal("5.0"), self._kalshi_like_fee_model, Decimal("1"), "YES",
        )
        above = max_price + Decimal("0.01")
        fee = self._kalshi_like_fee_model("YES", above, Decimal("1")).fee
        cost = above * Decimal("1") + fee
        profit = Decimal("0.64") * Decimal("1") - cost
        net_ev_pct = profit / cost * 100
        assert net_ev_pct < Decimal("5.0")

    def test_returns_zero_when_no_price_can_qualify(self):
        """Model probability lower than what even the cheapest price
        (1 cent) could satisfy for an unreasonably high EV target."""
        result = max_acceptable_price(
            Decimal("0.02"), Decimal("500.0"), self._kalshi_like_fee_model, Decimal("1"), "YES",
        )
        assert result == Decimal("0.00")

    def test_result_is_a_whole_cent(self):
        result = max_acceptable_price(
            Decimal("0.64"), Decimal("5.0"), self._kalshi_like_fee_model, Decimal("1"), "YES",
        )
        assert (result * 100) == (result * 100).to_integral_value()
