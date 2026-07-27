"""Live-readiness command.

Validates all prerequisites before enabling live production mode.
Returns structured readiness report with pass/fail per check.
"""

from __future__ import annotations

import json
import logging
import os
from database.db_manager import get_connection
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.production_config import load_config, ProductionConfig
from src.shadow_mode import load_shadow_config

logger = logging.getLogger(__name__)

# ── Exit codes ─────────────────────────────────────────────────────

EXIT_READY = 0
EXIT_READY_WITH_WARNINGS = 1
EXIT_NOT_READY = 2
EXIT_CONFIG_FAILURE = 3
EXIT_NETWORK_FAILURE = 4
EXIT_DB_FAILURE = 5


@dataclass
class ReadinessCheck:
    """A single readiness check result."""
    name: str = ""
    status: str = "pending"  # pass, warn, fail, skip
    message: str = ""
    details: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.details is None:
            self.details = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessReport:
    """Full readiness report."""
    timestamp: str = ""
    overall_status: str = "not_ready"  # ready, ready_with_warnings, not_ready
    checks: list[ReadinessCheck] = None  # type: ignore[assignment]
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0
    skip_count: int = 0

    def __post_init__(self):
        if self.checks is None:
            self.checks = []
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def add(self, check: ReadinessCheck) -> None:
        self.checks.append(check)
        if check.status == "pass":
            self.pass_count += 1
        elif check.status == "warn":
            self.warn_count += 1
        elif check.status == "fail":
            self.fail_count += 1
        elif check.status == "skip":
            self.skip_count += 1

    def finalize(self) -> None:
        if self.fail_count > 0:
            self.overall_status = "not_ready"
        elif self.warn_count > 0:
            self.overall_status = "ready_with_warnings"
        else:
            self.overall_status = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status,
            "pass_count": self.pass_count,
            "warn_count": self.warn_count,
            "fail_count": self.fail_count,
            "skip_count": self.skip_count,
            "checks": [c.to_dict() for c in self.checks],
        }

    def to_human_readable(self) -> str:
        lines = [
            f"Live Readiness Report — {self.timestamp}",
            f"Overall: {self.overall_status.upper()}",
            f"  Pass: {self.pass_count}  Warn: {self.warn_count}  Fail: {self.fail_count}  Skip: {self.skip_count}",
            "",
        ]
        for c in self.checks:
            icon = {"pass": "OK", "warn": "WARN", "fail": "FAIL", "skip": "---"}.get(c.status, "?")
            lines.append(f"  [{icon:4s}] {c.name}: {c.message}")
        return "\n".join(lines)


# ── Acknowledgement ────────────────────────────────────────────────

ACKNOWLEDGEMENT_FILE = "data/.live_acknowledgement.json"


def acknowledge_live_data(config: ProductionConfig) -> dict[str, Any]:
    """Persist live-data acknowledgement."""
    import hashlib
    ack = {
        "acknowledged": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "software_version": "1.0.0",
        "config_fingerprint": hashlib.sha256(
            json.dumps(config.redacted(), sort_keys=True).encode()
        ).hexdigest()[:16],
        "database_path": config.database_path,
        "operator_note": "Acknowledged via --acknowledge-live-data",
    }
    ack_path = Path(ACKNOWLEDGEMENT_FILE)
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    ack_path.write_text(json.dumps(ack, indent=2))
    logger.info("Live data acknowledgement persisted to %s", ack_path)
    return ack


def get_acknowledgement() -> dict[str, Any] | None:
    """Read the live-data acknowledgement if it exists."""
    ack_path = Path(ACKNOWLEDGEMENT_FILE)
    if not ack_path.exists():
        return None
    try:
        return json.loads(ack_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ── Check functions ────────────────────────────────────────────────

def _check_api_credentials(config: ProductionConfig, *, skip_network: bool = False) -> ReadinessCheck:
    if not config.api_key:
        return ReadinessCheck(name="api_credentials", status="fail",
                              message="API key not configured")
    if len(config.api_key) < 8:
        return ReadinessCheck(name="api_credentials", status="warn",
                              message="API key appears too short")
    return ReadinessCheck(name="api_credentials", status="pass",
                          message="API key configured")


def _check_api_connectivity(config: ProductionConfig, *, skip_network: bool = False) -> ReadinessCheck:
    if skip_network:
        return ReadinessCheck(name="api_connectivity", status="skip",
                              message="Network checks skipped (--skip-network)")
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.sportsdata.io/health",
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return ReadinessCheck(name="api_connectivity", status="pass",
                                  message=f"API reachable (HTTP {resp.getcode()})")
    except Exception as e:
        return ReadinessCheck(name="api_connectivity", status="fail",
                              message=f"API unreachable: {e}")


def _check_database(config: ProductionConfig) -> ReadinessCheck:
    db_path = Path(config.database_path)
    if not db_path.exists():
        return ReadinessCheck(name="database", status="fail",
                              message=f"Database not found: {db_path}")
    try:
        conn = get_connection(str(db_path))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            required = {"games", "raw_responses", "odds", "historical_recommendations"}
            missing = required - tables
            if missing:
                return ReadinessCheck(name="database", status="fail",
                                      message=f"Missing tables: {', '.join(missing)}")
            return ReadinessCheck(name="database", status="pass",
                                  message=f"Database OK ({len(tables)} tables)")
        finally:
            conn.close()
    except Exception as e:
        return ReadinessCheck(name="database", status="fail",
                              message=f"Database error: {e}")


def _check_database_writable(config: ProductionConfig) -> ReadinessCheck:
    db_path = Path(config.database_path)
    if not db_path.exists():
        return ReadinessCheck(name="database_writable", status="fail",
                              message="Database file does not exist")
    try:
        if not os.access(db_path, os.W_OK):
            return ReadinessCheck(name="database_writable", status="fail",
                                  message="Database file not writable")
        return ReadinessCheck(name="database_writable", status="pass",
                              message="Database is writable")
    except Exception as e:
        return ReadinessCheck(name="database_writable", status="fail",
                              message=f"Write check failed: {e}")


def _check_cache_writable(config: ProductionConfig) -> ReadinessCheck:
    cache_dir = Path(config.cache_path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        test_file = cache_dir / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return ReadinessCheck(name="cache_writable", status="pass",
                              message="Cache directory is writable")
    except Exception as e:
        return ReadinessCheck(name="cache_writable", status="fail",
                              message=f"Cache not writable: {e}")


def _check_output_writable(config: ProductionConfig) -> ReadinessCheck:
    output_dir = Path(config.output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        test_file = output_dir / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return ReadinessCheck(name="output_writable", status="pass",
                              message="Output directory is writable")
    except Exception as e:
        return ReadinessCheck(name="output_writable", status="fail",
                              message=f"Output not writable: {e}")


def _check_timezone(config: ProductionConfig) -> ReadinessCheck:
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(config.timezone)
        return ReadinessCheck(name="timezone", status="pass",
                              message=f"Timezone valid: {config.timezone}")
    except Exception:
        return ReadinessCheck(name="timezone", status="fail",
                              message=f"Invalid timezone: {config.timezone}")


def _check_system_clock() -> ReadinessCheck:
    now = datetime.now(timezone.utc)
    if now.year < 2024 or now.year > 2030:
        return ReadinessCheck(name="system_clock", status="warn",
                              message=f"System clock may be wrong: {now.isoformat()}")
    return ReadinessCheck(name="system_clock", status="pass",
                          message=f"System clock OK: {now.strftime('%Y-%m-%d %H:%M UTC')}")


def _check_disk_space(config: ProductionConfig) -> ReadinessCheck:
    import shutil
    try:
        usage = shutil.disk_usage(config.output_dir)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1:
            return ReadinessCheck(name="disk_space", status="fail",
                                  message=f"Low disk space: {free_gb:.1f} GB free")
        if free_gb < 5:
            return ReadinessCheck(name="disk_space", status="warn",
                                  message=f"Disk space below 5 GB: {free_gb:.1f} GB free")
        return ReadinessCheck(name="disk_space", status="pass",
                              message=f"Disk OK: {free_gb:.1f} GB free")
    except Exception as e:
        return ReadinessCheck(name="disk_space", status="warn",
                              message=f"Could not check disk space: {e}")


def _check_shadow_mode(config: ProductionConfig) -> ReadinessCheck:
    shadow = load_shadow_config()
    if shadow.shadow_mode:
        return ReadinessCheck(name="shadow_mode", status="pass",
                              message="Shadow mode is ON (safe default)")
    return ReadinessCheck(name="shadow_mode", status="warn",
                          message="Shadow mode is OFF — live delivery enabled")


def _check_live_acknowledgement() -> ReadinessCheck:
    ack = get_acknowledgement()
    if ack and ack.get("acknowledged"):
        return ReadinessCheck(name="live_acknowledgement", status="pass",
                              message=f"Acknowledged at {ack.get('timestamp', 'unknown')}")
    return ReadinessCheck(name="live_acknowledgement", status="fail",
                          message="No live-data acknowledgement found")


def _check_scheduler() -> ReadinessCheck:
    try:
        from src.scheduler import get_default_schedules
        schedules = get_default_schedules()
        return ReadinessCheck(name="scheduler", status="pass",
                              message=f"Scheduler configured ({len(schedules)} entries)")
    except Exception as e:
        return ReadinessCheck(name="scheduler", status="warn",
                              message=f"Scheduler check failed: {e}")


def _check_backup_config(config: ProductionConfig) -> ReadinessCheck:
    if config.backup_retention_count <= 0:
        return ReadinessCheck(name="backup_config", status="warn",
                              message="Backup retention is disabled")
    return ReadinessCheck(name="backup_config", status="pass",
                          message=f"Backup configured (retention: {config.backup_retention_count})")


def _check_health_thresholds(config: ProductionConfig) -> ReadinessCheck:
    if config.freshness_threshold_seconds <= 0:
        return ReadinessCheck(name="health_thresholds", status="warn",
                              message="Freshness threshold not configured")
    return ReadinessCheck(name="health_thresholds", status="pass",
                          message=f"Freshness threshold: {config.freshness_threshold_seconds}s")


def _check_integrations(config: ProductionConfig) -> ReadinessCheck:
    parts = []
    if config.spreadsheet_id:
        parts.append("Sheets")
    if config.discord_webhook_urls:
        parts.append("Discord")
    if not parts:
        return ReadinessCheck(name="integrations", status="pass",
                              message="No integrations configured (optional)")
    return ReadinessCheck(name="integrations", status="pass",
                          message=f"Integrations: {', '.join(parts)}")


def _check_discord_config(config: ProductionConfig) -> ReadinessCheck:
    if not config.discord_webhook_urls:
        return ReadinessCheck(name="discord_config", status="skip",
                              message="Discord not configured")
    urls = [u.strip() for u in config.discord_webhook_urls.split(",") if u.strip()]
    if not urls:
        return ReadinessCheck(name="discord_config", status="fail",
                              message="Discord webhook URLs configured but empty")
    return ReadinessCheck(name="discord_config", status="pass",
                          message=f"Discord: {len(urls)} webhook(s) configured")


def _check_sheets_config(config: ProductionConfig) -> ReadinessCheck:
    if not config.spreadsheet_id:
        return ReadinessCheck(name="sheets_config", status="skip",
                              message="Google Sheets not configured")
    if not config.google_credentials_path:
        return ReadinessCheck(name="sheets_config", status="fail",
                              message="Sheets ID set but credentials path missing")
    if not Path(config.google_credentials_path).exists():
        return ReadinessCheck(name="sheets_config", status="fail",
                              message=f"Credentials file not found: {config.google_credentials_path}")
    return ReadinessCheck(name="sheets_config", status="pass",
                          message="Google Sheets configured")


def _check_last_job_runs(config: ProductionConfig) -> ReadinessCheck:
    try:
        conn = get_connection(config.database_path)
        try:
            row = conn.execute("""
                SELECT job_type, status, completed_at
                FROM job_runs
                ORDER BY completed_at DESC LIMIT 3
            """).fetchall()
            if not row:
                return ReadinessCheck(name="last_jobs", status="warn",
                                      message="No job runs found in database")
            recent = [f"{r[0]}({r[1]})" for r in row]
            return ReadinessCheck(name="last_jobs", status="pass",
                                  message=f"Recent jobs: {', '.join(recent)}")
        finally:
            conn.close()
    except Exception:
        return ReadinessCheck(name="last_jobs", status="skip",
                              message="Could not check job runs")


# ── Main readiness check ───────────────────────────────────────────

def run_readiness_checks(
    config: ProductionConfig | None = None,
    *,
    skip_network: bool = False,
) -> ReadinessReport:
    """Run all readiness checks and return report."""
    if config is None:
        config = load_config()

    report = ReadinessReport()

    checks = [
        _check_api_credentials(config, skip_network=skip_network),
        _check_api_connectivity(config, skip_network=skip_network),
        _check_database(config),
        _check_database_writable(config),
        _check_cache_writable(config),
        _check_output_writable(config),
        _check_timezone(config),
        _check_system_clock(),
        _check_disk_space(config),
        _check_shadow_mode(config),
        _check_live_acknowledgement(),
        _check_scheduler(),
        _check_backup_config(config),
        _check_health_thresholds(config),
        _check_integrations(config),
        _check_discord_config(config),
        _check_sheets_config(config),
        _check_last_job_runs(config),
    ]

    for check in checks:
        report.add(check)

    report.finalize()
    return report


# ── CLI ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="live_readiness", description="Validate live-readiness")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    parser.add_argument("--skip-network", action="store_true", help="Skip network checks")
    parser.add_argument("--show-secrets-redacted", action="store_true", help="Show redacted config")
    parser.add_argument("--acknowledge-live-data", action="store_true",
                        help="Persist live-data acknowledgement")
    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="WARNING")

    config = load_config()

    if args.acknowledge_live_data:
        ack = acknowledge_live_data(config)
        if args.json:
            print(json.dumps(ack, indent=2))
        else:
            print(f"Live data acknowledgement persisted at {ack['timestamp']}")
        return EXIT_READY

    report = run_readiness_checks(config, skip_network=args.skip_network)

    if args.strict and report.warn_count > 0:
        report.fail_count += report.warn_count
        report.warn_count = 0
        report.overall_status = "not_ready"

    if args.show_secrets_redacted:
        print(json.dumps(config.redacted(), indent=2))
        print()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.to_human_readable())

    if report.overall_status == "ready":
        return EXIT_READY
    elif report.overall_status == "ready_with_warnings":
        return EXIT_READY_WITH_WARNINGS
    elif report.overall_status == "not_ready":
        if any(c.name == "api_connectivity" and c.status == "fail" for c in report.checks):
            return EXIT_NETWORK_FAILURE
        if any(c.name == "database" and c.status == "fail" for c in report.checks):
            return EXIT_DB_FAILURE
        return EXIT_NOT_READY
    return EXIT_CONFIG_FAILURE


if __name__ == "__main__":
    import sys
    sys.exit(main())
