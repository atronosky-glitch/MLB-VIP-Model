"""ApprovalService: turns a qualified Stage 2B ExecutionOpportunity into
a PreparedLiveOrder (via Stage 3's existing sizing.py/risk.py, called
directly -- never reimplemented), and turns a human's decision on that
prepared order into an ExecutionAuthorization.

Nothing in this file ever calls a provider mutation method. Approving
an order does NOT submit it -- that is LiveExecutionService's job,
after a full fresh revalidation (see revalidation.py / service.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.execution.base import PredictionMarketProvider
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.live import store
from src.execution.live.models import (
    ApprovalStatus, ExecutionAuthorization, PreparedLiveOrder, PreparedOrderStatus, new_random_id,
)
from src.execution.live.revalidation import build_live_risk_context
from src.execution.paper.models import event_identity, fingerprint as compute_fingerprint
from src.execution.risk import RiskEngine
from src.execution.sizing import size_ev_tiered, size_flat, size_kelly

_LIVE_STRATEGY_TAG = "live"


@dataclass(frozen=True)
class PrepareResult:
    prepared_order: PreparedLiveOrder | None
    prepared_order_id: int | None
    rejected: bool
    rejection_detail: str | None


def _size(opportunity: ExecutionOpportunity, side: str, config: Any, available_bankroll: Decimal):
    unit_size = Decimal(str(config.unit_size_usd))
    mode = config.bet_sizing_mode
    if mode == "FLAT":
        return size_flat(Decimal(str(config.default_units)), unit_size)
    if mode == "EV_TIERED":
        return size_ev_tiered(opportunity.net_ev_pct, config.ev_tiered_sizing_tiers(), unit_size)
    if mode == "FRACTIONAL_KELLY":
        q = opportunity.model_probability if side == "YES" else (Decimal("1") - opportunity.model_probability)
        gross_cost = opportunity.quantity_analyzed * opportunity.expected_fill_price
        total_entry_cost = gross_cost + opportunity.estimated_fees
        effective_cost = total_entry_cost / opportunity.quantity_analyzed if opportunity.quantity_analyzed > 0 else Decimal("1")
        return size_kelly(
            q, effective_cost, Decimal(str(config.kelly_multiplier)), unit_size,
            Decimal(str(config.max_units_per_bet)), available_bankroll,
        )
    raise ValueError(f"unknown BET_SIZING_MODE: {mode!r}")


def prepare_order(
    conn: Any, opportunity: ExecutionOpportunity, signal: ExecutionSignal, provider: PredictionMarketProvider,
    config: Any,
) -> PrepareResult:
    side = opportunity.side
    fp = compute_fingerprint(
        opportunity.recommendation_id, opportunity.provider, opportunity.provider_market_id,
        side, signal.line, _LIVE_STRATEGY_TAG,
    )

    # Section 26: don't immediately re-spam the queue with the same
    # opportunity while an equivalent prepared order is still live.
    existing = store.get_open_prepared_order_by_fingerprint(conn, fp)
    if existing is not None:
        return PrepareResult(None, existing["prepared_order_id"], rejected=False, rejection_detail="already queued")

    event_id = event_identity(
        signal.league, signal.home_team, signal.away_team,
        signal.event_start_time.date().isoformat() if signal.event_start_time else "unknown",
    )

    try:
        balance = provider.get_balance()
        available_bankroll = Decimal(str(balance.available))
    except Exception as exc:
        store.log_event(conn, "PREPARE_REJECTED", f"could not fetch live balance: {type(exc).__name__}: {exc}")
        return PrepareResult(None, None, rejected=True, rejection_detail="PROVIDER_UNAVAILABLE")

    sizing = _size(opportunity, side, config, available_bankroll)

    risk_context = build_live_risk_context(
        conn, provider, event_id, opportunity.provider, opportunity.league,
        opportunity.recommendation_id, side,
    )
    decision = RiskEngine(config).evaluate(
        sizing.recommended_stake_usd, sizing.recommended_units, Decimal(str(config.unit_size_usd)),
        risk_context, False,
    )

    if not decision.approved:
        store.log_event(
            conn, "PREPARE_REJECTED", f"risk rejected: {decision.rejection_reason}",
            prepared_order_id=None,
        )
        return PrepareResult(None, None, rejected=True, rejection_detail=str(decision.rejection_reason))

    now = datetime.now(timezone.utc)
    order = PreparedLiveOrder(
        prepared_order_id=None, opportunity_id=None, recommendation_id=opportunity.recommendation_id,
        provider=opportunity.provider, provider_market_id=opportunity.provider_market_id,
        league=opportunity.league, event=opportunity.event, event_id=event_id, side=side,
        event_start_time=signal.event_start_time, model_probability=opportunity.model_probability,
        current_price=opportunity.best_ask or opportunity.expected_fill_price,
        expected_fill_price=opportunity.expected_fill_price, net_ev_pct=opportunity.net_ev_pct,
        recommended_units=sizing.recommended_units, recommended_stake=sizing.recommended_stake_usd,
        risk_approved_stake=decision.approved_stake_usd, risk_approved_units=decision.approved_units,
        quantity=opportunity.quantity_analyzed, maximum_entry_price=opportunity.max_acceptable_price,
        fees_estimate=opportunity.estimated_fees, slippage_estimate=opportunity.expected_slippage,
        available_liquidity=opportunity.available_liquidity, fingerprint=fp,
        risk_snapshot={
            "limiting_constraint": decision.limiting_constraint.value if decision.limiting_constraint else None,
            "checks": list(decision.checks),
            "available_bankroll": str(available_bankroll),
        },
        status=PreparedOrderStatus.READY, created_at=now,
        expires_at=now + timedelta(seconds=config.max_opportunity_age_seconds),
    )
    prepared_order_id = store.persist_prepared_order(conn, order)
    store.log_event(conn, "PREPARED", f"net_ev_pct={opportunity.net_ev_pct}", prepared_order_id=prepared_order_id)
    return PrepareResult(order, prepared_order_id, rejected=False, rejection_detail=None)


def refresh_queue(conn: Any) -> None:
    """Marks any READY prepared order past its expiry as EXPIRED --
    called before every queue read so a stale card never shows an
    active Approve control (section 24)."""
    conn.execute(
        "UPDATE prepared_live_orders SET status = 'EXPIRED' WHERE status = 'READY' AND expires_at < ?",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    store.expire_stale_authorizations(conn)


def get_pending(conn: Any) -> list[dict]:
    refresh_queue(conn)
    return store.get_ready_prepared_orders(conn)


def approve(
    conn: Any, prepared_order_id: int, config: Any, approved_by: str = "local_operator",
) -> ExecutionAuthorization | None:
    """Approving does NOT submit anything -- it only creates a bounded,
    time-limited (APPROVAL_TTL_SECONDS), single-use ExecutionAuthorization.
    Consumes the PreparedLiveOrder (marks it APPROVED, via an atomic
    conditional UPDATE rather than read-then-write) so it can never
    spawn a second approval -- this is what makes a Streamlit
    double-click or script rerun harmless."""
    refresh_queue(conn)
    prepared = store.get_prepared_order(conn, prepared_order_id)
    if prepared is None or prepared["status"] != "READY":
        return None

    cursor = conn.execute(
        "UPDATE prepared_live_orders SET status = 'APPROVED' WHERE prepared_order_id = ? AND status = 'READY'",
        (prepared_order_id,),
    )
    conn.commit()
    if cursor.rowcount != 1:
        return None

    now = datetime.now(timezone.utc)
    authorization = ExecutionAuthorization(
        approval_id=new_random_id(), prepared_order_id=prepared_order_id,
        opportunity_id=prepared["opportunity_id"], recommendation_id=prepared["recommendation_id"],
        provider=prepared["provider"], provider_market_id=prepared["provider_market_id"], side=prepared["side"],
        approved_units=Decimal(str(prepared["risk_approved_units"])),
        approved_stake_usd=Decimal(str(prepared["risk_approved_stake"])),
        approved_quantity=Decimal(str(prepared["quantity"])),
        approved_max_price=Decimal(str(prepared["maximum_entry_price"])),
        approved_min_net_ev_pct=Decimal(str(prepared["net_ev_pct"])),
        approved_at=now, expires_at=now + timedelta(seconds=int(config.approval_ttl_seconds)),
        approved_by=approved_by, status=ApprovalStatus.APPROVED, used_at=None, invalidated_at=None,
        invalidation_reason=None,
    )
    store.persist_authorization(conn, authorization)
    store.log_event(
        conn, "APPROVED", f"approved_by={approved_by} stake={authorization.approved_stake_usd}",
        approval_id=authorization.approval_id, prepared_order_id=prepared_order_id,
    )
    return authorization


def reject(conn: Any, prepared_order_id: int, reason: str | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE prepared_live_orders SET status = 'REJECTED' WHERE prepared_order_id = ? AND status = 'READY'",
        (prepared_order_id,),
    )
    conn.commit()
    if cursor.rowcount != 1:
        return False
    store.log_event(conn, "REJECTED", reason or "", prepared_order_id=prepared_order_id)
    return True
