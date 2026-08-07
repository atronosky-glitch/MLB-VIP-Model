"""Odds Observations and Line Movement Tracking.

Append-only odds observation table for tracking line movement
from morning to pregame to closing for official picks.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


# ── Observation types ──────────────────────────────────────────────

OBSERVATION_TYPES = ("MORNING", "PREGAME", "CLOSING", "POSTGAME", "MANUAL")


# ── Storage functions ──────────────────────────────────────────────


def record_observation(
    conn: sqlite3.Connection,
    official_pick_id: str,
    observation_type: str,
    sportsbook: str,
    american_odds: int,
    decimal_odds: float,
    implied_prob: float,
    line: float | None = None,
    consensus_prob: float | None = None,
    avg_other_prob: float | None = None,
    median_other_prob: float | None = None,
    unique_book_count: int | None = None,
    freshness_status: str | None = None,
    source_run_id: str | None = None,
    market_status: str | None = None,
) -> str:
    """Record an odds observation for an official pick. Returns observation_id."""
    import uuid
    obs_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO pick_observations (
            observation_id, official_pick_id, observation_type, sportsbook,
            american_odds, decimal_odds, implied_prob, line,
            consensus_prob, avg_other_prob, median_other_prob,
            unique_book_count, freshness_status, source_run_id,
            market_status, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        obs_id, official_pick_id, observation_type, sportsbook,
        american_odds, decimal_odds, implied_prob, line,
        consensus_prob, avg_other_prob, median_other_prob,
        unique_book_count, freshness_status, source_run_id,
        market_status,
    ))
    conn.commit()
    return obs_id


def get_observations(
    conn: sqlite3.Connection,
    official_pick_id: str | None = None,
    observation_type: str | None = None,
) -> list[dict]:
    """Query observations, optionally filtered."""
    where = []
    params: list = []
    if official_pick_id:
        where.append("official_pick_id = ?")
        params.append(official_pick_id)
    if observation_type:
        where.append("observation_type = ?")
        params.append(observation_type)
    where_clause = " WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"SELECT * FROM pick_observations {where_clause} ORDER BY observed_at",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def has_observation(
    conn: sqlite3.Connection,
    official_pick_id: str,
    observation_type: str,
) -> bool:
    """Check if an observation already exists (dedup)."""
    row = conn.execute(
        "SELECT 1 FROM pick_observations WHERE official_pick_id = ? AND observation_type = ?",
        (official_pick_id, observation_type),
    ).fetchone()
    return row is not None


def record_pipeline_observations(
    conn,
    opportunities: list[dict],
    observation_type: str,
    source_run_id: str | None = None,
) -> int:
    """Record a scan's prices for already-frozen official picks.

    Matching uses stable event/player/market/side/line/sportsbook fields. A
    later price or odds change therefore attaches to the original official
    pick instead of creating a second identity. One observation per phase is
    retained by design; final CLV remains handled by ``closing_prices``.
    """
    if observation_type not in OBSERVATION_TYPES:
        raise ValueError(f"Unknown observation type: {observation_type}")
    recorded = 0
    for opp in opportunities:
        rows = conn.execute(
            """SELECT hr.*, op.recommendation_id AS official_id
               FROM historical_recommendations hr
               JOIN official_picks op ON op.recommendation_id = hr.recommendation_id
              WHERE hr.event_id = ? AND hr.player_id = ? AND hr.market_type = ?
                AND hr.market_form = ? AND hr.side = ? AND hr.sportsbook = ?
              ORDER BY hr.scan_timestamp DESC""",
            (
                opp.get("event_id"), opp.get("player_id"), opp.get("market_type"),
                "yn" if opp.get("line") is None else "ou", opp.get("side"),
                opp.get("sportsbook"),
            ),
        ).fetchall()
        match = next(
            (dict(row) for row in rows if row["line"] == opp.get("line")),
            None,
        )
        if not match or has_observation(conn, match["official_id"], observation_type):
            continue
        record_observation(
            conn,
            match["official_id"],
            observation_type,
            opp.get("sportsbook", ""),
            opp.get("american_odds"),
            opp.get("decimal_odds"),
            opp.get("offered_implied_prob") or 0.0,
            line=opp.get("line"),
            consensus_prob=opp.get("fair_prob") or opp.get("market_reference_probability"),
            unique_book_count=opp.get("n_consensus_books"),
            freshness_status=opp.get("freshness_status"),
            source_run_id=source_run_id,
            market_status=opp.get("market_quality") or opp.get("comparison_status"),
        )
        recorded += 1
    return recorded


def compute_movement(
    conn: sqlite3.Connection,
    official_pick_id: str,
) -> dict:
    """Compute odds movement between morning and pregame observations."""
    morning = conn.execute(
        "SELECT american_odds, implied_prob, line FROM pick_observations "
        "WHERE official_pick_id = ? AND observation_type = 'MORNING' "
        "ORDER BY observed_at DESC LIMIT 1",
        (official_pick_id,),
    ).fetchone()

    pregame = conn.execute(
        "SELECT american_odds, implied_prob, line FROM pick_observations "
        "WHERE official_pick_id = ? AND observation_type = 'PREGAME' "
        "ORDER BY observed_at DESC LIMIT 1",
        (official_pick_id,),
    ).fetchone()

    closing = conn.execute(
        "SELECT american_odds, implied_prob, line FROM pick_observations "
        "WHERE official_pick_id = ? AND observation_type = 'CLOSING' "
        "ORDER BY observed_at DESC LIMIT 1",
        (official_pick_id,),
    ).fetchone()

    result = {
        "morning_odds": morning["american_odds"] if morning else None,
        "pregame_odds": pregame["american_odds"] if pregame else None,
        "closing_odds": closing["american_odds"] if closing else None,
        "morning_implied_prob": morning["implied_prob"] if morning else None,
        "pregame_implied_prob": pregame["implied_prob"] if pregame else None,
        "closing_implied_prob": closing["implied_prob"] if closing else None,
        "morning_line": morning["line"] if morning else None,
        "pregame_line": pregame["line"] if pregame else None,
        "closing_line": closing["line"] if closing else None,
    }

    # Calculate movements
    if morning and pregame:
        result["odds_movement_morning_to_pregame"] = (
            pregame["american_odds"] - morning["american_odds"]
        )
        result["prob_movement_morning_to_pregame"] = (
            (pregame["implied_prob"] or 0) - (morning["implied_prob"] or 0)
        )
        if morning["line"] and pregame["line"]:
            result["line_movement"] = pregame["line"] - morning["line"]

    if pregame and closing:
        result["odds_movement_pregame_to_closing"] = (
            closing["american_odds"] - pregame["american_odds"]
        )

    return result
