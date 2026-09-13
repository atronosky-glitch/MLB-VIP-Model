"""Provider-independent settlement: resolve a paper position's real-
world outcome using ONLY the existing, unmodified provider.get_market()
call (Stage 1) -- no new method is added to base.py/kalshi.py/
polymarket_us.py, so this stage makes zero changes to any Stage 1/2B
provider file.

Kalshi's public market object is documented to carry a 'result'
('yes'/'no'/'') and 'status' ('finalized' when settled) pair, but --
consistent with this project's established posture on unconfirmed
provider fields (the same posture that flags the Kalshi title parser
and Polymarket's book structure) -- this is treated as BEST-EFFORT,
not asserted with confidence, pending live verification.

Polymarket US settlement always returns UNKNOWN in this stage: no
resolved-market payload has ever been fetched from it (Stage 2A only
sampled open markets), so guessing field names here would violate the
explicit "never fabricate a result" instruction. This is a known,
documented limitation, not an oversight -- see the Stage 3 completion
report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.execution.base import Market
from src.execution.paper.models import PaperPositionStatus, PaperSettlementResult

_KALSHI_VOID_STATUSES = frozenset({"voided", "void"})
_KALSHI_FINALIZED_STATUSES = frozenset({"finalized", "settled"})


def resolve_kalshi_market(market: Market) -> PaperSettlementResult:
    raw = market.raw or {}
    status = str(raw.get("status", "")).strip().lower()
    result = str(raw.get("result", "")).strip().lower()

    if status in _KALSHI_VOID_STATUSES:
        return PaperSettlementResult(
            provider="kalshi", market_id=market.id, resolved=True, winning_side=None,
            payout_per_contract=None, source="kalshi_market_status_void_unverified",
            resolved_at=datetime.now(timezone.utc), raw=raw,
        )

    if status in _KALSHI_FINALIZED_STATUSES and result in ("yes", "no"):
        return PaperSettlementResult(
            provider="kalshi", market_id=market.id, resolved=True, winning_side=result.upper(),
            payout_per_contract=Decimal("1"), source="kalshi_market_result_unverified",
            resolved_at=datetime.now(timezone.utc), raw=raw,
        )

    return PaperSettlementResult(
        provider="kalshi", market_id=market.id, resolved=False, winning_side=None,
        payout_per_contract=None, source="unresolved", resolved_at=None, raw=raw,
    )


def resolve_polymarket_market(market: Market) -> PaperSettlementResult:
    return PaperSettlementResult(
        provider="polymarket_us", market_id=market.id, resolved=False, winning_side=None,
        payout_per_contract=None, source="polymarket_us_settlement_not_yet_verified",
        resolved_at=None, raw=market.raw or {},
    )


def resolve_market(provider_name: str, market: Market) -> PaperSettlementResult:
    if provider_name == "kalshi":
        return resolve_kalshi_market(market)
    return resolve_polymarket_market(market)


def determine_position_outcome(
    settlement: PaperSettlementResult, side: str, quantity: Decimal, entry_cost: Decimal, fees_paid: Decimal,
) -> tuple[PaperPositionStatus, Decimal | None, Decimal | None]:
    """Only ever called when settlement.resolved is True -- the caller
    (paper/broker.py::settle_positions) leaves a position untouched
    (still OPEN) whenever resolution can't be determined yet, rather
    than asking this function to guess between OPEN and UNKNOWN.
    UNKNOWN is reserved for a future manual-override capability, not
    produced automatically by this stage. Returns (status,
    settlement_value, realized_pnl)."""
    if not settlement.resolved:
        raise ValueError("determine_position_outcome requires a resolved PaperSettlementResult")

    if settlement.winning_side is None:
        # Void/refund: contract cost is refunded, fees already paid are
        # not (matches typical exchange void semantics of "as if the
        # order never happened" for the stake, but fees already
        # assessed are not reversed).
        settlement_value = entry_cost - fees_paid
        realized_pnl = -fees_paid
        return PaperPositionStatus.VOID, settlement_value, realized_pnl

    payout_per_contract = Decimal("1") if settlement.winning_side == side else Decimal("0")
    gross_payout = payout_per_contract * quantity
    realized_pnl = gross_payout - entry_cost
    status = PaperPositionStatus.WON if gross_payout > 0 else PaperPositionStatus.LOST
    return status, gross_payout, realized_pnl
