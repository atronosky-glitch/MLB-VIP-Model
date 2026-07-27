"""Model Score: transparent 1-10 recommendation quality score.

Computes a weighted composite score from measurable factors.
Not a win probability. Not an AI prediction. A quality ranking.

Score range: 1.0 to 9.8
Components:
  1. Value           — 35%  (EV for O/U, price advantage for YN)
  2. Market Quality   — 20%  (book count, completeness, consensus)
  3. Price Reliability — 15%  (distance from consensus, stale risk)
  4. Data Freshness   — 10%  (quote age, source quality)
  5. Confidence       — 10%  (corrected confidence score)
  6. Risk/Validation  — 10%  (penalties for risk factors)

Version: model_score_v1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Configuration ───────────────────────────────────────────────────

SCORE_VERSION = "model_score_v1"

# Component weights (must sum to 1.0)
@dataclass(frozen=True)
class ScoreWeights:
    value: float = 0.35
    market_quality: float = 0.20
    reliability: float = 0.15
    freshness: float = 0.10
    confidence: float = 0.10
    risk: float = 0.10

    def __post_init__(self) -> None:
        total = self.value + self.market_quality + self.reliability + self.freshness + self.confidence + self.risk
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Weights must sum to 1.0, got {total}")

DEFAULT_WEIGHTS = ScoreWeights()

# Score caps by market quality / status
SCORE_CAPS: dict[str, float] = {
    "VALID_MARKET": 9.8,
    "STRONG_EDGE": 9.8,
    "POSITIVE_EDGE": 9.8,
    "MARGINAL_EDGE": 9.8,
    "NO_EDGE": 9.8,
    "STRONG_PRICE_OUTLIER": 8.5,
    "PRICE_OUTLIER": 8.5,
    "MARGINAL_PRICE_OUTLIER": 8.5,
    "NEEDS_REVIEW": 7.5,
    "INSUFFICIENT_MARKET": 5.0,
    "EXCLUDED": 0.0,
    "IN_LINE_WITH_MARKET": 8.5,
    "WORSE_THAN_MARKET": 6.0,
}

# Normalization ranges (raw → 0..1)
VALUE_MAX_PP = 12.0       # 12 pp → 1.0
N_BOOKS_MAX = 8           # 8+ books → 1.0
RELIABILITY_MAX_PP = 10.0 # 10 pp from consensus → worst


# ── Component scorers ──────────────────────────────────────────────

def _score_value(rec: dict) -> float:
    """Value component: EV for O/U, price advantage for YN. Returns 0..1."""
    market_form = rec.get("market_form", "ou")
    if market_form == "yn":
        adv = rec.get("yn_implied_prob_adv") or rec.get("price_advantage_pct")
        if adv is None:
            return 0.0
        return min(1.0, max(0.0, adv / VALUE_MAX_PP))
    else:
        ev = rec.get("ev_pct")
        if ev is None:
            return 0.0
        return min(1.0, max(0.0, ev / VALUE_MAX_PP))


def _score_market_quality(rec: dict) -> float:
    """Market quality: book count + quality status. Returns 0..1."""
    n_books = rec.get("n_consensus_books") or 0
    book_score = min(1.0, n_books / N_BOOKS_MAX)

    quality = rec.get("market_quality", "")
    quality_scores = {
        "VALID_MARKET": 1.0,
        "NEEDS_REVIEW": 0.5,
        "INSUFFICIENT_MARKET": 0.2,
        "EXCLUDED": 0.0,
    }
    quality_score = quality_scores.get(quality, 0.5)

    # Both sides available bonus (O/U only)
    has_both = rec.get("market_form", "ou") == "ou" and rec.get("fair_prob") is not None
    side_bonus = 0.1 if has_both else 0.0

    return min(1.0, (book_score * 0.5 + quality_score * 0.4 + side_bonus))


def _score_reliability(rec: dict) -> float:
    """Price reliability: distance from consensus, outlier likelihood. Returns 0..1.

    Lower distance from consensus = higher reliability.
    """
    market_form = rec.get("market_form", "ou")

    if market_form == "yn":
        # YN: use decimal odds advantage as distance proxy
        adv = rec.get("yn_decimal_odds_adv")
        if adv is None:
            return 0.5
        distance = abs(adv)
        # Higher distance = less reliable (could be outlier)
        return max(0.0, min(1.0, 1.0 - (distance / 30.0)))
    else:
        # O/U: use EV magnitude as distance proxy
        ev = rec.get("ev_pct")
        if ev is None:
            return 0.5
        # Very high EV might be unreliable (outlier)
        if ev > 15.0:
            return 0.3
        if ev > 10.0:
            return 0.5
        if ev > 5.0:
            return 0.8
        return 1.0


def _score_freshness(rec: dict) -> float:
    """Data freshness: source quality + staleness. Returns 0..1."""
    freshness = rec.get("freshness_status", "")
    data_source = rec.get("data_source", "")

    if freshness == "STALE":
        return 0.2
    if data_source == "LIVE API":
        return 1.0
    if data_source == "CACHE":
        return 0.6
    return 0.5


def _score_confidence_component(rec: dict) -> float:
    """Confidence score component. Returns 0..1.

    Uses the corrected confidence score (0-100 scale).
    """
    conf = rec.get("confidence_score")
    if conf is None:
        return 0.5
    return min(1.0, max(0.0, conf / 100.0))


def _score_risk(rec: dict) -> float:
    """Risk/validation: penalties for risk factors. Returns 0..1 (1 = no risk).

    Penalties:
    - PRICE_OUTLIER status
    - NEEDS_REVIEW quality
    - Low book count
    - Extreme disagreement
    """
    score = 1.0

    # Status penalties
    status = rec.get("rec_status", "")
    comparison = rec.get("comparison_status", "")
    quality = rec.get("market_quality", "")

    if "OUTLIER" in comparison or "OUTLIER" in status:
        score -= 0.2
    if quality == "NEEDS_REVIEW":
        score -= 0.15
    if quality == "INSUFFICIENT_MARKET":
        score -= 0.3

    # Low book count penalty
    n_books = rec.get("n_consensus_books") or 0
    if n_books < 3:
        score -= 0.2
    elif n_books < 5:
        score -= 0.1

    return max(0.0, score)


# ── Main scoring function ──────────────────────────────────────────

@dataclass
class ScoreResult:
    """Complete score result with components and explanation."""
    score: float
    components: dict[str, float]
    component_values: dict[str, float]
    applied_cap: float | None
    cap_reason: str
    penalties: list[str]
    explanation: str
    version: str = SCORE_VERSION

    # ── Diagnostic fields ──────────────────────────────────────
    points_to_7: float = 0.0
    price_outlier_capped: bool = False
    true_ev_unavailable: bool = False
    one_sided_market: bool = False
    insufficient_books_failure: bool = False
    contributing_book_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_score": round(self.score, 1),
            "score_version": self.version,
            "components": self.components,
            "component_values": self.component_values,
            "applied_cap": self.applied_cap,
            "cap_reason": self.cap_reason,
            "penalties": self.penalties,
            "explanation": self.explanation,
            "points_to_7": self.points_to_7,
            "price_outlier_capped": self.price_outlier_capped,
            "true_ev_unavailable": self.true_ev_unavailable,
            "one_sided_market": self.one_sided_market,
            "insufficient_books_failure": self.insufficient_books_failure,
            "contributing_book_count": self.contributing_book_count,
        }


def compute_model_score(
    rec: dict,
    weights: ScoreWeights | None = None,
) -> ScoreResult:
    """Compute the 1-10 Model Score for a recommendation.

    Parameters
    ----------
    rec : dict
        Recommendation with measurable fields.
    weights : ScoreWeights or None
        Uses DEFAULT_WEIGHTS if None.

    Returns
    -------
    ScoreResult with score, components, explanation.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS

    # Compute raw component scores (0..1)
    raw_components = {
        "value": _score_value(rec),
        "market_quality": _score_market_quality(rec),
        "reliability": _score_reliability(rec),
        "freshness": _score_freshness(rec),
        "confidence": _score_confidence_component(rec),
        "risk": _score_risk(rec),
    }

    # Weighted contributions (0..weight)
    weighted = {
        "value": raw_components["value"] * weights.value,
        "market_quality": raw_components["market_quality"] * weights.market_quality,
        "reliability": raw_components["reliability"] * weights.reliability,
        "freshness": raw_components["freshness"] * weights.freshness,
        "confidence": raw_components["confidence"] * weights.confidence,
        "risk": raw_components["risk"] * weights.risk,
    }

    # Sum → raw score (0..1 scale)
    raw_sum = sum(weighted.values())

    # Scale to 1-10 range
    raw_score = 1.0 + raw_sum * 8.8  # 1.0 + (0..1)*8.8 = 1.0..9.8

    # Determine score cap
    quality = rec.get("market_quality", "VALID_MARKET")
    status = rec.get("rec_status", "")
    comparison = rec.get("comparison_status", "")

    cap_key = quality
    if comparison in SCORE_CAPS:
        cap_key = comparison
    elif status in SCORE_CAPS:
        cap_key = status

    cap = SCORE_CAPS.get(cap_key, 9.8)
    cap_reason = f"{cap_key} cap" if cap < 9.8 else ""

    # Apply cap
    final_score = min(raw_score, cap)
    applied_cap = cap if final_score < raw_score else None

    # ── Diagnostic fields ──────────────────────────────────────
    OFFICIAL_MIN = 7.0
    points_to_7 = max(0.0, round(OFFICIAL_MIN - final_score, 2))

    price_outlier_capped = applied_cap is not None and "OUTLIER" in cap_reason

    # True EV unavailable: YN market with no ev_pct, or O/U with no ev_pct
    market_form = rec.get("market_form", "ou")
    if market_form == "yn":
        true_ev_unavailable = rec.get("yn_implied_prob_adv") is None
    else:
        true_ev_unavailable = rec.get("ev_pct") is None

    # One-sided market: O/U with no fair_prob (no two-sided vig removal possible)
    one_sided_market = (market_form == "ou" and rec.get("fair_prob") is None)

    # Insufficient books caused the failure
    n_books_diag = rec.get("n_consensus_books") or 0
    insufficient_books_failure = n_books_diag < 4

    # Contributing book count
    contributing_book_count = n_books_diag

    # ── Penalties ──────────────────────────────────────────────
    penalties = []
    if raw_components["risk"] < 0.7:
        penalties.append(f"moderate risk (risk={raw_components['risk']:.2f})")
    if quality == "NEEDS_REVIEW":
        penalties.append("NEEDS_REVIEW market quality")
    if quality == "INSUFFICIENT_MARKET":
        penalties.append("INSUFFICIENT_MARKET")
    if applied_cap:
        penalties.append(f"score capped at {cap} for {cap_key}")
    if points_to_7 > 0:
        penalties.append(f"needs {points_to_7:.2f} more points to reach 7.0")

    # Component values for display (weighted contribution, not raw 0..1)
    max_contributions = {
        "value": weights.value,
        "market_quality": weights.market_quality,
        "reliability": weights.reliability,
        "freshness": weights.freshness,
        "confidence": weights.confidence,
        "risk": weights.risk,
    }
    display_components = {}
    for k in weighted:
        display_components[k] = round(weighted[k] * 8.8, 2)  # scale to 0..8.8

    # Build explanation
    explanation_lines = [f"Model Score: {final_score:.1f}"]
    for k in ["value", "market_quality", "reliability", "freshness", "confidence", "risk"]:
        val = display_components[k]
        mx = round(max_contributions[k] * 8.8, 2)
        explanation_lines.append(f"  {k.replace('_', ' ').title()}: {val:.1f} / {mx:.1f}")
    explanation_lines.append(f"  Contributing Books: {contributing_book_count}")
    if price_outlier_capped:
        explanation_lines.append("  Diagnostic: PRICE_OUTLIER capped the score")
    if true_ev_unavailable:
        explanation_lines.append("  Diagnostic: True EV was unavailable")
    if one_sided_market:
        explanation_lines.append("  Diagnostic: Market was one-sided (no vig removal)")
    if insufficient_books_failure:
        explanation_lines.append(f"  Diagnostic: Insufficient books ({contributing_book_count} < 4)")
    if points_to_7 > 0:
        explanation_lines.append(f"  Missing: {points_to_7:.2f} points to reach 7.0")
    if penalties:
        for p in penalties:
            explanation_lines.append(f"  Penalty: {p}")
    explanation_lines.append(f"  Final: {final_score:.1f}")

    return ScoreResult(
        score=round(final_score, 1),
        components=raw_components,
        component_values=display_components,
        applied_cap=applied_cap,
        cap_reason=cap_reason,
        penalties=penalties,
        explanation="\n".join(explanation_lines),
        points_to_7=points_to_7,
        price_outlier_capped=price_outlier_capped,
        true_ev_unavailable=true_ev_unavailable,
        one_sided_market=one_sided_market,
        insufficient_books_failure=insufficient_books_failure,
        contributing_book_count=contributing_book_count,
    )
