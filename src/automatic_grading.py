"""Idempotent automatic grading from verified stored result facts.

This module deliberately consumes only ``player_stat_results`` rows already
ingested by a trusted result source. It never infers a stat from game status,
odds, or event-array position.
"""

from __future__ import annotations

import logging

from database.db_manager import (
    DB,
    get_unsettled_recommendations,
    get_player_stat_result,
    record_grading_completed,
    save_bet_units,
    settle_recommendation,
)
from src.grading import GRADER_VERSION, SETTLEMENT_UNRESOLVED, grade_ou

logger = logging.getLogger(__name__)


def grade_available_recommendations(conn: DB, event_id: str | None = None) -> dict:
    """Grade unresolved O/U recommendations with verified final stats.

    Y/N recommendations remain explicitly unresolved until a result contract
    supplies their binary settlement fact. Repeated calls are safe because
    settled recommendations are excluded and units use an idempotent key.
    """
    recommendations = get_unsettled_recommendations(conn)
    if event_id:
        recommendations = [r for r in recommendations if r.get("event_id") == event_id]

    result = {"examined": len(recommendations), "graded": 0, "unresolved": 0, "errors": 0}
    for rec in recommendations:
        stat = get_player_stat_result(
            conn, rec.get("event_id"), rec.get("player_id"), rec.get("market_type")
        )
        final_value = stat.get("final_stat_value") if stat else None
        if not stat or final_value is None or stat.get("result_status") not in (None, "AVAILABLE", "FINAL"):
            result["unresolved"] += 1
            continue

        if (rec.get("market_type") or "").endswith("_yn"):
            from src.grading import grade_yn
            status = grade_yn(final_value, rec.get("side"))
        else:
            status = grade_ou(final_value, rec.get("line"), rec.get("side", ""))
        if status == SETTLEMENT_UNRESOLVED:
            result["unresolved"] += 1
            continue

        try:
            if not settle_recommendation(
                conn,
                rec["recommendation_id"],
                status,
                final_stat_value=final_value,
                settlement_reason=f"verified result source: {stat.get('result_source') or 'stored result'}",
                grader_version=GRADER_VERSION,
            ):
                result["errors"] += 1
                continue
            save_bet_units(conn, rec["recommendation_id"], status, rec["offered_american_odds"])
            record_grading_completed(
                conn,
                rec,
                status,
                final_stat_value=final_value,
                grader_version=GRADER_VERSION,
            )
            result["graded"] += 1
        except Exception:
            logger.exception("Automatic grading failed recommendation=%s", rec.get("recommendation_id"))
            result["errors"] += 1
    return result
