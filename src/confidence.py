"""Recommendation confidence scoring.

Builds a confidence score using only measurable variables:
- comparison-book count (n_consensus_books)
- market quality (VALID_MARKET, NEEDS_REVIEW, INSUFFICIENT)
- EV magnitude (ev_pct)
- consensus agreement (how close to median)
- freshness (how recent the data is)
- mapping confidence (how well the player was identified)

Weights are configurable. No ML. No arbitrary magic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConfidenceWeights:
    """Configurable weights for each confidence component.

    Each weight is a multiplier applied to the normalized component
    score (0.0 to 1.0). The final confidence score is the weighted
    sum normalized to 0-100 range.
    """
    n_books: float = 2.0
    market_quality: float = 1.5
    ev_magnitude: float = 2.5
    freshness: float = 1.0
    mapping_confidence: float = 1.0

    def total_weight(self) -> float:
        """Sum of all weights."""
        return (self.n_books + self.market_quality +
                self.ev_magnitude + self.freshness + self.mapping_confidence)


# Default weights
DEFAULT_WEIGHTS = ConfidenceWeights()


# ── Component scorers ──────────────────────────────────────────────

def _score_n_books(n_books: int | None) -> float:
    """Score comparison-book count. 0.0 = 0 books, 1.0 = 8+ books."""
    if n_books is None:
        return 0.0
    return min(1.0, n_books / 8.0)


def _score_market_quality(quality: str | None) -> float:
    """Score market quality status."""
    if quality is None:
        return 0.0
    scores = {
        "VALID_MARKET": 1.0,
        "NEEDS_REVIEW": 0.5,
        "INSUFFICIENT_MARKET": 0.2,
        "EXCLUDED": 0.0,
    }
    return scores.get(quality, 0.0)


def _score_ev_magnitude(ev_pct: float | None, yn_adv: float | None) -> float:
    """Score EV magnitude. Higher EV = higher confidence.

    For O/U markets uses ev_pct (percentage points, e.g. 5.0 = 5%).
    For YN markets uses yn_implied_prob_adv (percentage points, e.g. 6.4 = 6.4pp).
    Both are normalized to 0-1 range (capped at 15 pp = 1.0).
    """
    value = ev_pct if ev_pct is not None else yn_adv
    if value is None:
        return 0.0
    # Normalize: 0 pp = 0.0, 15 pp+ = 1.0
    return min(1.0, max(0.0, value / 15.0))


def _score_freshness(freshness: str | None, data_source: str | None) -> float:
    """Score data freshness. LIVE = 1.0, CACHE = 0.7, STALE = 0.3, UNKNOWN = 0.5."""
    if freshness == "STALE":
        return 0.3
    if data_source == "LIVE API":
        return 1.0
    if data_source == "CACHE":
        return 0.7
    return 0.5


def _score_mapping_confidence(confidence: str | None) -> float:
    """Score mapping confidence. HIGH = 1.0, MEDIUM = 0.7, LOW = 0.3, NONE = 0.0."""
    if confidence is None:
        return 0.0
    scores = {
        "HIGH": 1.0,
        "MEDIUM": 0.7,
        "LOW": 0.3,
        "NONE": 0.0,
    }
    return scores.get(confidence.upper(), 0.5)


# ── Main scoring function ──────────────────────────────────────────

def compute_confidence(
    rec: dict,
    weights: ConfidenceWeights | None = None,
) -> dict:
    """Compute confidence score for a recommendation.

    Parameters
    ----------
    rec : dict
        Recommendation dict with measurable fields.
    weights : ConfidenceWeights or None
        Uses DEFAULT_WEIGHTS if None.

    Returns
    -------
    dict with:
        confidence_score: float 0-100
        components: dict of individual component scores
        grade: str (A/B/C/D/F)
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS

    components = {
        "n_books": _score_n_books(rec.get("n_consensus_books")),
        "market_quality": _score_market_quality(rec.get("market_quality")),
        "ev_magnitude": _score_ev_magnitude(
            rec.get("ev_pct"), rec.get("yn_implied_prob_adv")
        ),
        "freshness": _score_freshness(
            rec.get("freshness_status"), rec.get("data_source")
        ),
        "mapping_confidence": _score_mapping_confidence(
            rec.get("mapping_confidence")
        ),
    }

    # Weighted sum
    weighted_sum = (
        components["n_books"] * weights.n_books +
        components["market_quality"] * weights.market_quality +
        components["ev_magnitude"] * weights.ev_magnitude +
        components["freshness"] * weights.freshness +
        components["mapping_confidence"] * weights.mapping_confidence
    )

    # Normalize to 0-100
    total_weight = weights.total_weight()
    confidence_score = round((weighted_sum / total_weight) * 100, 2) if total_weight > 0 else 0.0

    # Grade
    if confidence_score >= 80:
        grade = "A"
    elif confidence_score >= 60:
        grade = "B"
    elif confidence_score >= 40:
        grade = "C"
    elif confidence_score >= 20:
        grade = "D"
    else:
        grade = "F"

    return {
        "confidence_score": confidence_score,
        "components": components,
        "grade": grade,
    }
