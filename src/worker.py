"""Background worker for MLB VIP Model automation.

Runs as a persistent process (or one-shot for cron services).
Handles: schedule refresh, morning scan, pregame checks, grading,
closing-price capture, adaptive learning, backup, stale-job recovery.

Usage:
    # Persistent mode (for always-on services)
    python -m src.worker

    # One-shot mode (for cron services — run all due jobs once)
    python -m src.worker --run-once

    # Run a specific job type
    python -m src.worker --job morning-run
    python -m src.worker --job grading
    python -m src.worker --job backup
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.production_config import load_config
from src.structured_logging import setup_logging

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────

WORKER_HEARTBEAT_INTERVAL = 60  # seconds
STALE_JOB_THRESHOLD_MINUTES = 30
GRADING_CHECK_INTERVAL_MINUTES = 15
PREGAME_CHECK_INTERVAL_MINUTES = 10

TZ_NAME = os.environ.get("MLB_SCHEDULER_TIMEZONE", os.environ.get("MLB_TIMEZONE", "America/New_York"))


def _get_tz():
    """Return the configured timezone object."""
    import zoneinfo
    return zoneinfo.ZoneInfo(TZ_NAME)


def _now_local() -> datetime:
    """Return current time in the configured timezone."""
    return datetime.now(_get_tz())


# ── Job locking ───────────────────────────────────────────────────


def _acquire_lock(conn: sqlite3.Connection, job_type: str) -> str | None:
    """Attempt to acquire a distributed lock for a job type.

    Checks for an existing running lock of the same type before inserting.
    Returns the lock job_id or None if already locked.
    """
    # Check for existing running lock of this type
    existing = conn.execute(
        "SELECT 1 FROM scheduled_jobs "
        "WHERE job_type = ? AND status = 'running' AND metadata = 'worker-lock'",
        (job_type,),
    ).fetchone()
    if existing:
        return None

    lock_key = f"{job_type}_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(
            "INSERT INTO scheduled_jobs (job_id, job_type, status, scheduled_at, metadata) "
            "VALUES (?, ?, 'running', ?, ?)",
            (lock_key, job_type, datetime.now(timezone.utc).isoformat(), "worker-lock"),
        )
        conn.commit()
        return lock_key
    except Exception:
        return None


def _release_lock(conn: sqlite3.Connection, lock_key: str) -> None:
    """Release a job lock by marking it completed."""
    try:
        conn.execute(
            "UPDATE scheduled_jobs SET status = 'completed', completed_at = datetime('now') "
            "WHERE job_id = ? AND status = 'running'",
            (lock_key,),
        )
        conn.commit()
    except sqlite3.Error:
        pass


# ── Heartbeat ─────────────────────────────────────────────────────


def _write_heartbeat(conn: sqlite3.Connection) -> None:
    """Write a worker heartbeat to the database."""
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_heartbeat (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_heartbeat TEXT NOT NULL,
                worker_pid INTEGER,
                uptime_seconds REAL
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO worker_heartbeat (id, last_heartbeat, worker_pid)
            VALUES (1, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), os.getpid()))
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("Failed to write heartbeat: %s", e)


def _read_heartbeat(conn: sqlite3.Connection) -> dict | None:
    """Read the last worker heartbeat."""
    try:
        row = conn.execute(
            "SELECT last_heartbeat, worker_pid FROM worker_heartbeat WHERE id = 1"
        ).fetchone()
        if row:
            return {"last_heartbeat": row[0], "worker_pid": row[1]}
    except sqlite3.Error:
        pass
    return None


# ── Stale job recovery ────────────────────────────────────────────


def _recover_stale_jobs(conn: sqlite3.Connection) -> int:
    """Reset jobs stuck in 'running' for too long."""
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=STALE_JOB_THRESHOLD_MINUTES)).isoformat()
    cursor = conn.execute(
        "UPDATE scheduled_jobs SET status = 'pending', error_message = 'Recovered from stale running state' "
        "WHERE status = 'running' AND started_at < ?",
        (threshold,),
    )
    conn.commit()
    count = cursor.rowcount
    if count:
        logger.info("Recovered %d stale job(s)", count)
    return count


# ── Job execution ─────────────────────────────────────────────────


def _run_morning_scan(config) -> dict:
    """Execute the full morning pipeline."""
    from src.daily_pipeline import run_pipeline
    result = run_pipeline(
        api_key=config.api_key,
        db_path=config.database_path,
        cache_path=config.cache_path,
        output_dir=config.output_dir,
    )
    return {"status": "success" if result.get("exit_code", 1) == 0 else "failed", "result": result}


def _run_pregame_checks(conn: sqlite3.Connection, config) -> dict:
    """Schedule pregame checks for upcoming games."""
    from src.automation import schedule_pregame_checks
    count = schedule_pregame_checks(conn)
    return {"status": "success", "jobs_scheduled": count}


def _run_grading(conn: sqlite3.Connection, config) -> dict:
    """Run grading for completed games."""
    from src.automation import schedule_grading
    count = schedule_grading(conn)
    return {"status": "success", "grading_jobs": count}


def _run_backup(config) -> dict:
    """Create a database backup."""
    from src.backup_database import backup_database
    backup_dir = os.environ.get("MLB_BACKUP_DIR", "backups")
    backup_path = backup_database(
        db_path=config.database_path,
        backup_dir=backup_dir,
        retention_count=config.backup_retention_count,
        compress=config.backup_compression,
    )
    return {"status": "success", "backup_path": str(backup_path)}


def _run_adaptive_learning(conn: sqlite3.Connection, config) -> dict:
    """Collect adaptive learning data."""
    try:
        from src.adaptive_learning import AdaptiveLearningEngine
        engine = AdaptiveLearningEngine(db_path=config.database_path)
        engine.collect_observations(conn)
        return {"status": "success"}
    except Exception as e:
        return {"status": "skipped", "reason": str(e)}


def _run_health_check(config) -> dict:
    """Run health checks."""
    from src.health_check import run_health_checks
    report = run_health_checks(
        db_path=config.database_path,
        api_key=config.api_key,
        output_dir=config.output_dir,
    )
    return {"status": report.overall_status, "report": report.to_dict()}


# ── Job dispatcher ────────────────────────────────────────────────


def _execute_job(job_type: str, conn: sqlite3.Connection, config) -> dict:
    """Dispatch and execute a single job."""
    dispatch = {
        "morning-run": lambda: _run_morning_scan(config),
        "pregame-check": lambda: _run_pregame_checks(conn, config),
        "grading": lambda: _run_grading(conn, config),
        "backup": lambda: _run_backup(config),
        "adaptive-learning": lambda: _run_adaptive_learning(conn, config),
        "health-check": lambda: _run_health_check(config),
        "schedule-refresh": lambda: _run_pregame_checks(conn, config),
    }
    handler = dispatch.get(job_type)
    if not handler:
        return {"status": "skipped", "reason": f"Unknown job type: {job_type}"}
    return handler()


# ── Scheduler loop ────────────────────────────────────────────────


def _process_pending_jobs(conn: sqlite3.Connection, config) -> int:
    """Find and execute all pending jobs. Returns count executed."""
    from src.automation import get_pending_jobs, update_job_status
    pending = get_pending_jobs(conn)
    executed = 0

    for job in pending:
        job_id = job["job_id"]
        job_type = job["job_type"]

        # Try to acquire lock
        lock_key = _acquire_lock(conn, f"exec_{job_id}")
        if not lock_key:
            continue

        try:
            update_job_status(conn, job_id, "running")
            logger.info("Executing job %s (type=%s)", job_id[:8], job_type)

            result = _execute_job(job_type, conn, config)

            if result.get("status") == "success":
                update_job_status(conn, job_id, "completed")
            else:
                update_job_status(conn, job_id, "failed", error_message=str(result))
            executed += 1

        except Exception as e:
            logger.error("Job %s failed: %s", job_id[:8], e)
            update_job_status(conn, job_id, "failed", error_message=str(e))
        finally:
            _release_lock(conn, lock_key)

    return executed


def _check_and_schedule_pregame(conn: sqlite3.Connection) -> None:
    """Check if pregame checks need scheduling (hourly during game windows)."""
    now = _now_local()
    # Only schedule pregame checks between 1 PM and 11 PM ET (game window)
    if 13 <= now.hour <= 22:
        from src.automation import schedule_pregame_checks
        count = schedule_pregame_checks(conn)
        if count:
            logger.info("Scheduled %d pregame check(s)", count)


def _check_and_schedule_grading(conn: sqlite3.Connection) -> None:
    """Check if grading needs scheduling (after games complete)."""
    now = _now_local()
    # Only check for grading between 4 PM and 2 AM ET
    if now.hour >= 16 or now.hour <= 1:
        from src.automation import schedule_grading
        count = schedule_grading(conn)
        if count:
            logger.info("Scheduled %d grading job(s)", count)


def _is_backup_time(now: datetime) -> bool:
    """Check if it's time for the daily backup (3:30 AM ET)."""
    return now.hour == 3 and now.minute == 30


def _check_and_schedule_morning_run(conn: sqlite3.Connection) -> None:
    """Auto-schedule a morning run at ~9 AM ET if none exists for today."""
    from src.automation import create_job
    now = _now_local()
    # Only between 8:30 AM and 9:59 AM ET
    if now.hour < 8 or now.hour > 9 or (now.hour == 8 and now.minute < 30):
        return
    today = now.strftime("%Y-%m-%d")
    existing = conn.execute(
        "SELECT 1 FROM scheduled_jobs "
        "WHERE job_type = 'morning-run' AND scheduled_at LIKE ? "
        "AND status IN ('pending', 'running', 'completed')",
        (f"{today}%",),
    ).fetchone()
    if not existing:
        job_id = create_job(conn, job_type="morning-run", scheduled_at=datetime.now(timezone.utc).isoformat())
        logger.info("Auto-scheduled morning run: %s", job_id[:8])


# ── Main loop ─────────────────────────────────────────────────────


def run_worker_persistent(config) -> None:
    """Run the worker as a persistent background process."""
    logger.info("Starting persistent worker (pid=%d, tz=%s)", os.getpid(), TZ_NAME)

    conn = get_connection(config.database_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    _running = True

    def _handle_signal(signum, frame):
        nonlocal _running
        logger.info("Received signal %d, shutting down...", signum)
        _running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    last_heartbeat = 0
    last_pregame_check = 0
    last_grading_check = 0
    last_morning_check = 0
    last_backup_minute = -1

    while _running:
        try:
            now = time.time()
            now_local = _now_local()

            # Heartbeat
            if now - last_heartbeat >= WORKER_HEARTBEAT_INTERVAL:
                _write_heartbeat(conn)
                last_heartbeat = now

            # Recover stale jobs
            _recover_stale_jobs(conn)

            # Process pending jobs
            _process_pending_jobs(conn, config)

            # Auto-schedule morning run (check once per minute in window)
            if now - last_morning_check >= 60:
                _check_and_schedule_morning_run(conn)
                last_morning_check = now

            # Schedule pregame checks (every 10 min during game window)
            if now - last_pregame_check >= PREGAME_CHECK_INTERVAL_MINUTES * 60:
                _check_and_schedule_pregame(conn)
                last_pregame_check = now

            # Schedule grading checks (every 15 min)
            if now - last_grading_check >= GRADING_CHECK_INTERVAL_MINUTES * 60:
                _check_and_schedule_grading(conn)
                last_grading_check = now

            # Daily backup at 3:30 AM ET
            if _is_backup_time(now_local) and last_backup_minute != now_local.minute:
                try:
                    _run_backup(config)
                    last_backup_minute = now_local.minute
                    logger.info("Daily backup completed")
                except Exception as e:
                    logger.error("Backup failed: %s", e)

            # Adaptive learning data collection (hourly)
            if now_local.minute == 0 and now_local.second < 5:
                _run_adaptive_learning(conn, config)

            time.sleep(10)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("Worker loop error: %s\n%s", e, traceback.format_exc())
            time.sleep(30)

    conn.close()
    logger.info("Worker stopped")


def run_worker_once(config) -> None:
    """Run the worker once (for cron-style execution)."""
    logger.info("Running worker once (one-shot mode)")

    conn = get_connection(config.database_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        _write_heartbeat(conn)
        _recover_stale_jobs(conn)
        executed = _process_pending_jobs(conn, config)
        _check_and_schedule_morning_run(conn)
        _check_and_schedule_pregame(conn)
        _check_and_schedule_grading(conn)

        now_local = _now_local()
        if _is_backup_time(now_local):
            _run_backup(config)

        logger.info("One-shot complete: %d jobs executed", executed)
    finally:
        conn.close()


def run_specific_job(job_type: str, config) -> None:
    """Run a single specific job type."""
    logger.info("Running specific job: %s", job_type)

    conn = get_connection(config.database_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    try:
        result = _execute_job(job_type, conn, config)
        logger.info("Job %s result: %s", job_type, result)
    finally:
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="MLB VIP Background Worker")
    parser.add_argument("--run-once", action="store_true", help="Run once and exit")
    parser.add_argument("--job", type=str, help="Run a specific job type")
    args = parser.parse_args()

    setup_logging()

    config = load_config()
    errors = config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    if args.job:
        run_specific_job(args.job, config)
    elif args.run_once:
        run_worker_once(config)
    else:
        run_worker_persistent(config)


if __name__ == "__main__":
    main()
