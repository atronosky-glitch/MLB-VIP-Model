"""Tests for PolymarketUSProvider's Stage 4 additions -- the real
POST /v1/orders implementation. Exactly one HTTP call per
_submit_authorized_order invocation, never more (no internal retry --
that's LiveExecutionService's decision, and it defaults to never for
ambiguous outcomes)."""

from decimal import Decimal
from unittest import mock

import pytest
import requests

from src.execution.polymarket_us import PolymarketUSProvider


class _FakeAuthorization:
    provider_market_id = "aec-mlb-oak-tor-2026-09-13"
    side = "YES"


def _provider() -> PolymarketUSProvider:
    provider = object.__new__(PolymarketUSProvider)
    provider._api_key_id = "test-key-id"
    provider.session = mock.Mock()
    provider._signed_headers = mock.Mock(return_value={"X-PM-Access-Key": "k", "X-PM-Timestamp": "1", "X-PM-Signature": "s"})
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


class TestCapabilities:
    def test_no_idempotency_confirmed_absent(self):
        """Confirmed absent from current docs.polymarket.us -- no
        clientOrderId or equivalent field exists on order creation."""
        caps = _provider().capabilities
        assert caps.supports_client_idempotency is False

    def test_preview_confirmed_present(self):
        """Stage 4.1 correction: POST /v1/order/preview was confirmed
        to exist (same Ed25519 auth as create) -- not wired into the
        submission flow yet, but the capability flag must reflect
        reality, not the earlier (incomplete) research pass."""
        caps = _provider().capabilities
        assert caps.supports_preview is True

    def test_ioc_and_fok_supported_via_tif(self):
        caps = _provider().capabilities
        assert caps.supports_ioc is True
        assert caps.supports_fok is True

    def test_order_lookup_confirmed_via_get_order_by_id(self):
        """GET /v1/order/{orderId} is confirmed -- only useful once an
        order id is already known, not for a fully ambiguous submission."""
        caps = _provider().capabilities
        assert caps.supports_order_lookup is True

    def test_fill_lookup_still_not_available(self):
        """No separate fills endpoint is documented for Polymarket US."""
        caps = _provider().capabilities
        assert caps.supports_fill_lookup is False

    def test_cancel_confirmed_present_but_not_implemented(self):
        caps = _provider().capabilities
        assert caps.supports_cancel is True


class TestReconciliationReads:
    def test_get_order_by_id_returns_the_order(self):
        provider = _provider()
        provider._authenticated_get = mock.Mock(return_value={"order": {"id": "ORDER-1", "state": "FILLED"}})
        result = provider.get_order_by_id("ORDER-1")
        assert result == {"id": "ORDER-1", "state": "FILLED"}
        provider._authenticated_get.assert_called_once_with("/v1/order/ORDER-1")

    def test_get_order_by_id_returns_none_on_404_or_error(self):
        provider = _provider()
        provider._authenticated_get = mock.Mock(side_effect=RuntimeError("404 not found"))
        assert provider.get_order_by_id("does-not-exist") is None

    def test_get_recent_orders_hits_open_orders_endpoint(self):
        provider = _provider()
        provider._authenticated_get = mock.Mock(return_value={"orders": [{"id": "ORDER-1"}]})
        result = provider.get_recent_orders()
        assert result == [{"id": "ORDER-1"}]
        provider._authenticated_get.assert_called_once_with("/v1/orders/open", params=None)

    def test_get_recent_orders_returns_none_on_error(self):
        provider = _provider()
        provider._authenticated_get = mock.Mock(side_effect=RuntimeError("boom"))
        assert provider.get_recent_orders() is None

    def test_get_positions_returns_none_on_error_never_guesses(self):
        """If the uncertain base-URL issue means this 404s/errors in
        reality, this must fail safely to None, not raise."""
        provider = _provider()
        provider._authenticated_get = mock.Mock(side_effect=RuntimeError("wrong host"))
        assert provider.get_positions() is None

    def test_get_positions_returns_the_list_on_success(self):
        provider = _provider()
        provider._authenticated_get = mock.Mock(return_value={"positions": [{"symbol": "X", "netPosition": 14}]})
        result = provider.get_positions()
        assert result == [{"symbol": "X", "netPosition": 14}]


class TestPayloadGeneration:
    def test_sends_exactly_one_post(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert provider.session.post.call_count == 1

    def test_payload_fields_match_confirmed_schema(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))

        _, kwargs = provider.session.post.call_args
        body = kwargs["json"]
        assert body["marketSlug"] == "aec-mlb-oak-tor-2026-09-13"
        assert body["type"] == "ORDER_TYPE_LIMIT"
        assert body["price"] == {"value": "0.55", "currency": "USD"}
        assert body["quantity"] == 14.0
        assert body["outcomeSide"] == "YES"
        assert body["action"] == "BUY"
        assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_MANUAL"
        assert "tif" in body

    def test_no_client_order_id_field_is_ever_sent(self):
        """Confirmed absent from the real schema -- sending a fabricated
        one would be worse than sending none."""
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        _, kwargs = provider.session.post.call_args
        assert "clientOrderId" not in kwargs["json"]

    def test_posts_to_the_authenticated_orders_endpoint(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        args, _ = provider.session.post.call_args
        assert args[0] == "https://api.polymarket.us/v1/orders"


class TestSuccessfulSubmission:
    def test_definite_success_returns_confirmed(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "CONFIRMED"
        assert outcome.provider_order_id == "ORDER-1"

    def test_extracts_fill_data_from_executions_when_present(self):
        provider = _provider()
        body = {
            "id": "ORDER-1",
            "executions": [{"order": {"avgPx": "0.55", "cumQuantity": "14", "state": "FILLED"}}],
        }
        provider.session.post.return_value = _response(201, body)
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.quantity_filled == Decimal("14")
        assert outcome.average_fill_price == Decimal("0.55")
        assert outcome.order_state == "FILLED"

    def test_missing_executions_still_confirmed_but_fill_unknown(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "CONFIRMED"
        assert outcome.quantity_filled is None
        assert outcome.average_fill_price is None

    def test_malformed_executions_does_not_crash_just_leaves_fill_unknown(self):
        provider = _provider()
        body = {"id": "ORDER-1", "executions": [{"order": {"avgPx": "not-a-number"}}]}
        provider.session.post.return_value = _response(201, body)
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "CONFIRMED"
        assert outcome.average_fill_price is None

    def test_2xx_missing_order_id_is_ambiguous_not_confirmed(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_2xx_unparseable_body_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, raises_on_json=True)
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"


class TestDefiniteRejection:
    def test_400_is_a_proven_rejection_not_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(400, {"error": "insufficient balance"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_422_is_a_proven_rejection(self):
        provider = _provider()
        provider.session.post.return_value = _response(422, {"error": "validation"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_401_is_treated_as_rejection_not_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(401, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_403_is_treated_as_rejection(self):
        provider = _provider()
        provider.session.post.return_value = _response(403, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "REJECTED"

    def test_rejection_never_carries_a_provider_order_id(self):
        provider = _provider()
        provider.session.post.return_value = _response(400, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.provider_order_id is None


class TestAmbiguousOutcomes:
    def test_timeout_is_ambiguous(self):
        provider = _provider()
        provider.session.post.side_effect = requests.exceptions.Timeout("timed out")
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_connection_error_is_ambiguous(self):
        provider = _provider()
        provider.session.post.side_effect = requests.exceptions.ConnectionError("reset")
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_unexpected_exception_is_ambiguous_not_a_crash(self):
        provider = _provider()
        provider.session.post.side_effect = RuntimeError("something weird")
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_5xx_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(500, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_409_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(409, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_429_is_ambiguous(self):
        provider = _provider()
        provider.session.post.return_value = _response(429, {})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.outcome == "AMBIGUOUS"

    def test_ambiguous_never_carries_a_provider_order_id(self):
        provider = _provider()
        provider.session.post.side_effect = requests.exceptions.Timeout()
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert outcome.provider_order_id is None


class TestErrorBodySanitization:
    def test_credential_shaped_keys_are_redacted(self):
        provider = _provider()
        provider.session.post.return_value = _response(400, {"apiKey": "SECRET123", "error": "bad request"})
        outcome = provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        assert "SECRET123" not in (outcome.raw_reference or "")
        assert "<redacted>" in (outcome.raw_reference or "")

    def test_never_touches_get_for_a_mutation(self):
        provider = _provider()
        provider.session.post.return_value = _response(201, {"id": "ORDER-1"})
        provider._submit_authorized_order(_FakeAuthorization(), Decimal("14"), Decimal("0.55"))
        provider.session.get.assert_not_called()
