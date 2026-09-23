"""Per-customer Auto-Bet performance (My Performance page, 2026-09-23).

Powers the customer-private "My Performance" section -- profit/loss,
ROI, win rate, and the cumulative-P&L graph for a logged-in customer's
OWN Auto-Bet executions. Completely separate from the model's public
track record (src/grading.py) -- this only ever aggregates rows from
customer_autobet_executions for ONE account_id.

Settlement source (a deliberate design decision, not a shortcut):
neither platform's read API can be trusted equally -- Kalshi's
get_positions()/get_fills() are real and confirmed
(src/execution/kalshi.py), but Polymarket US's equivalent was never
confirmed (capabilities.supports_order_lookup=False, see
src/execution/polymarket_us.py) and this project's own discipline is
to never guess an unconfirmed endpoint. Building two different,
asymmetric reconciliation paths -- one trustworthy, one fabricated --
would be exactly the kind of misleading statistic the product spec
explicitly forbids.

Instead, EVERY execution (either platform, PAPER or LIVE) is settled
from this codebase's own already-verified real-world grading pipeline
(market_settlements, written by src/game_settlement.py /
src/automatic_grading.py for the sportsbook-side model record). This
is not an approximation: a Kalshi/Polymarket YES contract on "the
model's recommended side happens" settles WIN if and only if that same
real-world event resolves in the recommended side's favor -- the exact
fact market_settlements already tracks, independent of which platform
(if any) a customer traded it on. Reusing it means every Auto-Bet
execution across both platforms is graded from ONE consistent, already
production-verified source of truth, rather than two different partial
ones.

Contracts on both Kalshi and Polymarket settle at exactly $1.00/contract
on a win, $0 on a loss (both confirmed in their own execution-layer
citations) -- so realized P&L per execution is:
    WIN:                filled_quantity * 1.00 - stake_usd
    LOSS:                                       - stake_usd
    PUSH / VOID / CANCELLED:                       0 (stake refunded)
    unresolved / no market_settlements row:     OPEN (not yet counted)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

_SETTLED_STATUSES = {"WIN", "LOSS", "PUSH", "VOID", "CANCELLED"}
_COUNTED_EXECUTION_STATUSES = ("EXECUTED", "PARTIALLY_FILLED")


def _get_settlement_statuses(conn: Any, recommendation_ids: list[str]) -> dict[str, str]:
    """Bulk lookup: recommendation_id -> settlement_status, for every
    id that has a market_settlements row. Missing from the returned
    dict means unresolved/never graded -- callers must treat that as
    OPEN, never as any particular outcome."""
    if not recommendation_ids:
        return {}
    placeholders = ",".join("?" * len(recommendation_ids))
    rows = conn.execute(
        f"SELECT recommendation_id, settlement_status, settled_at FROM market_settlements "
        f"WHERE recommendation_id IN ({placeholders})",
        tuple(recommendation_ids),
    ).fetchall()
    return {dict(r)["recommendation_id"]: dict(r) for r in rows}


def _realized_pnl(execution: dict, settlement_status: str) -> Decimal:
    stake = Decimal(str(execution.get("stake_usd") or 0))
    filled_quantity = Decimal(str(execution.get("filled_quantity") or 0))
    if settlement_status == "WIN":
        return (filled_quantity * Decimal("1.00")) - stake
    if settlement_status == "LOSS":
        return -stake
    return Decimal("0")  # PUSH / VOID / CANCELLED -- stake refunded, no gain or loss


def get_customer_performance(
    conn: Any, account_id: str, *, platform: str | None = None,
    mode: str = "LIVE", days: int | None = None,
) -> dict:
    """Aggregate Auto-Bet performance for ONE customer.

    *platform*: "kalshi" / "polymarket_us" / None (both combined).
    *mode*: "LIVE" or "PAPER" -- these must never be blended into one
    number (a customer's simulated results are not their real
    financial performance); pass explicitly, no default "both".
    *days*: lookback window in days, or None for all-time.

    Returns a dict with summary stats, a per-platform breakdown, and a
    cumulative-P&L timeseries (settled executions only, ordered by
    settlement date) -- see this module's docstring for exactly how
    "settled" and "realized P&L" are determined and why.
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

    rec_ids = [e["recommendation_id"] for e in executions if e.get("recommendation_id")]
    settlements = _get_settlement_statuses(conn, rec_ids)

    total_wagered = Decimal("0")
    settled_wagered = Decimal("0")
    realized_pnl = Decimal("0")
    wins = losses = pushes = 0
    open_count = 0
    open_exposure = Decimal("0")
    by_platform: dict[str, Decimal] = {"kalshi": Decimal("0"), "polymarket_us": Decimal("0")}
    timeseries: list[tuple[str, Decimal]] = []

    for execution in executions:
        stake = Decimal(str(execution.get("stake_usd") or 0))
        total_wagered += stake
        plat = execution.get("platform") or "polymarket_us"

        settlement = settlements.get(execution.get("recommendation_id"))
        status = settlement["settlement_status"] if settlement else None
        if status not in _SETTLED_STATUSES:
            open_count += 1
            open_exposure += stake
            continue

        pnl = _realized_pnl(execution, status)
        settled_wagered += stake
        realized_pnl += pnl
        by_platform[plat] = by_platform.get(plat, Decimal("0")) + pnl
        if status == "WIN":
            wins += 1
        elif status == "LOSS":
            losses += 1
        else:
            pushes += 1
        settled_at = settlement.get("settled_at") or execution.get("created_at")
        timeseries.append((settled_at, pnl))

    timeseries.sort(key=lambda t: t[0] or "")
    running = Decimal("0")
    cumulative_series = []
    for ts, pnl in timeseries:
        running += pnl
        cumulative_series.append({"date": ts, "cumulative_pnl": float(running)})

    decided = wins + losses
    win_rate_pct = float(wins / decided * 100) if decided > 0 else None
    roi_pct = float(realized_pnl / settled_wagered * 100) if settled_wagered > 0 else None

    return {
        "account_id": account_id, "platform": platform, "mode": mode, "days": days,
        "total_bets": len(executions),
        "total_wagered_usd": float(total_wagered),
        "settled_wagered_usd": float(settled_wagered),
        "realized_pnl_usd": float(realized_pnl),
        "roi_pct": roi_pct,
        "wins": wins, "losses": losses, "pushes_voids": pushes,
        "win_rate_pct": win_rate_pct,
        "open_positions": open_count,
        "open_exposure_usd": float(open_exposure),
        "realized_pnl_by_platform": {k: float(v) for k, v in by_platform.items()},
        "cumulative_pnl_series": cumulative_series,
    }
