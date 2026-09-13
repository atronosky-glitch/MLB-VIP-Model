"""Bet/unit sizing for Stage 3 paper trading. Pure, Decimal, no I/O --
takes a net EV / model probability / effective cost in, returns a
recommended (not yet risk-approved) units/USD stake out.

Unit system: UNIT_SIZE_USD is the dollar value of one "unit" (a
sportsbook-style stake-sizing convention). 0.25u/0.5u/1u/1.5u/2u etc.
are fractional multiples of that dollar amount -- units_to_usd()/
usd_to_units() are the only two places this conversion happens.

Kelly formula for a binary contract (derived, not guessed -- see the
Stage 3 plan): bought at effective cost c (dollars per contract,
fees included) with true win probability q, a win pays $1 so the
profit-if-correct per dollar staked is (1-c)/c and the loss-if-wrong
is the full stake. Standard Kelly f* = q - (1-q)/b with b = (1-c)/c
simplifies to:

    f* = (q - c) / (1 - c)

Sanity check against the textbook even-money case: at c = 0.5, this
reduces to f* = 2q - 1, the well-known 50/50-stake Kelly result.
f* <= 0 (no edge) returns 0, never a negative stake.

c (the "effective cost") is exactly Stage 2B's
EVResult.break_even_probability (src/execution/ev.py) -- the price at
which profit is exactly zero is, by definition, the effective cost --
so Kelly sizing here reuses that already-computed value rather than
recomputing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Sequence


class SizingMode(str, Enum):
    FLAT = "FLAT"
    EV_TIERED = "EV_TIERED"
    FRACTIONAL_KELLY = "FRACTIONAL_KELLY"


@dataclass(frozen=True)
class SizingResult:
    mode: SizingMode
    recommended_units: Decimal
    recommended_stake_usd: Decimal
    detail: str


def units_to_usd(units: Decimal, unit_size_usd: Decimal) -> Decimal:
    return units * unit_size_usd


def usd_to_units(usd: Decimal, unit_size_usd: Decimal) -> Decimal:
    if unit_size_usd <= 0:
        raise ValueError("unit_size_usd must be positive")
    return usd / unit_size_usd


def size_flat(default_units: Decimal, unit_size_usd: Decimal) -> SizingResult:
    """Deterministic: always DEFAULT_UNITS, regardless of net EV."""
    stake = units_to_usd(default_units, unit_size_usd)
    return SizingResult(
        mode=SizingMode.FLAT, recommended_units=default_units, recommended_stake_usd=stake,
        detail=f"flat {default_units}u",
    )


def size_ev_tiered(
    net_ev_pct: Decimal,
    tiers: Sequence[tuple[float, float]],
    unit_size_usd: Decimal,
) -> SizingResult:
    """*tiers* is a sequence of (min_net_ev_pct, units) sorted ascending
    by threshold (production_config.ProductionConfig.ev_tiered_sizing_tiers()
    already returns them this way). Picks the HIGHEST threshold that
    net_ev_pct clears (>=, so a value exactly at a boundary gets that
    tier, not the one below it). A net_ev_pct below every configured
    threshold still uses the lowest tier's units -- the opportunity
    already cleared Stage 2B's own min_net_ev_pct gate to exist at all,
    so sizing must recommend *something*, not silently zero out a
    qualified opportunity just because the tier table starts higher."""
    if not tiers:
        raise ValueError("tiers must not be empty")

    sorted_tiers = sorted(tiers, key=lambda t: t[0])
    chosen_threshold, chosen_units = sorted_tiers[0]
    for threshold, units in sorted_tiers:
        if net_ev_pct >= Decimal(str(threshold)):
            chosen_threshold, chosen_units = threshold, units

    units_dec = Decimal(str(chosen_units))
    stake = units_to_usd(units_dec, unit_size_usd)
    return SizingResult(
        mode=SizingMode.EV_TIERED, recommended_units=units_dec, recommended_stake_usd=stake,
        detail=f"net_ev_pct={net_ev_pct}% -> tier>={chosen_threshold}% ({units_dec}u)",
    )


def kelly_fraction(q: Decimal, effective_cost: Decimal) -> Decimal:
    """f* = (q - c) / (1 - c). Returns 0 (never negative) when there's
    no edge (q <= c) or the inputs are degenerate (c not in (0, 1))."""
    if effective_cost <= 0 or effective_cost >= 1:
        return Decimal("0")
    f = (q - effective_cost) / (Decimal("1") - effective_cost)
    return f if f > 0 else Decimal("0")


def size_kelly(
    q: Decimal,
    effective_cost: Decimal,
    kelly_multiplier: Decimal,
    unit_size_usd: Decimal,
    max_units_per_bet: Decimal,
    available_bankroll_usd: Decimal,
) -> SizingResult:
    """Quarter-Kelly (or whatever kelly_multiplier is) of the full Kelly
    fraction, applied to available_bankroll_usd, then clamped to
    max_units_per_bet and to available_bankroll_usd itself -- a
    reasonableness cap at the sizing layer; RiskEngine re-enforces the
    same and other limits afterward as the authoritative gate."""
    full_kelly = kelly_fraction(q, effective_cost)
    fractional = full_kelly * kelly_multiplier
    stake = fractional * available_bankroll_usd

    max_stake_from_units = units_to_usd(max_units_per_bet, unit_size_usd)
    capped_stake = min(stake, max_stake_from_units, available_bankroll_usd)
    capped_stake = max(capped_stake, Decimal("0"))

    units = usd_to_units(capped_stake, unit_size_usd) if capped_stake > 0 else Decimal("0")
    return SizingResult(
        mode=SizingMode.FRACTIONAL_KELLY, recommended_units=units, recommended_stake_usd=capped_stake,
        detail=(
            f"full_kelly={full_kelly}, multiplier={kelly_multiplier}, "
            f"pre_clamp_stake={stake}, clamped_stake={capped_stake}"
        ),
    )
