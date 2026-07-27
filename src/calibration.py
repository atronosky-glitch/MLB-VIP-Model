"""Recommendation calibration analyzer.

Analyzes historical performance by EV bucket to determine optimal
thresholds. Does NOT automatically change thresholds — only recommends.

The output is a list of calibration recommendations with supporting
evidence from the data.
"""

from __future__ import annotations

import sqlite3

from src.analytics import roi_by_ev_bucket
from src.grading import EV_BUCKETS


def analyze_calibration(
    conn: sqlite3.Connection,
    buckets: list[tuple] | None = None,
) -> dict:
    """Analyze recommendation calibration by EV bucket.

    Returns a dict with:
        buckets: list of per-bucket performance metrics
        recommendations: list of suggested threshold adjustments
        evidence: raw data supporting the recommendations

    Each bucket entry contains:
        bucket, count, wins, losses, units_risked, units_won,
        roi, win_rate, avg_ev
    """
    if buckets is None:
        buckets = EV_BUCKETS

    bucket_results = roi_by_ev_bucket(conn, buckets)

    recommendations = []
    evidence = []

    for b in bucket_results:
        if b["count"] == 0:
            continue

        evidence.append({
            "bucket": b["bucket"],
            "count": b["count"],
            "roi": b["roi"],
            "win_rate": b["win_rate"],
            "avg_ev": b["avg_ev"],
        })

        # A bucket is "profitable" if ROI > 0 and has at least 5 bets
        if b["count"] >= 5 and b["roi"] > 0:
            # Check if the next-lower bucket is unprofitable
            # This suggests the threshold could be lowered
            bucket_idx = next(
                (i for i, (label, _) in enumerate(buckets) if label == b["bucket"]),
                None,
            )
            if bucket_idx is not None and bucket_idx > 0:
                prev_label = buckets[bucket_idx - 1][0]
                prev_b = next((x for x in bucket_results if x["bucket"] == prev_label), None)
                if prev_b and prev_b["count"] >= 5 and prev_b["roi"] <= 0:
                    recommendations.append({
                        "type": "consider_lowering_threshold",
                        "current_bucket": b["bucket"],
                        "current_roi": b["roi"],
                        "adjacent_unprofitable": prev_label,
                        "adjacent_roi": prev_b["roi"],
                        "reason": (
                            f"Bucket '{b['bucket']}' is profitable (ROI={b['roi']:.2%}, "
                            f"n={b['count']}), but adjacent bucket '{prev_label}' is not "
                            f"(ROI={prev_b['roi']:.2%}, n={prev_b['count']}). "
                            f"Consider lowering threshold."
                        ),
                    })

        # A bucket is "unprofitable" if ROI < 0 and has at least 5 bets
        if b["count"] >= 5 and b["roi"] < 0:
            bucket_idx = next(
                (i for i, (label, _) in enumerate(buckets) if label == b["bucket"]),
                None,
            )
            if bucket_idx is not None and bucket_idx < len(buckets) - 1:
                next_label = buckets[bucket_idx + 1][0]
                next_b = next((x for x in bucket_results if x["bucket"] == next_label), None)
                if next_b and next_b["count"] >= 5 and next_b["roi"] > 0:
                    recommendations.append({
                        "type": "consider_raising_threshold",
                        "current_bucket": b["bucket"],
                        "current_roi": b["roi"],
                        "adjacent_profitable": next_label,
                        "adjacent_roi": next_b["roi"],
                        "reason": (
                            f"Bucket '{b['bucket']}' is unprofitable (ROI={b['roi']:.2%}, "
                            f"n={b['count']}), but adjacent bucket '{next_label}' is profitable "
                            f"(ROI={next_b['roi']:.2%}, n={next_b['count']}). "
                            f"Consider raising threshold."
                        ),
                    })

    return {
        "buckets": bucket_results,
        "recommendations": recommendations,
        "evidence": evidence,
    }
