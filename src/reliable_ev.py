"""Safeguards for treating calculated O/U EV as reliable.

Calculated EV is only a candidate edge until its inputs pass validation. This
module deliberately does not estimate a new probability or change thresholds;
it validates provenance and arithmetic and provides descriptive, advisory
metrics for graded recommendations.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


RELIABLE_EV_VERSION = "reliable_ev_v1"
DEFAULT_MIN_BOOKS = 4
DEFAULT_EV_TOLERANCE_PP = 0.15
DEFAULT_MAX_EV_PCT = 20.0
DEFAULT_MIN_DECIMAL_ODDS = 1.05
DEFAULT_MAX_DECIMAL_ODDS = 10.0
_EXCLUDED_QUALITY = {
    "EXCLUDED", "INSUFFICIENT_MARKET", "NEEDS_REVIEW",
    "PRICE_OUTLIER", "STRONG_PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
}


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _implied_ev_pct(fair_prob: float, decimal_odds: float) -> float:
    return (fair_prob * decimal_odds - 1.0) * 100.0


def assess_reliable_ev(
    rec: dict,
    *,
    min_books: int = DEFAULT_MIN_BOOKS,
    tolerance_pp: float = DEFAULT_EV_TOLERANCE_PP,
    max_ev_pct: float = DEFAULT_MAX_EV_PCT,
    min_decimal_odds: float = DEFAULT_MIN_DECIMAL_ODDS,
    max_decimal_odds: float = DEFAULT_MAX_DECIMAL_ODDS,
) -> dict:
    """Validate the inputs and arithmetic behind an O/U EV estimate.

    The result is safe to persist alongside a recommendation. A failed check
    means the EV must not qualify as official, not that the market is useless
    for research.
    """
    reasons: list[str] = []
    fair_prob = _number(rec.get("fair_prob"))
    decimal_odds = _number(rec.get("offered_decimal_odds"))
    ev_pct = _number(rec.get("ev_pct"))
    books = _number(rec.get("n_consensus_books"))

    if (rec.get("market_form") or "ou").lower() != "ou":
        reasons.append("not_an_ou_market")
    if fair_prob is None or not 0.0 < fair_prob < 1.0:
        reasons.append("invalid_fair_probability")
    if decimal_odds is None or not min_decimal_odds <= decimal_odds <= max_decimal_odds:
        reasons.append("invalid_offered_odds")
    if ev_pct is None:
        reasons.append("missing_ev")
    elif abs(ev_pct) > max_ev_pct:
        reasons.append("extreme_ev_outlier")
    if books is None or books < min_books:
        reasons.append("insufficient_independent_books")
    if (rec.get("freshness_status") or "").upper() == "STALE":
        reasons.append("stale_quote")
    if (rec.get("market_quality") or "").upper() in _EXCLUDED_QUALITY:
        reasons.append("market_quality_excluded")
    if rec.get("true_ev_unavailable"):
        reasons.append("true_ev_unavailable")
    if rec.get("one_sided_market"):
        reasons.append("one_sided_market")

    calculated_ev = None
    if fair_prob is not None and decimal_odds is not None and decimal_odds > 1.0:
        calculated_ev = _implied_ev_pct(fair_prob, decimal_odds)
        if ev_pct is not None and abs(calculated_ev - ev_pct) > tolerance_pp:
            reasons.append("ev_arithmetic_mismatch")

    reliable = not reasons
    return {
        "reliable_ev": reliable,
        "reliable_ev_status": "RELIABLE" if reliable else "UNRELIABLE",
        "reliable_ev_reasons": reasons,
        "reliable_ev_version": RELIABLE_EV_VERSION,
        "reliable_ev_calculated_pct": round(calculated_ev, 4) if calculated_ev is not None else None,
        "reliable_ev_min_books": min_books,
    }


def summarize_realized_ev(records: Iterable[dict], *, min_sample: int = 30) -> dict:
    """Compare predicted O/U EV with realized returns, without claiming proof.

    Records must contain ``ev_pct`` and ``profit_units``. This is intentionally
    descriptive and marks small samples as insufficient rather than promoting
    them to a model conclusion.
    """
    usable = [
        r for r in records
        if _number(r.get("ev_pct")) is not None
        and _number(r.get("profit_units")) is not None
    ]
    n = len(usable)
    total_risk = sum(_number(r.get("risk_units")) or 1.0 for r in usable)
    profit = sum(_number(r.get("profit_units")) or 0.0 for r in usable)
    avg_ev = sum(_number(r["ev_pct"]) or 0.0 for r in usable) / n if n else None
    roi = profit / total_risk if total_risk else None
    return {
        "sample_size": n,
        "average_predicted_ev_pct": round(avg_ev, 4) if avg_ev is not None else None,
        "realized_roi": round(roi, 6) if roi is not None else None,
        "sufficient_sample": n >= min_sample,
        "status": "SUFFICIENT" if n >= min_sample else "INSUFFICIENT_DATA",
    }


def summarize_realized_ev_segments(
    records: Iterable[dict],
    *,
    dimensions: tuple[str, ...] = ("market_type", "sportsbook", "ev_bucket"),
    min_sample: int = 30,
) -> dict:
    """Return sample-gated realized-EV summaries by explicit dimensions.

    Segments are descriptive until their own sample gate passes. This prevents
    a profitable pooled result from hiding a losing market or sportsbook.
    Missing dimensions are grouped under ``UNKNOWN`` rather than discarded.
    """
    rows = list(records)
    segments: dict[tuple[str, ...], list[dict]] = {}
    for record in rows:
        key = tuple(str(record.get(d) or "UNKNOWN") for d in dimensions)
        segments.setdefault(key, []).append(record)
    result = []
    for key, segment in sorted(segments.items()):
        metrics = summarize_realized_ev(segment, min_sample=min_sample)
        metrics.update({dimension: value for dimension, value in zip(dimensions, key)})
        result.append(metrics)
    return {
        "dimensions": list(dimensions),
        "segment_count": len(result),
        "segments": result,
        "sample_gate": min_sample,
    }
