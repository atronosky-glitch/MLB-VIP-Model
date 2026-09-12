"""ExecutableQuote assembly: combine a normalized order book, a
requested side/quantity, and a fee estimate into the full picture of
"what would it actually cost to execute this right now." Pure, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.execution.base import FeeEstimate, NormalizedOrderBook
from src.execution.orderbook_math import walk_book


@dataclass(frozen=True)
class ExecutableQuote:
    provider: str
    provider_market_id: str
    side: str                          # "YES" / "NO"
    desired_contracts: Decimal
    best_price: Decimal | None
    best_quantity: Decimal | None
    expected_fill_price: Decimal | None   # VWAP
    expected_fill_quantity: Decimal
    requested_quantity: Decimal
    unfilled_quantity: Decimal
    gross_cost: Decimal
    estimated_fees: Decimal
    expected_total_cost: Decimal
    spread: Decimal | None
    spread_pct: Decimal | None
    slippage_absolute: Decimal | None
    slippage_pct: Decimal | None
    liquidity_available: Decimal        # total notional depth on the ask side, not just what was filled
    orderbook_timestamp: datetime


def build_executable_quote(
    provider: str,
    provider_market_id: str,
    book: NormalizedOrderBook,
    side: str,
    quantity: Decimal,
    fee_estimate: FeeEstimate,
) -> ExecutableQuote:
    """Buying *quantity* contracts of *side* ("YES"/"NO") against *book*."""
    if side not in ("YES", "NO"):
        raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")

    bids = book.yes_bids if side == "YES" else book.no_bids
    asks = book.yes_asks if side == "YES" else book.no_asks

    fill = walk_book(asks, quantity)
    best_bid = bids[0].price if bids else None
    best_ask = fill.best_price

    spread = (best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None
    mid = ((best_ask + best_bid) / 2) if spread is not None else None
    spread_pct = (spread / mid) if (spread is not None and mid is not None and mid > 0) else None

    liquidity_available = sum((lvl.price * lvl.quantity for lvl in asks), Decimal("0"))
    total_cost = fill.gross_cost + fee_estimate.fee

    return ExecutableQuote(
        provider=provider,
        provider_market_id=provider_market_id,
        side=side,
        desired_contracts=quantity,
        best_price=best_ask,
        best_quantity=(asks[0].quantity if asks else None),
        expected_fill_price=fill.vwap,
        expected_fill_quantity=fill.quantity_filled,
        requested_quantity=quantity,
        unfilled_quantity=fill.quantity_unfilled,
        gross_cost=fill.gross_cost,
        estimated_fees=fee_estimate.fee,
        expected_total_cost=total_cost,
        spread=spread,
        spread_pct=spread_pct,
        slippage_absolute=fill.price_impact,
        slippage_pct=fill.slippage_pct,
        liquidity_available=liquidity_available,
        orderbook_timestamp=book.timestamp,
    )
