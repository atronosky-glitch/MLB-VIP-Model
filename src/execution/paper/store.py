"""Raw DB persistence for Stage 3 paper trading. Mirrors
src/execution/opportunity_store.py's split from evaluator.py: broker.py
stays testable against fakes, this is the only file that touches SQL.

All money columns are stored as SQLite REAL and converted to/from
Decimal at this boundary (Decimal(str(x)) on read, float(x) on write) --
the same boundary-conversion discipline used throughout src/execution/.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.execution.paper.models import PaperFill, PaperOrder, PaperPosition

DEFAULT_ACCOUNT_ID = "default"


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


# ── Account / bankroll ──────────────────────────────────────────────

def get_account(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID) -> dict | None:
    row = conn.execute("SELECT * FROM paper_accounts WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row is not None else None


def get_or_create_account(
    conn: Any, starting_bankroll_usd: Decimal, account_id: str = DEFAULT_ACCOUNT_ID,
) -> dict:
    existing = get_account(conn, account_id)
    if existing is not None:
        return existing
    conn.execute(
        "INSERT INTO paper_accounts (account_id, starting_bankroll_usd, cash_usd, realized_pnl_usd) "
        "VALUES (?, ?, ?, 0)",
        (account_id, float(starting_bankroll_usd), float(starting_bankroll_usd)),
    )
    conn.commit()
    return get_account(conn, account_id)


def adjust_account_cash(
    conn: Any, account_id: str, delta_cash: Decimal, delta_realized_pnl: Decimal = Decimal("0"),
) -> None:
    conn.execute(
        "UPDATE paper_accounts SET cash_usd = cash_usd + ?, realized_pnl_usd = realized_pnl_usd + ?, "
        "updated_at = datetime('now') WHERE account_id = ?",
        (float(delta_cash), float(delta_realized_pnl), account_id),
    )
    conn.commit()


# ── Orders / fills / positions ───────────────────────────────────────

def persist_paper_order(conn: Any, order: PaperOrder, account_id: str = DEFAULT_ACCOUNT_ID) -> int:
    cursor = conn.execute(
        """INSERT INTO paper_orders (
               account_id, opportunity_id, recommendation_id, provider, provider_market_id,
               league, event, side, sizing_mode, model_probability, net_ev_pct,
               requested_units, requested_stake, approved_units, approved_stake,
               requested_quantity, limit_price, fingerprint, status, rejection_reason,
               limiting_constraint, submitted_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            account_id, order.opportunity_id, order.recommendation_id, order.provider,
            order.provider_market_id, order.league, order.event, order.side, order.sizing_mode,
            _f(order.model_probability), _f(order.net_ev_pct), _f(order.requested_units),
            _f(order.requested_stake), _f(order.approved_units), _f(order.approved_stake),
            _f(order.requested_quantity), _f(order.limit_price), order.fingerprint,
            order.status.value, order.rejection_reason, order.limiting_constraint,
            order.submitted_at.isoformat() if order.submitted_at else None,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def persist_paper_fill(conn: Any, fill: PaperFill) -> int:
    cursor = conn.execute(
        """INSERT INTO paper_fills (
               paper_order_id, quantity_requested, quantity_filled, average_fill_price,
               gross_cost, fees, total_cost, slippage, filled_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fill.paper_order_id, _f(fill.quantity_requested), _f(fill.quantity_filled),
            _f(fill.average_fill_price), _f(fill.gross_cost), _f(fill.fees), _f(fill.total_cost),
            _f(fill.slippage), fill.timestamp.isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def persist_paper_position(conn: Any, position: PaperPosition, account_id: str = DEFAULT_ACCOUNT_ID) -> int:
    cursor = conn.execute(
        """INSERT INTO paper_positions (
               account_id, paper_order_id, recommendation_id, provider, provider_market_id,
               event_id, league, side, quantity, average_entry_price, entry_cost, fees_paid,
               model_probability_at_entry, net_ev_at_entry, fingerprint, opened_at, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            account_id, position.paper_order_id, position.recommendation_id, position.provider,
            position.provider_market_id, position.event_id, position.league, position.side,
            float(position.quantity), _f(position.average_entry_price), float(position.entry_cost),
            float(position.fees_paid), _f(position.model_probability_at_entry),
            _f(position.net_ev_at_entry), position.fingerprint, position.opened_at.isoformat(),
            position.status.value,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def settle_paper_position(
    conn: Any, position_id: int, status: str, settlement_value: Decimal | None, realized_pnl: Decimal | None,
) -> None:
    conn.execute(
        "UPDATE paper_positions SET status = ?, settled_at = datetime('now'), "
        "settlement_value = ?, realized_pnl = ? WHERE position_id = ?",
        (status, _f(settlement_value), _f(realized_pnl), position_id),
    )
    conn.commit()


def get_position_by_id(conn: Any, position_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)).fetchone()
    return dict(row) if row is not None else None


def get_open_positions(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM paper_positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_position_by_fingerprint(conn: Any, fingerprint: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_positions WHERE fingerprint = ? AND status = 'OPEN' LIMIT 1", (fingerprint,)
    ).fetchone()
    return dict(row) if row is not None else None


def get_settled_position_by_fingerprint(conn: Any, fingerprint: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM paper_positions WHERE fingerprint = ? AND status IN ('WON','LOST','VOID') "
        "ORDER BY position_id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return dict(row) if row is not None else None


# ── Exposure / rate-limit queries (small, bounded by MAX_OPEN_POSITIONS) ──

def count_open_positions(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)
    ).fetchone()
    return int(row["n"])


def sum_open_exposure(
    conn: Any, account_id: str = DEFAULT_ACCOUNT_ID, event_id: str | None = None,
    provider: str | None = None, league: str | None = None,
) -> Decimal:
    query = "SELECT COALESCE(SUM(entry_cost), 0) AS total FROM paper_positions WHERE account_id = ? AND status = 'OPEN'"
    params: list[Any] = [account_id]
    if event_id is not None:
        query += " AND event_id = ?"
        params.append(event_id)
    if provider is not None:
        query += " AND provider = ?"
        params.append(provider)
    if league is not None:
        query += " AND league = ?"
        params.append(league)
    row = conn.execute(query, params).fetchone()
    return Decimal(str(row["total"]))


def count_trades_in_last_hour(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_orders WHERE account_id = ? "
        "AND status IN ('FILLED', 'PARTIALLY_FILLED') "
        "AND created_at >= datetime('now', '-1 hour')",
        (account_id,),
    ).fetchone()
    return int(row["n"])


def sum_daily_wagered(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID, date: str | None = None) -> Decimal:
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(approved_stake), 0) AS total FROM paper_orders "
        "WHERE account_id = ? AND status IN ('FILLED', 'PARTIALLY_FILLED') AND date(created_at) = ?",
        (account_id, date),
    ).fetchone()
    return Decimal(str(row["total"]))


def sum_daily_realized_pnl(conn: Any, account_id: str = DEFAULT_ACCOUNT_ID, date: str | None = None) -> Decimal:
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM paper_positions "
        "WHERE account_id = ? AND date(settled_at) = ?",
        (account_id, date),
    ).fetchone()
    return Decimal(str(row["total"]))


# ── Risk decisions (full audit row, every evaluation, approved or not) ──

def persist_risk_decision(conn: Any, data: dict[str, Any]) -> int:
    cursor = conn.execute(
        """INSERT INTO risk_decisions (
               recommendation_id, provider, opportunity_id, paper_order_id,
               recommended_stake_usd, approved_stake_usd, approved, rejection_reason,
               limiting_constraint, bankroll_before, event_exposure_before,
               provider_exposure_before, sport_exposure_before, daily_exposure_before,
               daily_pnl_before, open_positions_before
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["recommendation_id"], data["provider"], data.get("opportunity_id"),
            data.get("paper_order_id"), _f(data.get("recommended_stake_usd")),
            _f(data.get("approved_stake_usd")), 1 if data["approved"] else 0,
            data.get("rejection_reason"), data.get("limiting_constraint"),
            _f(data.get("bankroll_before")), _f(data.get("event_exposure_before")),
            _f(data.get("provider_exposure_before")), _f(data.get("sport_exposure_before")),
            _f(data.get("daily_exposure_before")), _f(data.get("daily_pnl_before")),
            data.get("open_positions_before"),
        ),
    )
    conn.commit()
    return cursor.lastrowid
