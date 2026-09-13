"""Data model for Stage 4 live execution: prepared orders, human
approvals, live orders/fills/positions, and submission attempts.

ApprovalStatus/InvalidationReason are the canonical, exhaustive enums
for the approval lifecycle -- every rejection/invalidation anywhere in
this package uses one of these values, never a scattered string,
mirroring Stage 2B's RejectionReason / Stage 3's PaperRejectionReason
convention.

Reuses src.execution.paper.models.fingerprint/event_identity as-is for
duplicate-suppression identity -- not reimplemented.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from src.execution.base import ProviderCapabilities  # re-exported for callers importing from live.models

__all__ = [
    "ProviderCapabilities", "new_random_id", "PreparedOrderStatus", "ApprovalStatus", "InvalidationReason",
    "LiveOrderStatus", "LivePositionStatus", "SubmissionAttemptState", "PreparedLiveOrder",
    "ExecutionAuthorization", "LiveSubmissionAttempt", "LiveOrder", "LiveFill", "LivePosition",
]


def new_random_id() -> str:
    """Random, unguessable id for approvals/attempts (section 34)."""
    return secrets.token_urlsafe(32)


class PreparedOrderStatus(str, Enum):
    READY = "READY"          # visible in the approval queue, awaiting a human decision
    APPROVED = "APPROVED"    # consumed into exactly one ExecutionAuthorization
    REJECTED = "REJECTED"    # a human explicitly rejected it
    EXPIRED = "EXPIRED"      # went stale before any decision was made
    STALE = "STALE"          # display-only transient state before a queue refresh marks it EXPIRED


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    USED = "USED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    REJECTED = "REJECTED"


class InvalidationReason(str, Enum):
    """Canonical, exhaustive reasons an ExecutionAuthorization never
    results in a submitted order. Section 7's list, plus
    PROVIDER_ORDER_SCHEMA_UNVERIFIED for Kalshi's permanent fail-closed
    guard (not in the original list, but required by the user's
    explicit Kalshi decision -- documented here rather than reusing an
    unrelated reason)."""

    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_USED = "APPROVAL_ALREADY_USED"
    PRICE_MOVED_ABOVE_APPROVED_LIMIT = "PRICE_MOVED_ABOVE_APPROVED_LIMIT"
    NET_EV_BELOW_APPROVED_MINIMUM = "NET_EV_BELOW_APPROVED_MINIMUM"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_CLOSED = "MARKET_CLOSED"
    EVENT_STARTED = "EVENT_STARTED"
    LIQUIDITY_DROPPED = "LIQUIDITY_DROPPED"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    RISK_LIMIT_CHANGED = "RISK_LIMIT_CHANGED"
    DAILY_STOP_LOSS_REACHED = "DAILY_STOP_LOSS_REACHED"
    BALANCE_TOO_LOW = "BALANCE_TOO_LOW"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MARKET_MAPPING_CHANGED = "MARKET_MAPPING_CHANGED"
    SETTLEMENT_RULES_CHANGED = "SETTLEMENT_RULES_CHANGED"
    ORDER_QUANTITY_CHANGED = "ORDER_QUANTITY_CHANGED"
    STAKE_CHANGED = "STAKE_CHANGED"
    PROVIDER_ORDER_SCHEMA_UNVERIFIED = "PROVIDER_ORDER_SCHEMA_UNVERIFIED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"
    UNKNOWN_REVALIDATION_FAILURE = "UNKNOWN_REVALIDATION_FAILURE"


class LiveOrderStatus(str, Enum):
    SUBMISSION_PENDING = "SUBMISSION_PENDING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class LivePositionStatus(str, Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


class SubmissionAttemptState(str, Enum):
    CREATED = "CREATED"
    SUBMISSION_IN_PROGRESS = "SUBMISSION_IN_PROGRESS"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"                        # definite provider rejection, proven no order created
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"     # transport failure; order MAY exist -- never auto-retried
    RECONCILED_FOUND = "RECONCILED_FOUND"
    RECONCILED_NOT_FOUND = "RECONCILED_NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    FAILED_BEFORE_SEND = "FAILED_BEFORE_SEND"     # e.g. Kalshi's permanent schema-unverified guard


@dataclass(frozen=True)
class PreparedLiveOrder:
    prepared_order_id: int | None
    opportunity_id: int | None
    recommendation_id: str
    provider: str
    provider_market_id: str
    league: str
    event: str
    event_id: str
    side: str
    event_start_time: datetime | None
    model_probability: Decimal
    current_price: Decimal
    expected_fill_price: Decimal
    net_ev_pct: Decimal
    recommended_units: Decimal
    recommended_stake: Decimal
    risk_approved_stake: Decimal
    risk_approved_units: Decimal
    quantity: Decimal
    maximum_entry_price: Decimal
    fees_estimate: Decimal
    slippage_estimate: Decimal | None
    available_liquidity: Decimal
    fingerprint: str
    risk_snapshot: dict[str, Any]
    status: PreparedOrderStatus
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ExecutionAuthorization:
    approval_id: str
    prepared_order_id: int
    opportunity_id: int | None
    recommendation_id: str
    provider: str
    provider_market_id: str
    side: str
    approved_units: Decimal
    approved_stake_usd: Decimal
    approved_quantity: Decimal
    approved_max_price: Decimal
    approved_min_net_ev_pct: Decimal
    approved_at: datetime
    expires_at: datetime
    approved_by: str
    status: ApprovalStatus
    used_at: datetime | None
    invalidated_at: datetime | None
    invalidation_reason: str | None


@dataclass(frozen=True)
class LiveSubmissionAttempt:
    attempt_id: str
    approval_id: str
    prepared_order_id: int
    provider: str
    market_id: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    state: SubmissionAttemptState
    created_at: datetime
    request_started_at: datetime | None
    response_received_at: datetime | None
    provider_order_id: str | None
    reconciliation_status: str | None
    reconciliation_detail: str | None


@dataclass(frozen=True)
class LiveOrder:
    live_order_id: int | None
    approval_id: str
    prepared_order_id: int
    provider: str
    provider_order_id: str | None
    client_order_id: str | None
    market_id: str
    side: str
    quantity_requested: Decimal
    quantity_filled: Decimal
    limit_price: Decimal
    average_fill_price: Decimal | None
    fees: Decimal
    status: LiveOrderStatus
    submitted_at: datetime
    last_updated_at: datetime
    provider_response_reference: str | None


@dataclass(frozen=True)
class LiveFill:
    fill_id: int | None
    provider_fill_id: str | None
    live_order_id: int
    quantity: Decimal
    price: Decimal
    fees: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class LivePosition:
    position_id: int | None
    approval_id: str
    prepared_order_id: int
    live_order_id: int
    recommendation_id: str
    provider: str
    provider_market_id: str
    event_id: str
    side: str
    quantity: Decimal
    average_entry_price: Decimal
    total_entry_cost: Decimal
    fees_paid: Decimal
    opened_at: datetime
    status: LivePositionStatus
    settled_at: datetime | None
    settlement_value: Decimal | None
    realized_pnl: Decimal | None
