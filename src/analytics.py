"""Historical analytics engine.

Provides SQL-backed analytics queries against the historical_recommendations,
market_settlements, bet_units, and closing_prices tables. All functions
accept a connection and return plain dicts/lists suitable for CLI display
or CSV export.

No ML. No arbitrary thresholds. Pure SQL aggregation.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict


def roi_by_market(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by market_type.

    Returns list of dicts sorted by ROI descending:
        market_type, settled, wins, losses, pushes, units_risked,
        units_won, roi, win_rate
    """
    cur = conn.execute("""
        SELECT
            hr.market_type,
            COUNT(*) as settled,
            SUM(CASE WHEN ms.settlement_status = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ms.settlement_status = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN ms.settlement_status = 'PUSH' THEN 1 ELSE 0 END) as pushes,
            COALESCE(SUM(bu.risk_units), 0) as units_risked,
            COALESCE(SUM(bu.profit_units), 0) as units_won
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
        GROUP BY hr.market_type
        ORDER BY (COALESCE(SUM(bu.profit_units), 0) / NULLIF(SUM(bu.risk_units), 0)) DESC
    """)
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["roi"] = round(d["units_won"] / d["units_risked"], 4) if d["units_risked"] > 0 else 0.0
        d["win_rate"] = round(d["wins"] / d["settled"], 4) if d["settled"] > 0 else 0.0
        results.append(d)
    return results


def roi_by_sportsbook(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by sportsbook.

    Returns list of dicts sorted by ROI descending.
    """
    cur = conn.execute("""
        SELECT
            hr.sportsbook,
            COUNT(*) as settled,
            SUM(CASE WHEN ms.settlement_status = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ms.settlement_status = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN ms.settlement_status = 'PUSH' THEN 1 ELSE 0 END) as pushes,
            COALESCE(SUM(bu.risk_units), 0) as units_risked,
            COALESCE(SUM(bu.profit_units), 0) as units_won
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
        GROUP BY hr.sportsbook
        ORDER BY (COALESCE(SUM(bu.profit_units), 0) / NULLIF(SUM(bu.risk_units), 0)) DESC
    """)
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["roi"] = round(d["units_won"] / d["units_risked"], 4) if d["units_risked"] > 0 else 0.0
        d["win_rate"] = round(d["wins"] / d["settled"], 4) if d["settled"] > 0 else 0.0
        results.append(d)
    return results


def roi_by_rec_status(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by recommendation status (STRONG_EDGE, POSITIVE_EDGE, etc.).

    Returns list of dicts sorted by ROI descending.
    """
    cur = conn.execute("""
        SELECT
            hr.rec_status,
            COUNT(*) as settled,
            SUM(CASE WHEN ms.settlement_status = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ms.settlement_status = 'LOSS' THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(bu.risk_units), 0) as units_risked,
            COALESCE(SUM(bu.profit_units), 0) as units_won
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
        GROUP BY hr.rec_status
        ORDER BY (COALESCE(SUM(bu.profit_units), 0) / NULLIF(SUM(bu.risk_units), 0)) DESC
    """)
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["roi"] = round(d["units_won"] / d["units_risked"], 4) if d["units_risked"] > 0 else 0.0
        d["win_rate"] = round(d["wins"] / d["settled"], 4) if d["settled"] > 0 else 0.0
        results.append(d)
    return results


def roi_by_ev_bucket(conn: sqlite3.Connection, buckets: list[tuple]) -> list[dict]:
    """ROI breakdown by EV bucket.

    Parameters
    ----------
    buckets : list of (label, test_fn) tuples
        Bucket definitions. Each test_fn takes an EV value and returns bool.

    Returns list of dicts sorted by bucket order.
    """
    cur = conn.execute("""
        SELECT
            hr.recommendation_id,
            hr.ev_pct,
            ms.settlement_status,
            bu.risk_units,
            bu.profit_units
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
          AND hr.ev_pct IS NOT NULL
    """)

    bucket_data: dict[str, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        ev = d["ev_pct"]
        # ev_pct is already in percentage points (5.0 = 5%)
        assigned = False
        if ev is not None:
            for label, test_fn in buckets:
                if test_fn(ev):
                    bucket_data[label].append(d)
                    assigned = True
                    break
        if not assigned:
            bucket_data["unknown"].append(d)

    results = []
    for label, _ in buckets:
        recs = bucket_data.get(label, [])
        if not recs:
            results.append({
                "bucket": label, "count": 0, "wins": 0, "losses": 0,
                "units_risked": 0.0, "units_won": 0.0, "roi": 0.0,
                "win_rate": 0.0, "avg_ev": 0.0,
            })
            continue
        settled = len(recs)
        wins = sum(1 for r in recs if r["settlement_status"] == "WIN")
        losses = sum(1 for r in recs if r["settlement_status"] == "LOSS")
        risked = sum(r.get("risk_units", 0) or 0 for r in recs)
        won = sum(r.get("profit_units", 0) or 0 for r in recs)
        avg_ev = sum(r["ev_pct"] for r in recs) / settled if settled else 0.0
        results.append({
            "bucket": label, "count": settled, "wins": wins, "losses": losses,
            "units_risked": round(risked, 4), "units_won": round(won, 4),
            "roi": round(won / risked, 4) if risked > 0 else 0.0,
            "win_rate": round(wins / settled, 4) if settled > 0 else 0.0,
            "avg_ev": round(avg_ev, 4),
        })
    return results


def roi_by_odds_bucket(conn: sqlite3.Connection, buckets: list[tuple]) -> list[dict]:
    """ROI breakdown by American odds bucket."""
    cur = conn.execute("""
        SELECT
            hr.recommendation_id,
            hr.offered_american_odds,
            ms.settlement_status,
            bu.risk_units,
            bu.profit_units
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
          AND hr.offered_american_odds IS NOT NULL
    """)

    bucket_data: dict[str, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        odds = d["offered_american_odds"]
        for label, test_fn in buckets:
            if test_fn(odds):
                bucket_data[label].append(d)
                break

    results = []
    for label, _ in buckets:
        recs = bucket_data.get(label, [])
        settled = len(recs)
        if settled == 0:
            results.append({
                "bucket": label, "count": 0, "wins": 0, "losses": 0,
                "units_risked": 0.0, "units_won": 0.0, "roi": 0.0,
                "win_rate": 0.0,
            })
            continue
        wins = sum(1 for r in recs if r["settlement_status"] == "WIN")
        losses = sum(1 for r in recs if r["settlement_status"] == "LOSS")
        risked = sum(r.get("risk_units", 0) or 0 for r in recs)
        won = sum(r.get("profit_units", 0) or 0 for r in recs)
        results.append({
            "bucket": label, "count": settled, "wins": wins, "losses": losses,
            "units_risked": round(risked, 4), "units_won": round(won, 4),
            "roi": round(won / risked, 4) if risked > 0 else 0.0,
            "win_rate": round(wins / settled, 4) if settled > 0 else 0.0,
        })
    return results


def roi_by_n_books(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by comparison-book count."""
    cur = conn.execute("""
        SELECT
            hr.recommendation_id,
            hr.n_consensus_books,
            ms.settlement_status,
            bu.risk_units,
            bu.profit_units
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
          AND hr.n_consensus_books IS NOT NULL
    """)

    bucket_data: dict[int, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        n = d["n_consensus_books"]
        bucket_data[n].append(d)

    results = []
    for n in sorted(bucket_data.keys()):
        recs = bucket_data[n]
        settled = len(recs)
        wins = sum(1 for r in recs if r["settlement_status"] == "WIN")
        losses = sum(1 for r in recs if r["settlement_status"] == "LOSS")
        risked = sum(r.get("risk_units", 0) or 0 for r in recs)
        won = sum(r.get("profit_units", 0) or 0 for r in recs)
        results.append({
            "n_books": n, "count": settled, "wins": wins, "losses": losses,
            "units_risked": round(risked, 4), "units_won": round(won, 4),
            "roi": round(won / risked, 4) if risked > 0 else 0.0,
            "win_rate": round(wins / settled, 4) if settled > 0 else 0.0,
        })
    return results


def roi_by_day(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by date (day of scan_timestamp)."""
    cur = conn.execute("""
        SELECT
            DATE(hr.scan_timestamp) as scan_date,
            hr.recommendation_id,
            ms.settlement_status,
            bu.risk_units,
            bu.profit_units
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
    """)

    day_data: dict[str, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        day_data[d["scan_date"]].append(d)

    results = []
    for date in sorted(day_data.keys()):
        recs = day_data[date]
        settled = len(recs)
        wins = sum(1 for r in recs if r["settlement_status"] == "WIN")
        losses = sum(1 for r in recs if r["settlement_status"] == "LOSS")
        risked = sum(r.get("risk_units", 0) or 0 for r in recs)
        won = sum(r.get("profit_units", 0) or 0 for r in recs)
        results.append({
            "date": date, "count": settled, "wins": wins, "losses": losses,
            "units_risked": round(risked, 4), "units_won": round(won, 4),
            "roi": round(won / risked, 4) if risked > 0 else 0.0,
            "win_rate": round(wins / settled, 4) if settled > 0 else 0.0,
        })
    return results


def roi_by_hour_before_pitch(conn: sqlite3.Connection) -> list[dict]:
    """ROI breakdown by hour-before-first-pitch buckets.

    Computes the difference between scan_timestamp and event_start_time,
    bucketed into hourly intervals.
    """
    cur = conn.execute("""
        SELECT
            hr.recommendation_id,
            hr.scan_timestamp,
            hr.event_start_time,
            ms.settlement_status,
            bu.risk_units,
            bu.profit_units
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
          AND hr.event_start_time IS NOT NULL
          AND hr.scan_timestamp IS NOT NULL
          AND hr.event_start_time != ''
    """)

    hour_buckets: dict[int, list[dict]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        try:
            from datetime import datetime as dt
            scan = dt.fromisoformat(d["scan_timestamp"].replace("Z", "+00:00"))
            start = dt.fromisoformat(d["event_start_time"].replace("Z", "+00:00"))
            diff_hours = int((start - scan).total_seconds() / 3600)
            bucket = max(0, diff_hours)
        except (ValueError, TypeError):
            bucket = -1
        hour_buckets[bucket].append(d)

    results = []
    for hour in sorted(k for k in hour_buckets.keys() if k >= 0):
        recs = hour_buckets[hour]
        settled = len(recs)
        wins = sum(1 for r in recs if r["settlement_status"] == "WIN")
        losses = sum(1 for r in recs if r["settlement_status"] == "LOSS")
        risked = sum(r.get("risk_units", 0) or 0 for r in recs)
        won = sum(r.get("profit_units", 0) or 0 for r in recs)
        results.append({
            "hours_before_pitch": hour, "count": settled, "wins": wins,
            "losses": losses,
            "units_risked": round(risked, 4), "units_won": round(won, 4),
            "roi": round(won / risked, 4) if risked > 0 else 0.0,
            "win_rate": round(wins / settled, 4) if settled > 0 else 0.0,
        })
    return results


def clv_by_sportsbook(conn: sqlite3.Connection) -> list[dict]:
    """Average CLV by sportsbook (only for recs with CLV available)."""
    cur = conn.execute("""
        SELECT
            hr.sportsbook,
            COUNT(*) as count,
            AVG(cp.clv_probability) as avg_clv_prob,
            SUM(CASE WHEN cp.clv_probability > 0 THEN 1 ELSE 0 END) as favorable_clv,
            SUM(CASE WHEN cp.clv_probability < 0 THEN 1 ELSE 0 END) as unfavorable_clv,
            AVG(cp.clv_price_diff) as avg_price_diff
        FROM historical_recommendations hr
        JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        WHERE cp.clv_available = 1
        GROUP BY hr.sportsbook
        ORDER BY AVG(cp.clv_probability) DESC
    """)
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["avg_clv_prob"] = round(d["avg_clv_prob"], 6) if d["avg_clv_prob"] else 0.0
        d["avg_price_diff"] = round(d["avg_price_diff"], 1) if d["avg_price_diff"] else 0.0
        d["clv_rate"] = round(d["favorable_clv"] / d["count"], 4) if d["count"] > 0 else 0.0
        results.append(d)
    return results


def clv_by_market(conn: sqlite3.Connection) -> list[dict]:
    """CLV breakdown by market_type."""
    cur = conn.execute("""
        SELECT
            hr.market_type,
            COUNT(*) as count,
            AVG(cp.clv_probability) as avg_clv_prob,
            SUM(CASE WHEN cp.clv_probability > 0 THEN 1 ELSE 0 END) as favorable_clv,
            AVG(cp.clv_price_diff) as avg_price_diff
        FROM historical_recommendations hr
        JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        WHERE cp.clv_available = 1
        GROUP BY hr.market_type
        ORDER BY AVG(cp.clv_probability) DESC
    """)
    results = []
    for row in cur.fetchall():
        d = dict(row)
        d["avg_clv_prob"] = round(d["avg_clv_prob"], 6) if d["avg_clv_prob"] else 0.0
        d["avg_price_diff"] = round(d["avg_price_diff"], 1) if d["avg_price_diff"] else 0.0
        d["clv_rate"] = round(d["favorable_clv"] / d["count"], 4) if d["count"] > 0 else 0.0
        results.append(d)
    return results


def hit_rate_by_market(conn: sqlite3.Connection) -> list[dict]:
    """Hit rate (win rate) by market_type."""
    return roi_by_market(conn)


def overall_summary(conn: sqlite3.Connection) -> dict:
    """Overall performance summary across all settled recommendations."""
    cur = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN ms.settlement_status = 'WIN' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ms.settlement_status = 'LOSS' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN ms.settlement_status = 'PUSH' THEN 1 ELSE 0 END) as pushes,
            SUM(CASE WHEN ms.settlement_status IN ('VOID', 'CANCELLED') THEN 1 ELSE 0 END) as voids,
            COALESCE(SUM(bu.risk_units), 0) as units_risked,
            COALESCE(SUM(bu.profit_units), 0) as units_won,
            AVG(hr.ev_pct) as avg_ev,
            AVG(hr.offered_american_odds) as avg_odds
        FROM historical_recommendations hr
        JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        WHERE ms.settlement_status != 'UNRESOLVED'
    """)
    row = dict(cur.fetchone())
    row["roi"] = round(row["units_won"] / row["units_risked"], 4) if row["units_risked"] > 0 else 0.0
    row["win_rate"] = round(row["wins"] / row["total"], 4) if row["total"] > 0 else 0.0
    row["avg_ev"] = round(row["avg_ev"], 4) if row["avg_ev"] else 0.0
    row["avg_odds"] = round(row["avg_odds"], 1) if row["avg_odds"] else 0.0
    return row
