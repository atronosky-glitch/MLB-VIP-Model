"""Tests for src/execution/kalshi.py. All requests are mocked at the
requests.Session level -- no network calls are ever made, and no
write-verb call (post/put/delete) is ever issued by this provider."""

from unittest import mock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.execution.base import Market, Orderbook
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


class TestParseGameEvent:
    """UNCONFIRMED against real Kalshi data -- see kalshi.py's
    parse_game_event docstring. These tests only prove the guessed
    separator patterns behave as intended, not that they match reality."""

    def test_parses_a_vs_separated_title(self, provider):
        market = Market(id="T1", title="Toronto Blue Jays vs Athletics", status="open")
        event = provider.parse_game_event(market)
        assert event.away_team == "Toronto Blue Jays"
        assert event.home_team == "Athletics"
        assert event.market_type == "moneyline"

    def test_parses_an_at_separated_title(self, provider):
        market = Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open")
        event = provider.parse_game_event(market)
        assert event.away_team == "Athletics"
        assert event.home_team == "Toronto Blue Jays"

    def test_unrecognized_title_returns_none(self, provider):
        market = Market(id="T1", title="Will inflation exceed 5%?", status="open")
        assert provider.parse_game_event(market) is None

    def test_extracts_event_start_time_from_raw_close_time(self, provider):
        market = Market(
            id="T1", title="Athletics @ Toronto Blue Jays", status="open",
            raw={"close_time": "2026-09-12T23:00:00Z"},
        )
        event = provider.parse_game_event(market)
        assert event.event_start_time is not None

    def test_missing_date_field_is_none_not_a_crash(self, provider):
        market = Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open")
        event = provider.parse_game_event(market)
        assert event.event_start_time is None


class TestEstimateFees:
    """fee = ceil_to_cent(0.07*C*P*(1-P)), confirmed against multiple
    2026 sources describing Kalshi's public taker-fee framework."""

    def test_fee_peaks_at_fifty_cents(self, provider):
        from decimal import Decimal
        fee_50 = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1")).fee
        fee_10 = provider.estimate_fees("YES", Decimal("0.10"), Decimal("1")).fee
        fee_90 = provider.estimate_fees("YES", Decimal("0.90"), Decimal("1")).fee
        assert fee_50 > fee_10
        assert fee_50 > fee_90

    def test_fee_is_symmetric_around_fifty_cents(self, provider):
        from decimal import Decimal
        fee_30 = provider.estimate_fees("YES", Decimal("0.30"), Decimal("1")).fee
        fee_70 = provider.estimate_fees("YES", Decimal("0.70"), Decimal("1")).fee
        assert fee_30 == fee_70

    def test_rounds_up_to_the_next_cent(self, provider):
        from decimal import Decimal
        # 0.07 * 1 * 0.50 * 0.50 = 0.0175 -> ceil to 2 cents, not 1
        fee = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1")).fee
        assert fee == Decimal("0.02")

    def test_scales_with_contract_count(self, provider):
        from decimal import Decimal
        fee_1 = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1")).fee
        fee_100 = provider.estimate_fees("YES", Decimal("0.50"), Decimal("100")).fee
        assert fee_100 > fee_1 * 50  # scales roughly linearly with C

    def test_near_zero_and_near_one_prices_have_low_fees(self, provider):
        from decimal import Decimal
        fee_low = provider.estimate_fees("YES", Decimal("0.01"), Decimal("1")).fee
        fee_mid = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1")).fee
        assert fee_low < fee_mid

    def test_maker_fee_is_not_applied_by_this_taker_only_stage(self, provider):
        """25% maker discount exists on Kalshi but isn't implemented --
        this stage only ever analyzes taker (marketable) fills."""
        from decimal import Decimal
        result = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1"))
        assert result.fee == Decimal("0.02")  # the full taker fee, not 25% of it

    def test_fee_result_is_flagged_as_an_estimate(self, provider):
        from decimal import Decimal
        result = provider.estimate_fees("YES", Decimal("0.50"), Decimal("1"))
        assert result.fee_estimate is True


class TestNormalizeOrderbook:
    """Kalshi's raw book only ever returns bids for each side; asks are
    synthesized via the 1-P transform, which is a mechanism fact (see
    normalize_orderbook's docstring), not an assumption."""

    def test_yes_ask_and_no_bid_sum_to_exactly_one_dollar(self, provider):
        from decimal import Decimal
        raw = Orderbook(
            market_id="T1",
            bids=[(0.55, 100.0)],   # yes_dollars
            asks=[(0.44, 50.0)],    # Stage 1's name for no_dollars
            raw={},
        )
        book = provider.normalize_orderbook(raw)
        assert book.yes_asks[0].price + book.no_bids[0].price == Decimal("1")
        assert book.no_asks[0].price + book.yes_bids[0].price == Decimal("1")

    def test_yes_bids_and_no_bids_come_from_the_correct_raw_side(self, provider):
        from decimal import Decimal
        raw = Orderbook(market_id="T1", bids=[(0.55, 100.0)], asks=[(0.44, 50.0)], raw={})
        book = provider.normalize_orderbook(raw)
        assert book.yes_bids[0].price == Decimal("0.55")
        assert book.no_bids[0].price == Decimal("0.44")

    def test_empty_book_produces_empty_sides_not_a_crash(self, provider):
        raw = Orderbook(market_id="T1", bids=[], asks=[], raw={})
        book = provider.normalize_orderbook(raw)
        assert book.yes_bids == []
        assert book.yes_asks == []
        assert book.no_bids == []
        assert book.no_asks == []

    def test_uses_decimal_not_float(self, provider):
        from decimal import Decimal
        raw = Orderbook(market_id="T1", bids=[(0.55, 100.0)], asks=[(0.44, 50.0)], raw={})
        book = provider.normalize_orderbook(raw)
        assert isinstance(book.yes_bids[0].price, Decimal)
        assert isinstance(book.yes_asks[0].price, Decimal)


class TestPlaceOrderStillDisabled:
    """Regression: this safety property must remain true after Stage 2B."""

    def test_place_order_raises_not_implemented(self, provider):
        with pytest.raises(NotImplementedError):
            provider.place_order()

    def test_cancel_order_raises_not_implemented(self, provider):
        with pytest.raises(NotImplementedError):
            provider.cancel_order()
