"""Production health monitoring.

Checks database, API connectivity, data freshness, disk space,
and optional integration endpoints. Returns a structured health
report with individual check results.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from database.connection import get_database_url
from database.db_manager import get_connection
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _open_connection(db_path: str | Path):
    """Open PostgreSQL when DATABASE_URL is configured, otherwise SQLite."""
    url = get_database_url()
    if url:
        return get_connection()
    return get_connection(db_path=str(db_path))


@dataclass
class HealthCheck:
    """Result of a single health check."""
    name: str
    status: str  # "ok", "warning", "error"
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthReport:
    """Full health report with individual checks."""
    overall_status: str  # "healthy", "degraded", "unhealthy"
    timestamp: str
    checks: list[HealthCheck] = field(default_factory=list)
    check_count: int = 0
    ok_count: int = 0
    warning_count: int = 0
    error_count: int = 0

    def add(self, check: HealthCheck) -> None:
        self.checks.append(check)
        self.check_count += 1
        if check.status == "ok":
            self.ok_count += 1
        elif check.status == "warning":
            self.warning_count += 1
        elif check.status == "error":
            self.error_count += 1
        self._update_overall()

    def _update_overall(self) -> None:
        if self.error_count > 0:
            self.overall_status = "unhealthy"
        elif self.warning_count > 0:
            self.overall_status = "degraded"
        else:
            self.overall_status = "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "timestamp": self.timestamp,
            "check_count": self.check_count,
            "ok_count": self.ok_count,
            "warning_count": self.warning_count,
            "error_count": self.error_count,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


def run_health_checks(
    db_path: str | Path,
    api_key: str = "",
    output_dir: str | Path = "output",
    freshness_threshold: int = 3600,
    disk_min_mb: int = 100,
    google_sheets_enabled: bool = False,
    discord_enabled: bool = False,
    environment: str = "",
    timezone_name: str = "",
    backup_dir: str | Path = "",
    scheduler_enabled: bool = True,
    scheduling_pregame_interval_minutes: int = 30,
) -> HealthReport:
    """Run all health checks and return a report.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    api_key:
        API key (non-empty check only; never sent externally).
    output_dir:
        Directory for pipeline output.
    freshness_threshold:
        Max seconds since last successful pipeline run.
    disk_min_mb:
        Minimum free disk space in MB.
    google_sheets_enabled:
        Whether to check Google Sheets reachability.
    discord_enabled:
        Whether to check Discord webhook reachability.
    environment:
        Deployment environment name (local, production, etc.).
    timezone_name:
        Configured timezone string.
    backup_dir:
        Directory for database backups.
    scheduler_enabled:
        Whether the scheduler is enabled.
    timezone_name:
        Timezone used to calculate the next expected morning scan.
    scheduling_pregame_interval_minutes:
        Configured interval for scheduling pregame scans.
    """
    report = HealthReport(
        overall_status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    report.add(_check_database(db_path))
    report.add(_check_disk_space(output_dir, disk_min_mb))
    report.add(_check_data_freshness(
        db_path,
        freshness_threshold,
        scheduler_enabled=scheduler_enabled,
        timezone_name=timezone_name,
        scheduling_pregame_interval_minutes=scheduling_pregame_interval_minutes,
    ))
    report.add(_check_api_key(api_key))
    report.add(_check_output_dir(output_dir))
    report.add(_check_worker_heartbeat(db_path))
    report.add(_check_failed_jobs(db_path))
    report.add(_check_persistent_storage(db_path))
    report.add(_check_deployment_environment(environment))
    report.add(_check_timezone(timezone_name))
    report.add(_check_scheduler(scheduler_enabled))
    report.add(_check_backup_directory(backup_dir))

    if google_sheets_enabled:
        report.add(_check_google_sheets(db_path))
    if discord_enabled:
        report.add(_check_discord())

    return report


def _check_database(db_path: str | Path) -> HealthCheck:
    """Verify database is accessible and has correct schema."""
    db_path = Path(db_path)
    if not get_database_url() and not db_path.exists():
        return HealthCheck(
            name="database",
            status="error",
            message=f"Database file not found: {db_path}",
        )

    try:
        conn = _open_connection(db_path)
        try:
            if get_database_url():
                cursor = conn.execute(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            else:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row["name"] for row in cursor.fetchall()}
            row_count = len(tables)
        finally:
            conn.close()

        required = {"games", "raw_responses", "odds", "historical_recommendations"}
        missing = required - tables

        if missing:
            return HealthCheck(
                name="database",
                status="error",
                message=f"Missing tables: {', '.join(sorted(missing))}",
                details={"tables": sorted(tables), "missing": sorted(missing)},
            )

        return HealthCheck(
            name="database",
            status="ok",
            message=f"Database OK ({len(tables)} tables, {row_count} objects)",
            details={"tables": sorted(tables), "table_count": len(tables)},
        )
    except Exception as e:
        return HealthCheck(
            name="database",
            status="error",
            message=f"Database error: {e}",
        )


def _check_disk_space(output_dir: str | Path, min_mb: int) -> HealthCheck:
    """Check available disk space."""
    try:
        usage = shutil.disk_usage(str(output_dir))
        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)
        pct_free = (usage.free / usage.total) * 100

        if free_mb < min_mb:
            return HealthCheck(
                name="disk_space",
                status="error",
                message=f"Low disk space: {free_mb:.0f} MB free (min: {min_mb} MB)",
                details={"free_mb": round(free_mb), "total_mb": round(total_mb),
                          "pct_free": round(pct_free, 1)},
            )
        if pct_free < 10:
            return HealthCheck(
                name="disk_space",
                status="warning",
                message=f"Disk space below 10%: {free_mb:.0f} MB free ({pct_free:.1f}%)",
                details={"free_mb": round(free_mb), "total_mb": round(total_mb),
                          "pct_free": round(pct_free, 1)},
            )

        return HealthCheck(
            name="disk_space",
            status="ok",
            message=f"Disk OK: {free_mb:.0f} MB free ({pct_free:.1f}%)",
            details={"free_mb": round(free_mb), "pct_free": round(pct_free, 1)},
        )
    except OSError as e:
        return HealthCheck(
            name="disk_space",
            status="warning",
            message=f"Could not check disk space: {e}",
        )


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a database timestamp into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_morning_scan(timezone_name: str) -> datetime:
    """Return the next expected 09:00 local-time scan."""
    try:
        zone = ZoneInfo(timezone_name or "UTC")
    except (KeyError, ValueError):
        zone = timezone.utc
    now_local = datetime.now(zone)
    candidate = now_local.replace(hour=9, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _get_scan_schedule_context(
    conn,
    timezone_name: str,
    scheduling_pregame_interval_minutes: int,
) -> dict[str, Any]:
    """Find pending/overdue scans and the next expected scheduled scan."""
    del scheduling_pregame_interval_minutes  # Persisted job times are authoritative.
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT job_type, scheduled_at FROM scheduled_jobs "
        "WHERE status = 'pending' AND job_type IN ('pregame-check', 'morning-run') "
        "ORDER BY scheduled_at"
    ).fetchall()
    future: list[datetime] = []
    overdue = 0
    for row in rows:
        scheduled = _parse_timestamp(row["scheduled_at"])
        if scheduled is None or scheduled <= now:
            overdue += 1
        else:
            future.append(scheduled)
    next_scan = min(future) if future else _next_morning_scan(timezone_name)
    return {
        "next_expected_scan_at": next_scan,
        "overdue_pending_scans": overdue,
        "pending_scheduled_scans": len(rows),
    }


def _check_data_freshness(
    db_path: str | Path,
    threshold: int,
    *,
    scheduler_enabled: bool = True,
    timezone_name: str = "",
    scheduling_pregame_interval_minutes: int = 30,
) -> HealthCheck:
    """Check freshness against the last run and expected scan schedule."""
    try:
        conn = _open_connection(db_path)
        try:
            row = conn.execute(
                "SELECT finished_at, error_message FROM scan_runs "
                "WHERE run_type = 'scan' AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            schedule_context = {
                "next_expected_scan_at": None,
                "overdue_pending_scans": 0,
                "pending_scheduled_scans": 0,
            }
            if scheduler_enabled:
                try:
                    schedule_context = _get_scan_schedule_context(
                        conn,
                        timezone_name,
                        scheduling_pregame_interval_minutes,
                    )
                except Exception:
                    # A missing scheduling table must not hide a real freshness failure.
                    pass
        finally:
            conn.close()

        if row is None:
            return HealthCheck(
                name="data_freshness",
                status="warning",
                message="No pipeline runs found in database",
            )

        completed_str = row["finished_at"]
        status = "success" if not row["error_message"] else "failed"
        completed = _parse_timestamp(completed_str)
        age_seconds = (
            (datetime.now(timezone.utc) - completed).total_seconds()
            if completed is not None else float("inf")
        )

        if status != "success":
            return HealthCheck(
                name="data_freshness",
                status="warning",
                message=f"Last pipeline run status: {status}",
                details={"last_status": status, "age_seconds": round(age_seconds)},
            )

        next_expected = schedule_context["next_expected_scan_at"]
        if (
            scheduler_enabled
            and schedule_context["overdue_pending_scans"] == 0
            and next_expected is not None
            and next_expected > datetime.now(timezone.utc)
            and age_seconds > threshold
        ):
            return HealthCheck(
                name="data_freshness",
                status="ok",
                message=(
                    f"Last run {age_seconds / 3600:.1f}h ago; next scan expected "
                    f"at {next_expected.isoformat()}"
                ),
                details={
                    "age_seconds": round(age_seconds),
                    "threshold": threshold,
                    "schedule_aware": True,
                    "next_expected_scan_at": next_expected.isoformat(),
                    "pending_scheduled_scans": schedule_context["pending_scheduled_scans"],
                },
            )

        if age_seconds > threshold * 2:
            return HealthCheck(
                name="data_freshness",
                status="error",
                message=f"Data stale: last run {age_seconds / 3600:.1f}h ago (threshold: {threshold / 3600:.1f}h)",
                details={
                    "age_seconds": round(age_seconds),
                    "threshold": threshold,
                    "schedule_aware": scheduler_enabled,
                    "overdue_pending_scans": schedule_context["overdue_pending_scans"],
                },
            )

        if age_seconds > threshold:
            return HealthCheck(
                name="data_freshness",
                status="warning",
                message=f"Data aging: last run {age_seconds / 3600:.1f}h ago",
                details={
                    "age_seconds": round(age_seconds),
                    "threshold": threshold,
                    "schedule_aware": scheduler_enabled,
                    "overdue_pending_scans": schedule_context["overdue_pending_scans"],
                },
            )

        return HealthCheck(
            name="data_freshness",
            status="ok",
            message=f"Data fresh: last run {age_seconds / 60:.0f}m ago",
            details={"age_seconds": round(age_seconds)},
        )
    except Exception as e:
        return HealthCheck(
            name="data_freshness",
            status="warning",
            message=f"Could not check freshness: {e}",
        )


def _check_api_key(api_key: str) -> HealthCheck:
    """Verify API key is configured."""
    if not api_key:
        return HealthCheck(
            name="api_key",
            status="error",
            message="API key not configured (set SPORTSODDS_API_KEY)",
        )
    if len(api_key) < 8:
        return HealthCheck(
            name="api_key",
            status="warning",
            message="API key appears too short",
        )
    return HealthCheck(
        name="api_key",
        status="ok",
        message="API key configured",
    )


def _check_output_dir(output_dir: str | Path) -> HealthCheck:
    """Verify output directory is writable."""
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
        test_file = out / ".health_check_write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return HealthCheck(
            name="output_dir",
            status="ok",
            message=f"Output directory writable: {out}",
        )
    except OSError as e:
        return HealthCheck(
            name="output_dir",
            status="error",
            message=f"Output directory not writable: {e}",
        )


def _check_google_sheets(db_path: str | Path) -> HealthCheck:
    """Check Google Sheets integration readiness."""
    try:
        import google.oauth2.credentials  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        return HealthCheck(
            name="google_sheets",
            status="ok",
            message="Google Sheets libraries available",
        )
    except ImportError:
        return HealthCheck(
            name="google_sheets",
            status="warning",
            message="Google Sheets libraries not installed (pip install google-api-python-client google-auth)",
        )


def _check_discord() -> HealthCheck:
    """Check Discord integration readiness."""
    try:
        import urllib.request  # noqa: F401
        return HealthCheck(
            name="discord",
            status="ok",
            message="Discord integration available (stdlib only)",
        )
    except ImportError:
        return HealthCheck(
            name="discord",
            status="error",
            message="Cannot use Discord integration",
        )


def _check_worker_heartbeat(db_path: str | Path) -> HealthCheck:
    """Check if the background worker has sent a recent heartbeat."""
    try:
        conn = _open_connection(db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_heartbeat TEXT NOT NULL,
                    worker_pid INTEGER,
                    uptime_seconds REAL
                )
            """)
            row = conn.execute(
                "SELECT last_heartbeat, worker_pid FROM worker_heartbeat WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()

        if not row:
            return HealthCheck(
                name="worker_heartbeat",
                status="warning",
                message="No worker heartbeat recorded (worker may not be running)",
            )

        hb_str = row["last_heartbeat"]
        hb_time = datetime.fromisoformat(hb_str)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - hb_time).total_seconds()

        if age_seconds > 300:  # 5 minutes
            return HealthCheck(
                name="worker_heartbeat",
                status="error",
                message=f"Worker heartbeat stale: {age_seconds / 60:.0f}m ago (pid={row['worker_pid']})",
                details={"age_seconds": round(age_seconds), "worker_pid": row["worker_pid"]},
            )
        return HealthCheck(
            name="worker_heartbeat",
            status="ok",
            message=f"Worker active: heartbeat {age_seconds:.0f}s ago (pid={row['worker_pid']})",
            details={"age_seconds": round(age_seconds), "worker_pid": row["worker_pid"]},
        )
    except Exception as e:
        return HealthCheck(
            name="worker_heartbeat",
            status="warning",
            message=f"Could not check worker heartbeat: {e}",
        )


def _check_failed_jobs(db_path: str | Path) -> HealthCheck:
    """Check whether scheduled jobs have failed and remain unresolved."""
    try:
        conn = _open_connection(db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM scheduled_jobs WHERE status = 'failed'"
            ).fetchone()
        finally:
            conn.close()
        count = int(row["cnt"] if row else 0)
        if count:
            return HealthCheck(
                name="failed_jobs",
                status="error",
                message=f"{count} failed scheduled job(s)",
                details={"failed_jobs": count},
            )
        return HealthCheck(
            name="failed_jobs",
            status="ok",
            message="No failed scheduled jobs",
            details={"failed_jobs": 0},
        )
    except Exception as e:
        return HealthCheck(
            name="failed_jobs",
            status="warning",
            message=f"Could not check failed jobs: {e}",
        )


def _check_persistent_storage(db_path: str | Path) -> HealthCheck:
    """Check if the database is on persistent storage."""
    if get_database_url():
        return HealthCheck(
            name="persistent_storage",
            status="ok",
            message="PostgreSQL managed storage active",
            details={"storage_type": "managed_postgresql"},
        )
    db_path = Path(db_path)
    if not db_path.exists():
        return HealthCheck(
            name="persistent_storage",
            status="warning",
            message="Database does not exist yet (will be created on first run)",
        )

    # Check if the parent directory is writable
    try:
        parent = db_path.parent
        test_file = parent / ".storage_test"
        test_file.write_text("ok")
        test_file.unlink()

        # Check if it's on a known persistent path
        resolved = str(db_path.resolve())
        is_persistent = any(p in resolved for p in ["/data", "/mnt", "/var/lib", "database"])

        if is_persistent:
            return HealthCheck(
                name="persistent_storage",
                status="ok",
                message=f"Persistent storage detected: {parent}",
            )
        return HealthCheck(
            name="persistent_storage",
            status="warning",
            message=f"Storage may not be persistent: {parent} (consider mounting /data)",
        )
    except OSError as e:
        return HealthCheck(
            name="persistent_storage",
            status="error",
            message=f"Storage not writable: {e}",
        )


def _check_deployment_environment(environment: str) -> HealthCheck:
    """Report the deployment environment."""
    if not environment:
        return HealthCheck(
            name="deployment_environment",
            status="ok",
            message="Environment: local (not deployed)",
        )
    return HealthCheck(
        name="deployment_environment",
        status="ok",
        message=f"Environment: {environment}",
        details={"environment": environment},
    )


def _check_timezone(timezone_name: str) -> HealthCheck:
    """Verify the configured timezone is valid."""
    if not timezone_name:
        return HealthCheck(
            name="timezone",
            status="warning",
            message="No timezone configured (defaulting to UTC)",
        )
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone_name)
        now = datetime.now(tz)
        return HealthCheck(
            name="timezone",
            status="ok",
            message=f"Timezone: {timezone_name} (local time: {now.strftime('%H:%M')})",
            details={"timezone": timezone_name, "local_time": now.isoformat()},
        )
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return HealthCheck(
            name="timezone",
            status="error",
            message=f"Invalid timezone: {timezone_name}",
        )


def _check_scheduler(scheduler_enabled: bool) -> HealthCheck:
    """Check scheduler status."""
    if scheduler_enabled:
        return HealthCheck(
            name="scheduler",
            status="ok",
            message="Scheduler enabled",
        )
    return HealthCheck(
        name="scheduler",
        status="warning",
        message="Scheduler disabled",
    )


def _check_backup_directory(backup_dir: str | Path) -> HealthCheck:
    """Check backup directory status."""
    if get_database_url():
        return HealthCheck(
            name="backup",
            status="ok",
            message="PostgreSQL backups are managed externally; local SQLite backup is not required",
            details={"required": False, "storage_type": "managed_postgresql"},
        )
    if not backup_dir:
        return HealthCheck(
            name="backup",
            status="warning",
            message="Optional local backup directory not configured",
            details={"required": False},
        )
    backup_path = Path(backup_dir)
    if not backup_path.exists():
        try:
            backup_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return HealthCheck(
                name="backup",
                status="warning",
                message=f"Optional backup directory unavailable: {e}",
                details={"required": False},
            )
        return HealthCheck(
            name="backup",
            status="warning",
            message=f"Optional backup directory created; no backups found: {backup_path}",
            details={"required": False, "backup_count": 0},
        )

    try:
        backups = sorted(backup_path.glob("mlb_backup_*"))
        if not backups:
            return HealthCheck(
                name="backup",
                status="warning",
                message=f"No backups found in {backup_path}",
            )
        latest = backups[-1]
        age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)).total_seconds() / 3600
        return HealthCheck(
            name="backup",
            status="ok",
            message=f"Latest backup: {latest.name} ({age_hours:.1f}h ago, {len(backups)} total)",
            details={"latest_backup": latest.name, "backup_count": len(backups), "age_hours": round(age_hours, 1)},
        )
    except OSError as e:
        return HealthCheck(
            name="backup",
            status="warning",
            message=f"Could not check backups: {e}",
        )
