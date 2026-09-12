"""Tests for src/execution/base.py's provider interface and normalized types."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution.base import (
    BestBidAsk, Balance, FeeEstimate, HealthCheckResult, Market,
    NormalizedOrderBook, Orderbook, OrderLevel,
    PredictionMarketProvider, mask_secret,
)


class _DummyProvider(PredictionMarketProvider):
    """Minimal concrete subclass implementing only the abstract methods."""

    name = "dummy"

    def get_balance(self):
        return Balance(currency="USD", available=0.0)

    def get_markets(self, **filters):
        return []

    def get_market(self, market_id):
        return Market(id=market_id, title="", status="")

    def get_orderbook(self, market_id):
        return Orderbook(market_id=market_id, bids=[], asks=[])

    def get_best_bid_ask(self, market_id):
        return BestBidAsk(market_id=market_id, best_bid=None, best_ask=None)

    def health_check(self):
        return HealthCheckResult(provider=self.name, ok=True, detail="", checked_at=datetime.now(timezone.utc))

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id=raw.market_id, yes_bids=[], yes_asks=[], no_bids=[], no_asks=[],
            timestamp=datetime.now(timezone.utc),
        )

    def estimate_fees(self, side, price, quantity):
        return FeeEstimate(fee=Decimal("0"), fee_estimate=True, detail="dummy")


class TestPredictionMarketProviderABC:
    def test_cannot_instantiate_the_bare_abc(self):
        with pytest.raises(TypeError):
            PredictionMarketProvider()

    def test_a_subclass_missing_an_abstract_method_cannot_be_instantiated(self):
        class Incomplete(PredictionMarketProvider):
            name = "incomplete"
            # missing every abstract method

        with pytest.raises(TypeError):
            Incomplete()

    def test_place_order_is_not_implemented_in_this_stage(self):
        provider = _DummyProvider()
        with pytest.raises(NotImplementedError):
            provider.place_order()

    def test_cancel_order_is_not_implemented_in_this_stage(self):
        provider = _DummyProvider()
        with pytest.raises(NotImplementedError):
            provider.cancel_order()

    def test_supports_live_execution_defaults_to_false(self):
        provider = _DummyProvider()
        assert provider.supports_live_execution is False


class TestNormalizedDataclasses:
    def test_market_round_trips_raw_payload(self):
        m = Market(id="TICKER-1", title="Will X happen?", status="open", raw={"foo": "bar"})
        assert m.raw == {"foo": "bar"}
        assert m.id == "TICKER-1"

    def test_orderbook_round_trips_bids_and_asks(self):
        ob = Orderbook(market_id="M1", bids=[(0.55, 100.0)], asks=[(0.60, 50.0)], raw={})
        assert ob.bids == [(0.55, 100.0)]
        assert ob.asks == [(0.60, 50.0)]

    def test_health_check_result_carries_a_timestamp(self):
        now = datetime.now(timezone.utc)
        result = HealthCheckResult(provider="kalshi", ok=True, detail="ok", checked_at=now)
        assert result.checked_at == now

    def test_order_level_unpacks_like_a_tuple_but_has_named_fields(self):
        level = OrderLevel(price=Decimal("0.55"), quantity=Decimal("100"))
        price, quantity = level
        assert price == Decimal("0.55")
        assert level.price == Decimal("0.55")
        assert level.quantity == Decimal("100")

    def test_normalized_order_book_round_trips_all_four_sides(self):
        now = datetime.now(timezone.utc)
        book = NormalizedOrderBook(
            market_id="M1",
            yes_bids=[OrderLevel(Decimal("0.54"), Decimal("10"))],
            yes_asks=[OrderLevel(Decimal("0.56"), Decimal("20"))],
            no_bids=[OrderLevel(Decimal("0.44"), Decimal("30"))],
            no_asks=[OrderLevel(Decimal("0.46"), Decimal("40"))],
            timestamp=now,
        )
        assert book.yes_bids[0].price == Decimal("0.54")
        assert book.no_asks[0].quantity == Decimal("40")

    def test_fee_estimate_flags_when_the_fee_is_approximate(self):
        fee = FeeEstimate(fee=Decimal("0.14"), fee_estimate=True, detail="taker fee, conservative")
        assert fee.fee_estimate is True
        assert fee.fee == Decimal("0.14")


class TestMaskSecret:
    def test_empty_value_is_unset(self):
        assert mask_secret("") == "<unset>"

    def test_short_value_shows_length_not_content(self):
        masked = mask_secret("abc")
        assert "abc" not in masked
        assert "3 chars" in masked

    def test_long_value_shows_only_prefix_and_suffix(self):
        masked = mask_secret("supersecretkeyid12345")
        assert masked.startswith("supe")
        assert masked.endswith("45")
        assert "secretkeyid" not in masked
