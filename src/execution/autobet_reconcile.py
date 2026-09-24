"""Reconcile customers' LIVE Kalshi Auto-Bet executions against Kalshi's
own order/fill records (read-only).

Why this exists: the synchronous order response Kalshi returns carries the
order's LIMIT price, not the average price actually paid, and its fee
estimate is a formula. Customer P&L must be computed from what the
customer's account really paid, so the worker replays each unreconciled
order against the venue's own ``GET /portfolio/fills`` (and, to confirm a
zero-fill outcome, ``GET /portfolio/orders/{id}``) -- both already exposed
by KalshiProvider as read-only GETs, using the customer's OWN decrypted
credentials. No order is ever placed, amended or cancelled here.

Fail-closed parsing: a fill whose side/action/price/count can't be read
unambiguously leaves the execution UNRECONCILED (still labeled as an
estimate in My Performance) -- a guessed number is never written.

Payload-shape note: field names follow Kalshi's official Python SDK
(``kalshi-python`` 2.1.4 ``Fill``/``Order`` models: order_id, side,
action, count, yes_price/no_price in cents, remaining_count) plus the
fixed-point ``*_dollars`` / ``fee_cost`` variants Kalshi's current API
documents. It has NOT been exercised against a live authenticated
account (none is available to this project's tests, and no order may be
placed to generate one), so the first controlled live test should confirm
a reconciled row -- until then rows simply stay ORDER_LIMIT_PRICE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

_TERMINAL_ORDER_STATUSES = {"canceled", "cancelled", "executed"}


@dataclass(frozen=True)
class FillSummary:
    quantity: Decimal
    avg_price: Decimal | None       # None only when quantity == 0
    fees_usd: Decimal | None        # platform-reported; None if any fill lacks it


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fill_price(fill: dict, side: str) -> Decimal | None:
    """Price paid per contract for *side* ("yes"/"no"), in dollars, or
    None if not unambiguously present. Dollar-denominated field wins;
    otherwise integer cents 1..99."""
    dollars = _to_decimal(fill.get(f"{side}_price_dollars"))
    if dollars is not None:
        return dollars if Decimal("0") < dollars < Decimal("1") else None
    cents = _to_decimal(fill.get(f"{side}_price"))
    if cents is None or cents != cents.to_integral_value():
        return None
    return (cents / 100) if Decimal("1") <= cents <= Decimal("99") else None


def summarize_kalshi_fills(fills: list[dict], side: str) -> FillSummary | None:
    """Aggregate the fills of ONE order into (quantity, VWAP, fees).

    Returns None (unreconcilable) if any fill is not a buy of *side*, or
    has an unreadable count/price. An empty list is a valid summary of
    zero contracts (the caller decides whether zero is confirmed)."""
    side = (side or "").lower()
    if side not in ("yes", "no"):
        return None
    total_qty = Decimal("0")
    total_cost = Decimal("0")
    fees_total: Decimal | None = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        if (fill.get("side") or "").lower() != side or (fill.get("action") or "buy").lower() != "buy":
            return None
        count = _to_decimal(fill.get("count") if fill.get("count") is not None else fill.get("count_fp"))
        if count is None or count <= 0 or count != count.to_integral_value():
            return None
        price = _fill_price(fill, side)
        if price is None:
            return None
        total_qty += count
        total_cost += count * price
        fee = _to_decimal(fill.get("fee_cost"))
        if fee is None or fee < 0:
            fees_total = None
        elif fees_total is not None:
            fees_total += fee
    if total_qty == 0:
        return FillSummary(Decimal("0"), None, Decimal("0") if fees_total is not None else None)
    return FillSummary(total_qty, total_cost / total_qty, fees_total)


def reconcile_execution(conn: Any, execution: dict, provider: Any) -> str:
    """Reconcile ONE execution row. Returns "RECONCILED", "PENDING" (venue
    data not conclusive yet) or "ERROR"; never raises."""
    order_id = execution.get("provider_order_id")
    side = (execution.get("side") or "").lower()
    try:
        fills = provider.get_fills(order_id=order_id)
        if fills is None:
            return "ERROR"
        summary = summarize_kalshi_fills(fills, side)
        if summary is None:
            logger.warning("Kalshi fills for an order could not be parsed unambiguously; leaving unreconciled")
            return "ERROR"
        if summary.quantity == 0:
            order = provider.get_order_by_id(order_id)
            if not order or (order.get("status") or "").lower() not in _TERMINAL_ORDER_STATUSES:
                return "PENDING"   # zero fills but the order isn't terminal (or unreadable) -- not conclusive
    except Exception:
        logger.exception("Kalshi reconciliation failed for one execution")
        return "ERROR"

    requested = _to_decimal(execution.get("requested_quantity"))
    if summary.fees_usd is not None:
        fees, fees_source = summary.fees_usd, "PLATFORM"
    elif summary.quantity > 0:
        try:
            fees = provider.estimate_fees(side.upper(), summary.avg_price, summary.quantity).fee
            fees_source = "ESTIMATED"
        except Exception:
            fees, fees_source = None, None
    else:
        fees, fees_source = Decimal("0"), "PLATFORM"

    if summary.quantity == 0:
        status = execution.get("status")
    elif requested is not None and summary.quantity < requested:
        status = "PARTIALLY_FILLED"
    else:
        status = "EXECUTED"

    conn.execute(
        """UPDATE customer_autobet_executions
              SET filled_quantity = ?, avg_fill_price = ?, price_at_execution = COALESCE(?, price_at_execution),
                  fees_usd = ?, fees_source = ?, fill_source = 'PLATFORM_FILLS', reconciled_at = ?, status = ?
            WHERE execution_id = ?""",
        (
            float(summary.quantity),
            float(summary.avg_price) if summary.avg_price is not None else None,
            float(summary.avg_price) if summary.avg_price is not None else None,
            float(fees) if fees is not None else None, fees_source,
            datetime.now(timezone.utc).isoformat(), status, execution["execution_id"],
        ),
    )
    conn.commit()
    return "RECONCILED"


def reconcile_customer_fills(conn: Any) -> dict:
    """Reconcile every unreconciled LIVE Kalshi execution that has a venue
    order id (called from customer_autobet.run_customer_autobet_pass).
    Each customer's provider is built from THEIR OWN credentials; one
    customer's failure never affects another's."""
    from src.execution.customer_autobet import _PLATFORM_TABLE, _build_customer_provider

    counts = {"reconciled": 0, "pending": 0, "errors": 0}
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM customer_autobet_executions
            WHERE platform = 'kalshi' AND mode = 'LIVE' AND provider_order_id IS NOT NULL
              AND status IN ('EXECUTED', 'PARTIALLY_FILLED')
              AND (fill_source IS NULL OR fill_source != 'PLATFORM_FILLS')
            ORDER BY created_at LIMIT 500"""
    ).fetchall()]
    providers: dict[str, Any] = {}
    for execution in rows:
        account_id = execution["account_id"]
        if account_id not in providers:
            account_row = conn.execute(
                f"SELECT * FROM {_PLATFORM_TABLE['kalshi']} WHERE account_id = ?", (account_id,)
            ).fetchone()
            providers[account_id] = (
                _build_customer_provider(dict(account_row), "kalshi") if account_row is not None else None
            )
        provider = providers[account_id]
        if provider is None:
            counts["errors"] += 1
            continue
        outcome = reconcile_execution(conn, execution, provider)
        counts[{"RECONCILED": "reconciled", "PENDING": "pending"}.get(outcome, "errors")] += 1
    return counts
