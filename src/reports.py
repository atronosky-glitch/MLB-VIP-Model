"""Report generation for Phase 9 intelligence layer.

Generates five CSV reports:
1. performance_report.csv — Overall performance summary
2. sportsbook_report.csv — Bookmaker quality rankings
3. market_report.csv — ROI and hit rate by market
4. recommendation_report.csv — All recommendations with confidence scores
5. confidence_report.csv — Confidence score distribution

All functions accept a connection and output directory path.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from src.analytics import (
    overall_summary, roi_by_market, roi_by_sportsbook,
    roi_by_ev_bucket, clv_by_sportsbook, clv_by_market,
)
from src.bookmaker_scores import bookmaker_quality_scores
from src.confidence import compute_confidence, DEFAULT_WEIGHTS
from src.grading import EV_BUCKETS


def generate_performance_report(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Generate performance_report.csv."""
    summary = overall_summary(conn)
    path = output_dir / "performance_report.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary.keys())
        writer.writeheader()
        writer.writerow(summary)
    return path


def generate_sportsbook_report(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Generate sportsbook_report.csv."""
    scores = bookmaker_quality_scores(conn)
    path = output_dir / "sportsbook_report.csv"
    if not scores:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("No data available\n")
        return path
    fieldnames = list(scores[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(scores)
    return path


def generate_market_report(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Generate market_report.csv with ROI and CLV by market."""
    market_roi = roi_by_market(conn)
    market_clv = {r["market_type"]: r for r in clv_by_market(conn)}

    rows = []
    for r in market_roi:
        clv_info = market_clv.get(r["market_type"], {})
        rows.append({
            "market_type": r["market_type"],
            "settled": r["settled"],
            "wins": r["wins"],
            "losses": r["losses"],
            "pushes": r["pushes"],
            "win_rate": r["win_rate"],
            "units_risked": r["units_risked"],
            "units_won": r["units_won"],
            "roi": r["roi"],
            "avg_clv": clv_info.get("avg_clv_prob", None),
            "clv_count": clv_info.get("count", 0),
        })

    path = output_dir / "market_report.csv"
    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("No data available\n")
        return path

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def generate_recommendation_report(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Generate recommendation_report.csv with all recommendations and confidence scores."""
    cur = conn.execute("""
        SELECT hr.*, ms.settlement_status,
               bu.risk_units, bu.profit_units,
               cp.clv_probability, cp.clv_available
        FROM historical_recommendations hr
        LEFT JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        LEFT JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        ORDER BY hr.created_at
    """)

    rows = []
    for row in cur.fetchall():
        rec = dict(row)
        conf = compute_confidence(rec)
        rec["confidence_score"] = conf["confidence_score"]
        rec["confidence_grade"] = conf["grade"]
        rows.append(rec)

    path = output_dir / "recommendation_report.csv"
    if not rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("No data available\n")
        return path

    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def generate_confidence_report(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Generate confidence_report.csv showing confidence score distribution."""
    cur = conn.execute("""
        SELECT *
        FROM historical_recommendations
        ORDER BY created_at
    """)

    grade_buckets = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    total_score = 0.0
    count = 0
    component_totals = {
        "n_books": 0.0,
        "market_quality": 0.0,
        "ev_magnitude": 0.0,
        "freshness": 0.0,
        "mapping_confidence": 0.0,
    }

    for row in cur.fetchall():
        rec = dict(row)
        conf = compute_confidence(rec)
        grade_buckets[conf["grade"]] = grade_buckets.get(conf["grade"], 0) + 1
        total_score += conf["confidence_score"]
        count += 1
        for k, v in conf["components"].items():
            if k in component_totals:
                component_totals[k] += v

    rows = []
    for grade in ["A", "B", "C", "D", "F"]:
        rows.append({
            "grade": grade,
            "count": grade_buckets[grade],
            "pct": round(grade_buckets[grade] / count * 100, 1) if count > 0 else 0.0,
        })

    rows.append({
        "grade": "TOTAL",
        "count": count,
        "pct": 100.0,
    })

    rows.append({
        "grade": "AVG_SCORE",
        "count": round(total_score / count, 2) if count > 0 else 0,
        "pct": 0.0,
    })

    for comp, total in component_totals.items():
        rows.append({
            "grade": f"AVG_{comp.upper()}",
            "count": round(total / count, 4) if count > 0 else 0,
            "pct": 0.0,
        })

    path = output_dir / "confidence_report.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["grade", "count", "pct"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def generate_all_reports(conn: sqlite3.Connection, output_dir: str | Path) -> list[Path]:
    """Generate all five reports. Returns list of paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = [
        generate_performance_report(conn, output_dir),
        generate_sportsbook_report(conn, output_dir),
        generate_market_report(conn, output_dir),
        generate_recommendation_report(conn, output_dir),
        generate_confidence_report(conn, output_dir),
    ]
    return paths
