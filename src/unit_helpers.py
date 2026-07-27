"""Unit conversion helpers for the MLB VIP Model.

Internal convention:
- Database stores percentage points (e.g. 5.23 = 5.23%)
- Display uses percentage points directly
- Probability calculations use decimals from 0 to 1
- Convert only at defined boundaries using these helpers

All conversion is centralized here. Never scatter /100 or *100
throughout the codebase.
"""

from __future__ import annotations


# ── Percentage point conversions ────────────────────────────────────

def pp_to_decimal(pp: float) -> float:
    """Convert percentage points to a decimal fraction.

    Example: 5.23 → 0.0523
    """
    return pp / 100.0


def decimal_to_pp(d: float) -> float:
    """Convert a decimal fraction to percentage points.

    Example: 0.0523 → 5.23
    """
    return d * 100.0


# ── Display formatting ──────────────────────────────────────────────

def format_ev_pct(ev_pct: float) -> str:
    """Format O/U EV as a percentage string.

    Example: 5.2341 → '5.23%'
    """
    return f"{ev_pct:+.2f}%"


def format_price_advantage(adv_pp: float) -> str:
    """Format YN price advantage as percentage points string.

    Example: 6.4059 → '6.41 pp'
    """
    return f"{adv_pp:+.2f} pp"


def format_score(score: float | None) -> str:
    """Format Model Score to one decimal place.

    Example: 9.3456 → '9.3', None → 'N/A'
    """
    if score is None:
        return "N/A"
    return f"{score:.1f}"


# ── EV thresholds (stored in percentage points) ────────────────────
# These mirror prop_config.py thresholds but in pp form for clarity.

EV_STRONG_PP = 5.0    # >= 5.0 pp
EV_POSITIVE_PP = 2.0  # >= 2.0 pp
EV_MARGINAL_PP = 0.0  # > 0.0 pp

YN_STRONG_PP = 8.0    # >= 8.0 pp
YN_OUTLIER_PP = 4.0   # >= 4.0 pp
YN_MARGINAL_PP = 2.0  # >= 2.0 pp
