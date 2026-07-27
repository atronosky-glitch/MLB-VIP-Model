"""Official Pick Tracker and Performance Metrics.

Flat staking: 1.0 unit risk per official pick.
Tracks win/loss/push/void with profit calculations.
Provides performance metrics and breakdowns.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.official_picks import TIER_OFFICIAL


# ── Unit calculations ──────────────────────────────────────────────

def compute_pick_units(american_odds: int, outcome: str) -> float:
    """Calculate profit units for a pick given its outcome.

    Flat staking: 1.0 unit risk.
    Positive odds: profit = odds / 100
    Negative odds: profit = 100 / abs(odds)
    Loss: -1.0
    Push/Void/Cancelled: 0.0
    """
    if outcome in ("push", "void", "cancelled"):
        return 0.0
    if outcome == "loss":
        return -1.0
    if outcome == "win":
        if american_odds > 0:
            return american_odds / 100.0
        else:
            return 100.0 / abs(american_odds)
    return 0.0


# ── Outcome constants ──────────────────────────────────────────────

PENDING = "pending"
WIN = "win"
LOSS = "loss"
PUSH = "push"
VOID = "void"
UNGRADED = "ungraded"
GRADING_ERROR = "grading_error"


@dataclass
class PerformanceMetrics:
    """Aggregate performance metrics for a set of picks."""

    total: int = 0
    pending: int = 0
    graded: int = 0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    voids: int = 0
    units_won: float = 0.0
    units_risked: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    avg_odds: float = 0.0
    avg_model_score: float = 0.0
    avg_price_advantage: float = 0.0
    avg_ev: float = 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "pending": self.pending,
            "graded": self.graded,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "voids": self.voids,
            "units_won": round(self.units_won, 2),
            "units_risked": round(self.units_risked, 2),
            "roi": round(self.roi, 4),
            "win_rate": round(self.win_rate, 4),
            "avg_odds": round(self.avg_odds, 1),
            "avg_model_score": round(self.avg_model_score, 2),
            "avg_price_advantage": round(self.avg_price_advantage, 2),
            "avg_ev": round(self.avg_ev, 2),
        }


# ── Tracker functions ──────────────────────────────────────────────


def get_official_picks(
    conn: sqlite3.Connection,
    tier: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Query official picks from the official_picks table."""
    where = []
    params: list = []
    if tier:
        where.append("op.tier = ?")
        params.append(tier)
    if status:
        where.append("op.outcome = ?")
        params.append(status)

    where_clause = " WHERE " + " AND ".join(where) if where else ""
    limit_clause = f" LIMIT {limit}" if limit else ""

    sql = f"""
        SELECT op.*, hr.player_name, hr.market_type, hr.market_form,
               hr.side, hr.line, hr.sportsbook, hr.offered_american_odds,
               hr.offered_decimal_odds, hr.ev_pct, hr.yn_implied_prob_adv,
               hr.n_consensus_books, hr.market_quality, hr.freshness_status,
               hr.rec_status, hr.matchup, hr.event_status, hr.event_start_time,
               hr.model_score, hr.score_explanation, hr.fingerprint
        FROM official_picks op
        JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
        {where_clause}
        ORDER BY op.selected_at DESC
        {limit_clause}
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_pick_outcome(conn: sqlite3.Connection, recommendation_id: str) -> str:
    """Get the current outcome of a pick."""
    row = conn.execute(
        "SELECT outcome FROM official_picks WHERE recommendation_id = ?",
        (recommendation_id,),
    ).fetchone()
    return row["outcome"] if row else PENDING


def update_pick_outcome(
    conn: sqlite3.Connection,
    recommendation_id: str,
    outcome: str,
    final_stat_value: float | None = None,
    grader_version: str = "v1.0",
) -> bool:
    """Update a pick's outcome after grading."""
    profit = None
    if outcome in (WIN, LOSS, PUSH, VOID, "cancelled"):
        row = conn.execute(
            "SELECT offered_american_odds FROM historical_recommendations "
            "WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        if row:
            profit = compute_pick_units(row["offered_american_odds"], outcome)

    conn.execute("""
        UPDATE official_picks SET
            outcome = ?,
            graded_at = datetime('now'),
            profit_units = ?,
            final_stat_value = ?,
            grader_version = ?
        WHERE recommendation_id = ?
    """, (outcome, profit, final_stat_value, grader_version, recommendation_id))
    conn.commit()
    return True


def compute_performance(
    conn: sqlite3.Connection,
    tier: str | None = TIER_OFFICIAL,
    start_date: str | None = None,
    end_date: str | None = None,
) -> PerformanceMetrics:
    """Compute aggregate performance metrics."""
    where = []
    params: list = []
    if tier:
        where.append("op.tier = ?")
        params.append(tier)
    if start_date:
        where.append("op.selected_at >= ?")
        params.append(start_date)
    if end_date:
        where.append("op.selected_at <= ?")
        params.append(end_date)

    where_clause = " WHERE " + " AND ".join(where) if where else ""

    sql = f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN op.outcome = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN op.outcome IN ('win','loss','push','void','cancelled') THEN 1 ELSE 0 END) as graded,
            SUM(CASE WHEN op.outcome = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN op.outcome = 'loss' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN op.outcome = 'push' THEN 1 ELSE 0 END) as pushes,
            SUM(CASE WHEN op.outcome = 'void' THEN 1 ELSE 0 END) as voids,
            SUM(COALESCE(op.profit_units, 0)) as units_won,
            SUM(CASE WHEN op.outcome IN ('win','loss') THEN 1.0 ELSE 0 END) as units_risked,
            AVG(hr.offered_american_odds) as avg_odds,
            AVG(hr.model_score) as avg_model_score,
            AVG(hr.yn_implied_prob_adv) as avg_pa,
            AVG(hr.ev_pct) as avg_ev
        FROM official_picks op
        JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
        {where_clause}
    """
    row = conn.execute(sql, params).fetchone()
    if not row or row["total"] == 0:
        return PerformanceMetrics()

    total = row["total"]
    graded = row["graded"] or 0
    wins = row["wins"] or 0
    units_won = row["units_won"] or 0.0
    units_risked = row["units_risked"] or 0.0
    roi = (units_won / units_risked) if units_risked > 0 else 0.0
    win_rate = (wins / graded) if graded > 0 else 0.0

    return PerformanceMetrics(
        total=total,
        pending=row["pending"] or 0,
        graded=graded,
        wins=wins,
        losses=row["losses"] or 0,
        pushes=row["pushes"] or 0,
        voids=row["voids"] or 0,
        units_won=units_won,
        units_risked=units_risked,
        roi=roi,
        win_rate=win_rate,
        avg_odds=row["avg_odds"] or 0.0,
        avg_model_score=row["avg_model_score"] or 0.0,
        avg_price_advantage=row["avg_pa"] or 0.0,
        avg_ev=row["avg_ev"] or 0.0,
    )


def breakdown_by_field(
    conn: sqlite3.Connection,
    field_name: str,
    tier: str | None = TIER_OFFICIAL,
) -> list[dict]:
    """Performance breakdown by a given field."""
    tier_filter = ""
    params: list = []
    if tier:
        tier_filter = "WHERE op.tier = ?"
        params.append(tier)

    valid_fields = {
        "market_type", "sportsbook", "market_form",
    }
    if field_name not in valid_fields:
        return []

    sql = f"""
        SELECT
            hr.{field_name} as bucket,
            COUNT(*) as total,
            SUM(CASE WHEN op.outcome = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN op.outcome IN ('win','loss') THEN 1 ELSE 0 END) as graded,
            SUM(COALESCE(op.profit_units, 0)) as units_won,
            SUM(CASE WHEN op.outcome IN ('win','loss') THEN 1.0 ELSE 0 END) as units_risked
        FROM official_picks op
        JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
        {tier_filter}
        GROUP BY hr.{field_name}
        ORDER BY total DESC
    """
    rows = conn.execute(sql, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["win_rate"] = (d["wins"] / d["graded"]) if d["graded"] > 0 else 0.0
        d["roi"] = (d["units_won"] / d["units_risked"]) if d["units_risked"] > 0 else 0.0
        results.append(d)
    return results


def grade_pending_picks(conn: sqlite3.Connection) -> int:
    """Grade all pending official picks using settled recommendations.

    Returns the number of picks graded.
    """
    pending = conn.execute("""
        SELECT op.recommendation_id, op.tier
        FROM official_picks op
        WHERE op.outcome = 'pending'
    """).fetchall()

    graded_count = 0
    for pick in pending:
        rec_id = pick["recommendation_id"]
        settled = conn.execute("""
            SELECT settlement_status, final_stat_value
            FROM market_settlements
            WHERE recommendation_id = ?
        """, (rec_id,)).fetchone()

        if settled:
            outcome = settled["settlement_status"].lower()
            stat = settled["final_stat_value"]
            if outcome in (WIN, LOSS, PUSH, VOID, "cancelled"):
                update_pick_outcome(conn, rec_id, outcome, stat)
                graded_count += 1
            elif outcome == "ungraded":
                continue
        else:
            result = conn.execute("""
                SELECT final_status FROM event_results
                WHERE event_id = (
                    SELECT event_id FROM historical_recommendations
                    WHERE recommendation_id = ?
                )
            """, (rec_id,)).fetchone()
            if result and result["final_status"] and result["final_status"] != "UNRESOLVED":
                hr = conn.execute("""
                    SELECT line, side, offered_american_odds FROM historical_recommendations
                    WHERE recommendation_id = ?
                """, (rec_id,)).fetchone()
                if hr:
                    outcome = _determine_outcome(
                        result["final_status"], hr["line"], hr["side"],
                        conn, rec_id,
                    )
                    if outcome:
                        update_pick_outcome(conn, rec_id, outcome)
                        graded_count += 1

    return graded_count


def _determine_outcome(
    final_status: str,
    line: float | None,
    side: str | None,
    conn: sqlite3.Connection,
    rec_id: str,
) -> str | None:
    """Determine pick outcome from event result and recommendation details."""
    stat_row = conn.execute("""
        SELECT final_stat_value FROM player_stat_results
        WHERE event_id = (
            SELECT event_id FROM historical_recommendations WHERE recommendation_id = ?
        )
        AND player_id = (
            SELECT player_id FROM historical_recommendations WHERE recommendation_id = ?
        )
    """, (rec_id, rec_id)).fetchone()

    if stat_row and line is not None and side:
        final_val = stat_row["final_stat_value"]
        if final_val > line:
            return "win" if side.lower() == "over" else "loss"
        elif final_val < line:
            return "loss" if side.lower() == "over" else "win"
        else:
            return "push"
    return None
