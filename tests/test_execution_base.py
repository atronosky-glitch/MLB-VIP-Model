"""Tests for src/execution/base.py's provider interface and normalized types."""

from datetime import datetime, timezone

import pytest

from src.execution.base import (
    BestBidAsk, Balance, HealthCheckResult, Market, Orderbook,
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
