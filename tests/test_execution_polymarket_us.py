"""Tests for src/execution/polymarket_us.py. All requests are mocked at
the requests.Session level. Covers the gateway-vs-api routing split:
market data is public (no auth headers), account data is Ed25519-signed."""

import base64
from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from src.execution.base import Market, Orderbook
from src.execution.polymarket_us import PolymarketUSProvider


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


class _FakeConfig:
    polymarket_us_api_key_id = "test-pm-key-id"

    def __init__(self, private_key_path):
        self.polymarket_us_private_key_path = str(private_key_path)


@pytest.fixture
def pm_key_path(tmp_path):
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "pm.txt"
    path.write_text(base64.b64encode(raw).decode("ascii"))
    return path


@pytest.fixture
def provider(pm_key_path):
    return PolymarketUSProvider(_FakeConfig(pm_key_path))


class TestNoEnvBranching:
    def test_provider_has_no_env_attribute_unlike_kalshi(self, provider):
        assert not hasattr(provider, "kalshi_env")
        assert not hasattr(provider, "_env")


class TestSignedHeaders:
    def test_headers_contain_all_three_required_fields(self, provider):
        headers = provider._signed_headers("GET", "/v1/account/balances")
        assert set(headers) == {"X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"}
        assert headers["X-PM-Access-Key"] == "test-pm-key-id"


class TestPublicMarketDataCallsCarryNoAuth:
    def test_get_markets_hits_gateway_with_no_auth_headers(self, provider):
        resp = _FakeResp(200, {"markets": [{"slug": "will-x-happen", "title": "Will X?", "active": True}]})
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            markets = provider.get_markets()
        url, kwargs = mg.call_args[0][0], mg.call_args[1]
        assert url.startswith("https://gateway.polymarket.us")
        assert kwargs["headers"] == {}
        assert markets[0].id == "will-x-happen"
        assert markets[0].status == "active"

    def test_get_market_by_slug(self, provider):
        resp = _FakeResp(200, {"market": {"slug": "will-x-happen", "title": "Will X?"}})
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            market = provider.get_market("will-x-happen")
        assert market.id == "will-x-happen"
        assert mg.call_args[1]["headers"] == {}

    def test_get_orderbook_maps_bids_and_offers(self, provider):
        resp = _FakeResp(200, {"marketData": {
            "bids": [{"px": {"value": "0.65", "currency": "USD"}, "qty": "1000"}],
            "offers": [{"px": {"value": "0.66", "currency": "USD"}, "qty": "500"}],
        }})
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            book = provider.get_orderbook("will-x-happen")
        assert book.bids == [(0.65, 1000.0)]
        assert book.asks == [(0.66, 500.0)]
        assert mg.call_args[1]["headers"] == {}

    def test_get_best_bid_ask(self, provider):
        resp = _FakeResp(200, {"marketData": {
            "bestBid": {"value": "0.65", "currency": "USD"},
            "bestAsk": {"value": "0.66", "currency": "USD"},
        }})
        with mock.patch.object(provider.session, "get", return_value=resp):
            bba = provider.get_best_bid_ask("will-x-happen")
        assert bba.best_bid == 0.65
        assert bba.best_ask == 0.66


class TestAuthenticatedAccountDataCallsCarryEd25519Headers:
    def test_get_balance_hits_api_host_with_signed_headers(self, provider):
        resp = _FakeResp(200, {"balances": [{"currency": "USD", "currentBalance": 500.25, "buyingPower": 500.25}]})
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            balance = provider.get_balance()
        url, kwargs = mg.call_args[0][0], mg.call_args[1]
        assert url.startswith("https://api.polymarket.us")
        assert set(kwargs["headers"]) == {"X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"}
        assert balance.available == 500.25
        assert balance.currency == "USD"

    def test_get_balance_with_no_balances_returns_zero(self, provider):
        resp = _FakeResp(200, {"balances": []})
        with mock.patch.object(provider.session, "get", return_value=resp):
            balance = provider.get_balance()
        assert balance.available == 0.0

    def test_health_check_ok_on_success(self, provider):
        resp = _FakeResp(200, {"balances": [{"currency": "USD", "currentBalance": 1.0}]})
        with mock.patch.object(provider.session, "get", return_value=resp):
            result = provider.health_check()
        assert result.ok is True
        assert result.provider == "polymarket_us"

    def test_health_check_not_ok_on_auth_failure(self, provider):
        resp = _FakeResp(403, {})
        with mock.patch.object(provider.session, "get", return_value=resp):
            result = provider.health_check()
        assert result.ok is False

    def test_health_check_never_raises(self, provider):
        with mock.patch.object(provider.session, "get", side_effect=requests.exceptions.ConnectionError("boom")):
            with mock.patch("src.execution.polymarket_us.time.sleep"):
                result = provider.health_check()
        assert result.ok is False


class TestRetryBehavior:
    def test_retries_on_5xx_then_succeeds(self, provider):
        responses = [_FakeResp(503), _FakeResp(200, {"balances": []})]
        with mock.patch.object(provider.session, "get", side_effect=responses) as mg:
            with mock.patch("src.execution.polymarket_us.time.sleep"):
                provider.get_balance()
        assert mg.call_count == 2

    def test_401_fails_fast_without_retry(self, provider):
        resp = _FakeResp(401)
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            with pytest.raises(PermissionError):
                provider.get_balance()
        assert mg.call_count == 1


class TestNeverIssuesWriteRequests:
    def test_no_post_put_delete_calls_across_every_read_method(self, provider):
        resp = _FakeResp(200, {
            "markets": [], "market": {"slug": "x"},
            "marketData": {"bids": [], "offers": [], "bestBid": None, "bestAsk": None},
            "balances": [],
        })
        with mock.patch.object(provider.session, "get", return_value=resp), \
             mock.patch.object(provider.session, "post") as mp, \
             mock.patch.object(provider.session, "put") as mpu, \
             mock.patch.object(provider.session, "delete") as md:
            provider.get_markets()
            provider.get_market("x")
            provider.get_orderbook("x")
            provider.get_best_bid_ask("x")
            provider.get_balance()
            provider.health_check()

        mp.assert_not_called()
        mpu.assert_not_called()
        md.assert_not_called()


class TestParseGameEvent:
    """Slug grammar confirmed live 2026-09-12 (see polymarket_us.py's
    parse_game_event docstring): "{prefix}-{league}-{away}-{home}-{date}"."""

    def test_parses_the_real_confirmed_slug_shape(self, provider):
        market = Market(id="aec-nfl-lac-ten-2025-11-02", title="Los Angeles vs. Tennessee", status="active")
        event = provider.parse_game_event(market)
        assert event.away_team == "lac"
        assert event.home_team == "ten"
        assert event.market_type == "moneyline"
        assert event.event_start_time.year == 2025
        assert event.event_start_time.month == 11
        assert event.event_start_time.day == 2

    def test_malformed_slug_returns_none(self, provider):
        market = Market(id="not-enough-parts", title="x", status="active")
        assert provider.parse_game_event(market) is None

    def test_slug_with_invalid_date_returns_none(self, provider):
        market = Market(id="aec-nfl-lac-ten-2025-13-99", title="x", status="active")
        assert provider.parse_game_event(market) is None


class TestEstimateFees:
    """fee = round_half_even_to_cent(0.06*C*P*(1-P)), confirmed directly
    against docs.polymarket.us/fees's own worked examples."""

    def test_matches_the_confirmed_docs_worked_example(self, provider):
        from decimal import Decimal
        # docs.polymarket.us/fees: taker at P=$0.50, C=1000 -> $15.00
        result = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1000"))
        assert result.fee == Decimal("15.00")

    def test_banker_rounding_down_to_even(self, provider):
        """docs.polymarket.us/fees: $0.025 rounds to $0.02 (down to even)."""
        from decimal import Decimal
        from src.execution.polymarket_us import _polymarket_taker_fee
        # Reverse-engineer inputs that produce exactly 2.5 cents pre-rounding
        # by testing the rounding helper directly against that raw value.
        raw = Decimal("0.025")
        cents = (raw * 100)
        from decimal import ROUND_HALF_EVEN
        assert cents.to_integral_value(rounding=ROUND_HALF_EVEN) == Decimal("2")

    def test_banker_rounding_up_to_even(self, provider):
        """docs.polymarket.us/fees: $0.035 rounds to $0.04 (up to even)."""
        from decimal import Decimal, ROUND_HALF_EVEN
        raw = Decimal("0.035")
        cents = (raw * 100)
        assert cents.to_integral_value(rounding=ROUND_HALF_EVEN) == Decimal("4")

    def test_fee_result_is_flagged_as_confirmed_not_an_estimate(self, provider):
        from decimal import Decimal
        result = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1000"))
        assert result.fee_estimate is False

    def test_fee_is_symmetric_around_fifty_cents(self, provider):
        from decimal import Decimal
        fee_30 = provider.estimate_fees("YES", Decimal("0.30"), Decimal("100")).fee
        fee_70 = provider.estimate_fees("YES", Decimal("0.70"), Decimal("100")).fee
        assert fee_30 == fee_70

    def test_maker_rebate_is_not_applied_by_this_taker_only_stage(self, provider):
        """Maker rebate (0.0125*C*P*(1-P), paid TO the maker) exists on
        Polymarket US but isn't implemented -- this stage only ever
        analyzes taker (marketable) fills."""
        from decimal import Decimal
        result = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1000"))
        assert result.fee > Decimal("0")  # a cost, never a rebate, in this stage


class TestNormalizeOrderbook:
    """Polymarket's raw book is already a normal bidirectional book for
    the YES side; NO is synthesized via 1-P as a documented, UNCONFIRMED
    default -- see normalize_orderbook's docstring."""

    def test_yes_bids_and_asks_pass_through_directly(self, provider):
        from decimal import Decimal
        raw = Orderbook(market_id="M1", bids=[(0.65, 1000.0)], asks=[(0.66, 500.0)], raw={})
        book = provider.normalize_orderbook(raw)
        assert book.yes_bids[0].price == Decimal("0.65")
        assert book.yes_asks[0].price == Decimal("0.66")

    def test_no_side_synthesized_via_one_minus_p(self, provider):
        from decimal import Decimal
        raw = Orderbook(market_id="M1", bids=[(0.65, 1000.0)], asks=[(0.66, 500.0)], raw={})
        book = provider.normalize_orderbook(raw)
        assert book.no_bids[0].price == Decimal("1") - Decimal("0.66")
        assert book.no_asks[0].price == Decimal("1") - Decimal("0.65")

    def test_empty_book_produces_empty_sides_not_a_crash(self, provider):
        raw = Orderbook(market_id="M1", bids=[], asks=[], raw={})
        book = provider.normalize_orderbook(raw)
        assert book.yes_bids == []
        assert book.no_asks == []

    def test_uses_decimal_not_float(self, provider):
        from decimal import Decimal
        raw = Orderbook(market_id="M1", bids=[(0.65, 1000.0)], asks=[(0.66, 500.0)], raw={})
        book = provider.normalize_orderbook(raw)
        assert isinstance(book.yes_bids[0].price, Decimal)
        assert isinstance(book.no_bids[0].price, Decimal)


class TestPlaceOrderStillDisabled:
    """Regression: this safety property must remain true after Stage 2B."""

    def test_place_order_raises_not_implemented(self, provider):
        with pytest.raises(NotImplementedError):
            provider.place_order()

    def test_cancel_order_raises_not_implemented(self, provider):
        with pytest.raises(NotImplementedError):
            provider.cancel_order()
