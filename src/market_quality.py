"""Market Quality Score — separate from Model Score.

Evaluates the structural quality of a market from a sportsbook coverage
perspective: sportsbook count, two-sided completeness, freshness,
mapping confidence, price consistency, and sportsbook diversity.

Range: 0.0 to 10.0
Version: market_quality_score_v1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MQS_VERSION = "market_quality_score_v1"


@dataclass(frozen=True)
class MarketQualityWeights:
    """Component weights for Market Quality Score."""
    book_count: float = 0.30
    two_sided: float = 0.20
    freshness: float = 0.15
    mapping_confidence: float = 0.10
    price_consistency: float = 0.15
    sportsbook_diversity: float = 0.10

    def __post_init__(self) -> None:
        total = (self.book_count + self.two_sided + self.freshness
                 + self.mapping_confidence + self.price_consistency
                 + self.sportsbook_diversity)
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Weights must sum to 1.0, got {total}")


DEFAULT_MQS_WEIGHTS = MarketQualityWeights()


@dataclass
class MarketQualityResult:
    """Complete market quality score result."""
    score: float
    components: dict[str, float]
    component_values: dict[str, float]
    explanation: str
    version: str = MQS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_quality_score": round(self.score, 2),
            "version": self.version,
            "components": self.components,
            "component_values": self.component_values,
            "explanation": self.explanation,
        }


def _score_book_count(n_books: int) -> float:
    """Sportsbook coverage: 0..1. 8+ books = 1.0."""
    return min(1.0, n_books / 8.0)


def _score_two_sided(has_both_sides: bool) -> float:
    """Two-sided completeness: 1.0 if both over/under available, 0.0 otherwise."""
    return 1.0 if has_both_sides else 0.0


def _score_freshness_mqs(freshness_status: str, data_source: str) -> float:
    """Data freshness for market quality: 0..1."""
    if freshness_status == "STALE":
        return 0.2
    if data_source == "LIVE API":
        return 1.0
    if data_source == "CACHE":
        return 0.6
    return 0.5


def _score_mapping_confidence_mqs(mapping_confidence: str) -> float:
    """Mapping confidence: 0..1."""
    scores = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.3, "NONE": 0.0, "FAILED": 0.0}
    return scores.get(mapping_confidence.upper(), 0.5)


def _score_price_consistency(std_dev: float | None, n_books: int) -> float:
    """Price consistency across sportsbooks: 0..1.

    Lower std_dev = more consistent = higher score.
    """
    if std_dev is None or n_books < 2:
        return 0.5
    # std_dev of 0 = perfect consistency, 5+ = high disagreement
    return max(0.0, min(1.0, 1.0 - (std_dev / 5.0)))


def _score_sportsbook_diversity(contributing_books: str, n_books: int) -> float:
    """Sportsbook diversity: 0..1.

    Based on number of unique contributing sportsbooks.
    """
    if n_books <= 0:
        return 0.0
    # Simple: more unique books = more diverse
    return min(1.0, n_books / 8.0)


def compute_market_quality_score(
    rec: dict,
    *,
    has_both_sides: bool = False,
    std_dev: float | None = None,
    weights: MarketQualityWeights | None = None,
) -> MarketQualityResult:
    """Compute Market Quality Score for a recommendation.

    Parameters
    ----------
    rec : dict
        Recommendation with relevant fields.
    has_both_sides : bool
        Whether both over and under sides are available.
    std_dev : float or None
        Standard deviation of prices across books.
    weights : MarketQualityWeights or None
        Uses DEFAULT_MQS_WEIGHTS if None.
    """
    if weights is None:
        weights = DEFAULT_MQS_WEIGHTS

    n_books = rec.get("n_consensus_books") or 0
    freshness = rec.get("freshness_status", "")
    data_source = rec.get("data_source", "")
    mapping_conf = rec.get("mapping_confidence", "")
    contributing_books = rec.get("contributing_books", "")

    components = {
        "book_count": _score_book_count(n_books),
        "two_sided": _score_two_sided(has_both_sides),
        "freshness": _score_freshness_mqs(freshness, data_source),
        "mapping_confidence": _score_mapping_confidence_mqs(mapping_conf),
        "price_consistency": _score_price_consistency(std_dev, n_books),
        "sportsbook_diversity": _score_sportsbook_diversity(contributing_books, n_books),
    }

    weighted = {
        "book_count": components["book_count"] * weights.book_count,
        "two_sided": components["two_sided"] * weights.two_sided,
        "freshness": components["freshness"] * weights.freshness,
        "mapping_confidence": components["mapping_confidence"] * weights.mapping_confidence,
        "price_consistency": components["price_consistency"] * weights.price_consistency,
        "sportsbook_diversity": components["sportsbook_diversity"] * weights.sportsbook_diversity,
    }

    raw_sum = sum(weighted.values())
    final_score = round(raw_sum * 10.0, 2)  # scale to 0..10

    max_contrib = {
        "book_count": weights.book_count * 10.0,
        "two_sided": weights.two_sided * 10.0,
        "freshness": weights.freshness * 10.0,
        "mapping_confidence": weights.mapping_confidence * 10.0,
        "price_consistency": weights.price_consistency * 10.0,
        "sportsbook_diversity": weights.sportsbook_diversity * 10.0,
    }

    display = {}
    for k in weighted:
        display[k] = round(weighted[k] * 10.0, 2)

    explanation_parts = [f"Market Quality Score: {final_score:.2f}/10"]
    for k in ["book_count", "two_sided", "freshness", "mapping_confidence",
              "price_consistency", "sportsbook_diversity"]:
        val = display[k]
        mx = round(max_contrib[k], 2)
        explanation_parts.append(f"  {k.replace('_', ' ').title()}: {val:.2f} / {mx:.2f}")

    return MarketQualityResult(
        score=final_score,
        components=components,
        component_values=display,
        explanation="\n".join(explanation_parts),
    )
