"""LiveExecutionService: the ONLY caller of
provider._submit_authorized_order anywhere in this repo (enforced by
tests/test_live_architecture.py). Every real order goes through
execute_authorized(), which requires an already-human-approved
ExecutionAuthorization and re-validates everything against fresh data
immediately before submitting.

Nothing here ever resizes an approval upward, retries an ambiguous
submission automatically, or bypasses RiskEngine/OpportunityEvaluator's
own math -- every recalculation reuses revalidation.py, which itself
reuses Stage 2B/3 functions unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.execution.base import PredictionMarketProvider
from src.execution.live import kill_switch, store
from src.execution.live.models import (
    InvalidationReason, LiveOrder, LiveOrderStatus, LivePosition, LivePositionStatus,
    LiveSubmissionAttempt, SubmissionAttemptState, new_random_id,
)
from src.execution.live.revalidation import revalidate_approved_order


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str  # "SUBMITTED" / "BLOCKED" / "INVALIDATED"
    reason: str | None
    detail: str
    live_order_id: int | None = None
    attempt_id: str | None = None


def _can_attempt_live_trade(config: Any, provider_name: str) -> tuple[bool, str]:
    """The multi-gate check (Stage 4 plan section 1): a real trade is
    only ever reachable if ALL of these are simultaneously true. No
    single toggle here is sufficient by itself. Checked again here even
    though ApprovalService already wouldn't have produced a PreparedLiveOrder
    without these being true at prepare-time -- config could theoretically
    change between prepare/approve and execute."""
    if not config.live_trading_enabled:
        return False, "LIVE_TRADING_ENABLED is false"
    if not config.require_human_approval:
        return False, "REQUIRE_HUMAN_APPROVAL is false -- refusing to trade without this safeguard"
    provider_flag = getattr(config, f"{provider_name}_live_enabled", False)
    if not provider_flag:
        return False, f"{provider_name}_live_enabled is false"
    return True, ""


def execute_authorized(
    conn: Any, approval_id: str, provider: PredictionMarketProvider, config: Any,
) -> ExecutionResult:
    authorization = store.get_authorization(conn, approval_id)
    if authorization is None:
        return ExecutionResult("BLOCKED", "APPROVAL_ALREADY_USED", "no such approval_id")

    if authorization["status"] != "APPROVED":
        return ExecutionResult(
            "BLOCKED", authorization["status"],
            f"approval status is {authorization['status']!r}, not APPROVED -- refusing to execute",
        )

    expires_at = datetime.fromisoformat(authorization["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        store.invalidate_authorization(conn, approval_id, InvalidationReason.APPROVAL_EXPIRED.value)
        store.log_event(conn, "APPROVAL_EXPIRED", "expired before execution", approval_id=approval_id)
        return ExecutionResult("BLOCKED", InvalidationReason.APPROVAL_EXPIRED.value, "approval expired")

    engaged, kill_reason = kill_switch.is_kill_switch_engaged(conn)
    if engaged:
        store.invalidate_authorization(conn, approval_id, InvalidationReason.KILL_SWITCH_ENGAGED.value)
        store.log_event(conn, "BLOCKED_KILL_SWITCH", kill_reason or "", approval_id=approval_id)
        return ExecutionResult("BLOCKED", InvalidationReason.KILL_SWITCH_ENGAGED.value, kill_reason or "kill switch engaged")

    if kill_switch.is_circuit_tripped(conn, authorization["provider"]):
        store.invalidate_authorization(conn, approval_id, InvalidationReason.CIRCUIT_BREAKER_TRIPPED.value)
        store.log_event(conn, "BLOCKED_CIRCUIT_BREAKER", authorization["provider"], approval_id=approval_id)
        return ExecutionResult(
            "BLOCKED", InvalidationReason.CIRCUIT_BREAKER_TRIPPED.value,
            f"{authorization['provider']} live execution paused by the error circuit breaker",
        )

    can_trade, gate_detail = _can_attempt_live_trade(config, authorization["provider"])
    if not can_trade:
        store.invalidate_authorization(conn, approval_id, InvalidationReason.PROVIDER_DISABLED.value)
        store.log_event(conn, "BLOCKED_CONFIG_GATE", gate_detail, approval_id=approval_id)
        return ExecutionResult("BLOCKED", InvalidationReason.PROVIDER_DISABLED.value, gate_detail)

    prepared_order = store.get_prepared_order(conn, authorization["prepared_order_id"])
    if prepared_order is None:
        store.invalidate_authorization(conn, approval_id, InvalidationReason.UNKNOWN_REVALIDATION_FAILURE.value)
        return ExecutionResult("BLOCKED", InvalidationReason.UNKNOWN_REVALIDATION_FAILURE.value, "prepared order not found")

    # Revalidation is read-only against the provider -- safe to run
    # before claiming the approval. Only the actual submission below
    # needs single-flight protection.
    revalidation = revalidate_approved_order(conn, authorization, prepared_order, provider, config)
    if not revalidation.passed:
        store.invalidate_authorization(conn, approval_id, revalidation.invalidation_reason.value)
        store.log_event(
            conn, "REVALIDATION_FAILED", revalidation.detail, approval_id=approval_id,
            prepared_order_id=authorization["prepared_order_id"],
        )
        return ExecutionResult("INVALIDATED", revalidation.invalidation_reason.value, revalidation.detail)

    store.log_event(conn, "REVALIDATION_PASSED", revalidation.detail, approval_id=approval_id)

    # The ENTIRE single-flight mechanism: an atomic conditional UPDATE.
    # Any concurrent caller (another process, a Streamlit rerun, a
    # double-click) that loses this race gets rowcount == 0 and returns
    # here WITHOUT ever calling the provider.
    claimed = store.claim_authorization_for_execution(conn, approval_id)
    if not claimed:
        return ExecutionResult("BLOCKED", InvalidationReason.APPROVAL_ALREADY_USED.value, "approval already claimed by another execution attempt")

    attempt_id = new_random_id()
    now = datetime.now(timezone.utc)
    store.persist_submission_attempt(conn, LiveSubmissionAttempt(
        attempt_id=attempt_id, approval_id=approval_id, prepared_order_id=authorization["prepared_order_id"],
        provider=authorization["provider"], market_id=authorization["provider_market_id"],
        side=authorization["side"], quantity=revalidation.quantity, limit_price=revalidation.fill_price,
        state=SubmissionAttemptState.SUBMISSION_IN_PROGRESS, created_at=now, request_started_at=now,
        response_received_at=None, provider_order_id=None, reconciliation_status=None, reconciliation_detail=None,
    ))
    store.log_event(conn, "SUBMISSION_CLAIMED", f"attempt_id={attempt_id}", approval_id=approval_id)

    class _AuthorizationView:
        """Thin attribute-access view over the authorization dict so
        provider._submit_authorized_order can use dot-notation without
        this service depending on a specific ExecutionAuthorization
        constructor shape."""
        def __init__(self, data: dict) -> None:
            self.provider_market_id = data["provider_market_id"]
            self.side = data["side"]

    try:
        outcome = provider._submit_authorized_order(
            _AuthorizationView(authorization), revalidation.quantity, revalidation.fill_price,
        )
    except Exception as exc:
        # Even an unexpected exception from the provider call is
        # AMBIGUOUS, not a crash -- we cannot prove no order was created.
        outcome = _ambiguous_from_exception(exc)

    now2 = datetime.now(timezone.utc)
    if outcome.outcome == "CONFIRMED":
        kill_switch.reset_provider_circuit(conn, authorization["provider"])
        store.update_submission_attempt_state(
            conn, attempt_id, SubmissionAttemptState.CONFIRMED.value,
            provider_order_id=outcome.provider_order_id, response_received_at=now2,
        )

        quantity_filled = outcome.quantity_filled if outcome.quantity_filled is not None else Decimal("0")
        order_status = _order_status_from_provider_state(outcome.order_state, quantity_filled, revalidation.quantity)

        live_order_id = store.persist_live_order(conn, LiveOrder(
            live_order_id=None, approval_id=approval_id, prepared_order_id=authorization["prepared_order_id"],
            provider=authorization["provider"], provider_order_id=outcome.provider_order_id,
            client_order_id=None, market_id=authorization["provider_market_id"], side=authorization["side"],
            quantity_requested=revalidation.quantity, quantity_filled=quantity_filled,
            limit_price=revalidation.fill_price, average_fill_price=outcome.average_fill_price,
            fees=revalidation.fees or Decimal("0"),
            status=order_status, submitted_at=now2, last_updated_at=now2,
            provider_response_reference=outcome.raw_reference,
        ))
        store.log_event(
            conn, "SUBMITTED", f"provider_order_id={outcome.provider_order_id} status={order_status.value}",
            approval_id=approval_id, prepared_order_id=authorization["prepared_order_id"],
        )

        if quantity_filled > 0 and outcome.average_fill_price is not None:
            fill_cost = quantity_filled * outcome.average_fill_price + (revalidation.fees or Decimal("0"))
            store.persist_live_position(conn, LivePosition(
                position_id=None, approval_id=approval_id, prepared_order_id=authorization["prepared_order_id"],
                live_order_id=live_order_id, recommendation_id=authorization["recommendation_id"],
                provider=authorization["provider"], provider_market_id=authorization["provider_market_id"],
                event_id=prepared_order["event_id"], side=authorization["side"], quantity=quantity_filled,
                average_entry_price=outcome.average_fill_price, total_entry_cost=fill_cost,
                fees_paid=revalidation.fees or Decimal("0"), opened_at=now2, status=LivePositionStatus.OPEN,
                settled_at=None, settlement_value=None, realized_pnl=None,
            ))
            store.log_event(
                conn, "POSITION_OPENED", f"quantity={quantity_filled} avg_price={outcome.average_fill_price}",
                approval_id=approval_id,
            )
        else:
            store.log_event(
                conn, "FILL_UNKNOWN",
                "order confirmed but fill quantity/price could not be confirmed from the response -- "
                "no position created; reconcile manually via provider account before assuming a fill",
                approval_id=approval_id,
            )

        return ExecutionResult("SUBMITTED", None, outcome.detail, live_order_id=live_order_id, attempt_id=attempt_id)

    if outcome.outcome == "REJECTED":
        kill_switch.record_provider_error(
            conn, authorization["provider"], config.live_provider_error_threshold,
            config.live_provider_error_window_minutes,
        )
        store.update_submission_attempt_state(
            conn, attempt_id, SubmissionAttemptState.REJECTED.value, response_received_at=now2,
        )
        store.log_event(conn, "PROVIDER_REJECTED", outcome.detail, approval_id=approval_id)
        return ExecutionResult("BLOCKED", "PROVIDER_REJECTED", outcome.detail, attempt_id=attempt_id)

    # AMBIGUOUS: never auto-retried. Recorded for manual review; the
    # circuit breaker still counts it as an error signal.
    tripped = kill_switch.record_provider_error(
        conn, authorization["provider"], config.live_provider_error_threshold,
        config.live_provider_error_window_minutes,
    )
    store.update_submission_attempt_state(
        conn, attempt_id, SubmissionAttemptState.MANUAL_REVIEW_REQUIRED.value, response_received_at=now2,
        reconciliation_status="MANUAL_REVIEW_REQUIRED",
        reconciliation_detail=(
            f"{authorization['provider']} does not support automatic order-lookup reconciliation "
            "in this stage -- ambiguous submissions always require manual review. " + outcome.detail
        ),
    )
    store.log_event(
        conn, "SUBMISSION_AMBIGUOUS",
        f"{outcome.detail}; circuit_breaker_tripped={tripped}", approval_id=approval_id,
    )
    return ExecutionResult(
        "BLOCKED", "MANUAL_REVIEW_REQUIRED",
        f"ambiguous submission outcome, manual review required: {outcome.detail}", attempt_id=attempt_id,
    )


def _order_status_from_provider_state(
    provider_state: str | None, quantity_filled: Decimal, quantity_requested: Decimal,
) -> LiveOrderStatus:
    """Never assumes HTTP 200/CONFIRMED == FILLED (section 16). Maps
    the provider's own reported order state when present and
    recognized; otherwise falls back to inferring from quantity_filled,
    and if that's unknown too, the conservative SUBMITTED (not FILLED)."""
    if provider_state == "FILLED":
        return LiveOrderStatus.FILLED
    if provider_state in ("PARTIALLY_FILLED",):
        return LiveOrderStatus.PARTIALLY_FILLED
    if provider_state in ("CANCELED", "EXPIRED", "REJECTED"):
        return LiveOrderStatus.CANCELED if provider_state == "CANCELED" else (
            LiveOrderStatus.EXPIRED if provider_state == "EXPIRED" else LiveOrderStatus.REJECTED
        )
    if quantity_filled > 0:
        return LiveOrderStatus.FILLED if quantity_filled >= quantity_requested else LiveOrderStatus.PARTIALLY_FILLED
    return LiveOrderStatus.SUBMITTED


def _ambiguous_from_exception(exc: Exception):
    from src.execution.base import LiveSubmissionOutcome
    return LiveSubmissionOutcome(
        outcome="AMBIGUOUS", provider_order_id=None,
        detail=f"unexpected exception calling provider: {type(exc).__name__}: {exc}", raw_reference=None,
    )


def sync_fills(conn: Any, providers: dict[str, PredictionMarketProvider]) -> list[str]:
    """Best-effort fill reconciliation for SUBMITTED live orders. Both
    providers currently report capabilities.supports_fill_lookup=False
    (no order/fill-status read endpoint was independently confirmed --
    see the provider modules) so this honestly reports that nothing
    could be synced rather than guessing an endpoint. Once a provider's
    real order-status endpoint is verified, this is the one place that
    needs updating."""
    notes: list[str] = []
    rows = conn.execute(
        "SELECT * FROM live_orders WHERE status IN ('SUBMISSION_PENDING', 'SUBMITTED', 'ACCEPTED', 'PARTIALLY_FILLED')"
    ).fetchall()
    for row in rows:
        provider = providers.get(row["provider"])
        if provider is None or not provider.capabilities.supports_fill_lookup:
            notes.append(
                f"live_order_id={row['live_order_id']}: {row['provider']} does not support fill lookup "
                "in this stage -- status unchanged, manual verification required"
            )
    return notes
