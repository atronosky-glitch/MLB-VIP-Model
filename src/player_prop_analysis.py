"""Analyse player prop over/under and yes/no markets (pitcher strikeouts).

For each exact O/U market group (same event, player, market type, line,
alt-line status), this module:

1. Collects approved Over and Under prices
2. Requires matching exact lines for pairing
3. Calculates consensus in implied-probability space
4. Removes vig using paired Over/Under prices
5. Calculates leave-one-sportsbook-out (LOO) fair probability
6. Computes EV for each book using LOO fair probability
7. Separates market quality from individual bet status

For YN market groups (same event, player, yes/no type):

1. Collects approved Yes-side prices
2. Computes LOO median implied probability as reference
3. Calculates price advantage metrics (not EV)
4. Classifies each book's price relative to the market

A market may be VALID_MARKET while every bet has NO_EDGE.
"""

from __future__ import annotations

import logging
from statistics import mean, median

from . import prop_config as cfg
from .validation_constants import APPROVED_STATUSES
from .market_analysis import american_to_decimal, american_to_probability

logger = logging.getLogger(__name__)


# ==================================================================
# Public entry point
# ==================================================================

def analyze_prop_group(
    group_key: str,
    over_prices: dict[str, dict],
    under_prices: dict[str, dict],
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    **kwargs,
) -> dict:
    """Analyse one market group (paired Over/Under at same line).

    Parameters
    ----------
    group_key : str
        The market group key (for identification in results).
    over_prices : dict[str, dict]
        ``{sportsbook: {price: int, decimal_odds: float, line: float}}``
        Only approved rows should be passed.
    under_prices : dict[str, dict]
        Same structure for the Under side.
    n_excluded_rows : int
        Number of excluded audit rows for this group.
    n_approved_rows : int
        Number of approved rows for this group.

    Returns
    -------
    dict with keys:
        group_key, line,
        market_quality: market_quality_status,
        quality_flags: list[str],
        n_paired_books, n_approved_rows, n_excluded_rows,
        consensus_over, consensus_under,
        nv_prob_over, nv_prob_under, vig_pct,
        books: list of per-book analysis dicts,
        best_ev: best EV entry or None,
        recommendation: "BET" or "NO_BET"
    """
    if not over_prices or not under_prices:
        return _empty_result(group_key, over_prices, under_prices,
                             n_excluded_rows, n_approved_rows)

    # Find common books (same sportsbook has both Over and Under)
    all_books = sorted(set(over_prices) & set(under_prices))
    line = _resolve_line(over_prices, under_prices)
    total_n = len(all_books)

    # Market quality — check early to avoid consensus crash on empty book list
    market_quality, quality_flags = _classify_market(total_n, all_books, over_prices, under_prices)
    if market_quality == cfg.MARKET_QUALITY_EXCLUDED:
        return _empty_result(group_key, over_prices, under_prices,
                             n_excluded_rows, n_approved_rows, line=line)

    # Consensus from all common books
    over_odds_list = [over_prices[b]["price"] for b in all_books]
    under_odds_list = [under_prices[b]["price"] for b in all_books]
    consensus_over = _consensus(over_odds_list)
    consensus_under = _consensus(under_odds_list)

    nv_prob_over, nv_prob_under = _remove_vig(consensus_over, consensus_under)
    vig_pct = _vig_percentage(consensus_over, consensus_under)

    # Per-book analysis with LOO fair probability and bet_status
    books = []
    for book in all_books:
        over_info = over_prices[book]
        under_info = under_prices[book]
        over_price = over_info["price"]
        under_price = under_info["price"]

        # Leave-one-out: fair probability from all OTHER books
        other_books = [b for b in all_books if b != book]
        if len(other_books) >= 2:
            other_over = [over_prices[b]["price"] for b in other_books]
            other_under = [under_prices[b]["price"] for b in other_books]
            loo_cons_over = _consensus(other_over)
            loo_cons_under = _consensus(other_under)
            loo_nv_over, _ = _remove_vig(loo_cons_over, loo_cons_under)
            fair_prob = loo_nv_over
        else:
            fair_prob = nv_prob_over

        over_dec = american_to_decimal(over_price)
        under_dec = american_to_decimal(under_price)
        ev_over = fair_prob * over_dec - 1.0
        ev_under = (1.0 - fair_prob) * under_dec - 1.0

        bet_status_over = _classify_bet(ev_over)
        bet_status_under = _classify_bet(ev_under)

        books.append({
            "sportsbook": book,
            "side": "OVER",
            "line": line,
            "american_odds": over_price,
            "decimal_odds": round(over_dec, 4),
            "fair_prob": round(fair_prob, 6),
            "ev_pct": round(ev_over * 100, 4),
            "bet_status": bet_status_over,
            "validation_status": over_info.get("validation_status", "VALID"),
            "included": True,
            "reason": "",
        })
        books.append({
            "sportsbook": book,
            "side": "UNDER",
            "line": line,
            "american_odds": under_price,
            "decimal_odds": round(under_dec, 4),
            "fair_prob": round(1.0 - fair_prob, 6),
            "ev_pct": round(ev_under * 100, 4),
            "bet_status": bet_status_under,
            "validation_status": under_info.get("validation_status", "VALID"),
            "included": True,
            "reason": "",
        })

    # If any extreme outlier, demote market quality
    if any(b["ev_pct"] > cfg.OUTLIER_EV_THRESHOLD * 100 or b["ev_pct"] < -cfg.OUTLIER_EV_THRESHOLD * 100
           for b in books if b["included"]):
        if market_quality == cfg.MARKET_QUALITY_VALID:
            market_quality = cfg.MARKET_QUALITY_NEEDS_REVIEW
            quality_flags.append("Extreme outlier EV detected")

    books.sort(key=lambda b: b["ev_pct"], reverse=True)

    # Best positive EV
    best_ev = None
    for b in books:
        if b["ev_pct"] > 0 and b["included"]:
            best_ev = {
                "sportsbook": b["sportsbook"],
                "side": b["side"],
                "ev_pct": b["ev_pct"],
                "american_odds": b["american_odds"],
                "bet_status": b["bet_status"],
            }
            break

    recommendation = "BET" if best_ev is not None else "NO_BET"

    return {
        "group_key": group_key,
        "line": line,
        "market_quality": market_quality,
        "quality_flags": quality_flags,
        "n_paired_books": total_n,
        "n_approved_rows": n_approved_rows or len(over_prices) + len(under_prices),
        "n_excluded_rows": n_excluded_rows,
        "consensus_over": consensus_over,
        "consensus_under": consensus_under,
        "nv_prob_over": round(nv_prob_over, 6),
        "nv_prob_under": round(nv_prob_under, 6),
        "vig_pct": round(vig_pct, 4),
        "books": books,
        "best_ev": best_ev,
        "recommendation": recommendation,
    }


# ==================================================================
# Helpers
# ==================================================================

def _empty_result(group_key, over_prices, under_prices,
                  n_excluded_rows=0, n_approved_rows=0, line=None):
    return {
        "group_key": group_key,
        "line": line if line is not None else _resolve_line(over_prices, under_prices),
        "market_quality": cfg.MARKET_QUALITY_EXCLUDED,
        "quality_flags": ["No paired Over/Under data"],
        "n_paired_books": 0,
        "n_approved_rows": n_approved_rows or len(over_prices) + len(under_prices),
        "n_excluded_rows": n_excluded_rows,
        "consensus_over": 0,
        "consensus_under": 0,
        "nv_prob_over": 0.0,
        "nv_prob_under": 0.0,
        "vig_pct": 0.0,
        "books": [],
        "best_ev": None,
        "recommendation": "NO_BET",
    }


def _classify_market(total_books, all_books, over_prices, under_prices):
    """Determine market quality based on paired book count and data cleanliness."""
    flags = []
    if total_books < 2:
        return cfg.MARKET_QUALITY_EXCLUDED, flags + ["Fewer than 2 paired books"]
    if total_books < cfg.MIN_COMPARISON_BOOKS + 1:
        flags.append(f"Only {total_books} paired books (need {cfg.MIN_COMPARISON_BOOKS + 1})")
        return cfg.MARKET_QUALITY_INSUFFICIENT, flags
    return cfg.MARKET_QUALITY_VALID, flags


def _classify_bet(ev: float) -> str:
    """Classify a single bet based on EV percentage."""
    if ev >= cfg.STRONG_EDGE_THRESHOLD:
        return cfg.BET_STATUS_STRONG
    if ev >= cfg.POSITIVE_EDGE_THRESHOLD:
        return cfg.BET_STATUS_POSITIVE
    if ev > 0:
        return cfg.BET_STATUS_MARGINAL
    return cfg.BET_STATUS_NO_EDGE


def _consensus(prices: list[int]) -> int:
    """Implied-probability-space consensus."""
    if not prices:
        return 0
    probs = [american_to_probability(p) for p in prices]
    avg_prob = mean(probs)
    if avg_prob <= 0:
        return 0
    dec = 1.0 / avg_prob
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _remove_vig(odds_a: int, odds_b: int) -> tuple[float, float]:
    """Remove vig from a two-outcome market."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    total = imp_a + imp_b
    if total <= 0:
        return (0.5, 0.5)
    return (imp_a / total, imp_b / total)


def _vig_percentage(odds_a: int, odds_b: int) -> float:
    """Return vig as a percentage."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    return (imp_a + imp_b - 1.0) * 100.0


def _resolve_line(over_prices, under_prices):
    """Get the line from the first available entry."""
    for d in (over_prices, under_prices):
        for entry in d.values():
            return entry.get("line")
    return None


# ==================================================================
# Yes/No (single-sided) analysis
# ==================================================================

def analyze_yn_group(
    group_key: str,
    yes_prices: dict[str, dict],
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    **kwargs,
) -> dict:
    """Analyse one Yes/No market group (single-sided price comparison).

    Reference method: median implied probability from LOO book set.
    No true EV is computed — only price advantage metrics.

    Parameters
    ----------
    group_key : str
        The market group key.
    yes_prices : dict[str, dict]
        ``{sportsbook: {price: int, decimal_odds: float}}``
        Only approved Yes-side rows should be passed.
    n_excluded_rows, n_approved_rows : int
        Row counts for reporting.

    Returns
    -------
    dict with keys:
        group_key, market_quality, quality_flags,
        n_books, n_approved_rows, n_excluded_rows,
        reference_book_count, reference_method,
        books: list of per-book comparison dicts,
        recommendation_eligible: bool
    """
    if not yes_prices:
        return _empty_yn_result(group_key, n_excluded_rows, n_approved_rows)

    all_books = sorted(yes_prices.keys())
    n_books = len(all_books)

    # Market quality check
    if n_books < cfg.YN_MIN_COMPARISON_BOOKS + 1:
        return {
            "group_key": group_key,
            "market_quality": cfg.MARKET_QUALITY_INSUFFICIENT,
            "quality_flags": [f"Only {n_books} books (need {cfg.YN_MIN_COMPARISON_BOOKS + 1})"],
            "n_books": n_books,
            "n_approved_rows": n_approved_rows or n_books,
            "n_excluded_rows": n_excluded_rows,
            "reference_book_count": 0,
            "reference_method": "LOO median implied probability",
            "books": [],
            "recommendation_eligible": False,
        }

    # Per-book analysis with LOO median reference
    books = []
    for book in all_books:
        offered_price = yes_prices[book]["price"]
        offered_dec = yes_prices[book]["decimal_odds"]
        offered_prob = american_to_probability(offered_price)

        # LOO: median implied probability from all OTHER books
        other_books = [b for b in all_books if b != book]
        other_probs = [american_to_probability(yes_prices[b]["price"]) for b in other_books]
        ref_prob = median(other_probs)
        ref_dec = 1.0 / ref_prob
        ref_american = _decimal_to_american_display(ref_dec)

        # Price advantage metrics
        price_advantage_pct = ref_prob - offered_prob  # positive = offered is better
        relative_payout_pct = (offered_dec / ref_dec - 1.0) * 100.0
        decimal_odds_adv = _compute_decimal_odds_advantage(offered_price, ref_american)

        comparison_status = _classify_yn_price(price_advantage_pct)

        books.append({
            "sportsbook": book,
            "side": "YES",
            "american_odds": offered_price,
            "decimal_odds": round(offered_dec, 4),
            "offered_implied_probability": round(offered_prob, 6),
            "market_reference_probability": round(ref_prob, 6),
            "market_reference_odds": ref_american,
            "price_advantage_pct": round(price_advantage_pct * 100, 4),
            "relative_payout_advantage_pct": round(relative_payout_pct, 4),
            "decimal_odds_advantage": decimal_odds_adv,
            "comparison_status": comparison_status,
            "recommendation_eligible": comparison_status in (
                cfg.YN_STATUS_STRONG_OUTLIER, cfg.YN_STATUS_OUTLIER,
            ),
            "validation_status": yes_prices[book].get("validation_status", "VALID"),
            "n_loo_books": len(other_books),
        })

    books.sort(key=lambda b: b["price_advantage_pct"], reverse=True)

    has_eligible = any(b["recommendation_eligible"] for b in books)

    return {
        "group_key": group_key,
        "market_quality": cfg.MARKET_QUALITY_VALID,
        "quality_flags": [],
        "n_books": n_books,
        "n_approved_rows": n_approved_rows or n_books,
        "n_excluded_rows": n_excluded_rows,
        "reference_book_count": n_books - 1,
        "reference_method": "LOO median implied probability",
        "books": books,
        "recommendation_eligible": has_eligible,
    }


def _empty_yn_result(
    group_key: str,
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    quality: str | None = None,
    flags: list[str] | None = None,
) -> dict:
    return {
        "group_key": group_key,
        "market_quality": quality or cfg.MARKET_QUALITY_EXCLUDED,
        "quality_flags": flags or ["No Yes-side data"],
        "n_books": 0,
        "n_approved_rows": n_approved_rows,
        "n_excluded_rows": n_excluded_rows,
        "reference_book_count": 0,
        "reference_method": "LOO median implied probability",
        "books": [],
        "recommendation_eligible": False,
    }


def _classify_yn_price(price_advantage_pct: float) -> str:
    """Classify a YN book's price relative to the LOO market reference.

    Parameters
    ----------
    price_advantage_pct : float
        Decimal difference: reference_prob - offered_prob.
        Positive means the offered price is better (lower implied probability).
    """
    if price_advantage_pct >= cfg.YN_STRONG_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_STRONG_OUTLIER
    if price_advantage_pct >= cfg.YN_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_OUTLIER
    if price_advantage_pct >= cfg.YN_MARGINAL_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_MARGINAL_OUTLIER
    if price_advantage_pct >= 0:
        return cfg.YN_STATUS_IN_LINE
    return cfg.YN_STATUS_WORSE


def _decimal_to_american_display(dec: float) -> int:
    """Convert decimal odds to American for display (no rounding of vig)."""
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _compute_decimal_odds_advantage(offered: int, reference: int) -> int:
    """Compute decimal-odds advantage (decimal difference × 100).

    Positive means offered odds are better (higher payout per dollar).
    This is NOT American-odds cents; it is a decimal-scale metric.
    """
    offered_dec = american_to_decimal(offered)
    ref_dec = american_to_decimal(reference)
    return round((offered_dec - ref_dec) * 100)
