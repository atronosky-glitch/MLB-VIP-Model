"""Tests for KalshiProvider's Stage 4 additions -- the permanent
fail-closed guard. Kalshi's live-order schema was not independently
verified, so _submit_authorized_order must NEVER build a request or
touch self.session, regardless of input."""

from decimal import Decimal
from unittest import mock

import pytest

from src.execution.kalshi import KALSHI_LIVE_SCHEMA_VERIFIED, KalshiLiveSchemaUnverifiedError, KalshiProvider


class _FakeAuthorization:
    provider_market_id = "KXMLBGAME-EX"
    side = "YES"


class TestKalshiLiveCapabilities:
    def test_capabilities_reflect_confirmed_and_unconfirmed_features(self):
        provider = object.__new__(KalshiProvider)
        caps = provider.capabilities
        assert caps.supports_client_idempotency is True  # client_order_id is a real, confirmed Kalshi concept
        assert caps.supports_preview is False
        assert caps.supports_order_lookup is False
        assert caps.supports_cancel is False


class TestKalshiFailsClosed:
    def test_submit_authorized_order_always_raises(self):
        provider = object.__new__(KalshiProvider)
        with pytest.raises(KalshiLiveSchemaUnverifiedError):
            provider._submit_authorized_order(_FakeAuthorization(), Decimal("10"), Decimal("0.55"))

    def test_raises_regardless_of_input_values(self):
        provider = object.__new__(KalshiProvider)
        for quantity, price in [(Decimal("0"), Decimal("0")), (Decimal("100000"), Decimal("0.99")), (Decimal("-5"), Decimal("1"))]:
            with pytest.raises(KalshiLiveSchemaUnverifiedError):
                provider._submit_authorized_order(_FakeAuthorization(), quantity, price)

    def test_never_touches_the_session_object(self):
        provider = object.__new__(KalshiProvider)
        provider.session = mock.Mock()
        try:
            provider._submit_authorized_order(_FakeAuthorization(), Decimal("10"), Decimal("0.55"))
        except KalshiLiveSchemaUnverifiedError:
            pass
        provider.session.post.assert_not_called()
        provider.session.get.assert_not_called()

    def test_the_module_constant_is_false(self):
        assert KALSHI_LIVE_SCHEMA_VERIFIED is False

    def test_error_message_explains_read_only_and_paper_are_unaffected(self):
        provider = object.__new__(KalshiProvider)
        with pytest.raises(KalshiLiveSchemaUnverifiedError) as exc_info:
            provider._submit_authorized_order(_FakeAuthorization(), Decimal("10"), Decimal("0.55"))
        assert "read-only" in str(exc_info.value).lower() or "paper" in str(exc_info.value).lower()

    def test_place_order_and_cancel_order_remain_separately_blocked(self):
        """Confirms this stage didn't accidentally wire
        _submit_authorized_order's real logic (however minimal) through
        place_order too."""
        provider = object.__new__(KalshiProvider)
        with pytest.raises(NotImplementedError):
            provider.place_order()
        with pytest.raises(NotImplementedError):
            provider.cancel_order()
