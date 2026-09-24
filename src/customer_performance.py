"""Per-customer Auto-Bet performance (My Performance page).

Powers the customer-private "My Performance" section -- profit/loss, ROI,
win rate and the cumulative-P&L graph for a logged-in customer's OWN
Auto-Bet executions. Completely separate from the model's public track
record (src/grading.py): this only ever aggregates rows from
customer_autobet_executions for ONE account_id.

REAL EXECUTION ECONOMICS (2026-09-23). Performance is computed from what
the customer's order actually did, never from the recommendation or the
intended stake:

* cost basis  = filled_quantity x avg_fill_price -- the contracts really
  bought at the price really paid. The approved/intended stake_usd is
  NOT used. An order that did not fill (0 contracts) is not a wager; a
  partial fill counts only its filled part, and the unfilled remainder
  (requested - filled) is reported separately, never as money at risk.
* fees        = fees_usd as recorded (platform-reported after Kalshi
  reconciliation, otherwise a documented-formula estimate, otherwise
  unknown). The headline P&L is labeled NET only when EVERY settled
  execution has a fee figure; if any is estimated it says so, and if any
  is missing the headline is GROSS and says so -- never presented as net.
* settlement  = the event's real-world result from market_settlements
  (the codebase's own verified grading pipeline), applied to the
  customer's actual contracts. Kalshi/Polymarket contracts pay exactly
  $1.00 per contract on a win and $0 on a loss, and a recommendation is
  only ever mapped to the contract whose payoff IS "the recommended side
  wins" (see src/execution/matching.py), so WIN pays filled_quantity.
* PUSH / VOID / CANCELLED: neither venue's refund/void handling is read
  back by this module, so these are NOT booked as P&L. They are counted
  separately (refund_pending) with their cost, excluded from realized P&L
  and from the ROI base, until a platform refund is confirmed.
* no settlement row / UNRESOLVED: an OPEN position -- unrealized, never a
  win, loss or zero.

Realized P&L per settled execution:
    gross:  WIN  = filled_quantity x 1.00 - cost      LOSS = - cost
    net:    gross - fees_usd
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

_SETTLED_WIN_LOSS = {"WIN", "LOSS"}
_REFUND_PENDING = {"PUSH", "VOID", "CANCELLED"}
_COUNTED_EXECUTION_STATUSES = ("EXECUTED", "PARTIALLY_FILLED")

# fill_source values whose quantity/price came from the venue's own report
# of what was actually paid (or, for PAPER, a deliberate simulation).
_CONFIRMED_FILL_SOURCES = {"PLATFORM_FILLS", "ORDER_RESPONSE"}

PNL_BASIS_NET = "NET"
PNL_BASIS_NET_ESTIMATED = "NET_ESTIMATED_FEES"
PNL_BASIS_GROSS = "GROSS"
_PNL_LABELS = {
    PNL_BASIS_NET: "Net P&L (after platform-reported fees)",
    PNL_BASIS_NET_ESTIMATED: "Net P&L (fees estimated)",
    PNL_BASIS_GROSS: "Gross P&L (fees not available)",
}


def _get_settlement_statuses(conn: Any, recommendation_ids: list[str]) -> dict[str, dict]:
    """Bulk lookup: recommendation_id -> market_settlements row, for every
    id that has one. Missing from the returned dict means unresolved/never
    graded -- callers must treat that as OPEN, never as any outcome."""
    if not recommendation_ids:
        return {}
    placeholders = ",".join("?" * len(recommendation_ids))
    rows = conn.execute(
        f"SELECT recommendation_id, settlement_status, settled_at FROM market_settlements "
        f"WHERE recommendation_id IN ({placeholders})",
        tuple(recommendation_ids),
    ).fetchall()
    return {dict(r)["recommendation_id"]: dict(r) for r in rows}


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _gross_pnl(quantity: Decimal, cost: Decimal, settlement_status: str) -> Decimal:
    if settlement_status == "WIN":
        return quantity * Decimal("1.00") - cost
    return -cost  # LOSS


def get_customer_performance(
    conn: Any, account_id: str, *, platform: str | None = None,
    mode: str = "LIVE", days: int | None = None,
) -> dict:
    """Aggregate Auto-Bet performance for ONE customer.

    *platform*: "kalshi" / "polymarket_us" / None (both combined).
    *mode*: "LIVE" or "PAPER" -- never blended (a customer's simulated
    results are not their real financial performance); pass explicitly.
    *days*: lookback window in days, or None for all-time.
    """
    if mode not in ("LIVE", "PAPER"):
        raise ValueError(f"mode must be 'LIVE' or 'PAPER', got {mode!r}")

    query = (
        "SELECT * FROM customer_autobet_executions WHERE account_id = ? AND mode = ? "
        "AND status IN ('EXECUTED', 'PARTIALLY_FILLED')"
    )
    params: list[Any] = [account_id, mode]
    if platform == "polymarket_us":
        query += " AND (platform = ? OR platform IS NULL)"
        params.append(platform)
    elif platform is not None:
        query += " AND platform = ?"
        params.append(platform)
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query += " AND created_at >= ?"
        params.append(cutoff)

    executions = [dict(r) for r in conn.execute(query, tuple(params)).fetchall()]
    settlements = _get_settlement_statuses(
        conn, [e["recommendation_id"] for e in executions if e.get("recommendation_id")],
    )

    filled_bets = 0
    unfilled_orders = 0          # accepted orders that bought nothing
    unconfirmed_fill_orders = 0  # a quantity but no readable fill price
    partial_fills = 0
    unfilled_contracts = Decimal("0")
    unreconciled_orders = 0      # LIVE fills not yet confirmed against the venue

    total_wagered = Decimal("0")     # actual cost of everything filled (open + settled)
    open_count = 0
    open_exposure = Decimal("0")     # cost + known fees of open filled positions
    refund_pending_count = 0
    refund_pending_usd = Decimal("0")

    # (settled_at, platform, gross, fees|None, fees_source, cost, wl_status)
    settled: list[tuple] = []

    for e in executions:
        quantity = _dec(e.get("filled_quantity")) or Decimal("0")
        price = _dec(e.get("avg_fill_price"))
        requested = _dec(e.get("requested_quantity"))

        if quantity <= 0:
            unfilled_orders += 1
            if requested is not None and requested > 0:
                unfilled_contracts += requested
            continue
        if price is None or price <= 0:
            unconfirmed_fill_orders += 1   # can't price it -- not counted, never guessed
            continue

        filled_bets += 1
        if requested is not None and requested > quantity:
            partial_fills += 1
            unfilled_contracts += requested - quantity
        if mode == "LIVE" and (e.get("fill_source") not in _CONFIRMED_FILL_SOURCES):
            unreconciled_orders += 1

        cost = quantity * price
        fees = _dec(e.get("fees_usd"))
        total_wagered += cost
        plat = e.get("platform") or "polymarket_us"

        settlement = settlements.get(e.get("recommendation_id"))
        status = settlement["settlement_status"] if settlement else None

        if status in _SETTLED_WIN_LOSS:
            settled_at = (settlement or {}).get("settled_at") or e.get("created_at")
            settled.append((
                settled_at, plat, _gross_pnl(quantity, cost, status), fees,
                e.get("fees_source"), cost, status,
            ))
        elif status in _REFUND_PENDING:
            refund_pending_count += 1
            refund_pending_usd += cost + (fees or Decimal("0"))
        else:
            open_count += 1
            open_exposure += cost + (fees or Decimal("0"))

    # Headline basis is decided over ALL settled executions so one number
    # is never a mix of net and gross figures.
    if any(fees is None for _t, _p, _g, fees, _s, _c, _st in settled):
        basis = PNL_BASIS_GROSS
    elif any(src != "PLATFORM" for _t, _p, _g, _f, src, _c, _st in settled):
        basis = PNL_BASIS_NET_ESTIMATED
    else:
        basis = PNL_BASIS_NET

    realized_pnl = Decimal("0")
    gross_total = Decimal("0")
    fees_total = Decimal("0")
    settled_cost = Decimal("0")
    wins = losses = 0
    by_platform: dict[str, Decimal] = {"kalshi": Decimal("0"), "polymarket_us": Decimal("0")}
    timeseries: list[tuple[str, Decimal]] = []

    for settled_at, plat, gross, fees, _src, cost, status in settled:
        headline = gross if basis == PNL_BASIS_GROSS else gross - (fees or Decimal("0"))
        realized_pnl += headline
        gross_total += gross
        fees_total += fees or Decimal("0")
        settled_cost += cost + (Decimal("0") if basis == PNL_BASIS_GROSS else (fees or Decimal("0")))
        by_platform[plat] = by_platform.get(plat, Decimal("0")) + headline
        if status == "WIN":
            wins += 1
        else:
            losses += 1
        timeseries.append((settled_at, headline))

    timeseries.sort(key=lambda t: t[0] or "")
    running = Decimal("0")
    cumulative_series = []
    for ts, pnl in timeseries:
        running += pnl
        cumulative_series.append({"date": ts, "cumulative_pnl": float(running)})

    decided = wins + losses
    win_rate_pct = float(wins / decided * 100) if decided > 0 else None
    roi_pct = float(realized_pnl / settled_cost * 100) if settled_cost > 0 else None

    return {
        "account_id": account_id, "platform": platform, "mode": mode, "days": days,
        "simulated": mode == "PAPER",
        # bets = orders that actually bought contracts
        "total_bets": filled_bets,
        "orders_placed": len(executions),
        "unfilled_orders": unfilled_orders,
        "partial_fills": partial_fills,
        "unfilled_contracts": float(unfilled_contracts),
        "unconfirmed_fill_orders": unconfirmed_fill_orders,
        "total_wagered_usd": float(total_wagered),
        "settled_wagered_usd": float(settled_cost),
        "realized_pnl_usd": float(realized_pnl),
        "gross_pnl_usd": float(gross_total),
        "fees_usd": float(fees_total),
        "pnl_basis": basis,
        "pnl_label": _PNL_LABELS[basis],
        "roi_pct": roi_pct,
        "wins": wins, "losses": losses,
        "pushes_voids": refund_pending_count,
        "refund_pending_usd": float(refund_pending_usd),
        "win_rate_pct": win_rate_pct,
        "open_positions": open_count,
        "open_exposure_usd": float(open_exposure),
        "fills_confirmed": unreconciled_orders == 0,
        "unreconciled_orders": unreconciled_orders,
        "realized_pnl_by_platform": {k: float(v) for k, v in by_platform.items()},
        "cumulative_pnl_series": cumulative_series,
    }
