"""Tests for src/execution/live/reconciliation.py -- the critical rule
under test throughout: absence from one list does NOT prove an order
never existed. Kalshi (real idempotency key, full-status order list)
can reach RECONCILED_NOT_FOUND; Polymarket US (no idempotency key,
open-orders-only list) can only ever reach RECONCILED_FOUND or
INCONCLUSIVE through this path -- never a false-positive
RECONCILED_NOT_FOUND for a provider that could have an
already-filled-and-gone order.
"""

from src.execution.base import ProviderCapabilities
from src.execution.live.reconciliation import reconcile_ambiguous_submission


class _FakeProvider:
    name = "fake"

    def __init__(self, capabilities: ProviderCapabilities, recent_orders_result=None, raises=False):
        self.capabilities = capabilities
        self._recent_orders_result = recent_orders_result
        self._raises = raises
        self.get_recent_orders_calls = []

    def get_recent_orders(self, **filters):
        self.get_recent_orders_calls.append(filters)
        if self._raises:
            raise RuntimeError("network error")
        return self._recent_orders_result


_KALSHI_LIKE_CAPS = ProviderCapabilities(
    supports_preview=False, supports_client_idempotency=True, supports_ioc=False, supports_fok=False,
    supports_order_lookup=True, supports_fill_lookup=True, supports_cancel=True, supports_modify=True,
)
_POLYMARKET_LIKE_CAPS = ProviderCapabilities(
    supports_preview=True, supports_client_idempotency=False, supports_ioc=True, supports_fok=True,
    supports_order_lookup=True, supports_fill_lookup=False, supports_cancel=True, supports_modify=False,
)
_NO_CAPS = ProviderCapabilities(False, False, False, False, False, False, False, False)


def _attempt(**overrides):
    defaults = dict(
        market_id="M1", side="YES", quantity=14, limit_price=0.68, approval_id="approval-xyz",
    )
    defaults.update(overrides)
    return defaults


class TestKalshiLikeIdempotentReconciliation:
    """Kalshi's order list is NOT restricted to open orders -- an exact
    client_order_id match or clean miss is meaningful evidence."""

    def test_exact_client_order_id_match_is_reconciled_found(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, recent_orders_result=[
            {"order_id": "ORD-1", "client_order_id": "approval-xyz", "side": "yes"},
            {"order_id": "ORD-2", "client_order_id": "some-other-approval", "side": "yes"},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "RECONCILED_FOUND"
        assert result.provider_order_id == "ORD-1"

    def test_clean_miss_across_full_status_list_is_reconciled_not_found(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, recent_orders_result=[
            {"order_id": "ORD-9", "client_order_id": "unrelated-approval", "side": "yes"},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "RECONCILED_NOT_FOUND"
        assert result.provider_order_id is None

    def test_empty_list_is_reconciled_not_found(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, recent_orders_result=[])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "RECONCILED_NOT_FOUND"

    def test_read_failure_falls_through_to_inconclusive_not_a_crash(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, raises=True)
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "INCONCLUSIVE"

    def test_reconciled_not_found_detail_explicitly_says_no_auto_retry(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, recent_orders_result=[])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert "retry" in result.detail.lower()


class TestPolymarketLikeNonIdempotentReconciliation:
    """Polymarket's open-orders-only list can prove presence but never
    absence -- an immediately-filled order simply won't be there."""

    def test_matching_open_order_is_reconciled_found(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[
            {"id": "ORDER-1", "side": "BUY", "outcomeSide": "YES", "quantity": 14, "price": {"value": "0.68"}},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "RECONCILED_FOUND"
        assert result.provider_order_id == "ORDER-1"

    def test_empty_open_orders_list_is_inconclusive_never_not_found(self):
        """THE critical rule: an immediately-filled order won't be in
        the open-orders list, so an empty list must NEVER be reported
        as RECONCILED_NOT_FOUND for a provider without a real
        idempotency key."""
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "INCONCLUSIVE"
        assert result.status != "RECONCILED_NOT_FOUND"

    def test_non_matching_open_orders_still_inconclusive_not_not_found(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[
            {"id": "ORDER-9", "side": "BUY", "outcomeSide": "NO", "quantity": 5, "price": {"value": "0.30"}},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "INCONCLUSIVE"

    def test_read_failure_is_inconclusive(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, raises=True)
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "INCONCLUSIVE"


class TestNoReconciliationCapability:
    def test_provider_with_no_read_capability_is_inconclusive(self):
        provider = _FakeProvider(_NO_CAPS)
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert result.status == "INCONCLUSIVE"
        assert provider.get_recent_orders_calls == []


class TestMatchingTolerances:
    def test_quantity_within_tolerance_still_matches(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[
            {"id": "ORDER-1", "outcomeSide": "YES", "quantity": 14.2, "price": {"value": "0.68"}},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt(quantity=14))
        assert result.status == "RECONCILED_FOUND"

    def test_price_far_outside_tolerance_does_not_match(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[
            {"id": "ORDER-1", "outcomeSide": "YES", "quantity": 14, "price": {"value": "0.20"}},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt(limit_price=0.68))
        assert result.status == "INCONCLUSIVE"

    def test_wrong_side_does_not_match(self):
        provider = _FakeProvider(_POLYMARKET_LIKE_CAPS, recent_orders_result=[
            {"id": "ORDER-1", "outcomeSide": "NO", "quantity": 14, "price": {"value": "0.68"}},
        ])
        result = reconcile_ambiguous_submission(provider, _attempt(side="YES"))
        assert result.status == "INCONCLUSIVE"


class TestNeverTriggersASecondSubmission:
    """This module never has access to a submission method at all --
    structurally confirms it can't retry even if it wanted to."""

    def test_reconciliation_result_has_no_retry_mechanism(self):
        provider = _FakeProvider(_KALSHI_LIKE_CAPS, recent_orders_result=[])
        result = reconcile_ambiguous_submission(provider, _attempt())
        assert not hasattr(result, "retry")
        assert not hasattr(result, "resubmit")
