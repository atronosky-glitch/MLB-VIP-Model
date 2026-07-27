"""Adaptive Learning and Model Calibration Engine.

Analyzes graded recommendations across all tiers (OFFICIAL, DISCOVERY, RESEARCH)
to produce calibration insights, learning recommendations, and champion/challenger
comparisons — without automatically changing any production settings.

All recommendations from this module are advisory only. Production thresholds,
scoring weights, and market eligibility require explicit human approval.
"""

from __future__ import annotations

import json
import math
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ADAPTIVE_LEARNING_VERSION = "adaptive_learning_v1"

# ── Score Buckets ──────────────────────────────────────────────────

SCORE_BUCKETS: list[tuple[str, float, float]] = [
    ("below_5.0", 0.0, 5.0),
    ("5.0-5.49", 5.0, 5.5),
    ("5.5-5.99", 5.5, 6.0),
    ("6.0-6.24", 6.0, 6.25),
    ("6.25-6.49", 6.25, 6.5),
    ("6.5-6.74", 6.5, 6.75),
    ("6.75-6.99", 6.75, 7.0),
    ("7.0-7.49", 7.0, 7.5),
    ("7.5+", 7.5, 100.0),
]

# ── High-Variance Markets (stricter sample-size rules) ─────────────

HIGH_VARIANCE_MARKETS = frozenset({
    "batter_home_runs", "batter_stolen_bases",
    "pitcher_strikeouts",
})

# ── Sample-Size Thresholds ────────────────────────────────────────

MIN_GRADED_OVERALL = 100
MIN_GRADED_PER_MARKET = 50
MIN_GRADED_PER_BUCKET = 30
MIN_BETTING_DAYS = 5
MIN_SPORTSBOOK_CONTRIBUTION = 0.20  # no single book > 20% of results

# ── Safety Statuses ────────────────────────────────────────────────

STATUS_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATUS_OBSERVE = "OBSERVE"
STATUS_CANDIDATE = "CANDIDATE"
STATUS_VALIDATED = "VALIDATED"
STATUS_REJECTED = "REJECTED"
STATUS_APPROVED = "APPROVED"


# ── Data Classes ───────────────────────────────────────────────────

@dataclass(frozen=True)
class SegmentKey:
    """Immutable key for a performance segment."""
    dimension: str
    value: str

    def __hash__(self) -> int:
        return hash((self.dimension, self.value))


@dataclass
class SegmentPerformance:
    """Performance metrics for a single segment."""
    dimension: str
    value: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    win_rate: float = 0.0
    avg_odds: float = 0.0
    implied_break_even: float = 0.0
    roi: float = 0.0
    units_won: float = 0.0
    avg_clv: float | None = None
    median_clv: float | None = None
    confidence_interval: tuple[float, float] | None = None
    max_drawdown: float = 0.0

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "total": self.total,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "win_rate": round(self.win_rate, 4),
            "avg_odds": round(self.avg_odds, 1),
            "implied_break_even": round(self.implied_break_even, 4),
            "roi": round(self.roi, 4),
            "units_won": round(self.units_won, 4),
            "avg_clv": round(self.avg_clv, 6) if self.avg_clv is not None else None,
            "median_clv": round(self.median_clv, 6) if self.median_clv is not None else None,
            "confidence_interval": (
                (round(self.confidence_interval[0], 4), round(self.confidence_interval[1], 4))
                if self.confidence_interval else None
            ),
            "max_drawdown": round(self.max_drawdown, 4),
        }


@dataclass
class ScoreBucketAnalysis:
    """Calibration analysis for a single score bucket."""
    bucket_label: str
    bucket_low: float
    bucket_high: float
    total: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    predicted_quality: float = 0.0
    actual_win_rate: float = 0.0
    implied_break_even: float = 0.0
    roi: float = 0.0
    avg_clv: float | None = None
    sample_sufficient: bool = False

    def to_dict(self) -> dict:
        return {
            "bucket_label": self.bucket_label,
            "bucket_low": self.bucket_low,
            "bucket_high": self.bucket_high,
            "total": self.total,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "predicted_quality": round(self.predicted_quality, 4),
            "actual_win_rate": round(self.actual_win_rate, 4),
            "implied_break_even": round(self.implied_break_even, 4),
            "roi": round(self.roi, 4),
            "avg_clv": round(self.avg_clv, 6) if self.avg_clv is not None else None,
            "sample_sufficient": self.sample_sufficient,
        }


@dataclass
class LearningRecommendation:
    """A suggested change to production configuration."""
    recommendation_id: str
    category: str
    proposed_change: str
    current_value: Any
    proposed_value: Any
    reason: str
    sample_size: int
    historical_roi_diff: float
    historical_clv_diff: float
    confidence_interval: tuple[float, float] | None
    expected_volume_effect: str
    overfitting_risk: str
    status: str = STATUS_INSUFFICIENT_DATA
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "category": self.category,
            "proposed_change": self.proposed_change,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "reason": self.reason,
            "sample_size": self.sample_size,
            "historical_roi_diff": round(self.historical_roi_diff, 4),
            "historical_clv_diff": round(self.historical_clv_diff, 6),
            "confidence_interval": (
                (round(self.confidence_interval[0], 4), round(self.confidence_interval[1], 4))
                if self.confidence_interval else None
            ),
            "expected_volume_effect": self.expected_volume_effect,
            "overfitting_risk": self.overfitting_risk,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class ChampionChallengerResult:
    """Comparison of Champion vs Challenger configurations."""
    champion_id: str
    challenger_id: str
    champion_picks: int = 0
    challenger_picks: int = 0
    champion_roi: float = 0.0
    challenger_roi: float = 0.0
    champion_units: float = 0.0
    challenger_units: float = 0.0
    champion_clv: float | None = None
    challenger_clv: float | None = None
    champion_drawdown: float = 0.0
    challenger_drawdown: float = 0.0
    winner: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "champion_id": self.champion_id,
            "challenger_id": self.challenger_id,
            "champion_picks": self.champion_picks,
            "challenger_picks": self.challenger_picks,
            "champion_roi": round(self.champion_roi, 4),
            "challenger_roi": round(self.challenger_roi, 4),
            "champion_units": round(self.champion_units, 4),
            "challenger_units": round(self.challenger_units, 4),
            "champion_clv": round(self.champion_clv, 6) if self.champion_clv is not None else None,
            "challenger_clv": round(self.challenger_clv, 6) if self.challenger_clv is not None else None,
            "champion_drawdown": round(self.champion_drawdown, 4),
            "challenger_drawdown": round(self.challenger_drawdown, 4),
            "winner": self.winner,
            "created_at": self.created_at,
        }


@dataclass
class VersionRecord:
    """Tracks a configuration version."""
    version_id: str
    scoring_version: str
    market_quality_version: str
    qualification_rules_version: str
    calibration_version: str
    activated_at: str
    deactivated_at: str = ""
    reason: str = ""
    experiment_id: str = ""
    approver: str = ""
    rollback_target: str = ""

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "scoring_version": self.scoring_version,
            "market_quality_version": self.market_quality_version,
            "qualification_rules_version": self.qualification_rules_version,
            "calibration_version": self.calibration_version,
            "activated_at": self.activated_at,
            "deactivated_at": self.deactivated_at,
            "reason": self.reason,
            "experiment_id": self.experiment_id,
            "approver": self.approver,
            "rollback_target": self.rollback_target,
        }


# ── Points-to-7 Calculation ───────────────────────────────────────

def compute_points_to_7(model_score: float | None) -> float:
    """Gap between model score and official minimum (7.0).

    This is the diagnostic field. Display rounding happens in the UI,
    never in storage.
    """
    if model_score is None:
        return 7.0
    return max(0.0, 7.0 - model_score)


# ── Probability Helpers ────────────────────────────────────────────

def american_to_implied_prob(american_odds: int) -> float:
    """Convert American odds to implied probability (0..1)."""
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def _compute_max_drawdown(profits: list[float]) -> float:
    """Compute maximum drawdown from a sequence of profit values."""
    if not profits:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in profits:
        cumulative += p
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)
    return max_dd


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    phat = wins / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    spread = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    lower = max(0.0, (centre - spread) / denom)
    upper = min(1.0, (centre + spread) / denom)
    return (round(lower, 4), round(upper, 4))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


# ── Part 1 + Part 2: Grade Analysis & Performance Segmentation ────

def _query_graded_recs(conn) -> list[dict]:
    """Query all graded recommendations with full snapshot fields."""
    rows = conn.execute("""
        SELECT
            hr.recommendation_id, hr.recommendation_tier,
            hr.event_id, hr.player_name, hr.market_type, hr.market_form,
            hr.side, hr.line, hr.sportsbook,
            hr.offered_american_odds, hr.offered_decimal_odds,
            hr.ev_pct, hr.yn_implied_prob_adv,
            hr.n_consensus_books, hr.market_quality,
            hr.freshness_status, hr.data_source,
            hr.model_score, hr.score_components, hr.score_version,
            hr.market_quality_score, hr.points_to_7,
            hr.price_outlier_capped, hr.true_ev_unavailable,
            hr.one_sided_market, hr.insufficient_books_failure,
            hr.contributing_book_count, hr.contributing_books,
            hr.scan_timestamp, hr.event_start_time,
            hr.qualification_rules_version,
            ms.settlement_status,
            bu.risk_units, bu.profit_units, bu.odds_at_settle,
            cp.clv_probability, cp.clv_price_diff, cp.clv_available
        FROM historical_recommendations hr
        LEFT JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        LEFT JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        WHERE ms.settlement_status IS NOT NULL
          AND ms.settlement_status != 'UNRESOLVED'
        ORDER BY hr.scan_timestamp ASC
    """).fetchall()
    return [dict(r) for r in rows]


def _compute_segment_performance(recs: list[dict]) -> SegmentPerformance:
    """Compute full performance metrics from a list of graded recs."""
    if not recs:
        return SegmentPerformance(dimension="", value="")

    wins = sum(1 for r in recs if r.get("settlement_status") == "WIN")
    losses = sum(1 for r in recs if r.get("settlement_status") == "LOSS")
    pushes = sum(1 for r in recs if r.get("settlement_status") == "PUSH")
    settled = wins + losses

    win_rate = wins / settled if settled > 0 else 0.0

    risked = sum(r.get("risk_units", 0) for r in recs
                 if r.get("settlement_status") in ("WIN", "LOSS"))
    won = sum(r.get("profit_units", 0) for r in recs
              if r.get("settlement_status") in ("WIN", "LOSS"))
    roi = won / risked if risked > 0 else 0.0

    odds_list = [r["offered_american_odds"] for r in recs if r.get("offered_american_odds")]
    avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0.0

    implied_be = american_to_implied_prob(int(avg_odds)) if avg_odds != 0 else 0.0

    clv_vals = [r["clv_probability"] for r in recs if r.get("clv_probability") is not None]
    avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None
    med_clv = _median(clv_vals) if clv_vals else None

    ci = _wilson_ci(wins, settled) if settled >= 5 else None

    # Max drawdown
    profits = sorted(
        [r.get("profit_units", 0) for r in recs if r.get("settlement_status") in ("WIN", "LOSS")],
        key=lambda x: 0,
    )
    max_dd = _compute_max_drawdown(profits)

    return SegmentPerformance(
        dimension="",
        value="",
        total=len(recs),
        wins=wins,
        losses=losses,
        pushes=pushes,
        win_rate=win_rate,
        avg_odds=avg_odds,
        implied_break_even=implied_be,
        roi=roi,
        units_won=round(won, 4),
        avg_clv=avg_clv,
        median_clv=med_clv,
        confidence_interval=ci,
        max_drawdown=max_dd,
    )


# Dimension extractors for segmentation
_DIMENSION_EXTRACTORS: dict[str, callable] = {
    "tier": lambda r: r.get("recommendation_tier", "RESEARCH_ONLY"),
    "market_family": lambda r: _market_family(r.get("market_type", "")),
    "exact_market": lambda r: r.get("market_type", ""),
    "score_range": lambda r: _score_range(r.get("model_score")),
    "mqs_range": lambda r: _mqs_range(r.get("market_quality_score")),
    "sportsbook_count": lambda r: _books_range(r.get("n_consensus_books")),
    "sportsbook": lambda r: r.get("sportsbook", ""),
    "side": lambda r: r.get("side", ""),
    "line_range": lambda r: _line_range(r.get("line")),
    "odds_range": lambda r: _odds_range(r.get("offered_american_odds")),
    "ev_range": lambda r: _ev_range(r.get("ev_pct"), r.get("yn_implied_prob_adv")),
    "price_adv_range": lambda r: _price_adv_range(r.get("yn_implied_prob_adv")),
    "freshness": lambda r: r.get("freshness_status", "UNKNOWN"),
    "one_sided": lambda r: "yes" if r.get("one_sided_market") else "no",
    "price_outlier": lambda r: "yes" if r.get("price_outlier_capped") else "no",
    "true_ev_unavailable": lambda r: "yes" if r.get("true_ev_unavailable") else "no",
    "day_of_week": lambda r: _day_of_week(r.get("scan_timestamp")),
    "time_before_pitch": lambda r: _time_bucket(r.get("scan_timestamp"), r.get("event_start_time")),
}


def _market_family(market_type: str) -> str:
    families = {
        "strikeouts": "pitcher", "outs": "pitcher", "hits_allowed": "pitcher",
        "walks_allowed": "pitcher", "earned_runs": "pitcher",
        "pitches_thrown": "pitcher", "pitching_win": "pitcher",
        "batter_hits": "batter", "total_bases": "batter",
        "hits_runs_rbi": "batter", "home_runs": "batter",
        "rbi": "batter", "runs_rbi": "batter", "singles": "batter",
        "doubles": "batter", "batter_walks": "batter",
        "stolen_bases": "batter", "triples": "batter",
        "batter_strikeouts": "batter", "first_home_run": "batter",
        "batter_runs": "batter",
    }
    return families.get(market_type, "other")


def _score_range(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score < 5.0:
        return "below_5.0"
    if score < 6.0:
        return "5.0-5.99"
    if score < 7.0:
        return "6.0-6.99"
    if score < 8.0:
        return "7.0-7.99"
    return "8.0+"


def _mqs_range(mqs: float | None) -> str:
    if mqs is None:
        return "unknown"
    if mqs < 5.0:
        return "below_5.0"
    if mqs < 7.0:
        return "5.0-6.99"
    if mqs < 9.0:
        return "7.0-8.99"
    return "9.0+"


def _books_range(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n <= 2:
        return "1-2"
    if n == 3:
        return "3"
    if n == 4:
        return "4"
    if n <= 6:
        return "5-6"
    return "7+"


def _line_range(line: float | None) -> str:
    if line is None:
        return "none"
    if line <= 0.5:
        return "0-0.5"
    if line <= 1.5:
        return "1.0-1.5"
    if line <= 3.5:
        return "2.0-3.5"
    if line <= 5.5:
        return "4.0-5.5"
    return "6.0+"


def _odds_range(odds: int | None) -> str:
    if odds is None:
        return "unknown"
    if odds < -200:
        return "below_-200"
    if odds < -150:
        return "-200_to_-150"
    if odds < -110:
        return "-150_to_-110"
    if odds <= -100:
        return "-110_to_-100"
    if odds <= 100:
        return "-100_to_+100"
    if odds <= 150:
        return "+100_to_+150"
    return "above_+150"


def _ev_range(ev_pct: float | None, yn_adv: float | None) -> str:
    if ev_pct is not None:
        val = ev_pct
    elif yn_adv is not None:
        return _price_adv_range(yn_adv)
    else:
        return "unknown"
    if val < 0:
        return "negative"
    if val < 2.0:
        return "0-2%"
    if val < 5.0:
        return "2-5%"
    if val < 10.0:
        return "5-10%"
    return "10%+"


def _price_adv_range(adv: float | None) -> str:
    if adv is None:
        return "n/a"
    if adv < 0:
        return "negative"
    if adv < 2.0:
        return "0-2pp"
    if adv < 5.0:
        return "2-5pp"
    if adv < 8.0:
        return "5-8pp"
    return "8pp+"


def _day_of_week(ts: str | None) -> str:
    if not ts:
        return "unknown"
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%A")
    except (ValueError, TypeError):
        return "unknown"


def _time_bucket(scan_ts: str | None, event_ts: str | None) -> str:
    if not scan_ts or not event_ts:
        return "unknown"
    try:
        from datetime import datetime as _dt
        s = _dt.fromisoformat(scan_ts.replace("Z", "+00:00"))
        e = _dt.fromisoformat(event_ts.replace("Z", "+00:00"))
        diff_hours = (e - s).total_seconds() / 3600.0
        if diff_hours < 0:
            return "post_start"
        if diff_hours < 2:
            return "0-2h"
        if diff_hours < 6:
            return "2-6h"
        if diff_hours < 12:
            return "6-12h"
        if diff_hours < 24:
            return "12-24h"
        return "24h+"
    except (ValueError, TypeError):
        return "unknown"


def compute_performance_segments(
    conn,
    recs: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Compute performance segmented by all dimensions.

    Parameters
    ----------
    conn : sqlite3.Connection
    recs : list[dict], optional
        Pre-fetched graded recs. If None, queries from DB.

    Returns
    -------
    dict mapping dimension name to list of segment performance dicts.
    """
    if recs is None:
        recs = _query_graded_recs(conn)

    result: dict[str, list[dict]] = {}

    for dim_name, extractor in _DIMENSION_EXTRACTORS.items():
        groups: dict[str, list[dict]] = {}
        for r in recs:
            try:
                val = extractor(r)
            except (KeyError, TypeError, ValueError):
                val = "unknown"
            groups.setdefault(str(val), []).append(r)

        segments = []
        for val, group in sorted(groups.items()):
            perf = _compute_segment_performance(group)
            perf.dimension = dim_name
            perf.value = val
            segments.append(perf.to_dict())
        result[dim_name] = segments

    return result


def compute_grade_summary(conn) -> dict:
    """Compute summary statistics by tier."""
    recs = _query_graded_recs(conn)
    tiers: dict[str, list[dict]] = {}
    for r in recs:
        tier = r.get("recommendation_tier", "RESEARCH_ONLY")
        tiers.setdefault(tier, []).append(r)

    summary = {}
    for tier, tier_recs in tiers.items():
        perf = _compute_segment_performance(tier_recs)
        summary[tier] = perf.to_dict()

    summary["total"] = _compute_segment_performance(recs).to_dict()
    summary["_meta"] = {
        "total_graded": len(recs),
        "tiers_found": list(tiers.keys()),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    return summary


# ── Part 3: Score Calibration ──────────────────────────────────────

def compute_score_calibration(conn) -> dict:
    """Calibration analysis for every score bucket.

    Returns score bucket analysis, score distribution stats,
    and compression/capping diagnostics.
    """
    recs = _query_graded_recs(conn)
    all_scores = [r["model_score"] for r in recs if r.get("model_score") is not None]

    buckets = []
    for label, low, high in SCORE_BUCKETS:
        bucket_recs = [
            r for r in recs
            if r.get("model_score") is not None and low <= r["model_score"] < high
        ]
        total = len(bucket_recs)
        wins = sum(1 for r in bucket_recs if r.get("settlement_status") == "WIN")
        losses = sum(1 for r in bucket_recs if r.get("settlement_status") == "LOSS")
        pushes = sum(1 for r in bucket_recs if r.get("settlement_status") == "PUSH")
        settled = wins + losses

        win_rate = wins / settled if settled > 0 else 0.0

        risked = sum(r.get("risk_units", 0) for r in bucket_recs
                     if r.get("settlement_status") in ("WIN", "LOSS"))
        won = sum(r.get("profit_units", 0) for r in bucket_recs
                  if r.get("settlement_status") in ("WIN", "LOSS"))
        roi = won / risked if risked > 0 else 0.0

        clv_vals = [r["clv_probability"] for r in bucket_recs if r.get("clv_probability") is not None]
        avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None

        # Predicted quality: midpoint score / 10
        predicted = ((low + min(high, 10.0)) / 2) / 10.0 if total > 0 else 0.0

        # Implied break-even from average odds
        odds_list = [r["offered_american_odds"] for r in bucket_recs if r.get("offered_american_odds")]
        avg_odds = sum(odds_list) / len(odds_list) if odds_list else 0
        implied_be = american_to_implied_prob(int(avg_odds)) if avg_odds != 0 else 0.0

        b = ScoreBucketAnalysis(
            bucket_label=label,
            bucket_low=low,
            bucket_high=high,
            total=total,
            wins=wins,
            losses=losses,
            pushes=pushes,
            predicted_quality=predicted,
            actual_win_rate=win_rate,
            implied_break_even=implied_be,
            roi=roi,
            avg_clv=avg_clv,
            sample_sufficient=total >= MIN_GRADED_PER_BUCKET,
        )
        buckets.append(b.to_dict())

    # Score distribution stats
    pct_above = {}
    for label, low, _high in SCORE_BUCKETS:
        above = sum(1 for s in all_scores if s >= low) if all_scores else 0
        pct_above[label] = round(above / len(all_scores) * 100, 1) if all_scores else 0.0

    # Capping/compression check
    capped_count = sum(1 for r in recs if r.get("price_outlier_capped"))
    scores_at_cap = sum(1 for r in recs if r.get("model_score") is not None and r["model_score"] >= 9.5)

    return {
        "buckets": buckets,
        "score_distribution": {
            "min": round(min(all_scores), 2) if all_scores else None,
            "max": round(max(all_scores), 2) if all_scores else None,
            "mean": round(statistics.mean(all_scores), 2) if all_scores else None,
            "median": round(statistics.median(all_scores), 2) if all_scores else None,
            "stdev": round(statistics.stdev(all_scores), 2) if len(all_scores) > 1 else None,
            "total_scored": len(all_scores),
        },
        "pct_above_threshold": pct_above,
        "capping_diagnostics": {
            "capped_count": capped_count,
            "at_cap_count": scores_at_cap,
            "capped_pct": round(capped_count / len(all_scores) * 100, 1) if all_scores else 0.0,
        },
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Parts 4 + 5: Learning Recommendations & Safety Rules ──────────

def _check_safety_rules(
    recs: list[dict],
    affected_market: str | None = None,
    min_clv: float | None = None,
) -> tuple[bool, list[str]]:
    """Check if sample size meets minimum safety requirements.

    Returns (passes, list of failure reasons).
    """
    failures = []

    if len(recs) < MIN_GRADED_OVERALL:
        failures.append(
            f"Overall sample {len(recs)} < {MIN_GRADED_OVERALL}"
        )

    if affected_market:
        market_recs = [r for r in recs if r.get("market_type") == affected_market]
        if len(market_recs) < MIN_GRADED_PER_MARKET:
            failures.append(
                f"Market '{affected_market}' sample {len(market_recs)} < {MIN_GRADED_PER_MARKET}"
            )

        if affected_market in HIGH_VARIANCE_MARKETS:
            if len(market_recs) < MIN_GRADED_PER_MARKET * 2:
                failures.append(
                    f"High-variance market '{affected_market}' needs "
                    f"{MIN_GRADED_PER_MARKET * 2}+ samples, has {len(market_recs)}"
                )

    # Check single-book dominance
    book_counts: dict[str, int] = {}
    for r in recs:
        bk = r.get("sportsbook", "")
        book_counts[bk] = book_counts.get(bk, 0) + 1
    total = len(recs)
    for bk, cnt in book_counts.items():
        if total > 0 and cnt / total > MIN_SPORTSBOOK_CONTRIBUTION:
            failures.append(
                f"Single book '{bk}' contributes {cnt}/{total} "
                f"({cnt/total*100:.0f}%) > {MIN_SPORTSBOOK_CONTRIBUTION*100:.0f}%"
            )
            break

    # Check betting days
    days = set()
    for r in recs:
        ts = r.get("scan_timestamp", "")
        if ts:
            days.add(ts[:10])
    if len(days) < MIN_BETTING_DAYS:
        failures.append(
            f"Betting days {len(days)} < {MIN_BETTING_DAYS}"
        )

    return (len(failures) == 0, failures)


def _assess_overfitting_risk(
    recs: list[dict],
    roi_improvement: float,
) -> str:
    """Assess overfitting risk level."""
    if len(recs) < 50:
        return "HIGH"
    if len(recs) < 100:
        return "MEDIUM" if abs(roi_improvement) > 0.10 else "LOW"
    if abs(roi_improvement) > 0.20:
        return "MEDIUM"
    return "LOW"


def _compute_clv_diff(
    group_a: list[dict],
    group_b: list[dict],
) -> float:
    """Compute average CLV difference between two groups."""
    clv_a = [r["clv_probability"] for r in group_a if r.get("clv_probability") is not None]
    clv_b = [r["clv_probability"] for r in group_b if r.get("clv_probability") is not None]
    avg_a = sum(clv_a) / len(clv_a) if clv_a else 0.0
    avg_b = sum(clv_b) / len(clv_b) if clv_b else 0.0
    return avg_b - avg_a


def _compute_roi_diff(group_a: list[dict], group_b: list[dict]) -> float:
    """Compute ROI difference between two groups."""
    perf_a = _compute_segment_performance(group_a)
    perf_b = _compute_segment_performance(group_b)
    return perf_b.roi - perf_a.roi


def generate_learning_recommendations(conn) -> list[dict]:
    """Generate learning recommendations without applying any changes.

    All recommendations start as INSUFFICIENT_DATA or OBSERVE until
    sufficient evidence exists.
    """
    recs = _query_graded_recs(conn)
    recommendations = []

    # --- Score weight analysis ---
    for component in ["value", "market_quality", "reliability", "freshness", "confidence", "risk"]:
        high_recs = [r for r in recs if _component_above_median(r, component)]
        low_recs = [r for r in recs if not _component_above_median(r, component) and _has_score_component(r, component)]
        if len(high_recs) >= MIN_GRADED_PER_BUCKET and len(low_recs) >= MIN_GRADED_PER_BUCKET:
            roi_diff = _compute_roi_diff(low_recs, high_recs)
            clv_diff = _compute_clv_diff(low_recs, high_recs)
            passes, failures = _check_safety_rules(recs)
            status = STATUS_OBSERVE if passes else STATUS_INSUFFICIENT_DATA

            rec = LearningRecommendation(
                recommendation_id=str(uuid.uuid4()),
                category="scoring_weights",
                proposed_change=f"Increase weight for '{component}' component",
                current_value="see ScoreWeights",
                proposed_value=f"+5% from current",
                reason=f"High-{component} recs have {roi_diff:+.2%} ROI advantage"
                       + (f"; safety failures: {'; '.join(failures[:2])}" if failures else ""),
                sample_size=min(len(high_recs), len(low_recs)),
                historical_roi_diff=roi_diff,
                historical_clv_diff=clv_diff,
                confidence_interval=_wilson_ci(
                    sum(1 for r in high_recs if r.get("settlement_status") == "WIN"),
                    sum(1 for r in high_recs if r.get("settlement_status") in ("WIN", "LOSS")),
                ) if high_recs else None,
                expected_volume_effect="neutral",
                overfitting_risk=_assess_overfitting_risk(recs, roi_diff),
                status=status,
            )
            recommendations.append(rec.to_dict())

    # --- Threshold analysis ---
    for threshold_name, threshold_field, threshold_values in [
        ("official_min_model_score", "model_score", [6.5, 7.0, 7.5]),
        ("official_min_ou_ev_pct", "ev_pct", [2.0, 3.0, 4.0, 5.0]),
    ]:
        for tv in threshold_values:
            above = [r for r in recs if _field_above(r, threshold_field, tv)]
            below = [r for r in recs if not _field_above(r, threshold_field, tv) and r.get(threshold_field) is not None]
            if len(above) >= MIN_GRADED_PER_BUCKET and len(below) >= MIN_GRADED_PER_BUCKET:
                roi_diff = _compute_roi_diff(below, above)
                passes, failures = _check_safety_rules(recs)
                status = STATUS_OBSERVE if passes else STATUS_INSUFFICIENT_DATA

                rec = LearningRecommendation(
                    recommendation_id=str(uuid.uuid4()),
                    category="thresholds",
                    proposed_change=f"Set {threshold_name} = {tv}",
                    current_value=f"current threshold",
                    proposed_value=tv,
                    reason=f"Recs above {tv} have {roi_diff:+.2%} ROI advantage over below"
                           + (f"; safety failures: {'; '.join(failures[:2])}" if failures else ""),
                    sample_size=min(len(above), len(below)),
                    historical_roi_diff=roi_diff,
                    historical_clv_diff=_compute_clv_diff(below, above),
                    confidence_interval=None,
                    expected_volume_effect=f"picks above {tv}: {len(above)}",
                    overfitting_risk=_assess_overfitting_risk(recs, roi_diff),
                    status=status,
                )
                recommendations.append(rec.to_dict())

    # --- Market eligibility analysis ---
    market_perf: dict[str, list[dict]] = {}
    for r in recs:
        mt = r.get("market_type", "")
        market_perf.setdefault(mt, []).append(r)

    for mt, m_recs in market_perf.items():
        perf = _compute_segment_performance(m_recs)
        if perf.roi < -0.10 and len(m_recs) >= MIN_GRADED_PER_MARKET:
            passes, failures = _check_safety_rules(m_recs, mt)
            status = STATUS_OBSERVE if passes else STATUS_INSUFFICIENT_DATA

            rec = LearningRecommendation(
                recommendation_id=str(uuid.uuid4()),
                category="market_eligibility",
                proposed_change=f"Restrict market '{mt}' (ROI: {perf.roi:+.2%})",
                current_value="eligible",
                proposed_value="restricted",
                reason=f"Market '{mt}' ROI {perf.roi:+.2%} over {len(m_recs)} graded recs"
                       + (f"; safety failures: {'; '.join(failures[:2])}" if failures else ""),
                sample_size=len(m_recs),
                historical_roi_diff=perf.roi,
                historical_clv_diff=perf.avg_clv if perf.avg_clv else 0.0,
                confidence_interval=perf.confidence_interval,
                expected_volume_effect=f"remove ~{len(m_recs)} picks",
                overfitting_risk=_assess_overfitting_risk(m_recs, perf.roi),
                status=status,
            )
            recommendations.append(rec.to_dict())

    # --- MQS weight analysis ---
    high_mqs = [r for r in recs if (r.get("market_quality_score") or 0) >= 7.0]
    low_mqs = [r for r in recs if (r.get("market_quality_score") or 0) < 7.0 and (r.get("market_quality_score") or 0) > 0]
    if len(high_mqs) >= MIN_GRADED_PER_BUCKET and len(low_mqs) >= MIN_GRADED_PER_BUCKET:
        roi_diff = _compute_roi_diff(low_mqs, high_mqs)
        passes, failures = _check_safety_rules(recs)
        status = STATUS_OBSERVE if passes else STATUS_INSUFFICIENT_DATA

        rec = LearningRecommendation(
            recommendation_id=str.uuid4(),
            category="mqs_weights",
            proposed_change="Increase MQS weight for market_quality component",
            current_value="see MarketQualityWeights",
            proposed_value="+5% from current",
            reason=f"High-MQS recs have {roi_diff:+.2%} ROI advantage"
                   + (f"; safety failures: {'; '.join(failures[:2])}" if failures else ""),
            sample_size=min(len(high_mqs), len(low_mqs)),
            historical_roi_diff=roi_diff,
            historical_clv_diff=_compute_clv_diff(low_mqs, high_mqs),
            confidence_interval=None,
            expected_volume_effect="neutral",
            overfitting_risk=_assess_overfitting_risk(recs, roi_diff),
            status=status,
        )
        recommendations.append(rec.to_dict())

    return recommendations


def _component_above_median(rec: dict, component: str) -> bool:
    """Check if a score component is above median."""
    sc = rec.get("score_components")
    if not sc:
        return False
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except (json.JSONDecodeError, TypeError):
            return False
    val = sc.get(component, 0)
    return val >= 0.5


def _has_score_component(rec: dict, component: str) -> bool:
    sc = rec.get("score_components")
    if not sc:
        return False
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except (json.JSONDecodeError, TypeError):
            return False
    return component in sc


def _field_above(rec: dict, field: str, threshold: float) -> bool:
    val = rec.get(field)
    return val is not None and val >= threshold


# ── Part 6: Training / Validation / Holdout Split ──────────────────

def chronological_split(
    recs: list[dict],
    train_pct: float = 0.60,
    val_pct: float = 0.20,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Chronological split: oldest → train, next → val, newest → holdout.

    Never mixes future results into earlier windows.
    """
    if not recs:
        return [], [], []

    sorted_recs = sorted(recs, key=lambda r: r.get("scan_timestamp", "") or "")

    n = len(sorted_recs)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))

    train = sorted_recs[:train_end]
    val = sorted_recs[train_end:val_end]
    holdout = sorted_recs[val_end:]

    return train, val, holdout


def evaluate_configuration(
    recs: list[dict],
    label: str = "current",
) -> dict:
    """Evaluate a set of recs as if scored by a configuration."""
    perf = _compute_segment_performance(recs)
    return {
        "label": label,
        "total": perf.total,
        "win_rate": perf.win_rate,
        "roi": perf.roi,
        "units_won": perf.units_won,
        "avg_clv": perf.avg_clv,
        "max_drawdown": perf.max_drawdown,
    }


def run_holdout_validation(conn, challenger_recs: list[dict] | None = None) -> dict:
    """Run chronological train/val/holdout validation.

    Compares current production (Champion) scoring against challenger
    if provided. Always returns holdout comparison.
    """
    all_graded = _query_graded_recs(conn)
    train, val, holdout = chronological_split(all_graded)

    champion_holdout = evaluate_configuration(holdout, "champion_holdout")
    champion_train = evaluate_configuration(train, "champion_train")

    result = {
        "train_size": len(train),
        "val_size": len(val),
        "holdout_size": len(holdout),
        "champion_train": champion_train,
        "champion_holdout": champion_holdout,
    }

    if challenger_recs:
        c_train, c_val, c_holdout = chronological_split(challenger_recs)
        challenger_holdout = evaluate_configuration(c_holdout, "challenger_holdout")
        challenger_train = evaluate_configuration(c_train, "challenger_train")

        result["challenger_train"] = challenger_train
        result["challenger_holdout"] = challenger_holdout
        result["improvement"] = {
            "roi_diff": challenger_holdout["roi"] - champion_holdout["roi"],
            "clv_diff": (
                (challenger_holdout.get("avg_clv") or 0) -
                (champion_holdout.get("avg_clv") or 0)
            ),
        }
        # A challenger is VALIDATED only if it improves on holdout
        result["validated"] = (
            result["improvement"]["roi_diff"] > 0
            and challenger_holdout["total"] >= MIN_GRADED_PER_BUCKET
        )
    else:
        result["challenger_holdout"] = None
        result["validated"] = False

    return result


# ── Part 7: Champion / Challenger ──────────────────────────────────

def create_challenger_experiment(
    conn,
    challenger_id: str,
    challenger_config: dict,
    challenger_recs: list[dict],
) -> dict:
    """Create a challenger experiment for shadow comparison."""
    experiment_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()

    # Evaluate champion (all graded recs)
    all_graded = _query_graded_recs(conn)
    champion_perf = _compute_segment_performance(all_graded)

    # Evaluate challenger
    challenger_perf = _compute_segment_performance(challenger_recs)

    result = ChampionChallengerResult(
        champion_id="champion_v1",
        challenger_id=challenger_id,
        champion_picks=champion_perf.total,
        challenger_picks=challenger_perf.total,
        champion_roi=champion_perf.roi,
        challenger_roi=challenger_perf.roi,
        champion_units=champion_perf.units_won,
        challenger_units=challenger_perf.units_won,
        champion_clv=champion_perf.avg_clv,
        challenger_clv=challenger_perf.avg_clv,
        champion_drawdown=champion_perf.max_drawdown,
        challenger_drawdown=challenger_perf.max_drawdown,
    )

    # Store experiment
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY,
            challenger_id TEXT NOT NULL,
            champion_config TEXT,
            challenger_config TEXT,
            created_at TEXT NOT NULL,
            training_window TEXT,
            validation_window TEXT,
            holdout_window TEXT,
            champion_metrics TEXT,
            challenger_metrics TEXT,
            conclusion TEXT DEFAULT 'pending',
            approved INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        INSERT INTO experiments
        (experiment_id, challenger_id, champion_config, challenger_config,
         created_at, champion_metrics, challenger_metrics)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        experiment_id,
        challenger_id,
        json.dumps({"id": "champion_v1"}),
        json.dumps(challenger_config),
        created_at,
        json.dumps(result.to_dict()),
        json.dumps(challenger_perf.to_dict()),
    ))
    conn.commit()

    return {
        "experiment_id": experiment_id,
        "comparison": result.to_dict(),
        "created_at": created_at,
    }


def compare_champion_challenger(conn, experiment_id: str) -> dict:
    """Compare champion vs challenger for an experiment."""
    row = conn.execute(
        "SELECT * FROM experiments WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if not row:
        return {"error": "Experiment not found"}

    return {
        "experiment_id": experiment_id,
        "champion_metrics": json.loads(row["champion_metrics"]) if row["champion_metrics"] else {},
        "challenger_metrics": json.loads(row["challenger_metrics"]) if row["challenger_metrics"] else {},
        "conclusion": row["conclusion"],
        "approved": bool(row["approved"]),
    }


def approve_challenger(conn, experiment_id: str, approver: str = "manual") -> dict:
    """Approve a challenger experiment (requires holdout validation)."""
    row = conn.execute(
        "SELECT * FROM experiments WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if not row:
        return {"error": "Experiment not found"}

    champion_m = json.loads(row["champion_metrics"]) if row["champion_metrics"] else {}
    challenger_m = json.loads(row["challenger_metrics"]) if row["challenger_metrics"] else {}

    # Safety: must have sufficient holdout sample
    c_picks = challenger_m.get("total", 0)
    if c_picks < MIN_GRADED_PER_BUCKET:
        return {"error": f"Insufficient challenger sample: {c_picks} < {MIN_GRADED_PER_BUCKET}"}

    # Must improve ROI on holdout
    ch_roi = champion_m.get("champion_roi", champion_m.get("roi", 0))
    c_roi = challenger_m.get("roi", challenger_m.get("challenger_roi", 0))
    if c_roi <= ch_roi:
        return {"error": "Challenger does not improve ROI over Champion"}

    # Must not materially increase drawdown
    ch_dd = champion_m.get("champion_drawdown", champion_m.get("max_drawdown", 0))
    c_dd = challenger_m.get("max_drawdown", challenger_m.get("challenger_drawdown", 0))
    if c_dd > ch_dd * 1.5:
        return {"error": "Challender drawdown too high"}

    conn.execute("""
        UPDATE experiments
        SET conclusion = 'VALIDATED', approved = 1
        WHERE experiment_id = ?
    """, (experiment_id,))
    conn.commit()

    return {"status": "approved", "experiment_id": experiment_id, "approver": approver}


# ── Part 8: Versioning & Rollback ──────────────────────────────────

def save_version(
    conn,
    scoring_version: str,
    market_quality_version: str,
    qualification_rules_version: str,
    calibration_version: str,
    reason: str = "",
    experiment_id: str = "",
    approver: str = "",
) -> dict:
    """Save a configuration version record."""
    version_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS config_versions (
            version_id TEXT PRIMARY KEY,
            scoring_version TEXT NOT NULL,
            market_quality_version TEXT NOT NULL,
            qualification_rules_version TEXT NOT NULL,
            calibration_version TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            deactivated_at TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            experiment_id TEXT DEFAULT '',
            approver TEXT DEFAULT '',
            rollback_target TEXT DEFAULT ''
        )
    """)

    # Deactivate previous version
    conn.execute("""
        UPDATE config_versions SET deactivated_at = ?
        WHERE deactivated_at = ''
    """, (now,))

    # Get previous version as rollback target
    prev = conn.execute(
        "SELECT version_id FROM config_versions WHERE version_id != ? ORDER BY activated_at DESC LIMIT 1",
        (version_id,),
    ).fetchone()
    rollback_target = prev["version_id"] if prev else ""

    conn.execute("""
        INSERT INTO config_versions
        (version_id, scoring_version, market_quality_version,
         qualification_rules_version, calibration_version,
         activated_at, reason, experiment_id, approver, rollback_target)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        version_id, scoring_version, market_quality_version,
        qualification_rules_version, calibration_version,
        now, reason, experiment_id, approver, rollback_target,
    ))
    conn.commit()

    return VersionRecord(
        version_id=version_id,
        scoring_version=scoring_version,
        market_quality_version=market_quality_version,
        qualification_rules_version=qualification_rules_version,
        calibration_version=calibration_version,
        activated_at=now,
        reason=reason,
        experiment_id=experiment_id,
        approver=approver,
        rollback_target=rollback_target,
    ).to_dict()


def get_active_version(conn) -> dict | None:
    """Get the currently active configuration version."""
    row = conn.execute(
        "SELECT * FROM config_versions WHERE deactivated_at = '' ORDER BY activated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return dict(row)


def rollback_version(conn, target_version_id: str) -> dict:
    """Rollback to a previous configuration version."""
    target = conn.execute(
        "SELECT * FROM config_versions WHERE version_id = ?",
        (target_version_id,),
    ).fetchone()
    if not target:
        return {"error": "Target version not found"}

    now = datetime.now(timezone.utc).isoformat()

    # Deactivate current
    conn.execute("UPDATE config_versions SET deactivated_at = ? WHERE deactivated_at = ''", (now,))

    # Reactivate target (create a new version record referencing the old one)
    new_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO config_versions
        (version_id, scoring_version, market_quality_version,
         qualification_rules_version, calibration_version,
         activated_at, reason, experiment_id, approver, rollback_target)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        new_id,
        target["scoring_version"],
        target["market_quality_version"],
        target["qualification_rules_version"],
        target["calibration_version"],
        now,
        f"Rollback to {target_version_id}",
        "",
        "manual",
        target_version_id,
    ))
    conn.commit()

    return {"status": "rolled_back", "new_version_id": new_id, "target": target_version_id}


# ── Part 10: Auto-Change Prevention ───────────────────────────────

def can_auto_change(conn) -> tuple[bool, str]:
    """Check if system has enough data to auto-change production settings.

    Returns (allowed, reason).
    """
    recs = _query_graded_recs(conn)
    n_graded = len(recs)

    if n_graded < MIN_GRADED_OVERALL:
        return False, f"INSUFFICIENT_DATA: {n_graded}/{MIN_GRADED_OVERALL} graded recs"

    # Check for minimum betting days
    days = set()
    for r in recs:
        ts = r.get("scan_timestamp", "")
        if ts:
            days.add(ts[:10])
    if len(days) < MIN_BETTING_DAYS:
        return False, f"INSUFFICIENT_DATA: {len(days)}/{MIN_BETTING_DAYS} betting days"

    # Check no single book dominates
    book_counts: dict[str, int] = {}
    for r in recs:
        bk = r.get("sportsbook", "")
        book_counts[bk] = book_counts.get(bk, 0) + 1
    for bk, cnt in book_counts.items():
        if n_graded > 0 and cnt / n_graded > MIN_SPORTSBOOK_CONTRIBUTION:
            return (
                False,
                f"BIASED_DATA: book '{bk}' contributes {cnt/n_graded*100:.0f}%",
            )

    return True, "sufficient_data"


def is_pending(rec: dict) -> bool:
    """Check if a recommendation is pending (should not be learned from)."""
    status = rec.get("settlement_status", "")
    return status in ("", "UNRESOLVED", "PENDING", None)


def is_stale(rec: dict) -> bool:
    """Check if a recommendation is stale."""
    return (rec.get("freshness_status") or "").upper() == "STALE"


def is_improperly_mapped(rec: dict) -> bool:
    """Check if a recommendation has mapping issues."""
    quality = (rec.get("market_quality") or "").upper()
    return quality in ("EXCLUDED", "INSUFFICIENT_MARKET")


def _is_eligible_for_learning(rec: dict) -> bool:
    """Check if a rec is eligible for adaptive learning.

    Excludes: pending, improperly mapped, stale, live-game starts.
    """
    if is_pending(rec):
        return False
    if is_stale(rec):
        return False
    if is_improperly_mapped(rec):
        return False

    event_status = (rec.get("event_status") or "").lower()
    if event_status in ("live", "inprogress", "completed", "final"):
        return False

    return True
