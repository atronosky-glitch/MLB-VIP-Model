"""RiskEngine: the single gate every paper trade must pass through
before a PaperOrder can be marked RISK_APPROVED. Pure business logic,
no DB/network I/O -- src/execution/paper/portfolio.py gathers the
RiskContext inputs from the database; this module only reasons about
already-gathered numbers, the same layering discipline Stage 2B's
evaluator.py uses for a NormalizedOrderBook.

Duplicate/already-traded detection is folded into RiskEngine (as
RiskContext.has_open_duplicate/has_settled_duplicate) rather than being
a separate broker-level short-circuit, so EVERY rejection that would
otherwise have produced a trade -- including a duplicate -- goes
through one call and produces one auditable risk_decisions row (Stage
3 plan section 1: "Nothing should bypass the RiskEngine"). Opportunity
staleness/event-started checks stay in the broker, since they reject
before any stake is even recommended -- there is no risk decision to
make yet at that point, mirroring Stage 2B's own UNSUPPORTED_MARKET_TYPE
short-circuit before any provider call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from src.execution.paper.models import PaperRejectionReason
from src.execution.sizing import units_to_usd


@dataclass(frozen=True)
class RiskContext:
    available_bankroll_usd: Decimal
    open_positions_count: int
    event_exposure_usd: Decimal
    provider_exposure_usd: Decimal
    sport_exposure_usd: Decimal
    total_open_exposure_usd: Decimal
    daily_wagered_usd: Decimal
    daily_realized_pnl_usd: Decimal
    trades_in_last_hour: int
    has_open_duplicate: bool = False
    has_settled_duplicate: bool = False


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    recommended_stake_usd: Decimal
    approved_stake_usd: Decimal
    approved_units: Decimal
    limiting_constraint: PaperRejectionReason | None
    rejection_reason: PaperRejectionReason | None
    checks: tuple[str, ...] = field(default_factory=tuple)


class RiskEngine:
    def __init__(self, config: Any) -> None:
        self.config = config

    def evaluate(
        self,
        recommended_stake_usd: Decimal,
        recommended_units: Decimal,
        unit_size_usd: Decimal,
        context: RiskContext,
        allow_retrade_settled: bool,
    ) -> RiskDecision:
        checks: list[str] = []
        cfg = self.config

        def reject(reason: PaperRejectionReason, message: str) -> RiskDecision:
            checks.append(message)
            return RiskDecision(
                approved=False, recommended_stake_usd=recommended_stake_usd,
                approved_stake_usd=Decimal("0"), approved_units=Decimal("0"),
                limiting_constraint=reason, rejection_reason=reason, checks=tuple(checks),
            )

        if context.has_open_duplicate and not cfg.allow_position_addons:
            return reject(
                PaperRejectionReason.DUPLICATE_POSITION,
                "an OPEN position with this fingerprint already exists and ALLOW_POSITION_ADDONS=false",
            )

        if context.has_settled_duplicate and not allow_retrade_settled:
            return reject(
                PaperRejectionReason.ALREADY_TRADED_RECOMMENDATION,
                "this recommendation was already settled and allow_retrade_settled_recommendation=false",
            )

        if context.daily_realized_pnl_usd <= -Decimal(str(cfg.max_daily_loss_usd)):
            return reject(
                PaperRejectionReason.DAILY_STOP_LOSS_REACHED,
                f"daily realized P&L {context.daily_realized_pnl_usd} <= -{cfg.max_daily_loss_usd} "
                "-- new trades blocked; existing positions keep tracking",
            )

        if cfg.stop_after_daily_profit_target and context.daily_realized_pnl_usd >= Decimal(
            str(cfg.daily_profit_target_usd)
        ):
            return reject(
                PaperRejectionReason.DAILY_PROFIT_TARGET_REACHED,
                f"daily realized P&L {context.daily_realized_pnl_usd} >= target {cfg.daily_profit_target_usd}",
            )

        if context.open_positions_count >= cfg.max_open_positions:
            return reject(
                PaperRejectionReason.MAX_OPEN_POSITIONS,
                f"{context.open_positions_count} open positions >= max {cfg.max_open_positions}",
            )

        if context.trades_in_last_hour >= cfg.max_trades_per_hour:
            return reject(
                PaperRejectionReason.MAX_TRADES_PER_HOUR,
                f"{context.trades_in_last_hour} trades in the last hour >= max {cfg.max_trades_per_hour}",
            )

        if context.available_bankroll_usd <= 0:
            return reject(
                PaperRejectionReason.BANKROLL_TOO_LOW,
                f"available bankroll {context.available_bankroll_usd} <= 0",
            )

        caps: list[tuple[PaperRejectionReason, Decimal]] = [
            (PaperRejectionReason.MAX_BET_USD, Decimal(str(cfg.max_bet_usd))),
            (
                PaperRejectionReason.MAX_BET_PCT_BANKROLL,
                context.available_bankroll_usd * Decimal(str(cfg.max_bet_pct_bankroll)),
            ),
            (
                PaperRejectionReason.MAX_UNITS_PER_BET,
                units_to_usd(Decimal(str(cfg.max_units_per_bet)), unit_size_usd),
            ),
            (
                PaperRejectionReason.MAX_EVENT_EXPOSURE,
                Decimal(str(cfg.max_event_exposure_usd)) - context.event_exposure_usd,
            ),
            (
                PaperRejectionReason.MAX_PROVIDER_EXPOSURE,
                Decimal(str(cfg.max_provider_exposure_usd)) - context.provider_exposure_usd,
            ),
            (
                PaperRejectionReason.MAX_SPORT_EXPOSURE,
                Decimal(str(cfg.max_sport_exposure_usd)) - context.sport_exposure_usd,
            ),
            (
                PaperRejectionReason.MAX_OPEN_EXPOSURE,
                Decimal(str(cfg.max_open_exposure_usd)) - context.total_open_exposure_usd,
            ),
            (
                PaperRejectionReason.MAX_DAILY_WAGERED,
                Decimal(str(cfg.max_daily_wagered_usd)) - context.daily_wagered_usd,
            ),
            (None, context.available_bankroll_usd),  # can never recommend more than exists
        ]

        approved_stake = recommended_stake_usd
        limiting_constraint: PaperRejectionReason | None = None
        for reason, cap in caps:
            cap = max(cap, Decimal("0"))
            checks.append(f"cap {reason.value if reason else 'AVAILABLE_BANKROLL'}={cap}")
            if cap < approved_stake:
                approved_stake = cap
                limiting_constraint = reason

        if limiting_constraint is not None and approved_stake < recommended_stake_usd:
            if not cfg.allow_risk_size_reduction:
                return reject(
                    limiting_constraint,
                    f"recommended {recommended_stake_usd} exceeds {limiting_constraint.value} "
                    f"({approved_stake}) and ALLOW_RISK_SIZE_REDUCTION=false",
                )
            checks.append(
                f"resized {recommended_stake_usd} -> {approved_stake} due to {limiting_constraint.value}"
            )

        if approved_stake < Decimal(str(cfg.min_paper_trade_usd)):
            return reject(
                PaperRejectionReason.BET_TOO_SMALL,
                f"approved stake {approved_stake} < MIN_PAPER_TRADE_USD={cfg.min_paper_trade_usd}",
            )

        approved_units = approved_stake / unit_size_usd if unit_size_usd > 0 else Decimal("0")
        checks.append(f"approved {approved_stake} ({approved_units}u)")
        return RiskDecision(
            approved=True, recommended_stake_usd=recommended_stake_usd,
            approved_stake_usd=approved_stake, approved_units=approved_units,
            limiting_constraint=limiting_constraint, rejection_reason=None, checks=tuple(checks),
        )
