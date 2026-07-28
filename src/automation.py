"""Automation Service Layer.

Provides the service layer for automated jobs: morning run,
pregame checks, and postgame grading. Also provides manual
trigger functions for the Streamlit dashboard.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta


# ── Job management ─────────────────────────────────────────────────


def create_job(
    conn: sqlite3.Connection,
    job_type: str,
    scheduled_at: str | None = None,
    event_id: str | None = None,
    metadata: str | None = None,
) -> str:
    """Create a scheduled job. Returns job_id."""
    job_id = str(uuid.uuid4())
    if not scheduled_at:
        scheduled_at = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO scheduled_jobs (
            job_id, job_type, status, scheduled_at, event_id, metadata
        ) VALUES (?, ?, 'pending', ?, ?, ?)
    """, (job_id, job_type, scheduled_at, event_id, metadata))
    conn.commit()
    return job_id


def update_job_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    """Update job status."""
    conn.execute("""
        UPDATE scheduled_jobs SET
            status = ?,
            error_message = ?,
            started_at = CASE WHEN ? = 'running' THEN datetime('now') ELSE started_at END,
            completed_at = CASE WHEN ? IN ('completed', 'failed') THEN datetime('now') ELSE completed_at END
        WHERE job_id = ?
    """, (status, error_message, status, status, job_id))
    conn.commit()


def get_pending_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Get all pending jobs."""
    rows = conn.execute(
        "SELECT * FROM scheduled_jobs WHERE status = 'pending' ORDER BY scheduled_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_failed_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Get all failed jobs."""
    rows = conn.execute(
        "SELECT * FROM scheduled_jobs WHERE status = 'failed' ORDER BY scheduled_at DESC LIMIT 50"
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_jobs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Get recent jobs of any status."""
    rows = conn.execute(
        "SELECT * FROM scheduled_jobs ORDER BY scheduled_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def retry_failed_jobs(conn: sqlite3.Connection) -> int:
    """Reset failed jobs to pending. Returns count."""
    cursor = conn.execute(
        "UPDATE scheduled_jobs SET status = 'pending', error_message = NULL "
        "WHERE status = 'failed'"
    )
    conn.commit()
    return cursor.rowcount


# ── Morning job scheduling ─────────────────────────────────────────


def schedule_pregame_checks(
    conn: sqlite3.Connection,
    source_run_id: str | None = None,
) -> int:
    """Create one pregame job per relevant upcoming game.

    Target time = scheduled game start - 60 minutes.
    Returns count of jobs created.
    """
    upcoming = conn.execute("""
        SELECT event_id, start_time, away_team, home_team
        FROM games
        WHERE status = 'scheduled'
        AND date(start_time) = date('now')
    """).fetchall()

    count = 0
    for game in upcoming:
        event_id = game["event_id"]
        start_time = game["start_time"]

        # Dedup: skip if already has a pending pregame job
        existing = conn.execute(
            "SELECT 1 FROM scheduled_jobs "
            "WHERE event_id = ? AND job_type = 'pregame' AND status IN ('pending','running')",
            (event_id,),
        ).fetchone()
        if existing:
            continue

        # Calculate target time
        try:
            game_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            target = game_dt - timedelta(minutes=60)
            if target < datetime.now(timezone.utc):
                continue  # Game starts within 60 minutes, skip
            scheduled_at = target.isoformat()
        except (ValueError, TypeError):
            continue

        create_job(
            conn,
            job_type="pregame",
            scheduled_at=scheduled_at,
            event_id=event_id,
            metadata=f"{game['away_team']} @ {game['home_team']}",
        )
        count += 1

    return count


def schedule_grading(
    conn: sqlite3.Connection,
) -> int:
    """Create grading jobs for completed games that haven't been graded."""
    completed = conn.execute("""
        SELECT g.event_id, g.away_team, g.home_team
        FROM games g
        WHERE g.status IN ('final', 'completed')
        AND NOT EXISTS (
            SELECT 1 FROM scheduled_jobs sj
            WHERE sj.event_id = g.event_id
            AND sj.job_type = 'grading'
            AND sj.status IN ('completed', 'running')
        )
    """).fetchall()

    count = 0
    for game in completed:
        create_job(
            conn,
            job_type="grading",
            event_id=game["event_id"],
            metadata=f"{game['away_team']} @ {game['home_team']}",
        )
        count += 1
    return count


# ── Manual triggers ────────────────────────────────────────────────


def trigger_morning_run(conn: sqlite3.Connection) -> str:
    """Create an immediate morning run job."""
    return create_job(conn, job_type="morning")


def trigger_pregame_refresh(conn: sqlite3.Connection, event_id: str) -> str:
    """Create an immediate pregame check job for a specific game."""
    return create_job(conn, job_type="pregame", event_id=event_id)


def trigger_grading(conn: sqlite3.Connection) -> str:
    """Create an immediate grading job."""
    return create_job(conn, job_type="grading")


def get_automation_status(conn: sqlite3.Connection) -> dict:
    """Get summary status of automation system."""
    last_morning = conn.execute(
        "SELECT completed_at FROM scheduled_jobs "
        "WHERE job_type = 'morning' AND status = 'completed' "
        "ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()

    next_morning = conn.execute(
        "SELECT scheduled_at FROM scheduled_jobs "
        "WHERE job_type = 'morning' AND status = 'pending' "
        "ORDER BY scheduled_at ASC LIMIT 1"
    ).fetchone()

    pending_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM scheduled_jobs WHERE status = 'pending'"
    ).fetchone()["cnt"]

    failed_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM scheduled_jobs WHERE status = 'failed'"
    ).fetchone()["cnt"]

    pending_pregame = conn.execute(
        "SELECT COUNT(*) AS cnt FROM scheduled_jobs "
        "WHERE job_type = 'pregame' AND status = 'pending'"
    ).fetchone()["cnt"]

    last_grading = conn.execute(
        "SELECT completed_at FROM scheduled_jobs "
        "WHERE job_type = 'grading' AND status = 'completed' "
        "ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()

    return {
        "scheduler_enabled": True,
        "next_morning_run": next_morning["scheduled_at"] if next_morning else None,
        "last_morning_run": last_morning["completed_at"] if last_morning else None,
        "pending_pregame_checks": pending_pregame,
        "last_grading_run": last_grading["completed_at"] if last_grading else None,
        "pending_jobs": pending_count,
        "failed_jobs": failed_count,
    }
