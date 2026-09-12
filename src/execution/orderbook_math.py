"""Pure order-book-walking math. Decimal throughout, no I/O, no
provider knowledge -- operates only on src.execution.base.OrderLevel
lists (already-normalized price/quantity levels, best price first).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.execution.base import OrderLevel


@dataclass(frozen=True)
class FillResult:
    quantity_filled: Decimal
    quantity_unfilled: Decimal
    best_price: Decimal | None
    vwap: Decimal | None
    gross_cost: Decimal
    price_impact: Decimal | None    # vwap - best_price; None if nothing filled
    slippage_pct: Decimal | None    # price_impact / best_price; None if nothing filled


def walk_book(levels: list[OrderLevel], requested_quantity: Decimal) -> FillResult:
    """Simulate filling a hypothetical marketable order of
    *requested_quantity* contracts against *levels* (best price first).
    Never mutates *levels*. Zero liquidity (empty levels) is not an
    error -- it's a fully-unfilled result."""
    if requested_quantity <= 0:
        raise ValueError("requested_quantity must be positive")

    if not levels:
        return FillResult(
            quantity_filled=Decimal("0"), quantity_unfilled=requested_quantity,
            best_price=None, vwap=None, gross_cost=Decimal("0"),
            price_impact=None, slippage_pct=None,
        )

    best_price = levels[0].price
    remaining = requested_quantity
    filled = Decimal("0")
    cost = Decimal("0")

    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity)
        cost += take * level.price
        filled += take
        remaining -= take

    vwap = (cost / filled) if filled > 0 else None
    price_impact = (vwap - best_price) if vwap is not None else None
    slippage_pct = (
        price_impact / best_price if price_impact is not None and best_price > 0 else None
    )

    return FillResult(
        quantity_filled=filled, quantity_unfilled=remaining,
        best_price=best_price, vwap=vwap, gross_cost=cost,
        price_impact=price_impact, slippage_pct=slippage_pct,
    )


def max_quantity_for_budget(levels: list[OrderLevel], budget: Decimal) -> Decimal:
    """How many contracts (fractional -- rounding to a whole number is
    a future sizing stage's job, not this pure math's) *budget* dollars
    can buy walking *levels*."""
    if budget <= 0:
        return Decimal("0")

    remaining_budget = budget
    total_quantity = Decimal("0")

    for level in levels:
        level_cost = level.price * level.quantity
        if level_cost <= remaining_budget:
            total_quantity += level.quantity
            remaining_budget -= level_cost
        else:
            if level.price > 0:
                total_quantity += remaining_budget / level.price
            remaining_budget = Decimal("0")
            break

    return total_quantity
