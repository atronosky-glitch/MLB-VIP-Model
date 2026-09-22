"""Per-customer-scoped queries against the live-execution tables
(2026-09-21). Every existing table/function in store.py is completely
unmodified -- this module ADDS a new, parallel account_id-scoped query
path so a customer's exposure/position/duplicate checks never mix with
the operator's own manual Streamlit live-trading (account_id IS NULL)
or with another customer's numbers.

Tagging: prepared_live_orders/execution_authorizations/live_positions
each carry an additive, nullable account_id column (see
database/db_manager.py's init_db). A row is tagged via
tag_prepared_order_with_account/tag_authorization_with_account/
tag_position_with_account right after the EXISTING, unmodified
src.execution.live.approval/store functions create it -- so those
functions themselves never need to know about customers at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def tag_prepared_order_with_account(conn: Any, prepared_order_id: int, account_id: str) -> None:
    conn.execute(
        "UPDATE prepared_live_orders SET account_id = ? WHERE prepared_order_id = ?",
        (account_id, prepared_order_id),
    )
    conn.commit()


def tag_authorization_with_account(conn: Any, approval_id: str, account_id: str, approval_mode: str) -> None:
    conn.execute(
        "UPDATE execution_authorizations SET account_id = ?, approval_mode = ? WHERE approval_id = ?",
        (account_id, approval_mode, approval_id),
    )
    conn.commit()


def tag_position_with_account(conn: Any, position_id: int, account_id: str) -> None:
    conn.execute(
        "UPDATE live_positions SET account_id = ? WHERE position_id = ?",
        (account_id, position_id),
    )
    conn.commit()


def get_account_id_for_prepared_order(conn: Any, prepared_order_id: int) -> str | None:
    row = conn.execute(
        "SELECT account_id FROM prepared_live_orders WHERE prepared_order_id = ?", (prepared_order_id,)
    ).fetchone()
    return dict(row)["account_id"] if row else None


def get_account_id_for_position(conn: Any, position_id: int) -> str | None:
    row = conn.execute(
        "SELECT account_id FROM live_positions WHERE position_id = ?", (position_id,)
    ).fetchone()
    return dict(row)["account_id"] if row else None


def count_open_live_positions_for_account(conn: Any, account_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM live_positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)
    ).fetchone()
    return dict(row)["c"]


def sum_open_live_exposure_for_account(
    conn: Any, account_id: str, *, event_id: str | None = None,
    provider: str | None = None, league: str | None = None,
) -> Decimal:
    """Mirrors store.sum_open_live_exposure's shape exactly, scoped to
    one customer. league isn't a live_positions column -- joins through
    prepared_live_orders (which does carry league) the same way a
    global league-exposure figure would need to, if store.py's own
    version ever added that filter."""
    query = """
        SELECT COALESCE(SUM(lp.total_entry_cost), 0) AS total
        FROM live_positions lp
    """
    joins = ""
    where = ["lp.account_id = ?", "lp.status = 'OPEN'"]
    params: list[Any] = [account_id]
    if league is not None:
        joins = " JOIN prepared_live_orders plo ON plo.prepared_order_id = lp.prepared_order_id"
        where.append("plo.league = ?")
        params.append(league)
    if event_id is not None:
        where.append("lp.event_id = ?")
        params.append(event_id)
    if provider is not None:
        where.append("lp.provider = ?")
        params.append(provider)
    query += joins + " WHERE " + " AND ".join(where)
    row = conn.execute(query, tuple(params)).fetchone()
    return Decimal(str(dict(row)["total"] or 0))


def sum_daily_live_wagered_for_account(conn: Any, account_id: str, date: str | None = None) -> Decimal:
    """Mirrors store.sum_daily_live_wagered's table/column choices
    (live_orders, joined through prepared_live_orders for account_id
    since live_orders itself carries no customer tag)."""
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        """SELECT COALESCE(SUM(lo.quantity_filled * lo.average_fill_price + lo.fees), 0) AS total
           FROM live_orders lo
           JOIN prepared_live_orders plo ON plo.prepared_order_id = lo.prepared_order_id
           WHERE plo.account_id = ? AND lo.status IN ('FILLED', 'PARTIALLY_FILLED')
             AND date(lo.submitted_at) = ?""",
        (account_id, day),
    ).fetchone()
    return Decimal(str(dict(row)["total"] or 0))


def sum_daily_live_realized_pnl_for_account(conn: Any, account_id: str, date: str | None = None) -> Decimal:
    """Mirrors store.sum_daily_live_realized_pnl -- live_positions
    directly, which does carry account_id."""
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM live_positions "
        "WHERE account_id = ? AND date(settled_at) = ?",
        (account_id, day),
    ).fetchone()
    return Decimal(str(dict(row)["total"] or 0))


def count_live_trades_in_last_hour_for_account(conn: Any, account_id: str) -> int:
    """Mirrors store.count_live_trades_in_last_hour's table choice
    (live_orders, joined through prepared_live_orders for account_id)."""
    row = conn.execute(
        """SELECT COUNT(*) AS c FROM live_orders lo
           JOIN prepared_live_orders plo ON plo.prepared_order_id = lo.prepared_order_id
           WHERE plo.account_id = ? AND lo.status IN ('FILLED', 'PARTIALLY_FILLED')
             AND lo.submitted_at >= datetime('now', '-1 hour')""",
        (account_id,),
    ).fetchone()
    return dict(row)["c"]


def get_ready_prepared_orders_for_account(conn: Any, account_id: str) -> list[dict]:
    """Powers the customer-facing manual-approval queue (over-cap LIVE
    orders src.execution.customer_autobet's hybrid design queues rather
    than auto-submits) -- READY orders tagged to THIS account only,
    newest first."""
    rows = conn.execute(
        "SELECT * FROM prepared_live_orders WHERE account_id = ? AND status = 'READY' "
        "ORDER BY created_at DESC",
        (account_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_live_position_by_fingerprint_for_account(
    conn: Any, account_id: str, recommendation_id: str, provider: str, side: str,
) -> dict | None:
    row = conn.execute(
        """SELECT * FROM live_positions
           WHERE account_id = ? AND recommendation_id = ? AND provider = ? AND side = ? AND status = 'OPEN'""",
        (account_id, recommendation_id, provider, side),
    ).fetchone()
    return dict(row) if row else None
