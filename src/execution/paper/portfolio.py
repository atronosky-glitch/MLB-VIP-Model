"""DB-touching portfolio view: current bankroll, open exposure by
event/provider/sport, the RiskContext RiskEngine needs, and daily/
range performance stats -- all computed on demand from
paper_accounts/paper_orders/paper_positions (see store.py), never a
second mutable "stats" table that could drift from the rows it
summarizes (mirrors src/tracker.py::compute_performance()'s existing
on-the-fly pattern).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.execution.bankroll import Bankroll, compute_bankroll
from src.execution.paper import store
from src.execution.risk import RiskContext


def get_bankroll(conn: Any, account_id: str = store.DEFAULT_ACCOUNT_ID) -> Bankroll:
    account = store.get_account(conn, account_id)
    if account is None:
        raise ValueError(f"no paper account {account_id!r} -- call store.get_or_create_account first")
    open_cost = store.sum_open_exposure(conn, account_id)
    return compute_bankroll(
        starting_bankroll=Decimal(str(account["starting_bankroll_usd"])),
        cash=Decimal(str(account["cash_usd"])),
        open_position_cost=open_cost,
        realized_pnl=Decimal(str(account["realized_pnl_usd"])),
    )


def get_risk_context(
    conn: Any,
    event_id: str,
    provider: str,
    league: str,
    fingerprint: str,
    account_id: str = store.DEFAULT_ACCOUNT_ID,
) -> RiskContext:
    bankroll = get_bankroll(conn, account_id)
    return RiskContext(
        available_bankroll_usd=bankroll.available_bankroll,
        open_positions_count=store.count_open_positions(conn, account_id),
        event_exposure_usd=store.sum_open_exposure(conn, account_id, event_id=event_id),
        provider_exposure_usd=store.sum_open_exposure(conn, account_id, provider=provider),
        sport_exposure_usd=store.sum_open_exposure(conn, account_id, league=league),
        total_open_exposure_usd=store.sum_open_exposure(conn, account_id),
        daily_wagered_usd=store.sum_daily_wagered(conn, account_id),
        daily_realized_pnl_usd=store.sum_daily_realized_pnl(conn, account_id),
        trades_in_last_hour=store.count_trades_in_last_hour(conn, account_id),
        has_open_duplicate=store.get_open_position_by_fingerprint(conn, fingerprint) is not None,
        has_settled_duplicate=store.get_settled_position_by_fingerprint(conn, fingerprint) is not None,
    )


def compute_daily_stats(
    conn: Any, unit_size_usd: Decimal, account_id: str = store.DEFAULT_ACCOUNT_ID, date: str | None = None,
) -> dict[str, Any]:
    """Section 20/27's daily breakdown, for exactly one calendar date
    (UTC), computed fresh from paper_orders/paper_positions."""
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _compute_stats_for_range(conn, account_id, date, date, unit_size_usd)


def compute_stats_range(
    conn: Any, unit_size_usd: Decimal, account_id: str = store.DEFAULT_ACCOUNT_ID, days: int = 7,
) -> dict[str, Any]:
    """The trailing *days* calendar days (UTC), inclusive of today."""
    row = conn.execute("SELECT date('now', ?) AS start_date", (f"-{days - 1} days",)).fetchone()
    start_date = row["start_date"]
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _compute_stats_for_range(conn, account_id, start_date, end_date, unit_size_usd)


def _compute_stats_for_range(
    conn: Any, account_id: str, start_date: str, end_date: str, unit_size_usd: Decimal,
) -> dict[str, Any]:
    orders = conn.execute(
        "SELECT * FROM paper_orders WHERE account_id = ? AND status IN ('FILLED', 'PARTIALLY_FILLED') "
        "AND date(created_at) BETWEEN ? AND ?",
        (account_id, start_date, end_date),
    ).fetchall()
    positions = conn.execute(
        "SELECT * FROM paper_positions WHERE account_id = ? AND date(opened_at) BETWEEN ? AND ?",
        (account_id, start_date, end_date),
    ).fetchall()

    account = store.get_account(conn, account_id)
    ending_bankroll = get_bankroll(conn, account_id).equity if account else None

    trades = len(orders)
    amount_risked = sum((Decimal(str(o["approved_stake"] or 0)) for o in orders), Decimal("0"))
    fees = sum((Decimal(str((_position_fees(p))) ) for p in positions), Decimal("0"))
    avg_net_ev = (
        sum((Decimal(str(o["net_ev_pct"] or 0)) for o in orders), Decimal("0")) / trades
        if trades else Decimal("0")
    )

    wins = sum(1 for p in positions if p["status"] == "WON")
    losses = sum(1 for p in positions if p["status"] == "LOST")
    voids = sum(1 for p in positions if p["status"] == "VOID")
    pending = sum(1 for p in positions if p["status"] == "OPEN")

    settled = [p for p in positions if p["status"] in ("WON", "LOST", "VOID")]
    gross_payout = sum((Decimal(str(p["settlement_value"] or 0)) for p in settled), Decimal("0"))
    realized_pnl = sum((Decimal(str(p["realized_pnl"] or 0)) for p in settled), Decimal("0"))

    pnls = [Decimal(str(p["realized_pnl"])) for p in settled if p["realized_pnl"] is not None]
    best_trade = max(pnls) if pnls else None
    worst_trade = min(pnls) if pnls else None

    kalshi_pnl = sum(
        (Decimal(str(p["realized_pnl"] or 0)) for p in settled if p["provider"] == "kalshi"), Decimal("0")
    )
    polymarket_pnl = sum(
        (Decimal(str(p["realized_pnl"] or 0)) for p in settled if p["provider"] == "polymarket_us"), Decimal("0")
    )

    roi_pct = (realized_pnl / amount_risked * 100) if amount_risked > 0 else Decimal("0")
    units_won_lost = realized_pnl / unit_size_usd if unit_size_usd > 0 else Decimal("0")

    return {
        "start_date": start_date,
        "end_date": end_date,
        "starting_bankroll": Decimal(str(account["starting_bankroll_usd"])) if account else None,
        "ending_bankroll": ending_bankroll,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "pending": pending,
        "amount_risked": amount_risked,
        "gross_payout": gross_payout,
        "fees": fees,
        "realized_pnl": realized_pnl,
        "roi_pct": roi_pct,
        "units_won_lost": units_won_lost,
        "avg_net_ev_at_entry": avg_net_ev,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "kalshi_pnl": kalshi_pnl,
        "polymarket_us_pnl": polymarket_pnl,
    }


def _position_fees(position_row: dict) -> float:
    return position_row["fees_paid"] or 0
