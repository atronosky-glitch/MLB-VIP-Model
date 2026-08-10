"""Evaluation of the independent strikeout challenger.

This module is descriptive only. It never changes production qualification,
thresholds, staking, or delivery.
"""

from __future__ import annotations

import math
from collections.abc import Iterable


def evaluate_shadow_records(records: Iterable[dict], *, min_sample: int = 30) -> dict:
    """Compare challenger probability with results, price, ROI, and CLV."""
    rows = [
        row for row in records
        if row.get("challenger_fair_probability") is not None
        and row.get("settlement_status") in ("WIN", "LOSS")
    ]
    if not rows:
        return {"sample_size": 0, "status": "INSUFFICIENT_DATA"}

    brier_values = []
    log_loss_values = []
    expected_ev = []
    profit = 0.0
    risk = 0.0
    clv = []
    for row in rows:
        probability = min(max(float(row["challenger_fair_probability"]), 1e-9), 1 - 1e-9)
        outcome = 1.0 if row["settlement_status"] == "WIN" else 0.0
        brier_values.append((probability - outcome) ** 2)
        log_loss_values.append(-(outcome * math.log(probability) + (1 - outcome) * math.log(1 - probability)))
        odds = row.get("offered_decimal_odds")
        if odds is not None:
            expected_ev.append(probability * float(odds) - 1.0)
        profit += float(row.get("profit_units") or 0.0)
        risk += float(row.get("risk_units") or 0.0)
        if row.get("clv_probability") is not None:
            clv.append(float(row["clv_probability"]))

    return {
        "sample_size": len(rows),
        "status": "SUFFICIENT" if len(rows) >= min_sample else "INSUFFICIENT_DATA",
        "brier_score": round(sum(brier_values) / len(brier_values), 6),
        "log_loss": round(sum(log_loss_values) / len(log_loss_values), 6),
        "average_expected_ev": round(sum(expected_ev) / len(expected_ev), 6) if expected_ev else None,
        "realized_profit_units": round(profit, 6),
        "realized_roi": round(profit / risk, 6) if risk else None,
        "average_clv_probability": round(sum(clv) / len(clv), 6) if clv else None,
        "clv_sample_size": len(clv),
        "comparison": "challenger_probability_vs_settled_result",
    }


def evaluate_shadow_from_connection(conn, *, min_sample: int = 30) -> dict:
    """Evaluate persisted challenger fields from an application connection."""
    rows = conn.execute("""
        SELECT hr.challenger_fair_probability, hr.offered_decimal_odds,
               ms.settlement_status, bu.profit_units, bu.risk_units,
               cp.clv_probability
        FROM historical_recommendations hr
        JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
        LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
        LEFT JOIN closing_prices cp ON cp.recommendation_id = hr.recommendation_id
        WHERE hr.market_type = 'pitching_strikeouts_ou'
          AND hr.challenger_fair_probability IS NOT NULL
    """).fetchall()
    return evaluate_shadow_records([dict(row) for row in rows], min_sample=min_sample)
