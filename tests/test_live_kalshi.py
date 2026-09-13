"""Tests for KalshiProvider's Stage 4.1 live-order implementation --
schema verified directly from the official `kalshi-python` PyPI SDK
(v2.1.4, OpenAPI doc v2.0.0). Exactly one HTTP POST per
_submit_authorized_order call, never more (no internal retry)."""

from decimal import Decimal
from unittest import mock

import pytest
import requests

from src.execution.kalshi import KALSHI_LIVE_SCHEMA_VERIFIED, KalshiLiveSchemaUnverifiedError, KalshiProvider


class _FakeAuthorization:
    provider_market_id = "KXMLBGAME-EX"
    side = "YES"
    approval_id = "approval-abc123"


class _FakeAuthorizationNo(_FakeAuthorization):
    side = "NO"


def _provider() -> KalshiProvider:
    provider = object.__new__(KalshiProvider)
    provider._api_key_id = "test-key-id"
    provider._base_url = "https://external-api.demo.kalshi.co"
    provider.session = mock.Mock()
    provider._signed_headers = mock.Mock(return_value={
        "KALSHI-ACCESS-KEY": "k", "KALSHI-ACCESS-TIMESTAMP": "1", "KALSHI-ACCESS-SIGNATURE": "s",
    })
    return provider


def _response(status_code, json_body=None, raises_on_json=False, content=b"{}"):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.content = content
    if raises_on_json:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body or {}
    return resp


class TestKalshiLiveSchemaVerified:
    def test_the_module_constant_is_true(self):
        assert KALSHI_LIVE_SCHEMA_VERIFIED is True


class TestKalshiCapabilities:
    def test_capabilities_reflect_the_sdk_confirmed_surface(self):
        caps = _provider().capabilities
        assert caps.supports_client_idempotency is True
        assert caps.supports_order_lookup is True
        assert caps.supports_fill_lookup is True
        assert caps.supports_preview is False
        assert caps.supports_ioc is False  # no native TIF; emulated via expiration_ts


class TestPayloadGeneration:
    def test_sends_exactly_one_post(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert provider.session.post.call_count == 1

    def test_yes_side_payload_fields(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))

        _, kwargs = provider.session.post.call_args
        body = kwargs["json"]
        assert body["ticker"] == "KXMLBGAME-EX"
        assert body["client_order_id"] == "approval-abc123"
        assert body["side"] == "yes"
        assert body["action"] == "buy"
        assert body["count"] == 14
        assert body["type"] == "limit"
        assert body["yes_price"] == 55
        assert "no_price" not in body
        assert "expiration_ts" in body

    def test_no_side_uses_no_price_field(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorizationNo(), Decimal("10"), Decimal("0.30"))
        _, kwargs = provider.session.post.call_args
        body = kwargs["json"]
        assert body["side"] == "no"
        assert body["no_price"] == 30
        assert "yes_price" not in body

    def test_price_is_integer_cents_not_a_decimal_dollar_fraction(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.5678"))
        _, kwargs = provider.session.post.call_args
        assert isinstance(kwargs["json"]["yes_price"], int)
        assert kwargs["json"]["yes_price"] == 57  # rounds to nearest cent

    def test_price_clamped_to_1_99_cent_range(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.999"))
        _, kwargs = provider.session.post.call_args
        assert kwargs["json"]["yes_price"] == 99

    def test_count_is_a_whole_number_of_contracts(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14.9"), Decimal("0.55"))
        _, kwargs = provider.session.post.call_args
        assert kwargs["json"]["count"] == 14
        assert isinstance(kwargs["json"]["count"], int)

    def test_zero_count_after_truncation_is_ambiguous_not_sent(self):
        provider = _provider()
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("0.5"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"
        provider.session.post.assert_not_called()

    def test_posts_to_the_confirmed_portfolio_orders_path(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        args, _ = provider.session.post.call_args
        assert args[0] == "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/orders"

    def test_action_is_always_buy(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        _, kwargs = provider.session.post.call_args
        assert kwargs["json"]["action"] == "buy"


class TestSuccessfulSubmission:
    def test_confirmed_with_order_id(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "CONFIRMED"
        assert outcome.provider_order_id == "ORD-1"
        assert outcome.order_state == "resting"

    def test_fill_quantity_derived_from_count_minus_remaining(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {
            "order": {"order_id": "ORD-1", "status": "executed", "count": 14, "remaining_count": 0, "yes_price": 55},
        })
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.quantity_filled == Decimal("14")
        assert outcome.average_fill_price == Decimal("0.55")

    def test_partial_fill_derived_correctly(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {
            "order": {"order_id": "ORD-1", "status": "executed", "count": 14, "remaining_count": 4, "yes_price": 55},
        })
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.quantity_filled == Decimal("10")

    def test_resting_order_has_unknown_fill_not_assumed_zero_or_full(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {"order_id": "ORD-1", "status": "resting"}})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.quantity_filled is None
        assert outcome.average_fill_price is None

    def test_200_is_also_accepted_as_success(self):
        provider = _provider()
        provider.session.post.return_value = _response(200, {"order": {"order_id": "ORD-1", "status": "pending"}})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "CONFIRMED"

    def test_2xx_missing_order_id_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"order": {}})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_2xx_unparseable_body_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, raises_on_json=True)
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"


class TestDefiniteRejection:
    def test_400_is_a_proven_rejection(self):
        provider = _provider()
        provider.session.post.return_value = _response(400, {"error": "bad request"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_401_is_a_proven_rejection(self):
        provider = _provider()
        provider.session.post.return_value = _response(401, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_rejection_never_carries_an_order_id(self):
        provider = _provider()
        provider.session.post.return_value = _response(422, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.provider_order_id is None


class TestAmbiguousOutcomes:
    def test_timeout_is_ambiguous(self):
        provider = _provider()
        provider.session.post.side_effect = requests.exceptions.Timeout()
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_connection_error_is_ambiguous(self):
        provider = _provider()
        provider.session.post.side_effect = requests.exceptions.ConnectionError()
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_5xx_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(503, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_429_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(429, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"


class TestReconciliationReads:
    def test_get_order_by_id_returns_the_order_dict(self):
        provider = _provider()
        provider._get = mock.Mock(return_value={"order": {"order_id": "ORD-1", "status": "executed"}})
        result = provider.get_order_by_id("ORD-1")
        assert result == {"order_id": "ORD-1", "status": "executed"}
        provider._get.assert_called_once_with("/portfolio/orders/ORD-1")

    def test_get_order_by_id_returns_none_on_error(self):
        provider = _provider()
        provider._get = mock.Mock(side_effect=RuntimeError("network error"))
        assert provider.get_order_by_id("ORD-1") is None

    def test_get_recent_orders_passes_filters_as_query_params(self):
        provider = _provider()
        provider._get = mock.Mock(return_value={"orders": [{"order_id": "ORD-1"}], "cursor": None})
        result = provider.get_recent_orders(ticker="KXMLBGAME-EX", min_ts=100, max_ts=200)
        assert result == [{"order_id": "ORD-1"}]
        provider._get.assert_called_once_with(
            "/portfolio/orders", params={"ticker": "KXMLBGAME-EX", "min_ts": 100, "max_ts": 200},
        )

    def test_get_fills_supports_order_id_filter(self):
        provider = _provider()
        provider._get = mock.Mock(return_value={"fills": [{"fill_id": "F1", "order_id": "ORD-1"}]})
        result = provider.get_fills(order_id="ORD-1")
        assert result == [{"fill_id": "F1", "order_id": "ORD-1"}]

    def test_get_positions_returns_the_list(self):
        provider = _provider()
        provider._get = mock.Mock(return_value={"positions": [{"ticker": "KXMLBGAME-EX", "position": 14}]})
        result = provider.get_positions()
        assert result == [{"ticker": "KXMLBGAME-EX", "position": 14}]

    def test_reconciliation_reads_never_raise_on_failure(self):
        provider = _provider()
        provider._get = mock.Mock(side_effect=RuntimeError("boom"))
        assert provider.get_recent_orders() is None
        assert provider.get_fills() is None
        assert provider.get_positions() is None


class TestSchemaGateStillEnforceable:
    def test_manually_reverting_the_gate_blocks_submission_again(self):
        """Confirms the fail-closed mechanism itself still works --
        this is what protects the codebase if a future change ever
        needs to roll the verification back."""
        import src.execution.kalshi as kalshi_module
        provider = _provider()
        with mock.patch.object(kalshi_module, "KALSHI_LIVE_SCHEMA_VERIFIED", False):
            with pytest.raises(KalshiLiveSchemaUnverifiedError):
                provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        provider.session.post.assert_not_called()


class TestPlaceOrderStillBlocked:
    def test_place_order_and_cancel_order_remain_separately_blocked(self):
        provider = object.__new__(KalshiProvider)
        with pytest.raises(NotImplementedError):
            provider.place_order()
        with pytest.raises(NotImplementedError):
            provider.cancel_order()
