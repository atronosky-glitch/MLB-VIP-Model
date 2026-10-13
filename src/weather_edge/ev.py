"""Expected value of buying one binary weather contract at the ACTUAL executable price.

For a contract that pays $1 if it wins, buying ``q`` contracts that fill at volume-weighted
average price ``a`` with total fee ``F``:

    gross EV / contract = p_win - a
    net   EV / contract = p_win - (q*a + F) / q          (fees and slippage included)
    net   EV %          = net EV / contract  /  ((q*a + F) / q)     (relative to cost)

``a`` comes from walking the real order book (src/execution/orderbook_math.walk_book), so a
larger order than the book can absorb at the best price pays the worse prices behind it. Buying NO
uses ``p_win = 1 - P(YES)`` and the NO-side asks. A last-traded price is never an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from src.execution.base import OrderLevel
from src.execution.orderbook_math import walk_book

from .fees import FeeFn

_ZERO = Decimal("0")


@dataclass(frozen=True)
class EVResult:
    side: str                       # "YES" | "NO"
    requested_qty: Decimal
    filled_qty: Decimal
    fill_ratio: Decimal
    best_price: Decimal
    avg_price: Decimal
    gross_cost: Decimal
    fee: Decimal
    total_cost: Decimal
    p_win: Decimal
    gross_ev_per_contract: Decimal
    net_ev_per_contract: Decimal
    net_ev_pct: Decimal             # percent, relative to cost
    break_even_prob: Decimal        # win probability at which net EV is exactly zero
    slippage_per_contract: Decimal  # avg price paid minus best price


def target_quantity(stake_usd: Decimal, best_price: Decimal, min_order: Decimal, max_contracts: Decimal) -> Decimal:
    """Whole contracts a paper stake buys at the best price, within [min_order, max_contracts]."""
    if best_price <= 0:
        raise ValueError("best_price must be positive")
    qty = (stake_usd / best_price).to_integral_value(rounding=ROUND_FLOOR)
    return max(min_order, min(max_contracts, qty))


def evaluate_side(
    *, side: str, asks: tuple[OrderLevel, ...] | list[OrderLevel], p_win: float, stake_usd: Decimal,
    fee_fn: FeeFn, min_order: Decimal = Decimal("1"), max_contracts: Decimal = Decimal("2000"),
) -> EVResult | None:
    """EV of a marketable buy sized by ``stake_usd``; ``None`` when there is nothing to buy."""
    levels = [lv for lv in asks if lv.quantity > 0 and 0 < lv.price < 1]
    if not levels:
        return None
    levels.sort(key=lambda lv: lv.price)
    best = levels[0].price
    want = target_quantity(stake_usd, best, min_order, max_contracts)
    fill = walk_book(levels, want)
    if fill.quantity_filled <= 0 or fill.vwap is None:
        return None
    filled = fill.quantity_filled
    fee = fee_fn(filled, fill.vwap)
    total_cost = fill.gross_cost + fee
    cost_per = total_cost / filled
    p = Decimal(str(p_win))
    net = p - cost_per
    return EVResult(
        side=side, requested_qty=want, filled_qty=filled, fill_ratio=filled / want,
        best_price=best, avg_price=fill.vwap, gross_cost=fill.gross_cost, fee=fee, total_cost=total_cost,
        p_win=p, gross_ev_per_contract=p - fill.vwap, net_ev_per_contract=net,
        net_ev_pct=(net / cost_per) * 100, break_even_prob=cost_per, slippage_per_contract=fill.vwap - best,
    )


def paper_pnl(*, filled_qty: Decimal, total_cost: Decimal, won: bool) -> Decimal:
    """Realized paper P&L of one settled position: $1 per winning contract minus everything paid."""
    payout = filled_qty if won else _ZERO
    return payout - total_cost
