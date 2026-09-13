"""Data model for Stage 3 paper trading: orders, fills, positions,
settlement results, and the canonical enums/identity helpers shared
across sizing/risk/fills/settlement/broker.

PaperRejectionReason is deliberately a SEPARATE enum from Stage 2B's
own src.execution.evaluator.RejectionReason (which is untouched by this
stage) -- Stage 2B's enum covers "does this recommendation have a
tradeable market at an acceptable price"; this one covers "given a
qualified opportunity, did sizing/risk/fill-simulation actually turn it
into a paper trade." A rejection anywhere in the whole pipeline is
always exactly one of these two enums, never a scattered string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from src.execution.matching import normalize_team_name


class PaperOrderStatus(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SIMULATED_SUBMITTED = "SIMULATED_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELED = "CANCELED"


class PaperPositionStatus(str, Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


class PaperRejectionReason(str, Enum):
    """Canonical, exhaustive set of reasons a qualified Stage 2B
    ExecutionOpportunity never becomes a filled paper position. Every
    rejection in the sizing/risk/fill layer uses one of these values --
    never a scattered string literal, mirroring Stage 2B's own
    RejectionReason convention."""

    BET_TOO_SMALL = "BET_TOO_SMALL"
    BANKROLL_TOO_LOW = "BANKROLL_TOO_LOW"
    MAX_BET_USD = "MAX_BET_USD"
    MAX_BET_PCT_BANKROLL = "MAX_BET_PCT_BANKROLL"
    MAX_UNITS_PER_BET = "MAX_UNITS_PER_BET"
    MAX_EVENT_EXPOSURE = "MAX_EVENT_EXPOSURE"
    MAX_PROVIDER_EXPOSURE = "MAX_PROVIDER_EXPOSURE"
    MAX_SPORT_EXPOSURE = "MAX_SPORT_EXPOSURE"
    MAX_OPEN_EXPOSURE = "MAX_OPEN_EXPOSURE"
    MAX_DAILY_WAGERED = "MAX_DAILY_WAGERED"
    DAILY_STOP_LOSS_REACHED = "DAILY_STOP_LOSS_REACHED"
    DAILY_PROFIT_TARGET_REACHED = "DAILY_PROFIT_TARGET_REACHED"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_TRADES_PER_HOUR = "MAX_TRADES_PER_HOUR"
    DUPLICATE_POSITION = "DUPLICATE_POSITION"
    ALREADY_TRADED_RECOMMENDATION = "ALREADY_TRADED_RECOMMENDATION"
    OPPORTUNITY_STALE = "OPPORTUNITY_STALE"
    EVENT_STARTED = "EVENT_STARTED"
    MARKET_CLOSED = "MARKET_CLOSED"
    PRICE_MOVED_BEYOND_LIMIT = "PRICE_MOVED_BEYOND_LIMIT"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    RISK_ENGINE_ERROR = "RISK_ENGINE_ERROR"


@dataclass(frozen=True)
class PaperOrder:
    paper_order_id: int | None
    opportunity_id: int | None
    recommendation_id: str
    provider: str
    provider_market_id: str
    league: str
    event: str
    side: str
    sizing_mode: str
    model_probability: Decimal
    net_ev_pct: Decimal
    requested_units: Decimal
    requested_stake: Decimal
    approved_units: Decimal
    approved_stake: Decimal
    requested_quantity: Decimal
    limit_price: Decimal
    fingerprint: str
    status: PaperOrderStatus
    rejection_reason: str | None
    limiting_constraint: str | None
    submitted_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class PaperFill:
    paper_fill_id: int | None
    paper_order_id: int
    quantity_requested: Decimal
    quantity_filled: Decimal
    average_fill_price: Decimal | None
    gross_cost: Decimal
    fees: Decimal
    total_cost: Decimal
    slippage: Decimal | None
    timestamp: datetime


@dataclass(frozen=True)
class PaperPosition:
    position_id: int | None
    paper_order_id: int
    recommendation_id: str
    provider: str
    provider_market_id: str
    event_id: str
    league: str
    side: str
    quantity: Decimal
    average_entry_price: Decimal
    entry_cost: Decimal
    fees_paid: Decimal
    model_probability_at_entry: Decimal
    net_ev_at_entry: Decimal
    fingerprint: str
    opened_at: datetime
    status: PaperPositionStatus
    settled_at: datetime | None
    settlement_value: Decimal | None
    realized_pnl: Decimal | None


@dataclass(frozen=True)
class PaperSettlementResult:
    provider: str
    market_id: str
    resolved: bool
    winning_side: str | None      # "YES" / "NO" / None
    payout_per_contract: Decimal | None
    source: str                   # e.g. "kalshi_market_result", "unverified"
    resolved_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def fingerprint(
    recommendation_id: str, provider: str, provider_market_id: str,
    side: str, line: float | None, strategy: str,
) -> str:
    """Deterministic identity for a paper trade -- the same conceptual
    bet (same recommendation, same provider/market/side/line, same
    sizing strategy) always produces the same fingerprint, so repeated
    scans of the same opportunity can be recognized as duplicates
    rather than re-traded every run."""
    line_part = "" if line is None else str(line)
    return "|".join([recommendation_id, provider, provider_market_id, side, line_part, strategy])


def event_identity(league: str, home_team: str, away_team: str, event_date: str) -> str:
    """A provider-independent identity for "the same real-world game" --
    used to bucket exposure (MAX_EVENT_EXPOSURE_USD) across providers
    that would otherwise use different market ids for the same event.
    Reuses matching.py's existing, unmodified normalize_team_name so a
    Kalshi and a Polymarket opportunity for the same game land in the
    same bucket."""
    return "|".join([league, normalize_team_name(home_team), normalize_team_name(away_team), event_date])
