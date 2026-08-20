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
from database.db_manager import get_connection
from database.connection import DB, get_connection_dialect_name, get_database_url

logger = logging.getLogger(__name__)

# ── API quota alert state (avoid spam) ───────────────────────────
_last_quota_alert_date: str = ""

# ── Configuration ─────────────────────────────────────────────────

WORKER_HEARTBEAT_INTERVAL = 60  # seconds
STALE_JOB_THRESHOLD_MINUTES = 30
GRADING_CHECK_INTERVAL_MINUTES = 15
PREGAME_CHECK_INTERVAL_MINUTES = 10
# NFL/WNBA scheduling-CHECK cadence — how often the worker loop asks "is
# a job due yet". This is deliberately coarser than MLB's, and separate
# from the actual job cadence those checks decide (see
# src/league_schedule.py) — checking every 20-30 min is plenty responsive
# for kickoff/tip-off windows measured in hours, and keeps schedule
# discovery calls (NFL: rate-limited API; WNBA: free but still a network
# call) from firing every worker tick.
NFL_SCHEDULE_CHECK_INTERVAL_MINUTES = 30
WNBA_SCHEDULE_CHECK_INTERVAL_MINUTES = 20

TZ_NAME = os.environ.get("MLB_SCHEDULER_TIMEZONE", os.environ.get("MLB_TIMEZONE", "America/New_York"))


def _get_tz():
    """Return the configured timezone object."""
    import zoneinfo
    return zoneinfo.ZoneInfo(TZ_NAME)


def _now_local() -> datetime:
    """Return current time in the configured timezone."""
    return datetime.now(_get_tz())


# ── Job locking ───────────────────────────────────────────────────


def _acquire_lock(conn: DB, job_type: str) -> str | None:
    """Attempt to acquire a distributed lock for a job type.

    The SELECT below is a fast-path only — under real concurrency two
    callers can both pass it before either inserts. Correctness comes from
    the partial unique index ``idx_sj_running_lock`` on
    ``scheduled_jobs(job_type) WHERE status='running' AND metadata='worker-lock'``
    (see ``database/db_manager.py::init_db``): a second concurrent INSERT
    for the same job_type violates that constraint and raises, which the
    ``except Exception`` below already turns into "lock not acquired".
    Returns the lock job_id or None if already locked.
    """
    # Fast-path check for an existing running lock of this type
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


def _release_lock(conn, lock_key: str) -> None:
    """Release a job lock by marking it completed."""
    try:
        conn.execute(
            "UPDATE scheduled_jobs SET status = 'completed', completed_at = datetime('now') "
            "WHERE job_id = ? AND status = 'running'",
            (lock_key,),
        )
        conn.commit()
    except Exception:
        pass


# ── Heartbeat ─────────────────────────────────────────────────────


def _write_heartbeat(conn) -> None:
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
        ts = datetime.now(timezone.utc).isoformat()
        pid = os.getpid()
        if get_connection_dialect_name(conn) == "postgresql":
            conn.execute("""
                INSERT INTO worker_heartbeat (id, last_heartbeat, worker_pid)
                VALUES (1, %s, %s)
                ON CONFLICT (id) DO UPDATE SET last_heartbeat = EXCLUDED.last_heartbeat,
                                                worker_pid = EXCLUDED.worker_pid
            """, (ts, pid))
        else:
            conn.execute("""
                INSERT OR REPLACE INTO worker_heartbeat (id, last_heartbeat, worker_pid)
                VALUES (1, ?, ?)
            """, (ts, pid))
        conn.commit()
    except Exception as e:
        logger.warning("Failed to write heartbeat: %s", e)


def _read_heartbeat(conn) -> dict | None:
    """Read the last worker heartbeat."""
    try:
        row = conn.execute(
            "SELECT last_heartbeat, worker_pid FROM worker_heartbeat WHERE id = 1"
        ).fetchone()
        if row:
            return {"last_heartbeat": row["last_heartbeat"], "worker_pid": row["worker_pid"]}
    except Exception:
        pass
    return None


# ── Stale job recovery ────────────────────────────────────────────


def _recover_stale_jobs(conn: DB) -> int:
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


FAILED_JOB_RETENTION_DAYS = 7
STALE_PENDING_HOURS = 24


def _maintenance_cleanup(conn: DB) -> int:
    """Delete old failed jobs, stale pending jobs, and old worker-lock rows.

    Bounds the ``scheduled_jobs`` table so counters and storage don't grow
    without limit. Runs once per day from the worker loop.
    """
    now = datetime.now(timezone.utc)
    failed_cutoff = (now - timedelta(days=FAILED_JOB_RETENTION_DAYS)).strftime("%Y-%m-%d")
    pending_cutoff = (now - timedelta(hours=STALE_PENDING_HOURS)).isoformat()
    total = 0

    cursor = conn.execute(
        "DELETE FROM scheduled_jobs WHERE status = 'failed' AND completed_at IS NOT NULL AND completed_at < ?",
        (failed_cutoff,),
    )
    conn.commit()
    total += cursor.rowcount

    cursor = conn.execute(
        "DELETE FROM scheduled_jobs WHERE status = 'pending' AND scheduled_at IS NOT NULL AND scheduled_at < ?",
        (pending_cutoff,),
    )
    conn.commit()
    total += cursor.rowcount

    cursor = conn.execute(
        "DELETE FROM scheduled_jobs WHERE metadata = 'worker-lock' AND created_at < ?",
        (failed_cutoff,),
    )
    conn.commit()
    total += cursor.rowcount

    if total:
        logger.info("Maintenance cleanup removed %d stale job(s)", total)
    return total


# ── Job execution ─────────────────────────────────────────────────


def _run_morning_scan(config, league: str = "MLB") -> dict:
    """Execute the full morning pipeline for *league*.

    Defaults to MLB, matching current production scheduling (one worker
    service per league, e.g. ``mlb-vip-worker``). A future multi-league
    scheduler can call this per league with its own cadence — NFL runs
    weekly, not daily, so that is a scheduling-policy decision left for
    when a second league is actually put into production, not a data
    engine gap.
    """
    from src.daily_pipeline import run_pipeline, PipelineConfig
    pipeline_config = PipelineConfig(
        output_dir=config.output_dir, live=True,
        challenger_shadow=(league == "MLB"),  # challenger models are MLB-only research tools
        league=league,
    )
    exit_code = run_pipeline(pipeline_config)
    return {"status": "success" if exit_code == 0 else "failed", "exit_code": exit_code}


def _initialize_worker_schema(config) -> None:
    """Initialize the complete schema before any worker database activity."""
    from database.db_manager import init_db

    try:
        init_db(config.database_path)
    except Exception as exc:
        logger.exception("Worker database schema initialization failed")
        raise RuntimeError("Worker database schema initialization failed") from exc


def _run_pregame_checks(conn: DB, config) -> dict:
    """Schedule pregame checks for upcoming games."""
    from src.automation import schedule_pregame_checks
    count = schedule_pregame_checks(conn)
    return {"status": "success", "jobs_scheduled": count}


def _run_pregame_scan(conn: DB, config, event_id: str | None = None, league: str = "MLB") -> dict:
    """Run a scheduled pregame pipeline so it records a scan_run.

    Lock is scoped per-league (``pregame-pipeline-{league}``) — a
    long-running MLB pregame pipeline must never block an NFL or WNBA one
    that becomes due at the same time, and vice versa.
    """
    from src.daily_pipeline import run_pipeline, PipelineConfig

    lock_key = _acquire_lock(conn, f"pregame-pipeline-{league}")
    if not lock_key:
        logger.warning("[%s] PREGAME JOB SKIPPED reason=another_pregame_running event_id=%s", league, event_id)
        return {"status": "success", "skipped": True, "reason": "another_pregame_running"}
    started = time.monotonic()
    logger.info("[%s] PREGAME JOB START event_id=%s games_targeted=%s", league, event_id, 1 if event_id else "all")
    try:
        exit_code = run_pipeline(PipelineConfig(
            live=True,
            use_cache=False,
            auto=True,
            output_dir=config.output_dir,
            actionable_only=True,
            lifecycle_snapshot_kind="pregame",
            event_id=event_id,
            challenger_shadow=(league == "MLB"),  # challenger models are MLB-only research tools
            league=league,
        ))
        result = {
            "status": "success" if exit_code in (0, 1) else "failed",
            "exit_code": exit_code,
        }
        logger.info(
            "[%s] PREGAME JOB COMPLETE elapsed=%.1fs event_id=%s games_targeted=%s exit_code=%s",
            league, time.monotonic() - started, event_id, 1 if event_id else "all", exit_code,
        )
        return result
    finally:
        _release_lock(conn, lock_key)


def _run_wnba_odds_scan(config) -> dict:
    """WNBA game-market-only scan (moneyline/spread/total) — a flat,
    cheap 3-credit call regardless of slate size. Never fetches props;
    see _run_wnba_props_scan for that, gated much harder on credits."""
    from src.daily_pipeline import run_pipeline, PipelineConfig
    exit_code = run_pipeline(PipelineConfig(
        output_dir=config.output_dir, live=True, challenger_shadow=False,
        league="WNBA", fetch_props=False,
    ))
    return {"status": "success" if exit_code in (0, 1) else "failed", "exit_code": exit_code}


def _run_wnba_props_scan(config) -> dict:
    """WNBA player-prop scan — the credit-expensive path. Only reached
    when src.league_schedule.wnba_should_fetch_props already said yes
    (checked again against real-time budget inside fetch_and_parse_props
    itself, via src.odds_api_credits.credit_budget_check, as a second
    line of defense against a stale scheduling decision)."""
    from src.daily_pipeline import run_pipeline, PipelineConfig
    exit_code = run_pipeline(PipelineConfig(
        output_dir=config.output_dir, live=True, challenger_shadow=False,
        league="WNBA", fetch_props=True,
    ))
    return {"status": "success" if exit_code in (0, 1) else "failed", "exit_code": exit_code}


def _run_grading(conn: DB, config) -> dict:
    """Catch up grading from verified stored final-stat results."""
    from src.automation import schedule_grading

    catchup = _run_catchup_grading(conn)
    scheduled = schedule_grading(conn)
    return {
        "status": "success",
        **catchup,
        "grading_jobs": scheduled,
    }


def _run_catchup_grading(conn: DB) -> dict:
    """Fetch authoritative results per league, then grade unresolved recommendations.

    Every league's ``ingest_results_for_recommendations`` shares the same
    interface (``src/sports/<league>.py::get_settlement_module()``), so
    unresolved recommendations are grouped by their ``league`` column and
    each group is routed to its own verified results source (MLB StatsAPI,
    ESPN NFL, ESPN WNBA). A league with no settlement module is skipped
    rather than guessed.

    Each league's ingest runs inside its own try/except: a result-source
    outage or API exhaustion for one league (e.g. WNBA hitting its
    monthly credit ceiling) must never prevent MLB/NFL's ingest — or the
    grading passes below, which cover every league at once — from
    running. A single unhandled exception here previously meant one
    league's failure could silently stop grading for all three.
    """
    from database.db_manager import get_unsettled_recommendations
    from src.automatic_grading import grade_available_recommendations, grade_available_game_recommendations
    from src.sports import get_league

    unresolved = get_unsettled_recommendations(conn)
    by_league: dict[str, list] = {}
    for rec in unresolved:
        by_league.setdefault(rec.get("league") or "MLB", []).append(rec)

    results_by_league: dict[str, dict] = {}
    for league, recs in by_league.items():
        try:
            settlement_module = get_league(league).get_settlement_module()
        except ValueError:
            settlement_module = None
        if settlement_module is None:
            results_by_league[league] = {"skipped": True, "recommendations": len(recs)}
            continue
        try:
            results_by_league[league] = settlement_module.ingest_results_for_recommendations(conn, recs)
        except Exception:
            logger.exception("[%s] Result ingestion failed — other leagues continue", league)
            results_by_league[league] = {"error": True, "recommendations": len(recs)}

    try:
        result = grade_available_recommendations(conn)
    except Exception:
        logger.exception("Player-prop grading pass failed")
        result = {"graded": 0, "error": True}
    try:
        game_result = grade_available_game_recommendations(conn)
    except Exception:
        logger.exception("Game-market grading pass failed")
        game_result = {"graded": 0, "error": True}

    combined = {"results": results_by_league, "grading": result, "game_grading": game_result}
    total_facts_saved = sum(r.get("facts_saved", 0) for r in results_by_league.values())
    if result.get("graded") or game_result.get("graded") or total_facts_saved:
        logger.info("Automatic grading catch-up: %s", combined)
    return combined


def _run_backup(config) -> dict:
    """Create a database backup.

    ``backup_database`` uses the SQLite online-backup API and only makes
    sense for the local SQLite database. Production runs PostgreSQL
    (``DATABASE_URL`` set), which Render backs up externally — calling
    the SQLite backup path there would silently create an empty file at
    ``config.database_path`` (a stale SQLite default, not the real
    production data) and report false success.
    """
    if get_database_url():
        logger.info("Skipping SQLite backup: PostgreSQL backups are managed externally")
        return {"status": "skipped", "reason": "postgresql_managed_externally"}

    from src.backup_database import backup_database
    backup_dir = os.environ.get("MLB_BACKUP_DIR", "backups")
    backup_path = backup_database(
        db_path=config.database_path,
        backup_dir=backup_dir,
        retention_count=config.backup_retention_count,
        compress=config.backup_compression,
    )
    return {"status": "success", "backup_path": str(backup_path)}


def _run_adaptive_learning(conn: DB, config) -> dict:
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
        freshness_threshold=config.freshness_threshold_seconds,
        environment=config.environment,
        timezone_name=config.timezone,
        backup_dir=config.backup_dir,
        scheduler_enabled=config.scheduler_enabled,
        scheduling_pregame_interval_minutes=config.scheduling_pregame_interval_minutes,
    )
    return {"status": report.overall_status, "report": report.to_dict()}


# ── API quota alerts ──────────────────────────────────────────────


def _check_api_quota_and_alert(conn: DB, config) -> None:
    """Check API quota usage and send a Discord alert if running low or key has failed.

    Only sends one alert per day to avoid spam.
    """
    global _last_quota_alert_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_quota_alert_date == today:
        return  # already alerted today

    try:
        # Check quota from api_usage table
        from src.api_usage import check_quota_warning, USAGE_TABLE
        quota = check_quota_warning(conn, daily_limit=1000.0, warn_pct=80.0)
        messages = []

        if quota:
            messages.append(
                f"**API Quota Warning**\n"
                f"Usage: {quota['used']:.0f}/{quota['limit']:.0f} ({quota['pct']}%)\n"
                f"Key will need renewal soon."
            )

        # Check for 401/403 responses indicating a dead key
        failed = conn.execute(f"""
            SELECT COUNT(*) as cnt FROM {USAGE_TABLE}
            WHERE request_timestamp LIKE '{today}%'
            AND http_status IN (401, 403)
        """).fetchone()
        if failed and failed[0] > 0:
            messages.append(
                f"**API Key Failure**\n"
                f"{failed[0]} requests returned HTTP 401/403 today.\n"
                f"The API key may be invalid or out of credits."
            )

        if not messages:
            return

        alert_text = "\n\n".join(messages)

        # Send to all configured Discord webhooks
        if config.discord_webhook_urls:
            from src.discord_delivery import send_webhook_message
            urls = [u.strip() for u in config.discord_webhook_urls.split(",") if u.strip()]
            for url in urls:
                send_webhook_message(url, alert_text, embed_color=0xFF0000)
            logger.warning("API quota alert sent to Discord: %s", alert_text[:120])
        else:
            logger.warning("API quota alert (no Discord configured): %s", alert_text[:120])

        _last_quota_alert_date = today
    except Exception as e:
        logger.debug("Quota check skipped: %s", e)


# ── Job dispatcher ────────────────────────────────────────────────


def _execute_job(job_type: str, conn: DB, config, event_id: str | None = None) -> dict:
    """Dispatch and execute a single job.

    NFL/WNBA job types are separate entries rather than a league
    parameter on the shared ones — each league's cadence and credit
    profile is different enough (see src/league_schedule.py) that
    keeping them distinct in scheduled_jobs makes the job history/logs
    self-explanatory without needing to inspect metadata.
    """
    dispatch = {
        "morning-run": lambda: _run_morning_scan(config),
        "pregame-check": lambda: _run_pregame_scan(conn, config, event_id),
        "morning-run-nfl": lambda: _run_morning_scan(config, league="NFL"),
        "pregame-check-nfl": lambda: _run_pregame_scan(conn, config, event_id, league="NFL"),
        "wnba-odds-scan": lambda: _run_wnba_odds_scan(config),
        "wnba-props-scan": lambda: _run_wnba_props_scan(config),
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


def _process_pending_jobs(conn: DB, config) -> int:
    """Find and execute all due pending jobs. Returns count executed.

    Jobs scheduled for a future time are skipped until their scheduled_at
    has been reached. Jobs with no scheduled_at run immediately.
    """
    from src.automation import get_pending_jobs, update_job_status
    pending = get_pending_jobs(conn)
    executed = 0
    now = datetime.now(timezone.utc)

    for job in pending:
        job_id = job["job_id"]
        job_type = job["job_type"]

        # Only run jobs whose scheduled time has been reached.
        scheduled_at = job.get("scheduled_at")
        if scheduled_at:
            try:
                sched_dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                if sched_dt.tzinfo is None:
                    sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                if sched_dt > now:
                    continue
            except (ValueError, TypeError):
                pass  # Unparseable schedule → treat as due

        # Try to acquire lock
        lock_key = _acquire_lock(conn, f"exec_{job_id}")
        if not lock_key:
            continue

        try:
            update_job_status(conn, job_id, "running")
            logger.info("Executing job %s (type=%s)", job_id[:8], job_type)

            result = _execute_job(job_type, conn, config, job.get("event_id"))

            ts = datetime.now(timezone.utc).isoformat()
            if result.get("status") == "success":
                conn.execute(
                    "UPDATE scheduled_jobs SET status = 'completed', completed_at = ? WHERE job_id = ?",
                    (ts, job_id),
                )
                conn.commit()
            else:
                conn.execute(
                    "UPDATE scheduled_jobs SET status = 'failed', error_message = ?, completed_at = ? WHERE job_id = ?",
                    (str(result), ts, job_id),
                )
                conn.commit()
            executed += 1

        except Exception as e:
            logger.error("Job %s failed: %s", job_id[:8], e)
            ts = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "UPDATE scheduled_jobs SET status = 'failed', error_message = ?, completed_at = ? WHERE job_id = ?",
                    (str(e), ts, job_id),
                )
                conn.commit()
            except Exception:
                pass
        finally:
            _release_lock(conn, lock_key)

    return executed


def _discover_nfl_game_times() -> list:
    """Discover today's/upcoming NFL kickoff times via the same
    SportsGameOdds client/cache MLB and NFL scans already use — no extra
    rate cost beyond what a normal scan would incur, since the client's
    own disk cache absorbs repeated calls inside its TTL. Real schedule,
    never a day-of-week assumption — see src/league_schedule.py.

    There is no "date" query parameter on this API (verified live
    2026-08-20 — see SportsGameOddsClient.get_events's docstring);
    without an explicit startsAfter/startsBefore window the API returns
    an arbitrary default page, not "this week's games". A 9-day window
    (1 day back, 8 ahead) is wide enough to always see the full current
    NFL week regardless of which day this check runs on.
    """
    from src.api_client import SportsGameOddsClient
    from src.league_schedule import extract_game_start_times
    try:
        client = SportsGameOddsClient(max_cache_age=900.0)
        now = _now_local().astimezone(timezone.utc)
        data, _from_cache = client.get_events(
            league="NFL", odds_available=False,
            starts_after=(now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            starts_before=(now + timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        events = data.get("data", data.get("events", [])) or []
        return extract_game_start_times(events)
    except Exception:
        logger.warning("[NFL] Could not discover game schedule", exc_info=True)
        return []


def _discover_wnba_game_times() -> list:
    """Discover today's/upcoming WNBA game times via the FREE /events
    endpoint (0 credits, confirmed live — see src/odds_api_client.py) —
    safe to call often regardless of the monthly credit budget."""
    from src.odds_api_client import OddsAPIClient, OddsAPIKeyError
    from src.league_schedule import extract_game_start_times
    try:
        client = OddsAPIClient()
        events, _from_cache = client.get_events()
        return extract_game_start_times(events)
    except OddsAPIKeyError:
        return []  # no WNBA key configured — silently unavailable, not an error
    except Exception:
        logger.warning("[WNBA] Could not discover game schedule", exc_info=True)
        return []


def _get_last_completed_job_at(conn: DB, job_type: str):
    """Most recent completion time for a job_type, or None. Backs the
    "last_fetch"/"last_check" inputs src/league_schedule.py's decision
    functions need — persisted in scheduled_jobs so it survives worker
    restarts and works identically in one-shot (cron) mode.
    """
    row = conn.execute(
        "SELECT completed_at FROM scheduled_jobs "
        "WHERE job_type = ? AND status = 'completed' AND completed_at IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 1",
        (job_type,),
    ).fetchone()
    if not row or not row["completed_at"]:
        return None
    try:
        dt = datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _create_job_if_not_queued(conn: DB, job_type: str) -> str | None:
    """Create *job_type* unless one is already pending or running —
    prevents a scheduling check that fires every loop tick from queuing
    the same job repeatedly while the prior one is still in flight."""
    existing = conn.execute(
        "SELECT 1 FROM scheduled_jobs WHERE job_type = ? AND status IN ('pending','running') LIMIT 1",
        (job_type,),
    ).fetchone()
    if existing:
        return None
    from src.automation import create_job
    return create_job(conn, job_type=job_type, scheduled_at=datetime.now(timezone.utc).isoformat())


def _check_and_schedule_nfl(conn: DB) -> None:
    """NFL: daily scan + pregame tracking, but only on actual game days
    and around actual kickoff times — see src/league_schedule.py. Skips
    entirely (no API calls beyond the cheap, cached schedule discovery)
    on the many NFL-week days with no games at all.
    """
    from src.league_schedule import nfl_should_run_daily_scan, nfl_should_run_pregame_check

    now = _now_local()
    game_times = _discover_nfl_game_times()
    if not game_times:
        return

    last_scan = _get_last_completed_job_at(conn, "morning-run-nfl")
    already_ran_today = bool(
        last_scan and last_scan.astimezone(now.tzinfo).date() == now.date()
    )
    scan_decision = nfl_should_run_daily_scan(now, game_times, already_ran_today)
    if scan_decision.should_run:
        job_id = _create_job_if_not_queued(conn, "morning-run-nfl")
        if job_id:
            logger.info("[NFL] Scheduled daily scan: %s (%s)", job_id[:8], scan_decision.reason)

    pregame_decision = nfl_should_run_pregame_check(now, game_times)
    if pregame_decision.should_run:
        job_id = _create_job_if_not_queued(conn, "pregame-check-nfl")
        if job_id:
            logger.info("[NFL] Scheduled pregame check: %s (%s)", job_id[:8], pregame_decision.reason)


def _check_and_schedule_wnba(conn: DB) -> None:
    """WNBA: schedule discovery is free; game odds are a cheap flat rate;
    props are rationed hard against the monthly credit budget — see
    src/league_schedule.py and src/odds_api_credits.py.
    """
    from src.league_schedule import wnba_should_fetch_game_odds, wnba_should_fetch_props
    from src.odds_api_credits import get_latest_credit_status

    now = _now_local()
    game_times = _discover_wnba_game_times()
    if not game_times:
        return  # no key configured, or genuinely no games today/soon

    last_odds = _get_last_completed_job_at(conn, "wnba-odds-scan")
    odds_decision = wnba_should_fetch_game_odds(now, game_times, last_odds)
    if odds_decision.should_run:
        job_id = _create_job_if_not_queued(conn, "wnba-odds-scan")
        if job_id:
            logger.info("[WNBA] Scheduled odds scan: %s (%s)", job_id[:8], odds_decision.reason)

    try:
        status = get_latest_credit_status(conn)
    except Exception:
        status = None
    credits_remaining = status.get("requests_remaining") if status else None

    last_props = _get_last_completed_job_at(conn, "wnba-props-scan")
    props_decision = wnba_should_fetch_props(now, game_times, last_props, credits_remaining)
    if props_decision.should_run:
        job_id = _create_job_if_not_queued(conn, "wnba-props-scan")
        if job_id:
            logger.info("[WNBA] Scheduled props scan: %s (%s)", job_id[:8], props_decision.reason)


def _check_and_schedule_pregame(conn: DB) -> None:
    """Check if pregame checks need scheduling (hourly during game windows)."""
    now = _now_local()
    # Only schedule pregame checks between 1 PM and 11 PM ET (game window)
    if 13 <= now.hour <= 22:
        from src.automation import schedule_pregame_checks
        count = schedule_pregame_checks(conn)
        if count:
            logger.info("Scheduled %d pregame check(s)", count)


def _check_and_schedule_grading(conn: DB) -> None:
    """Check if grading needs scheduling (after games complete)."""
    # Final games can complete after 2 AM ET, so catch-up must run all day.
    from src.automation import schedule_grading
    count = schedule_grading(conn)
    if count:
        logger.info("Scheduled %d grading job(s)", count)


def _is_backup_time(now: datetime) -> bool:
    """Check if it's time for the daily backup (3:30 AM ET)."""
    return now.hour == 3 and now.minute == 30


def _check_and_schedule_morning_run(conn: DB) -> None:
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
        "AND status IN ('pending', 'running', 'completed', 'failed')",
        (f"{today}%",),
    ).fetchone()
    if not existing:
        job_id = create_job(conn, job_type="morning-run", scheduled_at=datetime.now(timezone.utc).isoformat())
        logger.info("Auto-scheduled morning run: %s", job_id[:8])


# ── Main loop ─────────────────────────────────────────────────────


def run_worker_persistent(config) -> None:
    """Run the worker as a persistent background process."""
    logger.info("Starting persistent worker (pid=%d, tz=%s)", os.getpid(), TZ_NAME)
    _initialize_worker_schema(config)

    conn = get_connection(config.database_path)
    if get_connection_dialect_name(conn) == "sqlite":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    _running = True
    _run_catchup_grading(conn)

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
    last_nfl_check = 0
    last_wnba_check = 0
    last_backup_minute = -1
    last_maintenance_day = ""

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

            # Daily maintenance cleanup (once per day)
            day_key = now_local.strftime("%Y-%m-%d")
            if day_key != last_maintenance_day:
                _maintenance_cleanup(conn)
                last_maintenance_day = day_key

            # Process pending jobs
            _process_pending_jobs(conn, config)

            # Check API quota after any job activity
            _check_api_quota_and_alert(conn, config)

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

            # NFL: daily scan + pregame tracking, only on real game days
            # (see src/league_schedule.py) — a failure here must never
            # affect MLB/WNBA scheduling in the same loop iteration.
            if now - last_nfl_check >= NFL_SCHEDULE_CHECK_INTERVAL_MINUTES * 60:
                try:
                    _check_and_schedule_nfl(conn)
                except Exception:
                    logger.exception("[NFL] Scheduling check failed")
                last_nfl_check = now

            # WNBA: schedule/odds/props, credit-aware (see
            # src/odds_api_credits.py) — isolated the same way.
            if now - last_wnba_check >= WNBA_SCHEDULE_CHECK_INTERVAL_MINUTES * 60:
                try:
                    _check_and_schedule_wnba(conn)
                except Exception:
                    logger.exception("[WNBA] Scheduling check failed")
                last_wnba_check = now

            # Daily backup at 3:30 AM ET
            if _is_backup_time(now_local) and last_backup_minute != now_local.minute:
                try:
                    backup_result = _run_backup(config)
                    last_backup_minute = now_local.minute
                    if backup_result.get("status") == "skipped":
                        logger.info("Daily backup skipped: %s", backup_result.get("reason"))
                    else:
                        logger.info("Daily backup completed")
                except Exception as e:
                    logger.error("Backup failed: %s", e)

            # API quota check (every ~5 min, alerts only once per day)
            if now_local.minute % 5 == 0 and now_local.second < 10:
                _check_api_quota_and_alert(conn, config)

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
    _initialize_worker_schema(config)

    conn = get_connection(config.database_path)
    if get_connection_dialect_name(conn) == "sqlite":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    try:
        _write_heartbeat(conn)
        _recover_stale_jobs(conn)
        _run_catchup_grading(conn)
        executed = _process_pending_jobs(conn, config)
        _check_and_schedule_morning_run(conn)
        _check_and_schedule_pregame(conn)
        _check_and_schedule_grading(conn)
        # NFL/WNBA scheduling checks — isolated so a failure in one
        # league's discovery/credit check never blocks MLB's one-shot run.
        try:
            _check_and_schedule_nfl(conn)
        except Exception:
            logger.exception("[NFL] Scheduling check failed")
        try:
            _check_and_schedule_wnba(conn)
        except Exception:
            logger.exception("[WNBA] Scheduling check failed")
        # Newly-queued NFL/WNBA jobs from the checks above are due
        # immediately — run them now rather than waiting for the next
        # one-shot invocation (which, on a cron schedule, could be hours
        # away for a job that's actually time-sensitive right now).
        executed += _process_pending_jobs(conn, config)
        _check_api_quota_and_alert(conn, config)

        now_local = _now_local()
        if _is_backup_time(now_local):
            _run_backup(config)

        logger.info("One-shot complete: %d jobs executed", executed)
    finally:
        conn.close()


def run_specific_job(job_type: str, config) -> None:
    """Run a single specific job type."""
    logger.info("Running specific job: %s", job_type)
    _initialize_worker_schema(config)

    conn = get_connection(config.database_path)
    if get_connection_dialect_name(conn) == "sqlite":
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
