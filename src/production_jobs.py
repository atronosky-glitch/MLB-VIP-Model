"""Job orchestration CLI for production workflows.

Coordinates daily pipeline runs, Google Sheets export, Discord delivery,
health checks, backup, and calibration within a single job framework.

Usage::

    python -m src.production_jobs morning-run
    python -m src.production_jobs pregame-run
    python -m src.production_jobs export-sheets
    python -m src.production_jobs deliver-discord
    python -m src.production_jobs health-check
    python -m src.production_jobs backup
    python -m src.production_jobs calibrate
    python -m src.production_jobs full-daily
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.production_config import load_config, ProductionConfig
from src.structured_logging import setup_logging, set_job_context
from database.db_manager import get_connection

logger = logging.getLogger(__name__)

# ── Exit codes ─────────────────────────────────────────────────────
EXIT_SUCCESS = 0
EXIT_SUCCESS_NO_RECS = 1
EXIT_CONFIG_FAILURE = 2
EXIT_API_FAILURE = 3
EXIT_DB_FAILURE = 4
EXIT_VALIDATION_FAILURE = 5
EXIT_UNEXPECTED_FAILURE = 6


@dataclass
class JobRun:
    """Tracks a single job execution."""
    job_id: str = ""
    job_type: str = ""
    started_at: str = ""
    completed_at: str = ""
    status: str = "RUNNING"
    exit_code: int = -1
    duration_seconds: float = 0.0
    error_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_job(job_type: str, config: ProductionConfig, **kwargs: Any) -> JobRun:
    """Run a single job and return the result.

    Parameters
    ----------
    job_type:
        One of: morning-run, pregame-run, export-sheets, deliver-discord,
        health-check, backup, calibrate, full-daily.
    config:
        Production configuration.
    **kwargs:
        Additional arguments passed to the job handler.
    """
    job_id = str(uuid.uuid4())
    run = JobRun(
        job_id=job_id,
        job_type=job_type,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    set_job_context(job_id)
    logger.info("Starting job %s (id=%s)", job_type, job_id)

    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        run.status = "FAILED"
        run.exit_code = EXIT_CONFIG_FAILURE
        run.error_message = f"Unknown job type: {job_type}"
        run.completed_at = datetime.now(timezone.utc).isoformat()
        return run

    t0 = time.monotonic()
    try:
        exit_code = handler(config, job_id=job_id, **kwargs)
        run.exit_code = exit_code
        run.status = "SUCCESS" if exit_code in (0, 1) else "FAILED"
    except Exception as exc:
        run.status = "FAILED"
        run.exit_code = EXIT_UNEXPECTED_FAILURE
        run.error_message = f"{type(exc).__name__}: {exc}"
        logger.exception("Job %s failed with exception", job_type)

    run.duration_seconds = round(time.monotonic() - t0, 2)
    run.completed_at = datetime.now(timezone.utc).isoformat()
    set_job_context(None)

    logger.info(
        "Job %s completed: status=%s exit_code=%d duration=%.2fs",
        job_type, run.status, run.exit_code, run.duration_seconds,
    )

    # Persist job run to DB
    _persist_job_run(run, config)

    return run


# ── Job handlers ───────────────────────────────────────────────────

def _handle_morning_run(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Morning pipeline: full scan, export, deliver, backup."""
    from src.daily_pipeline import run_pipeline, PipelineConfig

    pipeline_config = PipelineConfig(
        live=True,
        use_cache=False,
        auto=True,
        output_dir=config.output_dir,
        actionable_only=True,
    )
    exit_code = run_pipeline(pipeline_config)

    if exit_code in (0, 1):
        # Export to Google Sheets
        if config.spreadsheet_id and config.google_credentials_path:
            try:
                _run_export_sheets(config)
            except Exception as exc:
                logger.warning("Sheets export failed: %s", exc)

        # Deliver to Discord
        if config.discord_webhook_urls:
            try:
                _run_deliver_discord(config)
            except Exception as exc:
                logger.warning("Discord delivery failed: %s", exc)

        # Backup
        try:
            _run_backup(config)
        except Exception as exc:
            logger.warning("Backup failed: %s", exc)

    return exit_code


def _handle_pregame_run(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Pre-game scan: focused on actionable markets near game time."""
    from src.daily_pipeline import run_pipeline, PipelineConfig

    pipeline_config = PipelineConfig(
        live=True,
        use_cache=False,
        auto=True,
        output_dir=config.output_dir,
        actionable_only=True,
    )
    return run_pipeline(pipeline_config)


def _handle_export_sheets(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Export current recommendations to Google Sheets."""
    return _run_export_sheets(config)


def _handle_deliver_discord(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Deliver current recommendations to Discord."""
    return _run_deliver_discord(config)


def _handle_health_check(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Run health checks."""
    from src.health_check import run_health_checks

    report = run_health_checks(
        db_path=config.database_path,
        api_key=config.api_key,
        output_dir=config.output_dir,
        freshness_threshold=config.freshness_threshold_seconds,
        google_sheets_enabled=bool(config.spreadsheet_id),
        discord_enabled=bool(config.discord_webhook_urls),
    )

    if report.overall_status == "unhealthy":
        return EXIT_DB_FAILURE
    return EXIT_SUCCESS


def _handle_backup(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Run database backup."""
    return _run_backup(config)


def _handle_calibrate(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Run calibration analysis."""
    from src.calibration import analyze_calibration

    conn = get_connection(config.database_path)
    try:
        result = analyze_calibration(conn)
        logger.info("Calibration analysis complete: %d buckets analyzed", result.get("bucket_count", 0))
    finally:
        conn.close()
    return EXIT_SUCCESS


def _handle_full_daily(config: ProductionConfig, *, job_id: str = "", **kw: Any) -> int:
    """Full daily: morning run + health check."""
    exit_code = _handle_morning_run(config, job_id=job_id)

    health_exit = _handle_health_check(config, job_id=job_id)
    if health_exit != EXIT_SUCCESS:
        logger.warning("Health check reported issues (exit=%d)", health_exit)

    return exit_code


# ── Internal helpers ───────────────────────────────────────────────

def _run_export_sheets(config: ProductionConfig) -> int:
    """Export recommendations to Google Sheets."""
    try:
        from src.export_sheets import export_recommendations
        export_recommendations(
            db_path=config.database_path,
            spreadsheet_id=config.spreadsheet_id,
            credentials_path=config.google_credentials_path,
        )
        return EXIT_SUCCESS
    except ImportError:
        logger.warning("Google Sheets libraries not installed")
        return EXIT_SUCCESS
    except Exception as exc:
        logger.error("Sheets export failed: %s", exc)
        return EXIT_API_FAILURE


def _run_deliver_discord(config: ProductionConfig) -> int:
    """Deliver recommendations to Discord."""
    try:
        from src.discord_delivery import deliver_recommendations
        webhook_urls = [
            u.strip() for u in config.discord_webhook_urls.split(",") if u.strip()
        ]
        deliver_recommendations(
            db_path=config.database_path,
            webhook_urls=webhook_urls,
            min_confidence=config.min_confidence_score,
            min_ev_pct=config.min_ev_pct,
        )
        return EXIT_SUCCESS
    except Exception as exc:
        logger.error("Discord delivery failed: %s", exc)
        return EXIT_API_FAILURE


def _run_backup(config: ProductionConfig) -> int:
    """Run database backup."""
    from src.backup_database import backup_database
    from pathlib import Path

    backup_dir = Path(config.output_dir) / "backups"
    try:
        backup_database(
            db_path=config.database_path,
            backup_dir=backup_dir,
            retention_count=config.backup_retention_count,
            compress=config.backup_compression,
        )
        return EXIT_SUCCESS
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        return EXIT_DB_FAILURE


def _persist_job_run(run: JobRun, config: ProductionConfig) -> None:
    """Persist job run record to the database."""
    try:
        from database.db_manager import get_connection

        conn = get_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS job_runs (
                    job_id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_seconds REAL,
                    error_message TEXT,
                    details_json TEXT
                )"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO job_runs "
                "(job_id, job_type, started_at, completed_at, status, "
                "exit_code, duration_seconds, error_message, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.job_id,
                    run.job_type,
                    run.started_at,
                    run.completed_at,
                    run.status,
                    run.exit_code,
                    run.duration_seconds,
                    run.error_message,
                    json.dumps(run.details),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Failed to persist job run: %s", exc)


# ── Handler registry ───────────────────────────────────────────────

JOB_HANDLERS: dict[str, Any] = {
    "morning-run": _handle_morning_run,
    "pregame-run": _handle_pregame_run,
    "export-sheets": _handle_export_sheets,
    "deliver-discord": _handle_deliver_discord,
    "health-check": _handle_health_check,
    "backup": _handle_backup,
    "calibrate": _handle_calibrate,
    "full-daily": _handle_full_daily,
}


# ── CLI ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="production_jobs",
        description="Production job orchestration for MLB sportsbook analysis",
    )
    parser.add_argument(
        "job",
        choices=list(JOB_HANDLERS.keys()),
        help="Job type to run",
    )
    parser.add_argument(
        "--config",
        help="Path to production config JSON file",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output job result as JSON",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would run without executing",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.debug:
        setup_logging(level="DEBUG", fmt="human")
    else:
        setup_logging(level=config.log_level, fmt=config.log_format)

    if args.dry_run:
        print(json.dumps({
            "job": args.job,
            "config": config.redacted(),
            "dry_run": True,
        }, indent=2))
        return EXIT_SUCCESS

    run_result = run_job(args.job, config)

    if args.as_json:
        print(json.dumps(run_result.to_dict(), indent=2))
    else:
        if run_result.status == "SUCCESS":
            print(f"Job {args.job} completed successfully ({run_result.duration_seconds}s)")
        else:
            print(f"Job {args.job} FAILED (exit={run_result.exit_code}): {run_result.error_message}")

    return run_result.exit_code


if __name__ == "__main__":
    sys.exit(main())
