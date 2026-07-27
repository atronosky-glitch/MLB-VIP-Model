"""Grading engine for historical recommendations.

Implements deterministic settlement rules for O/U and YN markets,
CLV calculation, unit tracking, and performance summaries.

YN grading: The settlement meaning of each YN market was investigated:
- strikeouts YN: YES = pitcher records >= 1 strikeout. No hidden threshold beyond >= 1.
- walks YN: YES = pitcher allows >= 1 walk. Same binary structure.
- earned_runs YN: YES = pitcher allows >= 1 earned run. Same binary structure.

However, the stored market data does NOT include the threshold condition
explicitly (it is implicit in the market definition). Without access to
the game's box score or official settlement feed, we cannot reliably
determine whether the YES condition was met for YN markets.

Decision: YN grading is UNSUPPORTED in automated mode. YN recommendations
are trackable but settlement remains UNRESOLVED unless an explicit external
result is supplied via manual override.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.db_manager import (
    get_connection,
    get_unsettled_recommendations,
    get_settled_recommendations,
    get_recommendation_by_id,
    settle_recommendation,
    save_bet_units,
    save_closing_price,
    compute_units,
)

logger = logging.getLogger(__name__)

# Grader version — increment when grading logic changes
GRADER_VERSION = "v1.0"

# Settlement statuses
SETTLEMENT_WIN = "WIN"
SETTLEMENT_LOSS = "LOSS"
SETTLEMENT_PUSH = "PUSH"
SETTLEMENT_VOID = "VOID"
SETTLEMENT_CANCELLED = "CANCELLED"
SETTLEMENT_UNRESOLVED = "UNRESOLVED"

# Valid settlement statuses
VALID_SETTLEMENTS = frozenset({
    SETTLEMENT_WIN, SETTLEMENT_LOSS, SETTLEMENT_PUSH,
    SETTLEMENT_VOID, SETTLEMENT_CANCELLED, SETTLEMENT_UNRESOLVED,
})

# ── EV Buckets ────────────────────────────────────────────────────

EV_BUCKETS = [
    ("below_0", lambda ev: ev < 0),
    ("0_to_2", lambda ev: 0 <= ev < 2),
    ("2_to_5", lambda ev: 2 <= ev < 5),
    ("5_to_10", lambda ev: 5 <= ev < 10),
    ("10_plus", lambda ev: ev >= 10),
]

# ── American Odds Buckets ─────────────────────────────────────────

ODDS_BUCKETS = [
    ("shorter_than_-200", lambda odds: odds < -200),
    ("-200_to_-151", lambda odds: -200 <= odds <= -151),
    ("-150_to_-101", lambda odds: -150 <= odds <= -101),
    ("_-100_to_+100", lambda odds: -100 <= odds <= 100),
    ("+101_to_+150", lambda odds: 101 <= odds <= 150),
    ("+151_to_+200", lambda odds: 151 <= odds <= 200),
    ("longer_than_+200", lambda odds: odds > 200),
]

# ── Comparison-book Buckets ───────────────────────────────────────

N_BOOKS_BUCKETS = [
    ("fewer_than_3", lambda n: n < 3),
    ("3", lambda n: n == 3),
    ("4", lambda n: n == 4),
    ("5_plus", lambda n: n >= 5),
]

# ── YN Price-advantage Buckets (replaces EV buckets for YN) ──────

YN_ADV_BUCKETS = [
    ("below_0", lambda adv: adv < 0),
    ("0_to_2", lambda adv: 0 <= adv < 2),
    ("2_to_4", lambda adv: 2 <= adv < 4),
    ("4_to_8", lambda adv: 4 <= adv < 8),
    ("8_plus", lambda adv: adv >= 8),
]


def assign_bucket(value, buckets):
    """Assign a value to the first matching bucket."""
    for label, test in buckets:
        if test(value):
            return label
    return "unknown"


# ==================================================================
# O/U Grading
# ==================================================================

def grade_ou(final_stat: float, line: float, side: str) -> str:
    """Grade an O/U market given the final stat value and line.

    Rules:
    - OVER wins when final_stat > line
    - OVER loses when final_stat < line
    - UNDER wins when final_stat < line
    - UNDER loses when final_stat > line
    - Exact equality on whole-number lines is PUSH
    - Half-lines cannot push (equality is impossible for float stats,
      but if it happens, it counts as a LOSS for the side)
    - Returns SETTLEMENT_UNRESOLVED if inputs are invalid

    Parameters
    ----------
    final_stat : float
        The final stat value (e.g., strikeouts, outs, hits, walks, ER).
    line : float
        The market line (e.g., 5.5, 6.0).
    side : str
        "OVER" or "UNDER".

    Returns
    -------
    str: settlement status string.
    """
    side = side.upper().strip()
    if side not in ("OVER", "UNDER"):
        logger.error("Invalid side for O/U grading: %s", side)
        return SETTLEMENT_UNRESOLVED

    if final_stat is None or line is None:
        return SETTLEMENT_UNRESOLVED

    is_whole_line = line == int(line)

    if side == "OVER":
        if final_stat > line:
            return SETTLEMENT_WIN
        elif final_stat < line:
            return SETTLEMENT_LOSS
        else:
            # Equal — push only on whole lines
            return SETTLEMENT_PUSH if is_whole_line else SETTLEMENT_LOSS
    else:  # UNDER
        if final_stat < line:
            return SETTLEMENT_WIN
        elif final_stat > line:
            return SETTLEMENT_LOSS
        else:
            return SETTLEMENT_PUSH if is_whole_line else SETTLEMENT_LOSS


# ==================================================================
# YN Grading
# ==================================================================

def grade_yn() -> str:
    """YN grading is unsupported in automated mode.

    The settlement condition for YN markets (e.g., "YES = >= 1 K") is
    implicit in the market definition. Without an explicit external
    settlement feed, we cannot reliably determine the outcome.

    Always returns SETTLEMENT_UNRESOLVED.
    """
    return SETTLEMENT_UNRESOLVED


# ==================================================================
# CLV Calculation
# ==================================================================

def calculate_clv(
    bet_american: int,
    bet_line: float | None,
    closing_american: int | None,
    closing_line: float | None,
) -> dict:
    """Calculate CLV metrics for a recommendation.

    Returns dict with:
        clv_probability: bet_implied_prob - closing_implied_prob
            Positive = favorable (closing odds are better than bet odds,
            meaning closing implied probability is lower).
        clv_price_diff: closing_american - bet_american (signed, positive = line moved against us)
        clv_available: bool
        line_move_type: "same_line", "line_changed", "no_close", or None
    """
    from .market_analysis import american_to_decimal, american_to_probability

    result = {
        "clv_probability": None,
        "clv_price_diff": None,
        "clv_available": False,
        "line_move_type": None,
    }

    if closing_american is None:
        result["line_move_type"] = "no_close"
        return result

    if bet_line is not None and closing_line is not None and bet_line != closing_line:
        result["line_move_type"] = "line_changed"
        # Price CLV is not directly comparable when line changes
        return result

    result["line_move_type"] = "same_line"

    # Probability CLV: bet_implied_prob - closing_implied_prob
    # Positive = favorable (closing odds are better / lower implied prob)
    bet_prob = american_to_probability(bet_american)
    close_prob = american_to_probability(closing_american)
    result["clv_probability"] = round(bet_prob - close_prob, 6)
    result["clv_price_diff"] = closing_american - bet_american
    result["clv_available"] = True

    return result


# ==================================================================
# Performance Summaries
# ==================================================================

def performance_summary(
    recs: list[dict],
    *,
    include_unsettled: bool = False,
) -> dict:
    """Compute overall performance summary from settled recommendations.

    Parameters
    ----------
    recs : list[dict]
        Settled recommendation dicts (with settlement_status, profit_units, etc.).
    include_unsettled : bool
        If True, include unresolved in totals but exclude from win rate/ROI.

    Returns
    -------
    dict with summary metrics.
    """
    total = len(recs)
    wins = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_WIN)
    losses = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_LOSS)
    pushes = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_PUSH)
    voids = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_VOID)
    cancelled = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_CANCELLED)
    unresolved = sum(1 for r in recs if r.get("settlement_status") == SETTLEMENT_UNRESOLVED)

    # Settled = non-UNRESOLVED (used for win rate denominator)
    settled = wins + losses + pushes + voids + cancelled

    # Risked/won = only WIN and LOSS (pushes/voids/cancelled get 0 risk)
    risked = sum(r.get("risk_units", 0) for r in recs
                 if r.get("settlement_status") in (SETTLEMENT_WIN, SETTLEMENT_LOSS))
    won = sum(r.get("profit_units", 0) for r in recs
              if r.get("settlement_status") in (SETTLEMENT_WIN, SETTLEMENT_LOSS))

    win_rate = wins / settled if settled > 0 else 0.0
    roi = won / risked if risked > 0 else 0.0

    # Average odds
    odds_list = [r["offered_american_odds"] for r in recs if r.get("offered_american_odds")]
    avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0

    # Average EV (O/U only)
    ev_list = [r["ev_pct"] for r in recs if r.get("ev_pct") is not None]
    avg_ev = sum(ev_list) / len(ev_list) if ev_list else 0.0

    # Average YN advantage
    yn_adv_list = [r["yn_implied_prob_adv"] for r in recs if r.get("yn_implied_prob_adv") is not None]
    avg_yn_adv = sum(yn_adv_list) / len(yn_adv_list) if yn_adv_list else 0.0

    # Average CLV
    clv_list = [r.get("clv_probability") for r in recs if r.get("clv_probability") is not None]
    avg_clv = sum(clv_list) / len(clv_list) if clv_list else None

    return {
        "total_recommendations": total,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "voids": voids,
        "cancelled": cancelled,
        "unresolved": unresolved,
        "win_rate": round(win_rate, 4),
        "units_risked": round(risked, 4),
        "units_won": round(won, 4),
        "roi": round(roi, 4),
        "avg_offered_odds": round(avg_odds, 1),
        "avg_ev_pct": round(avg_ev, 4),
        "avg_yn_advantage": round(avg_yn_adv, 4),
        "avg_clv_probability": round(avg_clv, 6) if avg_clv is not None else None,
    }


def breakdown_by_field(recs: list[dict], field: str) -> dict[str, dict]:
    """Compute performance breakdown grouped by a field value."""
    groups: dict[str, list[dict]] = {}
    for r in recs:
        key = str(r.get(field, "unknown"))
        groups.setdefault(key, []).append(r)
    return {k: performance_summary(v) for k, v in groups.items()}
