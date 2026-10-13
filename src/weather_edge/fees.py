"""Taker-fee formulas for the two platforms (pure, Decimal).

Both formulas were verified against the platforms' own published rules on 2026-10-12:

* Kalshi (series metadata ``fee_type: "quadratic"``, ``fee_multiplier``):
  ``fee = ceil_to_cent(0.07 x multiplier x C x P x (1 - P))`` per order. Same formula the
  execution layer uses (src/execution/kalshi.py).
* Polymarket (https://docs.polymarket.com/trading/fees; market payload ``feeSchedule``):
  ``fee = C x rate x P x (1 - P)`` with ``rate = 0.05`` for weather, taker only, rounded to 5 decimal
  places. Makers are never charged.

Both are TAKER fees: this module prices a marketable order that crosses the spread right now,
which is the only fill the analysis assumes. Resting (maker) orders are out of scope.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Callable

KALSHI_FEE_COEFFICIENT = Decimal("0.07")
_ONE = Decimal("1")

FeeFn = Callable[[Decimal, Decimal], Decimal]


def kalshi_taker_fee(contracts: Decimal, price: Decimal, multiplier: Decimal = _ONE) -> Decimal:
    if contracts <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    raw = KALSHI_FEE_COEFFICIENT * multiplier * contracts * price * (_ONE - price)
    cents = (raw * 100).to_integral_value(rounding=ROUND_CEILING)
    return cents / 100


def polymarket_taker_fee(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    if shares <= 0 or price <= 0 or price >= 1:
        return Decimal("0")
    raw = shares * rate * price * (_ONE - price)
    return raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def fee_function(fee_model: str, fee_param: Decimal) -> FeeFn:
    """Bind a contract's fee parameters into ``fn(contracts, price) -> fee_usd``."""
    if fee_model == "kalshi_quadratic":
        return lambda contracts, price: kalshi_taker_fee(contracts, price, fee_param)
    if fee_model == "polymarket_curve":
        return lambda contracts, price: polymarket_taker_fee(contracts, price, fee_param)
    raise ValueError(f"unknown fee model {fee_model!r}")
