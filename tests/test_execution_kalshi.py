"""Tests for src/execution/kalshi.py. All requests are mocked at the
requests.Session level -- no network calls are ever made, and no
write-verb call (post/put/delete) is ever issued by this provider."""

from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.execution.kalshi import KalshiProvider


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
    kalshi_env = "demo"
    kalshi_api_key_id = "test-key-id-12345"

    def __init__(self, private_key_path):
        self.kalshi_private_key_path = str(private_key_path)


@pytest.fixture
def kalshi_key_path(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "kalshi.pem"
    path.write_bytes(pem)
    return path


@pytest.fixture
def provider(kalshi_key_path):
    return KalshiProvider(_FakeConfig(kalshi_key_path))


class TestBaseUrlSelection:
    def test_demo_env_uses_demo_domain(self, kalshi_key_path):
        class DemoConfig(_FakeConfig):
            kalshi_env = "demo"
        p = KalshiProvider(DemoConfig(kalshi_key_path))
        assert p._base_url == "https://external-api.demo.kalshi.co"

    def test_production_env_uses_production_domain(self, kalshi_key_path):
        class ProdConfig(_FakeConfig):
            kalshi_env = "production"
        p = KalshiProvider(ProdConfig(kalshi_key_path))
        assert p._base_url == "https://external-api.kalshi.com"

    def test_invalid_env_raises(self, kalshi_key_path):
        class BadConfig(_FakeConfig):
            kalshi_env = "staging"
        with pytest.raises(ValueError):
            KalshiProvider(BadConfig(kalshi_key_path))


class TestSignedHeaders:
    def test_headers_contain_all_three_required_fields(self, provider):
        headers = provider._signed_headers("GET", "/trade-api/v2/portfolio/balance")
        assert set(headers) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
        assert headers["KALSHI-ACCESS-KEY"] == "test-key-id-12345"
        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


class TestReadOnlyMethods:
    def test_get_balance_parses_dollars(self, provider):
        resp = _FakeResp(200, {"balance": 12345, "balance_dollars": "123.45"})
        with mock.patch.object(provider.session, "get", return_value=resp) as mg:
            balance = provider.get_balance()
        assert balance.available == 123.45
        assert balance.currency == "USD"
        called_path = mg.call_args[0][0]
        assert called_path == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance"

    def test_get_markets_normalizes_each_market(self, provider):
        resp = _FakeResp(200, {"markets": [
            {"ticker": "T1", "yes_sub_title": "Yes", "status": "open"},
        ]})
        with mock.patch.object(provider.session, "get", return_value=resp):
            markets = provider.get_markets(limit=10)
        assert len(markets) == 1
        assert markets[0].id == "T1"
        assert markets[0].status == "open"

    def test_get_market_single(self, provider):
        resp = _FakeResp(200, {"market": {"ticker": "T1", "status": "open"}})
        with mock.patch.object(provider.session, "get", return_value=resp):
            market = provider.get_market("T1")
        assert market.id == "T1"

    def test_get_orderbook_maps_yes_no_dollars_to_bids_asks(self, provider):
        resp = _FakeResp(200, {"orderbook_fp": {
            "yes_dollars": [["0.5500", "100.00"]],
            "no_dollars": [["0.4400", "50.00"]],
        }})
        with mock.patch.object(provider.session, "get", return_value=resp):
            book = provider.get_orderbook("T1")
        assert book.bids == [(0.55, 100.0)]
        assert book.asks == [(0.44, 50.0)]

    def test_get_best_bid_ask_uses_top_of_book(self, provider):
        resp = _FakeResp(200, {"orderbook_fp": {
            "yes_dollars": [["0.5500", "100.00"]],
            "no_dollars": [["0.4400", "50.00"]],
        }})
        with mock.patch.object(provider.session, "get", return_value=resp):
            bba = provider.get_best_bid_ask("T1")
        assert bba.best_bid == 0.55
        assert bba.best_ask == 0.44

    def test_health_check_ok_on_success(self, provider):
        resp = _FakeResp(200, {"balance": 100, "balance_dollars": "1.00"})
        with mock.patch.object(provider.session, "get", return_value=resp):
            result = provider.health_check()
        assert result.ok is True
        assert result.provider == "kalshi"

    def test_health_check_not_ok_on_auth_failure(self, provider):
        resp = _FakeResp(401, {})
        with mock.patch.object(provider.session, "get", return_value=resp):
            result = provider.health_check()
        assert result.ok is False

    def test_health_check_never_raises(self, provider):
        with mock.patch.object(provider.session, "get", side_effect=requests.exceptions.ConnectionError("boom")):
            with mock.patch("src.execution.kalshi.time.sleep"):
                result = provider.health_check()
        assert result.ok is False


class TestRetryBehavior:
    def test_retries_on_5xx_then_succeeds(self, provider):
        responses = [_FakeResp(503), _FakeResp(200, {"balance": 0, "balance_dollars": "0.00"})]
        with mock.patch.object(provider.session, "get", side_effect=responses) as mg:
            with mock.patch("src.execution.kalshi.time.sleep"):
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
            "balance": 0, "balance_dollars": "0.00",
            "markets": [], "market": {"ticker": "T1"},
            "orderbook_fp": {"yes_dollars": [], "no_dollars": []},
        })
        with mock.patch.object(provider.session, "get", return_value=resp), \
             mock.patch.object(provider.session, "post") as mp, \
             mock.patch.object(provider.session, "put") as mpu, \
             mock.patch.object(provider.session, "delete") as md:
            provider.get_balance()
            provider.get_markets()
            provider.get_market("T1")
            provider.get_orderbook("T1")
            provider.get_best_bid_ask("T1")
            provider.health_check()

        mp.assert_not_called()
        mpu.assert_not_called()
        md.assert_not_called()
