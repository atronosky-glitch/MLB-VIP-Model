"""Paper fill simulation: given a REAL, freshly-fetched normalized order
book (Stage 2's NormalizedOrderBook), simulate filling an approved-stake
order at whole-contract granularity, never paying above
ExecutionOpportunity.max_acceptable_price.

Reuses Stage 2's own src.execution.orderbook_math.walk_book/
max_quantity_for_budget directly (filter-then-delegate for the price
ceiling) rather than re-implementing book-walking math -- this module
contains zero pricing formulas of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal

from src.execution.base import FeeEstimate, OrderLevel
from src.execution.orderbook_math import FillResult, max_quantity_for_budget, walk_book
from src.execution.paper.models import PaperRejectionReason


@dataclass(frozen=True)
class SimulatedFill:
    quantity_requested: Decimal
    quantity_filled: Decimal
    average_fill_price: Decimal | None
    gross_cost: Decimal
    fees: Decimal
    total_cost: Decimal
    slippage: Decimal | None
    timestamp: datetime


@dataclass(frozen=True)
class PaperFillOutcome:
    status: str  # "FILLED" / "PARTIALLY_FILLED" / "REJECTED"
    fill: SimulatedFill | None
    rejection_reason: PaperRejectionReason | None
    detail: str


def _floor_to_whole_contract(quantity: Decimal) -> Decimal:
    return quantity.to_integral_value(rounding=ROUND_FLOOR)


def _empty_fill() -> FillResult:
    return FillResult(
        quantity_filled=Decimal("0"), quantity_unfilled=Decimal("0"),
        best_price=None, vwap=None, gross_cost=Decimal("0"),
        price_impact=None, slippage_pct=None,
    )


def simulate_paper_fill(
    asks: list[OrderLevel],
    side: str,
    desired_budget_usd: Decimal,
    max_acceptable_price: Decimal,
    fee_model,
    allow_partial_fills: bool,
) -> PaperFillOutcome:
    """*asks* must already be the correct side's ask levels (book.yes_asks
    or book.no_asks -- the caller picks based on *side*). *desired_budget_usd*
    is the RiskEngine-approved stake; the whole-contract quantity it can
    buy is derived here (not before), since it depends on the actual
    price paid, which depends on the ceiling. *fee_model* is
    provider.estimate_fees (side/price/quantity) -> FeeEstimate, reused
    from Stage 2's provider interface, not duplicated.

    A fill that spends the whole budget across MULTIPLE price levels is
    a completely normal FILLED outcome, not a partial fill -- "partial"
    specifically means the book (within the price ceiling) ran out of
    contracts before the budget did, which is detected by comparing the
    budget's true multi-level-aware demand (max_quantity_for_budget)
    against the capped book's total depth, not by a naive
    best-price-only estimate.
    """
    if not asks:
        return PaperFillOutcome(
            status="REJECTED", fill=None, rejection_reason=PaperRejectionReason.INSUFFICIENT_LIQUIDITY,
            detail="orderbook has no asks on this side",
        )

    if desired_budget_usd <= 0 or max_acceptable_price <= 0:
        return PaperFillOutcome(
            status="REJECTED", fill=None, rejection_reason=PaperRejectionReason.INSUFFICIENT_LIQUIDITY,
            detail="non-positive budget or price ceiling",
        )

    # Naive best-price estimate, kept only for the reported
    # quantity_requested audit field -- never used to decide fill status.
    naive_requested_qty = _floor_to_whole_contract(desired_budget_usd / asks[0].price)

    capped_levels = [lvl for lvl in asks if lvl.price <= max_acceptable_price]
    if not capped_levels:
        uncapped_qty = max_quantity_for_budget(asks, desired_budget_usd)
        price_limited = uncapped_qty > 0
        reason = (
            PaperRejectionReason.PRICE_MOVED_BEYOND_LIMIT if price_limited
            else PaperRejectionReason.INSUFFICIENT_LIQUIDITY
        )
        detail = (
            "book had liquidity but only above max_acceptable_price" if price_limited
            else "no fillable liquidity within budget"
        )
        return PaperFillOutcome(status="REJECTED", fill=None, rejection_reason=reason, detail=detail)

    target_qty_fractional = max_quantity_for_budget(capped_levels, desired_budget_usd)
    target_qty = _floor_to_whole_contract(target_qty_fractional)
    if target_qty <= 0:
        return PaperFillOutcome(
            status="REJECTED", fill=None, rejection_reason=PaperRejectionReason.INSUFFICIENT_LIQUIDITY,
            detail="budget cannot buy even one contract within the price ceiling",
        )

    final_fill = walk_book(capped_levels, target_qty)
    # Safety net: flooring should already guarantee affordability, but
    # re-walk down if rounding ever pushes cost a hair over budget.
    while final_fill.gross_cost > desired_budget_usd and target_qty > 0:
        target_qty -= 1
        final_fill = walk_book(capped_levels, target_qty) if target_qty > 0 else _empty_fill()

    if target_qty <= 0:
        return PaperFillOutcome(
            status="REJECTED", fill=None, rejection_reason=PaperRejectionReason.INSUFFICIENT_LIQUIDITY,
            detail="budget cannot buy even one contract within the price ceiling",
        )

    total_capped_book_qty = sum((lvl.quantity for lvl in capped_levels), Decimal("0"))
    book_exhausted_before_budget = target_qty_fractional >= total_capped_book_qty

    if book_exhausted_before_budget:
        uncapped_qty = max_quantity_for_budget(asks, desired_budget_usd)
        price_limited = uncapped_qty > total_capped_book_qty
        reason = (
            PaperRejectionReason.PRICE_MOVED_BEYOND_LIMIT if price_limited
            else PaperRejectionReason.INSUFFICIENT_LIQUIDITY
        )
        if not allow_partial_fills:
            return PaperFillOutcome(
                status="REJECTED", fill=None, rejection_reason=reason,
                detail=(
                    f"only {target_qty} contracts fillable (book exhausted within budget) "
                    "and ALLOW_PARTIAL_PAPER_FILLS=false"
                ),
            )
        status = "PARTIALLY_FILLED"
    else:
        # The budget ran out first, at a book that still had more depth
        # to sell -- the budget was fully deployed as intended.
        status = "FILLED"

    fee_estimate: FeeEstimate = fee_model(side, final_fill.vwap, final_fill.quantity_filled)
    fee = fee_estimate.fee
    total_cost = final_fill.gross_cost + fee

    fill = SimulatedFill(
        quantity_requested=naive_requested_qty, quantity_filled=target_qty,
        average_fill_price=final_fill.vwap, gross_cost=final_fill.gross_cost,
        fees=fee, total_cost=total_cost, slippage=final_fill.price_impact,
        timestamp=datetime.now(timezone.utc),
    )
    return PaperFillOutcome(status=status, fill=fill, rejection_reason=None, detail=f"filled {target_qty} contracts")
