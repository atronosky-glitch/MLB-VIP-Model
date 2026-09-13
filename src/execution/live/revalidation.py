"""Revalidation: the mandatory fresh re-check that runs between a human
clicking Approve and any provider submission (Stage 4 plan section 6,
steps 4-17; steps 1-3 -- approval valid/unused/unexpired -- are the
atomic DB claim in store.py, checked by service.py before this runs).

Every recalculation here reuses Stage 2B/3 math UNCHANGED:
build_executable_quote, compute_ev, walk_book, max_quantity_for_budget,
RiskEngine.evaluate. This file contains zero pricing formulas of its
own -- it only re-runs the existing ones against fresh data and
compares the result to the approval's stored bounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from src.execution.base import PredictionMarketProvider
from src.execution.ev import compute_ev
from src.execution.live import store
from src.execution.live.models import InvalidationReason
from src.execution.orderbook_math import max_quantity_for_budget, walk_book
from src.execution.quotes import build_executable_quote
from src.execution.risk import RiskContext, RiskDecision, RiskEngine

_CLOSED_STATUS_STRINGS = frozenset({"closed", "settled", "finalized", "inactive", "determined"})


@dataclass(frozen=True)
class RevalidationResult:
    passed: bool
    invalidation_reason: InvalidationReason | None
    detail: str
    quantity: Decimal | None = None
    fill_price: Decimal | None = None
    fees: Decimal | None = None
    net_ev_pct: Decimal | None = None
    risk_decision: RiskDecision | None = None


def _fail(reason: InvalidationReason, detail: str) -> RevalidationResult:
    return RevalidationResult(passed=False, invalidation_reason=reason, detail=detail)


def build_live_risk_context(
    conn: Any, provider: PredictionMarketProvider, event_id: str, provider_name: str, league: str,
    recommendation_id: str, side: str,
) -> RiskContext:
    """Live bankroll comes ONLY from the provider's authenticated,
    read-only get_balance() (Stage 1) -- never assumed, never a manual
    figure. Exposure/rate figures come from the live_* tables, entirely
    separate from paper's simulated bankroll/positions."""
    balance = provider.get_balance()
    return RiskContext(
        available_bankroll_usd=Decimal(str(balance.available)),
        open_positions_count=store.count_open_live_positions(conn),
        event_exposure_usd=store.sum_open_live_exposure(conn, event_id=event_id),
        provider_exposure_usd=store.sum_open_live_exposure(conn, provider=provider_name),
        sport_exposure_usd=store.sum_open_live_exposure(conn, league=league),
        total_open_exposure_usd=store.sum_open_live_exposure(conn),
        daily_wagered_usd=store.sum_daily_live_wagered(conn),
        daily_realized_pnl_usd=store.sum_daily_live_realized_pnl(conn),
        trades_in_last_hour=store.count_live_trades_in_last_hour(conn),
        has_open_duplicate=store.get_open_live_position_by_fingerprint(conn, recommendation_id, provider_name, side)
        is not None,
        has_settled_duplicate=False,
    )


def revalidate_approved_order(
    conn: Any, authorization: dict, prepared_order: dict, provider: PredictionMarketProvider, config: Any,
) -> RevalidationResult:
    side = authorization["side"]
    approved_max_price = Decimal(str(authorization["approved_max_price"]))
    approved_quantity = Decimal(str(authorization["approved_quantity"]))
    approved_stake = Decimal(str(authorization["approved_stake_usd"]))
    approved_min_net_ev_pct = Decimal(str(authorization["approved_min_net_ev_pct"]))

    # Step 12: event started (if this provider/config combination doesn't
    # support in-play live betting -- it never does in this stage).
    event_start_time = prepared_order.get("event_start_time")
    if event_start_time:
        dt = datetime.fromisoformat(event_start_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= dt:
            return _fail(InvalidationReason.EVENT_STARTED, "event start time has already passed")

    # Step 4: fresh market status.
    try:
        market = provider.get_market(authorization["provider_market_id"])
    except Exception as exc:
        return _fail(InvalidationReason.PROVIDER_UNAVAILABLE, f"could not fetch market: {type(exc).__name__}: {exc}")

    # Step 13: market still open. Conservative -- only a known
    # closed-like status string blocks; an unrecognized string is not
    # treated as evidence of closure (same posture as Stage 3's
    # settlement parsing: never guess).
    if str(market.status or "").strip().lower() in _CLOSED_STATUS_STRINGS:
        return _fail(InvalidationReason.MARKET_CLOSED, f"market status={market.status!r}")

    # Step 5/6: fresh orderbook.
    try:
        raw_book = provider.get_orderbook(authorization["provider_market_id"])
        book = provider.normalize_orderbook(raw_book)
    except Exception as exc:
        return _fail(InvalidationReason.MARKET_DATA_STALE, f"could not fetch/normalize orderbook: {type(exc).__name__}: {exc}")

    if config.live_require_fresh_orderbook:
        age = (datetime.now(timezone.utc) - book.timestamp).total_seconds() if book.timestamp else None
        if age is None or age > config.live_max_orderbook_age_seconds:
            return _fail(InvalidationReason.MARKET_DATA_STALE, f"orderbook age={age}")

    asks = book.yes_asks if side == "YES" else book.no_asks
    bids = book.yes_bids if side == "YES" else book.no_bids

    # Step 7/14: recalculate executable fill, NEVER above approved_max_price
    # and NEVER more than approved_quantity/approved_stake_usd.
    capped_levels = [lvl for lvl in asks if lvl.price <= approved_max_price]
    if not capped_levels:
        return _fail(
            InvalidationReason.PRICE_MOVED_ABOVE_APPROVED_LIMIT,
            f"no asks at or below approved_max_price={approved_max_price}",
        )

    budget_qty = max_quantity_for_budget(capped_levels, approved_stake)
    target_qty = min(approved_quantity, budget_qty).to_integral_value(rounding=ROUND_FLOOR)
    if target_qty <= 0:
        return _fail(InvalidationReason.LIQUIDITY_DROPPED, "no fillable quantity within approved bounds")

    fill = walk_book(capped_levels, target_qty)
    if fill.quantity_filled <= 0:
        return _fail(InvalidationReason.LIQUIDITY_DROPPED, "book walk filled zero contracts")
    if fill.best_price is not None and fill.best_price > approved_max_price:
        return _fail(InvalidationReason.PRICE_MOVED_ABOVE_APPROVED_LIMIT, f"best price {fill.best_price} > approved max {approved_max_price}")

    # Step 8: recalculate fees.
    fee_estimate = provider.estimate_fees(side, fill.vwap, fill.quantity_filled)

    quote = build_executable_quote(
        authorization["provider"], authorization["provider_market_id"], book, side,
        fill.quantity_filled, fee_estimate,
    )

    if quote.spread_pct is not None and quote.spread_pct * 100 > Decimal(str(config.max_spread_pct)) * 100:
        return _fail(InvalidationReason.SPREAD_TOO_WIDE, f"spread {quote.spread_pct * 100:.2f}%")
    if quote.slippage_pct is not None and quote.slippage_pct * 100 > Decimal(str(config.max_slippage_pct)) * 100:
        return _fail(InvalidationReason.SLIPPAGE_TOO_HIGH, f"slippage {quote.slippage_pct * 100:.2f}%")
    if quote.liquidity_available < Decimal(str(config.min_available_liquidity_usd)):
        return _fail(InvalidationReason.LIQUIDITY_DROPPED, f"liquidity {quote.liquidity_available}")

    # Step 9/17: recompute net EV -- must still clear the APPROVED minimum
    # (which may be stricter than config.min_net_ev_pct).
    model_probability = Decimal(str(prepared_order["model_probability"]))
    ev = compute_ev(model_probability, side, quote, approved_min_net_ev_pct, provider.estimate_fees)
    if ev.net_ev_pct < approved_min_net_ev_pct:
        return _fail(
            InvalidationReason.NET_EV_BELOW_APPROVED_MINIMUM,
            f"fresh net_ev_pct {ev.net_ev_pct:.2f}% < approved minimum {approved_min_net_ev_pct}%",
        )

    # Step 15/16: actual stake/quantity must not exceed approved bounds
    # (guaranteed by construction above, re-asserted defensively).
    if quote.gross_cost + fee_estimate.fee > approved_stake:
        return _fail(InvalidationReason.STAKE_CHANGED, "recalculated stake exceeds approved_stake_usd")
    if fill.quantity_filled > approved_quantity:
        return _fail(InvalidationReason.ORDER_QUANTITY_CHANGED, "recalculated quantity exceeds approved_quantity")

    # Step 11: rerun RiskEngine against a FRESH live risk context.
    league = prepared_order.get("league") or ""
    risk_context = build_live_risk_context(
        conn, provider, prepared_order["event_id"], authorization["provider"], league,
        authorization["recommendation_id"], side,
    )
    risk_decision = RiskEngine(config).evaluate(
        approved_stake, Decimal(str(authorization.get("approved_units") or 0)),
        Decimal(str(config.unit_size_usd)), risk_context, False,
    )
    if not risk_decision.approved:
        reason = (
            InvalidationReason.DAILY_STOP_LOSS_REACHED
            if risk_decision.rejection_reason and risk_decision.rejection_reason.value == "DAILY_STOP_LOSS_REACHED"
            else InvalidationReason.RISK_LIMIT_CHANGED
        )
        return _fail(reason, f"risk re-check failed: {risk_decision.rejection_reason}")

    if risk_decision.approved_stake_usd < approved_stake and not config.live_allow_post_approval_size_reduction:
        return _fail(
            InvalidationReason.RISK_LIMIT_CHANGED,
            f"risk now allows only {risk_decision.approved_stake_usd} < approved {approved_stake} "
            "and LIVE_ALLOW_POST_APPROVAL_SIZE_REDUCTION=false",
        )

    return RevalidationResult(
        passed=True, invalidation_reason=None, detail="revalidation passed",
        quantity=fill.quantity_filled, fill_price=fill.vwap, fees=fee_estimate.fee,
        net_ev_pct=ev.net_ev_pct, risk_decision=risk_decision,
    )
