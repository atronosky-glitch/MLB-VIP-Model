"""PaperBroker: the only place sizing, RiskEngine, fill simulation,
settlement, and persistence are wired together. Every path that could
create a filled PaperPosition goes through RiskEngine.evaluate() first
-- there is no helper here that creates a filled position while
bypassing risk checks (the one exception, per the Stage 3 plan, is
test-only fixtures that construct dataclasses directly without calling
this class at all).

No real order is ever placed: this module never calls
provider.place_order/cancel_order, and never will -- those remain
NotImplemented on every provider regardless of anything here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.execution.base import PredictionMarketProvider
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.opportunity_store import get_existing_opportunity
from src.execution.paper import portfolio, settlement, store
from src.execution.paper.fills import simulate_paper_fill
from src.execution.paper.models import (
    PaperFill, PaperOrder, PaperOrderStatus, PaperPosition, PaperPositionStatus,
    PaperRejectionReason, event_identity, fingerprint as compute_fingerprint,
)
from src.execution.risk import RiskDecision, RiskEngine
from src.execution.sizing import SizingMode, SizingResult, size_ev_tiered, size_flat, size_kelly


@dataclass(frozen=True)
class PaperTradeResult:
    status: str  # PaperOrderStatus value
    paper_order_id: int | None
    position_id: int | None
    sizing: SizingResult | None
    risk_decision: RiskDecision | None
    fill_detail: str | None
    rejection_reason: str | None
    detail: str


@dataclass(frozen=True)
class SettlementOutcome:
    position_id: int
    status: PaperPositionStatus
    settlement_value: Decimal | None
    realized_pnl: Decimal | None
    detail: str


class PaperBroker:
    def __init__(self, conn: Any, config: Any, account_id: str = store.DEFAULT_ACCOUNT_ID) -> None:
        self.conn = conn
        self.config = config
        self.account_id = account_id
        self.risk_engine = RiskEngine(config)
        store.get_or_create_account(conn, Decimal(str(config.paper_starting_bankroll_usd)), account_id)

    # ── sizing ────────────────────────────────────────────────────

    def _size(self, opportunity: ExecutionOpportunity, side: str) -> SizingResult:
        config = self.config
        unit_size = Decimal(str(config.unit_size_usd))
        mode = config.bet_sizing_mode

        if mode == SizingMode.FLAT.value:
            return size_flat(Decimal(str(config.default_units)), unit_size)

        if mode == SizingMode.EV_TIERED.value:
            return size_ev_tiered(opportunity.net_ev_pct, config.ev_tiered_sizing_tiers(), unit_size)

        if mode == SizingMode.FRACTIONAL_KELLY.value:
            q = opportunity.model_probability if side == "YES" else (Decimal("1") - opportunity.model_probability)
            gross_cost = opportunity.quantity_analyzed * opportunity.expected_fill_price
            total_entry_cost = gross_cost + opportunity.estimated_fees
            effective_cost = (
                total_entry_cost / opportunity.quantity_analyzed
                if opportunity.quantity_analyzed > 0 else Decimal("1")
            )
            bankroll = portfolio.get_bankroll(self.conn, self.account_id)
            return size_kelly(
                q, effective_cost, Decimal(str(config.kelly_multiplier)), unit_size,
                Decimal(str(config.max_units_per_bet)), bankroll.available_bankroll,
            )

        raise ValueError(f"unknown BET_SIZING_MODE: {mode!r}")

    # ── main flow ─────────────────────────────────────────────────

    def submit_opportunity(
        self, opportunity: ExecutionOpportunity, signal: ExecutionSignal, provider: PredictionMarketProvider,
    ) -> PaperTradeResult:
        config = self.config
        side = opportunity.side
        line = signal.line
        sizing_mode = config.bet_sizing_mode

        fp = compute_fingerprint(
            opportunity.recommendation_id, opportunity.provider, opportunity.provider_market_id,
            side, line, sizing_mode,
        )
        event_id = event_identity(
            signal.league, signal.home_team, signal.away_team,
            signal.event_start_time.date().isoformat() if signal.event_start_time else "unknown",
        )

        # Staleness / event-started: rejected before any stake is even
        # recommended, so there is no risk decision to make yet --
        # mirrors Stage 2B's own UNSUPPORTED_MARKET_TYPE short-circuit.
        age_seconds = (datetime.now(timezone.utc) - opportunity.generated_at).total_seconds()
        if age_seconds > config.max_opportunity_age_seconds:
            return self._reject_pre_risk(
                opportunity, fp, side, sizing_mode,
                PaperRejectionReason.OPPORTUNITY_STALE,
                f"opportunity age {age_seconds:.1f}s exceeds MAX_OPPORTUNITY_AGE_SECONDS={config.max_opportunity_age_seconds}",
            )
        if signal.event_start_time is not None and datetime.now(timezone.utc) >= signal.event_start_time:
            return self._reject_pre_risk(
                opportunity, fp, side, sizing_mode, PaperRejectionReason.EVENT_STARTED,
                "event start time has already passed",
            )

        sizing = self._size(opportunity, side)

        risk_context = portfolio.get_risk_context(
            self.conn, event_id, opportunity.provider, opportunity.league, fp, self.account_id,
        )
        decision = self.risk_engine.evaluate(
            sizing.recommended_stake_usd, sizing.recommended_units, Decimal(str(config.unit_size_usd)),
            risk_context, config.allow_retrade_settled_recommendation,
        )

        opportunity_id = self._lookup_opportunity_id(opportunity)
        store.persist_risk_decision(self.conn, {
            "recommendation_id": opportunity.recommendation_id, "provider": opportunity.provider,
            "opportunity_id": opportunity_id, "paper_order_id": None,
            "recommended_stake_usd": decision.recommended_stake_usd,
            "approved_stake_usd": decision.approved_stake_usd, "approved": decision.approved,
            "rejection_reason": decision.rejection_reason.value if decision.rejection_reason else None,
            "limiting_constraint": decision.limiting_constraint.value if decision.limiting_constraint else None,
            "bankroll_before": risk_context.available_bankroll_usd,
            "event_exposure_before": risk_context.event_exposure_usd,
            "provider_exposure_before": risk_context.provider_exposure_usd,
            "sport_exposure_before": risk_context.sport_exposure_usd,
            "daily_exposure_before": risk_context.daily_wagered_usd,
            "daily_pnl_before": risk_context.daily_realized_pnl_usd,
            "open_positions_before": risk_context.open_positions_count,
        })

        if not decision.approved:
            order_id = self._persist_order(
                opportunity, fp, side, sizing_mode, opportunity_id, sizing, decision,
                PaperOrderStatus.REJECTED, decision.rejection_reason.value,
            )
            return PaperTradeResult(
                status=PaperOrderStatus.REJECTED.value, paper_order_id=order_id, position_id=None,
                sizing=sizing, risk_decision=decision, fill_detail=None,
                rejection_reason=decision.rejection_reason.value,
                detail=f"risk rejected: {decision.rejection_reason.value}",
            )

        # Fresh-book refetch + fill simulation, capped at max_acceptable_price.
        try:
            raw_book = provider.get_orderbook(opportunity.provider_market_id)
            book = provider.normalize_orderbook(raw_book)
        except Exception as exc:
            order_id = self._persist_order(
                opportunity, fp, side, sizing_mode, opportunity_id, sizing, decision,
                PaperOrderStatus.REJECTED, PaperRejectionReason.INSUFFICIENT_LIQUIDITY.value,
            )
            return PaperTradeResult(
                status=PaperOrderStatus.REJECTED.value, paper_order_id=order_id, position_id=None,
                sizing=sizing, risk_decision=decision, fill_detail=None,
                rejection_reason=PaperRejectionReason.INSUFFICIENT_LIQUIDITY.value,
                detail=f"could not refetch orderbook: {type(exc).__name__}: {exc}",
            )

        asks = book.yes_asks if side == "YES" else book.no_asks
        outcome = simulate_paper_fill(
            asks, side, decision.approved_stake_usd, opportunity.max_acceptable_price,
            provider.estimate_fees, config.allow_partial_paper_fills,
        )

        if outcome.status == "REJECTED":
            order_id = self._persist_order(
                opportunity, fp, side, sizing_mode, opportunity_id, sizing, decision,
                PaperOrderStatus.REJECTED, outcome.rejection_reason.value,
            )
            return PaperTradeResult(
                status=PaperOrderStatus.REJECTED.value, paper_order_id=order_id, position_id=None,
                sizing=sizing, risk_decision=decision, fill_detail=outcome.detail,
                rejection_reason=outcome.rejection_reason.value, detail=outcome.detail,
            )

        fill = outcome.fill
        # Revalidate net EV against the fresh fill, using Stage 2B's own
        # net_ev_pct formula (expected_profit / total_entry_cost * 100)
        # applied to what was actually simulated, not what was seen at
        # scan time.
        q = opportunity.model_probability if side == "YES" else (Decimal("1") - opportunity.model_probability)
        expected_payout = q * fill.quantity_filled
        fresh_net_ev_pct = (
            (expected_payout - fill.total_cost) / fill.total_cost * 100 if fill.total_cost > 0 else Decimal("-100")
        )
        if fresh_net_ev_pct < Decimal(str(config.min_net_ev_pct)):
            order_id = self._persist_order(
                opportunity, fp, side, sizing_mode, opportunity_id, sizing, decision,
                PaperOrderStatus.REJECTED, PaperRejectionReason.OPPORTUNITY_STALE.value,
            )
            return PaperTradeResult(
                status=PaperOrderStatus.REJECTED.value, paper_order_id=order_id, position_id=None,
                sizing=sizing, risk_decision=decision, fill_detail=outcome.detail,
                rejection_reason=PaperRejectionReason.OPPORTUNITY_STALE.value,
                detail=f"fresh net EV {fresh_net_ev_pct:.2f}% no longer clears min_net_ev_pct={config.min_net_ev_pct}%",
            )

        final_status = PaperOrderStatus.FILLED if outcome.status == "FILLED" else PaperOrderStatus.PARTIALLY_FILLED
        order_id = self._persist_order(
            opportunity, fp, side, sizing_mode, opportunity_id, sizing, decision, final_status, None,
            requested_quantity=fill.quantity_requested, limit_price=opportunity.max_acceptable_price,
        )
        store.persist_paper_fill(self.conn, PaperFill(
            paper_fill_id=None, paper_order_id=order_id, quantity_requested=fill.quantity_requested,
            quantity_filled=fill.quantity_filled, average_fill_price=fill.average_fill_price,
            gross_cost=fill.gross_cost, fees=fill.fees, total_cost=fill.total_cost,
            slippage=fill.slippage, timestamp=fill.timestamp,
        ))
        position_id = store.persist_paper_position(self.conn, PaperPosition(
            position_id=None, paper_order_id=order_id, recommendation_id=opportunity.recommendation_id,
            provider=opportunity.provider, provider_market_id=opportunity.provider_market_id,
            event_id=event_id, league=opportunity.league, side=side, quantity=fill.quantity_filled,
            average_entry_price=fill.average_fill_price, entry_cost=fill.total_cost, fees_paid=fill.fees,
            model_probability_at_entry=opportunity.model_probability, net_ev_at_entry=fresh_net_ev_pct,
            fingerprint=fp, opened_at=fill.timestamp, status=PaperPositionStatus.OPEN,
            settled_at=None, settlement_value=None, realized_pnl=None,
        ), self.account_id)
        store.adjust_account_cash(self.conn, self.account_id, -fill.total_cost)

        return PaperTradeResult(
            status=final_status.value, paper_order_id=order_id, position_id=position_id,
            sizing=sizing, risk_decision=decision, fill_detail=outcome.detail, rejection_reason=None,
            detail=f"{outcome.detail}, cost=${fill.total_cost}",
        )

    def _lookup_opportunity_id(self, opportunity: ExecutionOpportunity) -> int | None:
        existing = get_existing_opportunity(self.conn, opportunity.recommendation_id, opportunity.provider)
        return existing["opportunity_id"] if existing else None

    def _reject_pre_risk(
        self, opportunity: ExecutionOpportunity, fp: str, side: str, sizing_mode: str,
        reason: PaperRejectionReason, detail: str,
    ) -> PaperTradeResult:
        order = PaperOrder(
            paper_order_id=None, opportunity_id=self._lookup_opportunity_id(opportunity),
            recommendation_id=opportunity.recommendation_id, provider=opportunity.provider,
            provider_market_id=opportunity.provider_market_id, league=opportunity.league,
            event=opportunity.event, side=side, sizing_mode=sizing_mode,
            model_probability=opportunity.model_probability, net_ev_pct=opportunity.net_ev_pct,
            requested_units=Decimal("0"), requested_stake=Decimal("0"), approved_units=Decimal("0"),
            approved_stake=Decimal("0"), requested_quantity=Decimal("0"), limit_price=opportunity.max_acceptable_price,
            fingerprint=fp, status=PaperOrderStatus.REJECTED, rejection_reason=reason.value,
            limiting_constraint=None, submitted_at=datetime.now(timezone.utc), created_at=datetime.now(timezone.utc),
        )
        order_id = store.persist_paper_order(self.conn, order, self.account_id)
        return PaperTradeResult(
            status=PaperOrderStatus.REJECTED.value, paper_order_id=order_id, position_id=None,
            sizing=None, risk_decision=None, fill_detail=None, rejection_reason=reason.value, detail=detail,
        )

    def _persist_order(
        self, opportunity: ExecutionOpportunity, fp: str, side: str, sizing_mode: str,
        opportunity_id: int | None, sizing: SizingResult, decision: RiskDecision,
        status: PaperOrderStatus, rejection_reason: str | None,
        requested_quantity: Decimal = Decimal("0"), limit_price: Decimal | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        order = PaperOrder(
            paper_order_id=None, opportunity_id=opportunity_id, recommendation_id=opportunity.recommendation_id,
            provider=opportunity.provider, provider_market_id=opportunity.provider_market_id,
            league=opportunity.league, event=opportunity.event, side=side, sizing_mode=sizing_mode,
            model_probability=opportunity.model_probability, net_ev_pct=opportunity.net_ev_pct,
            requested_units=sizing.recommended_units, requested_stake=sizing.recommended_stake_usd,
            approved_units=decision.approved_units, approved_stake=decision.approved_stake_usd,
            requested_quantity=requested_quantity, limit_price=limit_price or opportunity.max_acceptable_price,
            fingerprint=fp, status=status, rejection_reason=rejection_reason,
            limiting_constraint=decision.limiting_constraint.value if decision.limiting_constraint else None,
            submitted_at=now, created_at=now,
        )
        return store.persist_paper_order(self.conn, order, self.account_id)

    # ── read-only accessors ──────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        return store.get_open_positions(self.conn, self.account_id)

    def get_position(self, position_id: int) -> dict | None:
        return store.get_position_by_id(self.conn, position_id)

    def get_bankroll(self):
        return portfolio.get_bankroll(self.conn, self.account_id)

    def get_daily_stats(self, date: str | None = None) -> dict[str, Any]:
        return portfolio.compute_daily_stats(self.conn, Decimal(str(self.config.unit_size_usd)), self.account_id, date)

    # ── settlement ────────────────────────────────────────────────

    def settle_positions(self, providers: dict[str, PredictionMarketProvider]) -> list[SettlementOutcome]:
        outcomes: list[SettlementOutcome] = []
        for position in self.get_open_positions():
            provider = providers.get(position["provider"])
            if provider is None:
                continue
            try:
                market = provider.get_market(position["provider_market_id"])
            except Exception as exc:
                outcomes.append(SettlementOutcome(
                    position_id=position["position_id"], status=PaperPositionStatus.OPEN,
                    settlement_value=None, realized_pnl=None,
                    detail=f"could not fetch market for settlement: {type(exc).__name__}: {exc}",
                ))
                continue

            result = settlement.resolve_market(position["provider"], market)
            if not result.resolved:
                outcomes.append(SettlementOutcome(
                    position_id=position["position_id"], status=PaperPositionStatus.OPEN,
                    settlement_value=None, realized_pnl=None, detail="not yet resolved",
                ))
                continue

            status, settlement_value, realized_pnl = settlement.determine_position_outcome(
                result, position["side"], Decimal(str(position["quantity"])),
                Decimal(str(position["entry_cost"])), Decimal(str(position["fees_paid"])),
            )
            store.settle_paper_position(self.conn, position["position_id"], status.value, settlement_value, realized_pnl)
            store.adjust_account_cash(self.conn, self.account_id, settlement_value or Decimal("0"), realized_pnl or Decimal("0"))
            outcomes.append(SettlementOutcome(
                position_id=position["position_id"], status=status, settlement_value=settlement_value,
                realized_pnl=realized_pnl, detail=f"settled via {result.source}",
            ))
        return outcomes
