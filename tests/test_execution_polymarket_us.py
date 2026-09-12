"""Tests for src/execution/polymarket_us.py. All requests are mocked at
the requests.Session level. Covers the gateway-vs-api routing split:
market data is public (no auth headers), account data is Ed25519-signed."""

import base64
from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

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
