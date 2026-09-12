"""Persistence for src/execution/matching.py's results, against the
market_matches table. Kept separate from matching.py so the scoring
logic stays DB-free and trivially unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.execution.matching import MatchResult

MATCHER_VERSION = "v1"


def get_existing_match(
    conn: Any, recommendation_id: str, provider: str, matcher_version: str = MATCHER_VERSION,
) -> dict | None:
    """The stored match/rejection for this (recommendation, provider) at
    this matcher_version, or None if it's never been checked."""
    row = conn.execute(
        """SELECT * FROM market_matches
           WHERE recommendation_id = ? AND provider = ? AND matcher_version = ?""",
        (recommendation_id, provider, matcher_version),
    ).fetchone()
    return dict(row) if row is not None else None


def persist_match(
    conn: Any,
    *,
    recommendation_id: str,
    provider: str,
    league: str,
    market_type: str,
    match_status: str,
    result: MatchResult | None = None,
    matcher_version: str = MATCHER_VERSION,
) -> None:
    """Record a match or a rejection (no_candidates / rejected_low_confidence).

    *result* carries the score breakdown when available (even for a
    rejection -- the best-scoring candidate that still didn't clear the
    gate is worth keeping for audit) and is None only for no_candidates.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO market_matches (
               recommendation_id, provider, provider_market_id, league, market_type,
               confidence, match_status, team_score, market_type_score, line_score,
               date_score, matcher_version, provider_title, matched_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (recommendation_id, provider, matcher_version) DO UPDATE SET
               provider_market_id = excluded.provider_market_id,
               confidence = excluded.confidence,
               match_status = excluded.match_status,
               team_score = excluded.team_score,
               market_type_score = excluded.market_type_score,
               line_score = excluded.line_score,
               date_score = excluded.date_score,
               provider_title = excluded.provider_title,
               matched_at = excluded.matched_at
        """,
        (
            recommendation_id, provider,
            result.provider_market_id if result else "",
            league, market_type,
            result.confidence if result else 0.0,
            match_status,
            result.team_score if result else None,
            result.market_type_score if result else None,
            result.line_score if result else None,
            result.date_score if result else None,
            matcher_version,
            result.provider_title if result else None,
            now,
        ),
    )
    conn.commit()
