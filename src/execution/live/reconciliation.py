"""Ambiguous-submission reconciliation (Stage 4.1).

Called only for SUBMISSION_UNKNOWN attempts -- it never triggers a
second financial POST by itself (MAX_FINANCIAL_POST_ATTEMPTS_PER_APPROVAL
stays hard-enforced at 1 regardless of the result here). Its only job
is to improve the INFORMATION available for the audit trail and a
human's eventual manual-review decision.

Critical rule (explicitly required, not optional): absence from one
list does NOT prove an order never existed. An order that filled
immediately may never appear in an "open orders" list. This module's
confidence level is therefore provider-capability-dependent:

- Kalshi: has a REAL client-supplied idempotency key (client_order_id,
  confirmed from the official SDK) and its order-list endpoint
  (GET /portfolio/orders) is NOT restricted to open orders only --
  no status filter returns orders in every state (resting/executed/
  canceled/pending). An exact client_order_id match is conclusive
  (RECONCILED_FOUND); a clean miss across that full-status list is
  strong evidence of absence (RECONCILED_NOT_FOUND) -- still not
  automatically treated as license to retry, but strong enough to
  report as such.
- Polymarket US: has NO idempotency key, and its only order-list
  endpoint (GET /v1/orders/open) is explicitly open-orders-only. A
  clean miss there proves nothing (an immediately-filled order is
  already gone from that list) -- Polymarket can only ever reach
  RECONCILED_FOUND (positive evidence) or INCONCLUSIVE, never
  RECONCILED_NOT_FOUND, through this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.execution.base import PredictionMarketProvider

_QUANTITY_TOLERANCE = 0.5
_PRICE_TOLERANCE = 0.02


@dataclass(frozen=True)
class ReconciliationResult:
    status: str  # "RECONCILED_FOUND" / "RECONCILED_NOT_FOUND" / "INCONCLUSIVE"
    provider_order_id: str | None
    detail: str


def _order_side(order: dict) -> str:
    """Prefer outcomeSide (Polymarket's actual YES/NO field) over side
    (which on Polymarket means BUY/SELL action, not outcome) -- Kalshi
    has no outcomeSide field at all, so its own "side" (already "yes"/
    "no") is used there."""
    return str(order.get("outcomeSide") or order.get("side") or "").upper()


def _order_quantity(order: dict) -> float | None:
    value = order.get("count")
    if value is None:
        value = order.get("quantity")
    return float(value) if value is not None else None


def _order_price(order: dict) -> float | None:
    for key in ("yes_price", "no_price"):
        if order.get(key) is not None:
            return float(order[key]) / 100  # Kalshi: integer cents
    value = order.get("price")
    if isinstance(value, dict):
        value = value.get("value")
    return float(value) if value is not None else None


def _order_id_of(order: dict) -> str | None:
    return order.get("order_id") or order.get("id")


def _find_matching_order(orders: list[dict], side: str, quantity: float, limit_price: float) -> dict | None:
    target_side = (side or "").upper()
    for order in orders:
        order_side = _order_side(order)
        if order_side and order_side != target_side:
            continue
        order_qty = _order_quantity(order)
        if order_qty is not None and abs(order_qty - quantity) > _QUANTITY_TOLERANCE:
            continue
        order_price = _order_price(order)
        if order_price is not None and abs(order_price - limit_price) > _PRICE_TOLERANCE:
            continue
        return order
    return None


def reconcile_ambiguous_submission(provider: PredictionMarketProvider, attempt: dict) -> ReconciliationResult:
    """*attempt* is a live_submission_attempts row (dict) -- uses
    market_id, side, quantity, limit_price, and approval_id (the value
    that would have been sent as client_order_id for a provider that
    supports one)."""
    caps = provider.capabilities
    side = attempt.get("side") or ""
    quantity = float(attempt.get("quantity") or 0)
    limit_price = float(attempt.get("limit_price") or 0)
    market_id = attempt.get("market_id")
    client_order_id = attempt.get("approval_id")

    if caps.supports_client_idempotency and caps.supports_order_lookup:
        try:
            orders = provider.get_recent_orders(ticker=market_id) or []
        except Exception as exc:
            orders = None
            idempotent_error = exc
        else:
            idempotent_error = None

        if orders is not None:
            match = next((o for o in orders if o.get("client_order_id") == client_order_id), None)
            if match is not None:
                return ReconciliationResult(
                    "RECONCILED_FOUND", _order_id_of(match),
                    f"exact client_order_id={client_order_id!r} match found in the provider's order list",
                )
            return ReconciliationResult(
                "RECONCILED_NOT_FOUND", None,
                f"no order with client_order_id={client_order_id!r} found in {market_id}'s order list "
                "(a full-status list, not open-orders-only) -- strong evidence of absence, but this does "
                "NOT by itself authorize an automatic retry (MAX_FINANCIAL_POST_ATTEMPTS_PER_APPROVAL "
                "stays 1 regardless; a new attempt still requires a fresh human approval)",
            )
        # idempotent_error set -- fall through to the weaker path below.

    if caps.supports_order_lookup:
        try:
            orders = provider.get_recent_orders(ticker=market_id)
            if orders is None:
                orders = provider.get_recent_orders()
        except Exception:
            orders = None

        if orders:
            match = _find_matching_order(orders, side, quantity, limit_price)
            if match is not None:
                return ReconciliationResult(
                    "RECONCILED_FOUND", _order_id_of(match),
                    "matched an order in the provider's (open-orders-only) list by side/quantity/price",
                )
        # A clean miss here proves NOTHING for a provider without a real
        # idempotency key -- an immediately-filled order would already
        # be gone from an open-orders-only list. Never claim
        # RECONCILED_NOT_FOUND on this evidence alone.

    return ReconciliationResult(
        "INCONCLUSIVE", None,
        f"{provider.name} could not provide conclusive evidence either way -- manual review required. "
        "Per policy, no automatic retry occurs regardless of this result.",
    )
