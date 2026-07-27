"""Bookmaker quality scores.

Calculates sportsbook quality rankings based on:
- Average CLV (closing line value) — higher is better
- Average profitability (ROI) — higher is better
- Average disagreement from consensus — indicates pricing quality
"""

from __future__ import annotations

import sqlite3


def bookmaker_quality_scores(conn: sqlite3.Connection) -> list[dict]:
    """Calculate quality scores for each sportsbook.

    Returns a list of dicts sorted by quality_score descending:
        sportsbook, quality_score, avg_clv, avg_roi, avg_disagreement,
        n_recommendations, n_settled, clv_available_count
    """
    # Get CLV data per sportsbook
    clv_cur = conn.execute("""
        SELECT
            hr.sportsbook,
            AVG(cp.clv_probability) as avg_clv,
            COUNT(*) as clv_count
        FROM historical_recommendations hr
        JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        WHERE cp.clv_available = 1
        GROUP BY hr.sportsbook
    """)
    clv_data = {row["sportsbook"]: dict(row) for row in clv_cur.fetchall()}

    # Get ROI data per sportsbook
    roi_cur = conn.execute("""
        SELECT
            hr.sportsbook,
            COUNT(*) as settled,
            COALESCE(SUM(bu.profit_units), 0) as units_won,
            COALESCE(SUM(bu.risk_units), 0) as units_risked
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
        GROUP BY hr.sportsbook
    """)
    roi_data = {}
    for row in roi_cur.fetchall():
        d = dict(row)
        d["avg_roi"] = d["units_won"] / d["units_risked"] if d["units_risked"] > 0 else 0.0
        roi_data[d["sportsbook"]] = d

    # Get recommendation count per sportsbook
    rec_cur = conn.execute("""
        SELECT sportsbook, COUNT(*) as n_recommendations
        FROM historical_recommendations
        GROUP BY sportsbook
    """)
    rec_data = {row["sportsbook"]: row["n_recommendations"] for row in rec_cur.fetchall()}

    # Combine all sportsbooks
    all_books = set(clv_data.keys()) | set(roi_data.keys()) | set(rec_data.keys())

    results = []
    for book in all_books:
        clv = clv_data.get(book, {}).get("avg_clv", 0) or 0.0
        clv_count = clv_data.get(book, {}).get("clv_count", 0)
        roi = roi_data.get(book, {}).get("avg_roi", 0.0)
        settled = roi_data.get(book, {}).get("settled", 0)
        n_recs = rec_data.get(book, 0)

        # Quality score: weighted combination of CLV and ROI
        # CLV contribution: -1 to +1 range, scaled to 0-50
        clv_score = max(0, min(50, (clv + 1) * 25)) if clv_count > 0 else 25.0
        # ROI contribution: -100% to +100% range, scaled to 0-50
        roi_score = max(0, min(50, (roi + 1) * 25)) if settled > 0 else 25.0

        quality_score = round(clv_score + roi_score, 2)

        results.append({
            "sportsbook": book,
            "quality_score": quality_score,
            "avg_clv": round(clv, 6),
            "avg_roi": round(roi, 4),
            "n_recommendations": n_recs,
            "n_settled": settled,
            "clv_available_count": clv_count,
        })

    results.sort(key=lambda x: x["quality_score"], reverse=True)
    return results


def bookmaker_disagreement(conn: sqlite3.Connection) -> list[dict]:
    """Calculate average disagreement from consensus per sportsbook.

    Uses the difference between a sportsbook's offered odds and the
    average odds across all books for the same recommendation.
    """
    cur = conn.execute("""
        SELECT
            hr.sportsbook,
            AVG(ABS(hr.offered_american_odds - hr.fair_american_odds)) as avg_odds_diff,
            COUNT(*) as count
        FROM historical_recommendations hr
        WHERE hr.fair_american_odds IS NOT NULL
        GROUP BY hr.sportsbook
        HAVING COUNT(*) >= 3
        ORDER BY AVG(ABS(hr.offered_american_odds - hr.fair_american_odds)) DESC
    """)
    return [dict(row) for row in cur.fetchall()]
