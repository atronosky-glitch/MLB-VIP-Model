"""Raw DB persistence for Stage 4 live execution. Mirrors
src/execution/paper/store.py's split from broker.py -- approval.py/
revalidation.py/service.py stay testable against fakes, this is the
only file that touches SQL.

Money columns are stored as SQLite REAL and converted to/from Decimal
at this boundary, matching every other execution-layer store module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.execution.live.models import (
    ExecutionAuthorization, LiveFill, LiveOrder, LivePosition, LiveSubmissionAttempt, PreparedLiveOrder,
)


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── PreparedLiveOrder ────────────────────────────────────────────────

def persist_prepared_order(conn: Any, order: PreparedLiveOrder) -> int:
    cursor = conn.execute(
        """INSERT INTO prepared_live_orders (
               opportunity_id, recommendation_id, provider, provider_market_id, league, event, event_id,
               side, event_start_time, model_probability, current_price, expected_fill_price, net_ev_pct,
               recommended_units, recommended_stake, risk_approved_stake, risk_approved_units, quantity,
               maximum_entry_price, fees_estimate, slippage_estimate, available_liquidity, fingerprint,
               risk_snapshot, status, expires_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order.opportunity_id, order.recommendation_id, order.provider, order.provider_market_id,
            order.league, order.event, order.event_id, order.side,
            order.event_start_time.isoformat() if order.event_start_time else None,
            _f(order.model_probability), _f(order.current_price), _f(order.expected_fill_price),
            _f(order.net_ev_pct), _f(order.recommended_units), _f(order.recommended_stake),
            _f(order.risk_approved_stake), _f(order.risk_approved_units), _f(order.quantity),
            _f(order.maximum_entry_price), _f(order.fees_estimate), _f(order.slippage_estimate),
            _f(order.available_liquidity), order.fingerprint,
            json.dumps(order.risk_snapshot, default=str), order.status.value, order.expires_at.isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_prepared_order(conn: Any, prepared_order_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM prepared_live_orders WHERE prepared_order_id = ?", (prepared_order_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def set_prepared_order_status(conn: Any, prepared_order_id: int, status: str) -> None:
    conn.execute(
        "UPDATE prepared_live_orders SET status = ? WHERE prepared_order_id = ?", (status, prepared_order_id)
    )
    conn.commit()


def get_ready_prepared_orders(conn: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM prepared_live_orders WHERE status = 'READY' ORDER BY net_ev_pct DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_open_prepared_order_by_fingerprint(conn: Any, fingerprint: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM prepared_live_orders WHERE fingerprint = ? AND status IN ('READY', 'APPROVED') "
        "ORDER BY prepared_order_id DESC LIMIT 1",
        (fingerprint,),
    ).fetchone()
    return dict(row) if row is not None else None


# ── ExecutionAuthorization ───────────────────────────────────────────

def persist_authorization(conn: Any, authorization: ExecutionAuthorization) -> None:
    conn.execute(
        """INSERT INTO execution_authorizations (
               approval_id, prepared_order_id, opportunity_id, recommendation_id, provider,
               provider_market_id, side, approved_units, approved_stake_usd, approved_quantity,
               approved_max_price, approved_min_net_ev_pct, approved_at, expires_at, approved_by,
               status, used_at, invalidated_at, invalidation_reason
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            authorization.approval_id, authorization.prepared_order_id, authorization.opportunity_id,
            authorization.recommendation_id, authorization.provider, authorization.provider_market_id,
            authorization.side, _f(authorization.approved_units), float(authorization.approved_stake_usd),
            float(authorization.approved_quantity), float(authorization.approved_max_price),
            float(authorization.approved_min_net_ev_pct), authorization.approved_at.isoformat(),
            authorization.expires_at.isoformat(), authorization.approved_by, authorization.status.value,
            authorization.used_at.isoformat() if authorization.used_at else None,
            authorization.invalidated_at.isoformat() if authorization.invalidated_at else None,
            authorization.invalidation_reason,
        ),
    )
    conn.commit()


def get_authorization(conn: Any, approval_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM execution_authorizations WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def claim_authorization_for_execution(conn: Any, approval_id: str) -> bool:
    """The entire single-flight mechanism: an atomic UPDATE that only
    succeeds (rowcount == 1) if the approval is currently APPROVED.
    Two concurrent callers racing on the same approval_id can never
    both win -- SQLite serializes writes to the same row, so exactly
    one UPDATE's WHERE clause matches. No separate lock table needed."""
    cursor = conn.execute(
        "UPDATE execution_authorizations SET status = 'USED', used_at = ? "
        "WHERE approval_id = ? AND status = 'APPROVED'",
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def invalidate_authorization(conn: Any, approval_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE execution_authorizations SET status = 'INVALIDATED', invalidated_at = ?, "
        "invalidation_reason = ? WHERE approval_id = ? AND status IN ('PENDING', 'APPROVED')",
        (datetime.now(timezone.utc).isoformat(), reason, approval_id),
    )
    conn.commit()


def expire_stale_authorizations(conn: Any) -> int:
    cursor = conn.execute(
        "UPDATE execution_authorizations SET status = 'EXPIRED' "
        "WHERE status = 'APPROVED' AND expires_at < ?",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return cursor.rowcount


# ── LiveSubmissionAttempt ────────────────────────────────────────────

def persist_submission_attempt(conn: Any, attempt: LiveSubmissionAttempt) -> None:
    conn.execute(
        """INSERT INTO live_submission_attempts (
               attempt_id, approval_id, prepared_order_id, provider, market_id, side, quantity,
               limit_price, state, request_started_at, response_received_at, provider_order_id,
               reconciliation_status, reconciliation_detail
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            attempt.attempt_id, attempt.approval_id, attempt.prepared_order_id, attempt.provider,
            attempt.market_id, attempt.side, float(attempt.quantity), float(attempt.limit_price),
            attempt.state.value,
            attempt.request_started_at.isoformat() if attempt.request_started_at else None,
            attempt.response_received_at.isoformat() if attempt.response_received_at else None,
            attempt.provider_order_id, attempt.reconciliation_status, attempt.reconciliation_detail,
        ),
    )
    conn.commit()


def update_submission_attempt_state(
    conn: Any, attempt_id: str, state: str, provider_order_id: str | None = None,
    response_received_at: datetime | None = None, reconciliation_status: str | None = None,
    reconciliation_detail: str | None = None,
) -> None:
    conn.execute(
        "UPDATE live_submission_attempts SET state = ?, provider_order_id = COALESCE(?, provider_order_id), "
        "response_received_at = COALESCE(?, response_received_at), "
        "reconciliation_status = COALESCE(?, reconciliation_status), "
        "reconciliation_detail = COALESCE(?, reconciliation_detail) WHERE attempt_id = ?",
        (
            state, provider_order_id, response_received_at.isoformat() if response_received_at else None,
            reconciliation_status, reconciliation_detail, attempt_id,
        ),
    )
    conn.commit()


def get_unknown_submission_attempts(conn: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_submission_attempts WHERE state IN ('SUBMISSION_UNKNOWN', 'MANUAL_REVIEW_REQUIRED')"
    ).fetchall()
    return [dict(r) for r in rows]


# ── LiveOrder / LiveFill / LivePosition ──────────────────────────────

def persist_live_order(conn: Any, order: LiveOrder) -> int:
    cursor = conn.execute(
        """INSERT INTO live_orders (
               approval_id, prepared_order_id, provider, provider_order_id, client_order_id, market_id,
               side, quantity_requested, quantity_filled, limit_price, average_fill_price, fees, status,
               submitted_at, last_updated_at, provider_response_reference
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            order.approval_id, order.prepared_order_id, order.provider, order.provider_order_id,
            order.client_order_id, order.market_id, order.side, float(order.quantity_requested),
            float(order.quantity_filled), float(order.limit_price), _f(order.average_fill_price),
            float(order.fees), order.status.value, order.submitted_at.isoformat(),
            order.last_updated_at.isoformat(), order.provider_response_reference,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def update_live_order_status(
    conn: Any, live_order_id: int, status: str, quantity_filled: Decimal | None = None,
    average_fill_price: Decimal | None = None, provider_order_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE live_orders SET status = ?, quantity_filled = COALESCE(?, quantity_filled), "
        "average_fill_price = COALESCE(?, average_fill_price), "
        "provider_order_id = COALESCE(?, provider_order_id), last_updated_at = ? WHERE live_order_id = ?",
        (
            status, _f(quantity_filled), _f(average_fill_price), provider_order_id,
            datetime.now(timezone.utc).isoformat(), live_order_id,
        ),
    )
    conn.commit()


def get_live_order(conn: Any, live_order_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM live_orders WHERE live_order_id = ?", (live_order_id,)).fetchone()
    return dict(row) if row is not None else None


def persist_live_fill(conn: Any, fill: LiveFill) -> int:
    cursor = conn.execute(
        "INSERT INTO live_fills (provider_fill_id, live_order_id, quantity, price, fees, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            fill.provider_fill_id, fill.live_order_id, float(fill.quantity), float(fill.price),
            float(fill.fees), fill.timestamp.isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def persist_live_position(conn: Any, position: LivePosition) -> int:
    cursor = conn.execute(
        """INSERT INTO live_positions (
               approval_id, prepared_order_id, live_order_id, recommendation_id, provider,
               provider_market_id, event_id, side, quantity, average_entry_price, total_entry_cost,
               fees_paid, opened_at, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            position.approval_id, position.prepared_order_id, position.live_order_id,
            position.recommendation_id, position.provider, position.provider_market_id, position.event_id,
            position.side, float(position.quantity), _f(position.average_entry_price),
            float(position.total_entry_cost), float(position.fees_paid), position.opened_at.isoformat(),
            position.status.value,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_open_live_positions(conn: Any) -> list[dict]:
    rows = conn.execute("SELECT * FROM live_positions WHERE status = 'OPEN'").fetchall()
    return [dict(r) for r in rows]


def get_open_live_position_by_fingerprint(conn: Any, recommendation_id: str, provider: str, side: str) -> dict | None:
    """Live's duplicate check is scoped to (recommendation, provider,
    side) via a join back through prepared_live_orders/live_orders --
    live_positions itself has no fingerprint column (it's keyed by
    prepared_order_id already), so this mirrors Stage 3's fingerprint
    concept without duplicating a column across tables."""
    row = conn.execute(
        "SELECT lp.* FROM live_positions lp WHERE lp.recommendation_id = ? AND lp.provider = ? "
        "AND lp.side = ? AND lp.status = 'OPEN' LIMIT 1",
        (recommendation_id, provider, side),
    ).fetchone()
    return dict(row) if row is not None else None


def count_open_live_positions(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM live_positions WHERE status = 'OPEN'").fetchone()
    return int(row["n"])


def sum_open_live_exposure(
    conn: Any, event_id: str | None = None, provider: str | None = None, league: str | None = None,
) -> Decimal:
    query = "SELECT COALESCE(SUM(total_entry_cost), 0) AS total FROM live_positions WHERE status = 'OPEN'"
    params: list[Any] = []
    if event_id is not None:
        query += " AND event_id = ?"
        params.append(event_id)
    if provider is not None:
        query += " AND provider = ?"
        params.append(provider)
    if league is not None:
        query += (
            " AND prepared_order_id IN (SELECT prepared_order_id FROM prepared_live_orders WHERE league = ?)"
        )
        params.append(league)
    row = conn.execute(query, params).fetchone()
    return Decimal(str(row["total"]))


def sum_daily_live_wagered(conn: Any, date: str | None = None) -> Decimal:
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity_filled * average_fill_price + fees), 0) AS total FROM live_orders "
        "WHERE status IN ('FILLED', 'PARTIALLY_FILLED') AND date(submitted_at) = ?",
        (date,),
    ).fetchone()
    return Decimal(str(row["total"]))


def sum_daily_live_realized_pnl(conn: Any, date: str | None = None) -> Decimal:
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM live_positions WHERE date(settled_at) = ?",
        (date,),
    ).fetchone()
    return Decimal(str(row["total"]))


def count_live_trades_in_last_hour(conn: Any) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM live_orders WHERE status IN ('FILLED', 'PARTIALLY_FILLED') "
        "AND submitted_at >= datetime('now', '-1 hour')"
    ).fetchone()
    return int(row["n"])


# ── Audit log ────────────────────────────────────────────────────────

def log_event(
    conn: Any, event_type: str, detail: str = "", approval_id: str | None = None,
    prepared_order_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO live_execution_events (approval_id, prepared_order_id, event_type, detail) "
        "VALUES (?, ?, ?, ?)",
        (approval_id, prepared_order_id, event_type, detail),
    )
    conn.commit()


def get_events(conn: Any, approval_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_execution_events WHERE approval_id = ? ORDER BY event_id ASC", (approval_id,)
    ).fetchall()
    return [dict(r) for r in rows]
