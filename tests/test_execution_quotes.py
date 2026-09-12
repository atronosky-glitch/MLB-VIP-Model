"""Tests for src/execution/quotes.py. Pure -- hand-built fixtures, no
provider/mocking needed."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution.base import FeeEstimate, NormalizedOrderBook, OrderLevel
from src.execution.quotes import build_executable_quote


def _levels(*pairs) -> list[OrderLevel]:
    return [OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in pairs]


def _book(**kwargs) -> NormalizedOrderBook:
    defaults = dict(
        market_id="M1",
        yes_bids=_levels((0.54, 100)),
        yes_asks=_levels((0.56, 100)),
        no_bids=_levels((0.44, 100)),
        no_asks=_levels((0.46, 100)),
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return NormalizedOrderBook(**defaults)


def _fee(amount="0.10") -> FeeEstimate:
    return FeeEstimate(fee=Decimal(amount), fee_estimate=True, detail="test")


class TestBuildExecutableQuote:
    def test_yes_side_uses_yes_bids_and_asks(self):
        quote = build_executable_quote("kalshi", "M1", _book(), "YES", Decimal("10"), _fee())
        assert quote.best_price == Decimal("0.56")
        assert quote.expected_fill_price == Decimal("0.56")

    def test_no_side_uses_no_bids_and_asks(self):
        quote = build_executable_quote("kalshi", "M1", _book(), "NO", Decimal("10"), _fee())
        assert quote.best_price == Decimal("0.46")

    def test_spread_and_spread_pct(self):
        quote = build_executable_quote("kalshi", "M1", _book(), "YES", Decimal("10"), _fee())
        assert quote.spread == Decimal("0.56") - Decimal("0.54")
        mid = (Decimal("0.56") + Decimal("0.54")) / 2
        assert quote.spread_pct == quote.spread / mid

    def test_expected_total_cost_includes_fees(self):
        quote = build_executable_quote("kalshi", "M1", _book(), "YES", Decimal("10"), _fee("0.25"))
        assert quote.gross_cost == Decimal("5.60")  # 10 * 0.56
        assert quote.estimated_fees == Decimal("0.25")
        assert quote.expected_total_cost == Decimal("5.85")

    def test_unfilled_quantity_when_book_is_thin(self):
        thin_book = _book(yes_asks=_levels((0.56, 5)))
        quote = build_executable_quote("kalshi", "M1", thin_book, "YES", Decimal("10"), _fee())
        assert quote.expected_fill_quantity == Decimal("5")
        assert quote.unfilled_quantity == Decimal("5")

    def test_liquidity_available_is_total_ask_side_depth_not_just_filled(self):
        book = _book(yes_asks=_levels((0.56, 5), (0.58, 20)))
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("5"), _fee())
        # only 5 were needed/filled, but liquidity_available reflects the whole ask side
        expected_liquidity = Decimal("5") * Decimal("0.56") + Decimal("20") * Decimal("0.58")
        assert quote.liquidity_available == expected_liquidity

    def test_slippage_fields_come_from_the_walk(self):
        book = _book(yes_asks=_levels((0.54, 5), (0.60, 5)))
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.slippage_absolute is not None
        assert quote.slippage_absolute > Decimal("0")

    def test_rejects_invalid_side(self):
        with pytest.raises(ValueError):
            build_executable_quote("kalshi", "M1", _book(), "MAYBE", Decimal("10"), _fee())

    def test_empty_bids_side_gives_none_spread(self):
        book = _book(yes_bids=[])
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.spread is None
        assert quote.spread_pct is None

    def test_desired_and_requested_quantity_match_the_input(self):
        quote = build_executable_quote("kalshi", "M1", _book(), "YES", Decimal("7"), _fee())
        assert quote.desired_contracts == Decimal("7")
        assert quote.requested_quantity == Decimal("7")

    def test_orderbook_timestamp_is_carried_through(self):
        ts = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        book = _book(timestamp=ts)
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("1"), _fee())
        assert quote.orderbook_timestamp == ts


class TestSpreadEdgeCases:
    """Item 10: spread's exact formula (best_ask - best_bid, spread_pct =
    spread / midpoint) documented and tested at each boundary a real book
    can present -- not just the one already-tight-book happy path above."""

    def test_zero_spread_when_bid_equals_ask(self):
        book = _book(yes_bids=_levels((0.55, 100)), yes_asks=_levels((0.55, 100)))
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.spread == Decimal("0")
        assert quote.spread_pct == Decimal("0")

    def test_no_bid_at_all_gives_none_spread(self):
        book = _book(yes_bids=[])
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.spread is None
        assert quote.spread_pct is None
        assert quote.best_price == Decimal("0.56")  # ask side is independent of bid presence

    def test_no_ask_at_all_gives_none_spread_and_none_best_price(self):
        book = _book(yes_asks=[])
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.spread is None
        assert quote.spread_pct is None
        assert quote.best_price is None

    def test_zero_midpoint_does_not_raise_a_division_error(self):
        # A zero-priced bid and ask is not realistic on either exchange
        # (both use $0.01-$0.99 contract pricing), but the formula must
        # not raise ZeroDivisionError if it's ever fed one -- spread_pct
        # is simply undefined (None) rather than computed against a
        # zero denominator.
        book = _book(yes_bids=_levels((0.0, 100)), yes_asks=_levels((0.0, 100)))
        quote = build_executable_quote("kalshi", "M1", book, "YES", Decimal("10"), _fee())
        assert quote.spread == Decimal("0")
        assert quote.spread_pct is None
