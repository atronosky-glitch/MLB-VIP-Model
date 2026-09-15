"""Regression tests against REAL (sanitized) Polymarket US API
responses captured 2026-09-14 -- see tests/fixture_data_polymarket_us.py
for the full provenance. These confirm the existing parser/normalizer
implementation against actual live data, not just hand-built synthetic
fixtures, and are what finally proved the NO-side book structure (see
src/execution/polymarket_us.py::normalize_orderbook's docstring).
"""

from decimal import Decimal
from unittest import mock

from src.execution.base import Orderbook
from src.execution.polymarket_us import PolymarketUSProvider
from tests.fixture_data_polymarket_us import (
    REAL_MARKET_HOUSE_DEM, REAL_MARKET_SENATE_REP, REAL_ORDERBOOK_HOUSE_DEM, REAL_ORDERBOOK_SENATE_REP,
)


def _provider() -> PolymarketUSProvider:
    provider = object.__new__(PolymarketUSProvider)
    provider._api_key_id = "test"
    provider.session = mock.Mock()
    return provider


class TestRealMarketParsing:
    def test_get_markets_parses_the_real_house_market_correctly(self):
        provider = _provider()
        provider._public_get = mock.Mock(return_value={"markets": [REAL_MARKET_HOUSE_DEM]})
        markets = provider.get_markets()
        assert len(markets) == 1
        market = markets[0]
        assert market.id == "paccc-usho-midterms-2026-11-03-dem"
        assert market.title == "U.S House Midterm Winner"
        assert market.status == "active"  # active=True, closed=False

    def test_get_market_parses_the_real_senate_market_correctly(self):
        provider = _provider()
        provider._public_get = mock.Mock(return_value={"market": REAL_MARKET_SENATE_REP})
        market = provider.get_market("paccc-usse-midterms-2026-11-03-rep")
        assert market.id == "paccc-usse-midterms-2026-11-03-rep"
        assert market.title == "U.S Senate Midterm Winner"

    def test_real_market_outcomes_are_yes_no(self):
        assert REAL_MARKET_HOUSE_DEM["outcomes"] == ["Yes", "No"]
        assert REAL_MARKET_SENATE_REP["outcomes"] == ["No", "Yes"]

    def test_both_marketsides_share_the_market_slug_as_identifier(self):
        """The core evidence for the single-shared-book conclusion --
        confirmed directly against the real payload shape, not asserted
        from memory."""
        for market in (REAL_MARKET_HOUSE_DEM, REAL_MARKET_SENATE_REP):
            identifiers = {side["identifier"] for side in market["marketSides"]}
            assert identifiers == {market["slug"]}

    def test_long_and_short_prices_sum_to_exactly_one(self):
        for orderbook in (REAL_ORDERBOOK_HOUSE_DEM, REAL_ORDERBOOK_SENATE_REP):
            sample = orderbook["marketData"]["stats"]["lastPriceSample"]
            long_px = Decimal(sample["longPx"]["value"])
            short_px = Decimal(sample["shortPx"]["value"])
            assert long_px + short_px == Decimal("1.000") or long_px + short_px == Decimal("1")


class TestRealOrderbookParsing:
    def test_get_orderbook_parses_real_bids_and_offers(self):
        provider = _provider()
        provider._public_get = mock.Mock(return_value=REAL_ORDERBOOK_HOUSE_DEM)
        book = provider.get_orderbook("paccc-usho-midterms-2026-11-03-dem")
        assert book.bids[0] == (0.8530, 3625.0)
        assert book.asks[0] == (0.8540, 7203.0)

    def test_best_ask_is_in_the_yes_long_price_neighborhood_not_the_no_side(self):
        """Confirms bids/offers represent the YES ('long') side
        specifically, not the complementary NO ('short') side -- the
        exact fact the NO-side unblock rests on. A ~1-tick gap between
        the book's best ask and the last-trade longPx sample is normal
        market movement, not a parsing error -- what matters is that
        it's nowhere near shortPx (0.147), which it would be if bids/
        offers actually represented the NO side."""
        provider = _provider()
        provider._public_get = mock.Mock(return_value=REAL_ORDERBOOK_HOUSE_DEM)
        book = provider.get_orderbook("paccc-usho-midterms-2026-11-03-dem")
        stats = REAL_ORDERBOOK_HOUSE_DEM["marketData"]["stats"]["lastPriceSample"]
        long_px = float(stats["longPx"]["value"])
        short_px = float(stats["shortPx"]["value"])
        assert abs(book.asks[0][0] - long_px) < 0.01
        assert abs(book.asks[0][0] - short_px) > 0.5

    def test_normalize_orderbook_synthesizes_no_side_correctly(self):
        provider = _provider()
        provider._public_get = mock.Mock(return_value=REAL_ORDERBOOK_HOUSE_DEM)
        raw = provider.get_orderbook("paccc-usho-midterms-2026-11-03-dem")
        normalized = provider.normalize_orderbook(raw)

        assert normalized.yes_asks[0].price == Decimal("0.8540")
        assert normalized.yes_bids[0].price == Decimal("0.8530")
        # NO synthesized via 1-P: yes_ask 0.8540 -> no_bid 0.1460;
        # yes_bid 0.8530 -> no_ask 0.1470.
        assert normalized.no_bids[0].price == Decimal("1") - Decimal("0.8540")
        assert normalized.no_asks[0].price == Decimal("1") - Decimal("0.8530")

    def test_yes_ask_plus_no_bid_equals_one_exactly_on_real_data(self):
        """Same sanity check Kalshi's own normalize_orderbook test
        performs -- now proven true for Polymarket US too."""
        provider = _provider()
        provider._public_get = mock.Mock(return_value=REAL_ORDERBOOK_HOUSE_DEM)
        raw = provider.get_orderbook("paccc-usho-midterms-2026-11-03-dem")
        normalized = provider.normalize_orderbook(raw)
        assert normalized.yes_asks[0].price + normalized.no_bids[0].price == Decimal("1")

    def test_second_real_market_orderbook_also_parses_correctly(self):
        provider = _provider()
        provider._public_get = mock.Mock(return_value=REAL_ORDERBOOK_SENATE_REP)
        book = provider.get_orderbook("paccc-usse-midterms-2026-11-03-rep")
        assert book.bids[0] == (0.4820, 1000.0)
        assert book.asks[0] == (0.4830, 500.0)
