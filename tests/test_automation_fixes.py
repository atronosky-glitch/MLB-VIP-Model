"""Regression tests for automation fixes:

- pregame job-type consistency (``pregame`` vs ``pregame-check``)
- worker honoring ``scheduled_at`` when executing pending jobs
- maintenance cleanup of stale failed/pending/lock rows
- next-morning-run fallback in automation status
"""

from datetime import datetime, timedelta, timezone

import src.worker as worker
from src.automation import (
    create_job,
    get_automation_status,
    schedule_pregame_checks,
    trigger_pregame_refresh,
)


def _job_types(conn) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT job_type, status FROM scheduled_jobs"
    ).fetchall()
    return [(r["job_type"], r["status"]) for r in rows]


class TestPregameJobTypeConsistency:
    """Pregame jobs must use the job type the worker dispatcher handles."""

    def test_schedule_pregame_checks_uses_pregame_check(self, db_conn):
        start_time = (
            datetime.now(timezone.utc) + timedelta(hours=3)
        ).isoformat()
        db_conn.execute(
            "INSERT INTO games (event_id, away_team, home_team, start_time, status) "
            "VALUES ('ev_pc', 'NYY', 'BOS', ?, 'scheduled')",
            (start_time,),
        )
        db_conn.commit()
        count = schedule_pregame_checks(db_conn)
        assert count == 1
        types = [t for t, _ in _job_types(db_conn)]
        assert "pregame-check" in types
        assert "pregame" not in types

    def test_trigger_pregame_refresh_uses_pregame_check(self, db_conn):
        job_id = trigger_pregame_refresh(db_conn, "ev1")
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["job_type"] == "pregame-check"

    def test_execute_pregame_check_dispatches(self, db_conn):
        result = worker._execute_job("pregame-check", db_conn, None)
        assert result["status"] == "success"

    def test_legacy_pregame_type_no_longer_dispatched(self, db_conn):
        result = worker._execute_job("pregame", db_conn, None)
        assert result["status"] == "skipped"


class TestProcessPendingJobsDueTime:
    """Worker must not run jobs before their scheduled time."""

    def test_future_job_is_not_executed(self, db_conn):
        future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        job_id = create_job(db_conn, "test-unknown", scheduled_at=future)
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 0
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "pending"

    def test_due_job_is_executed(self, db_conn):
        past = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        job_id = create_job(db_conn, "test-unknown", scheduled_at=past)
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 1
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"  # unknown type -> marked failed

    def test_unparseable_schedule_is_treated_as_due(self, db_conn):
        job_id = create_job(db_conn, "test-unknown", scheduled_at="not-a-date")
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 1
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"


class TestMaintenanceCleanup:
    """Daily cleanup bounds scheduled_jobs growth."""

    def test_deletes_old_failed_and_stale_pending(self, db_conn):
        old_failed = create_job(db_conn, "pregame-check")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'failed', completed_at = ? "
            "WHERE job_id = ?",
            ("2026-01-01T00:00:00+00:00", old_failed),
        )
        stale_pending = create_job(
            db_conn, "pregame-check", scheduled_at="2026-01-01T00:00:00+00:00"
        )
        recent_pending = create_job(
            db_conn,
            "pregame-check",
            scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        db_conn.commit()

        removed = worker._maintenance_cleanup(db_conn)
        assert removed >= 2

        rows = db_conn.execute("SELECT job_id FROM scheduled_jobs").fetchall()
        remaining = {r["job_id"] for r in rows}
        assert old_failed not in remaining
        assert stale_pending not in remaining
        assert recent_pending in remaining

    def test_keeps_recent_failed(self, db_conn):
        recent_failed = create_job(db_conn, "pregame-check")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'failed', completed_at = ? "
            "WHERE job_id = ?",
            (datetime.now(timezone.utc).isoformat(), recent_failed),
        )
        db_conn.commit()
        worker._maintenance_cleanup(db_conn)
        rows = db_conn.execute("SELECT job_id FROM scheduled_jobs").fetchall()
        assert recent_failed in {r["job_id"] for r in rows}


class TestAutomationStatusNextMorning:
    """Dashboard shows a future next-morning-run time."""

    def test_next_morning_fallback_is_future(self, db_conn):
        status = get_automation_status(db_conn)
        assert status["next_morning_run"] is not None
        dt = datetime.fromisoformat(status["next_morning_run"].replace("Z", "+00:00"))
        assert dt > datetime.now(timezone.utc)

    def test_next_morning_prefers_pending_job(self, db_conn):
        scheduled = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        create_job(db_conn, "morning-run", scheduled_at=scheduled)
        status = get_automation_status(db_conn)
        assert status["next_morning_run"] == scheduled
