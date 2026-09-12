"""Expected-value math: raw EV, net EV, break-even probability, and the
maximum acceptable entry price. Pure, Decimal, no I/O, no provider
knowledge (fee computation is injected as a callable).

net_ev_pct definition (documented explicitly per the Stage 2B plan,
matching this project's one existing EV convention rather than
inventing a new one): src/player_prop_analysis.py::calculate_ev defines
sportsbook EV as (expected_profit / amount_at_risk) x 100 -- return
relative to what you could lose. A sportsbook bet has no separate fee
(vig is embedded in the odds), so "amount staked" already equals "total
amount at risk" there. Prediction-market fees are a real, separate,
unrecoverable cost, so the direct generalization of the SAME idea is:

    net_ev_pct = (expected_profit / total_entry_cost) x 100
    where total_entry_cost = gross_cost + estimated_fees

raw_ev_pct mirrors the existing sportsbook ev_pct almost exactly,
computed from an executable contract price instead of American odds:

    raw_ev_pct = (expected_profit_before_fees / gross_cost) x 100

Both exchanges settle binary contracts at exactly $1.00/contract to the
winning side (confirmed from docs.kalshi.com and docs.polymarket.us) --
so expected_payout = win_probability x quantity, using the fuller
per-contract accounting rather than the bare "p - c" shortcut, since
fees are always folded in as an explicit, separate cost term here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Callable

from src.execution.base import FeeEstimate
from src.execution.quotes import ExecutableQuote

FeeModel = Callable[[str, Decimal, Decimal], FeeEstimate]


@dataclass(frozen=True)
class EVResult:
    model_probability: Decimal
    contract_probability: Decimal
    expected_fill_price: Decimal
    gross_cost: Decimal
    estimated_fees: Decimal
    total_entry_cost: Decimal
    expected_payout: Decimal
    expected_profit: Decimal
    raw_ev_dollars: Decimal
    raw_ev_pct: Decimal
    net_ev_dollars: Decimal
    net_ev_pct: Decimal
    break_even_probability: Decimal
    max_acceptable_entry_price: Decimal


def break_even_probability(total_entry_cost: Decimal, quantity: Decimal) -> Decimal:
    """The win probability at which expected_profit is exactly zero."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    return total_entry_cost / quantity


def compute_ev(
    model_probability: Decimal,
    side: str,
    quote: ExecutableQuote,
    min_net_ev_pct: Decimal,
    fee_model: FeeModel,
) -> EVResult:
    """model_probability is always the model's YES probability;
    transformed to the NO probability internally when side == "NO"."""
    if side not in ("YES", "NO"):
        raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")
    if quote.expected_fill_quantity <= 0:
        raise ValueError("cannot compute EV with zero filled quantity")
    if quote.expected_fill_price is None:
        raise ValueError("cannot compute EV without an expected fill price")

    q = model_probability if side == "YES" else (Decimal("1") - model_probability)
    quantity = quote.expected_fill_quantity
    gross_cost = quote.gross_cost
    fees = quote.estimated_fees
    total_entry_cost = quote.expected_total_cost

    expected_payout = q * quantity
    raw_expected_profit = expected_payout - gross_cost
    expected_profit = expected_payout - total_entry_cost

    raw_ev_pct = (raw_expected_profit / gross_cost * 100) if gross_cost > 0 else Decimal("0")
    net_ev_pct = (expected_profit / total_entry_cost * 100) if total_entry_cost > 0 else Decimal("0")

    beep = break_even_probability(total_entry_cost, quantity)
    max_price = max_acceptable_price(q, min_net_ev_pct, fee_model, quantity, side)

    return EVResult(
        model_probability=model_probability,
        contract_probability=quote.expected_fill_price,
        expected_fill_price=quote.expected_fill_price,
        gross_cost=gross_cost,
        estimated_fees=fees,
        total_entry_cost=total_entry_cost,
        expected_payout=expected_payout,
        expected_profit=expected_profit,
        raw_ev_dollars=raw_expected_profit,
        raw_ev_pct=raw_ev_pct,
        net_ev_dollars=expected_profit,
        net_ev_pct=net_ev_pct,
        break_even_probability=beep,
        max_acceptable_entry_price=max_price,
    )


def max_acceptable_price(
    q: Decimal,
    min_net_ev_pct: Decimal,
    fee_model: FeeModel,
    quantity: Decimal,
    side: str,
) -> Decimal:
    """Highest whole-cent entry price P for which net_ev_pct(P) still
    satisfies >= min_net_ev_pct, solved by bisection against the
    provider's REAL fee function (fee_model) -- not an idealized
    continuous approximation. Verified independently (see Stage 2B
    plan): with a 7% fee coefficient, 64% model probability, and a 5%
    minimum net EV target, this converges to ~59.26 cents, below the
    64% fair probability as expected, and below the no-fee approximation
    of ~60.95 cents since fees visibly tighten the ceiling.

    Deterministic bisection over price in (0, 1) rather than a
    closed-form inversion, because the real fee is not continuous
    (Kalshi rounds up to the cent, Polymarket uses banker's rounding) --
    a closed-form solve of the idealized quadratic would only be a
    starting bracket, not the final answer, so bisecting directly
    against the real, injected fee_model is both simpler and exact
    against whatever a live quote would actually compute.
    """
    def net_ev_pct_at(price: Decimal) -> Decimal:
        fee = fee_model(side, price, quantity).fee
        cost = price * quantity + fee
        if cost <= 0:
            return Decimal("999")
        profit = q * quantity - cost
        return profit / cost * 100

    lo, hi = Decimal("0.01"), Decimal("0.99")

    if net_ev_pct_at(lo) < min_net_ev_pct:
        return Decimal("0.00")  # not even the cheapest price qualifies

    if net_ev_pct_at(hi) < min_net_ev_pct:
        for _ in range(60):
            mid = (lo + hi) / 2
            if net_ev_pct_at(mid) >= min_net_ev_pct:
                lo = mid
            else:
                hi = mid
        candidate = lo
    else:
        candidate = hi

    # Round DOWN to the nearest whole cent -- a lower price only ever
    # has MORE room to satisfy ">=", never less, so this can't turn a
    # qualifying candidate into a non-qualifying one.
    cents = int((candidate * 100).to_integral_value(rounding=ROUND_FLOOR))
    result = Decimal(cents) / 100

    # Safety net for the (very unlikely) case where fee rounding
    # introduces a non-monotonic step right at this boundary.
    while result > Decimal("0.01") and net_ev_pct_at(result) < min_net_ev_pct:
        result -= Decimal("0.01")

    return result
