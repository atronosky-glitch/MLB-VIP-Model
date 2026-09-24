"""Regression tests for the production retry storm found in the launch audit
(2026-09-23/24): from 8 PM ET the morning-run dedup could no longer see its own
earlier job (UTC date rolled over) and queued a new run every minute, ~360
failed runs in 3 days; league scans also re-queued every 30 minutes after a
failure with no backoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import src.worker as worker

ET = ZoneInfo("America/New_York")


def _jobs(conn, job_type):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM scheduled_jobs WHERE job_type = ? ORDER BY created_at", (job_type,)).fetchall()]


def _at(monkeypatch, year, month, day, hour, minute=0):
    fixed = datetime(year, month, day, hour, minute, tzinfo=ET)
    monkeypatch.setattr(worker, "_now_local", lambda: fixed)
    return fixed


class TestMorningRunEveningDedup:
    def test_evening_checks_never_stack_jobs_after_the_utc_date_rolls_over(self, db_conn, monkeypatch):
        _at(monkeypatch, 2026, 9, 23, 9, 30)
        worker._check_and_schedule_morning_run(db_conn)
        assert len(_jobs(db_conn, "morning-run")) == 1

        # the run fails mid-morning; the scheduler may retry after the 60 min cooldown
        done = datetime(2026, 9, 23, 9, 40, tzinfo=ET).astimezone(timezone.utc).isoformat()
        db_conn.execute("UPDATE scheduled_jobs SET status = 'failed', completed_at = ? WHERE job_type = 'morning-run'", (done,))
        db_conn.commit()

        # 9 PM ET == 01:00 UTC on the NEXT date. The loop checks once a minute.
        for minute in range(0, 30):
            _at(monkeypatch, 2026, 9, 23, 21, minute)
            worker._check_and_schedule_morning_run(db_conn)
        jobs = _jobs(db_conn, "morning-run")
        assert len(jobs) == 2, f"expected the original + exactly one retry, got {len(jobs)}"   # was ~30
        assert sum(1 for j in jobs if j["status"] == "pending") == 1

    def test_a_completed_run_is_never_rescheduled_in_the_evening(self, db_conn, monkeypatch):
        _at(monkeypatch, 2026, 9, 23, 9, 30)
        worker._check_and_schedule_morning_run(db_conn)
        db_conn.execute("UPDATE scheduled_jobs SET status = 'completed', completed_at = ? WHERE job_type = 'morning-run'",
                        (datetime.now(timezone.utc).isoformat(),))
        db_conn.commit()
        for hour in (12, 18, 20, 21, 22, 23):
            _at(monkeypatch, 2026, 9, 23, hour, 15)
            worker._check_and_schedule_morning_run(db_conn)
        assert len(_jobs(db_conn, "morning-run")) == 1

    def test_next_local_day_schedules_a_fresh_run(self, db_conn, monkeypatch):
        _at(monkeypatch, 2026, 9, 23, 9, 30)
        worker._check_and_schedule_morning_run(db_conn)
        db_conn.execute("UPDATE scheduled_jobs SET status = 'completed', completed_at = ?", (datetime.now(timezone.utc).isoformat(),))
        db_conn.commit()
        _at(monkeypatch, 2026, 9, 24, 9, 30)
        worker._check_and_schedule_morning_run(db_conn)
        assert len(_jobs(db_conn, "morning-run")) == 2

    def test_before_the_morning_window_nothing_is_scheduled(self, db_conn, monkeypatch):
        _at(monkeypatch, 2026, 9, 23, 7, 0)
        worker._check_and_schedule_morning_run(db_conn)
        assert _jobs(db_conn, "morning-run") == []


class TestFailedScanCooldown:
    def test_a_recent_failure_blocks_requeue_until_the_cooldown_passes(self, db_conn):
        assert worker._create_job_if_not_queued(db_conn, "morning-run-nfl", failure_cooldown_minutes=60)
        just_now = datetime.now(timezone.utc).isoformat()
        db_conn.execute("UPDATE scheduled_jobs SET status = 'failed', completed_at = ? WHERE job_type = 'morning-run-nfl'", (just_now,))
        db_conn.commit()
        assert worker._create_job_if_not_queued(db_conn, "morning-run-nfl", failure_cooldown_minutes=60) is None

        old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        db_conn.execute("UPDATE scheduled_jobs SET completed_at = ? WHERE job_type = 'morning-run-nfl'", (old,))
        db_conn.commit()
        assert worker._create_job_if_not_queued(db_conn, "morning-run-nfl", failure_cooldown_minutes=60)

    def test_without_a_cooldown_behaviour_is_unchanged_and_success_never_blocks(self, db_conn):
        assert worker._create_job_if_not_queued(db_conn, "arb-middle-scan")
        assert worker._create_job_if_not_queued(db_conn, "arb-middle-scan") is None            # pending
        db_conn.execute("UPDATE scheduled_jobs SET status = 'completed', completed_at = ?",
                        (datetime.now(timezone.utc).isoformat(),))
        db_conn.commit()
        assert worker._create_job_if_not_queued(db_conn, "arb-middle-scan", failure_cooldown_minutes=60)

    def test_league_schedulers_use_the_cooldown(self):
        import inspect
        for fn in (worker._check_and_schedule_nfl, worker._check_and_schedule_cfb):
            src = inspect.getsource(fn)
            assert "failure_cooldown_minutes=FAILED_SCAN_RETRY_COOLDOWN_MINUTES" in src
