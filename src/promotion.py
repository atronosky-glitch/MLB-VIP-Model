"""Promotion criteria evaluation.

Determines whether shadow mode can be promoted to live delivery based on:
- 14 consecutive operational days
- 98%+ job success rate
- No DB integrity failures
- Verified backups
- Manual YN review completed
- Readiness check passing

Does NOT auto-disable shadow mode. Only provides criteria evaluation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from src.production_config import load_config, ProductionConfig

logger = logging.getLogger(__name__)


@dataclass
class PromotionCriterion:
    """A single promotion criterion."""
    name: str = ""
    met: bool = False
    message: str = ""
    details: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.details is None:
            self.details = {}


@dataclass
class PromotionResult:
    """Full promotion criteria evaluation."""
    all_passed: bool = False
    met_count: int = 0
    total_count: int = 0
    criteria: list[PromotionCriterion] = None  # type: ignore[assignment]
    days_shadow_active: int = 0
    evaluated_at: str = ""

    def __post_init__(self):
        if self.criteria is None:
            self.criteria = []
        if not self.evaluated_at:
            self.evaluated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_passed": self.all_passed,
            "met_count": self.met_count,
            "total_count": self.total_count,
            "days_shadow_active": self.days_shadow_active,
            "evaluated_at": self.evaluated_at,
            "criteria": [asdict(c) for c in self.criteria],
        }


# ── Acknowledgement tracking ───────────────────────────────────────

SHADOW_START_FILE = "data/.shadow_start"
YN_REVIEW_FILE = "data/.yn_review_complete"


def mark_shadow_start() -> None:
    """Record the date shadow mode was first enabled."""
    path = Path(SHADOW_START_FILE)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(timezone.utc).isoformat())


def get_shadow_start_date() -> str | None:
    """Get the date shadow mode was first enabled."""
    path = Path(SHADOW_START_FILE)
    if path.exists():
        return path.read_text().strip()
    return None


def mark_yn_review_complete() -> None:
    """Mark that manual YN review has been completed."""
    path = Path(YN_REVIEW_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "completed": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))


def is_yn_review_complete() -> bool:
    """Check if manual YN review has been completed."""
    path = Path(YN_REVIEW_FILE)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        return data.get("completed", False)
    except (json.JSONDecodeError, OSError):
        return False


# ── Criteria checks ────────────────────────────────────────────────

def _check_consecutive_operational_days(config: ProductionConfig) -> PromotionCriterion:
    """Check 14 consecutive operational days."""
    try:
        conn = sqlite3.connect(config.database_path, timeout=5)
        try:
            rows = conn.execute("""
                SELECT DISTINCT date(completed_at) as day
                FROM job_runs
                WHERE status = 'success'
                ORDER BY day DESC
                LIMIT 30
            """).fetchall()
        finally:
            conn.close()

        if len(rows) < 14:
            return PromotionCriterion(
                name="consecutive_operational_days",
                met=False,
                message=f"Only {len(rows)} operational days (need 14)",
                details={"days": len(rows)},
            )

        # Check they are actually consecutive
        dates = [datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows[:14]]
        consecutive = True
        for i in range(len(dates) - 1):
            if (dates[i] - dates[i + 1]).days != 1:
                consecutive = False
                break

        return PromotionCriterion(
            name="consecutive_operational_days",
            met=consecutive,
            message=f"14 consecutive operational days" if consecutive else "Days are not consecutive",
            details={"days_found": len(rows), "consecutive": consecutive},
        )
    except Exception as e:
        return PromotionCriterion(
            name="consecutive_operational_days",
            met=False,
            message=f"Check failed: {e}",
        )


def _check_job_success_rate(config: ProductionConfig) -> PromotionCriterion:
    """Check 98%+ job success rate."""
    try:
        conn = sqlite3.connect(config.database_path, timeout=5)
        try:
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as succeeded
                FROM job_runs
                WHERE completed_at >= datetime('now', '-30 days')
            """).fetchone()
        finally:
            conn.close()

        total = row[0] or 0
        succeeded = row[1] or 0

        if total == 0:
            return PromotionCriterion(
                name="job_success_rate",
                met=False,
                message="No job runs in last 30 days",
                details={"total": 0, "succeeded": 0},
            )

        rate = (succeeded / total) * 100
        return PromotionCriterion(
            name="job_success_rate",
            met=rate >= 98.0,
            message=f"Job success rate: {rate:.1f}% ({succeeded}/{total})",
            details={"total": total, "succeeded": succeeded, "rate": round(rate, 1)},
        )
    except Exception as e:
        return PromotionCriterion(
            name="job_success_rate",
            met=False,
            message=f"Check failed: {e}",
        )


def _check_no_db_integrity_failures(config: ProductionConfig) -> PromotionCriterion:
    """Check no database integrity failures in last 30 days."""
    try:
        conn = sqlite3.connect(config.database_path, timeout=5)
        try:
            row = conn.execute("""
                SELECT COUNT(*) FROM job_runs
                WHERE status = 'db_failure'
                  AND completed_at >= datetime('now', '-30 days')
            """).fetchone()
            failures = row[0] or 0
        finally:
            conn.close()

        return PromotionCriterion(
            name="no_db_integrity_failures",
            met=failures == 0,
            message=f"No DB integrity failures" if failures == 0 else f"{failures} DB integrity failures",
            details={"failures": failures},
        )
    except Exception as e:
        return PromotionCriterion(
            name="no_db_integrity_failures",
            met=False,
            message=f"Check failed: {e}",
        )


def _check_verified_backups(config: ProductionConfig) -> PromotionCriterion:
    """Check that backups exist and are verified."""
    backup_dir = Path(config.output_dir) / "backups"
    if not backup_dir.exists():
        return PromotionCriterion(
            name="verified_backups",
            met=False,
            message="No backup directory found",
        )

    backups = sorted(backup_dir.glob("mlb_backup_*.db*"), reverse=True)
    if not backups:
        return PromotionCriterion(
            name="verified_backups",
            met=False,
            message="No backups found",
        )

    newest = backups[0]
    age_hours = (datetime.now().timestamp() - newest.stat().st_mtime) / 3600

    return PromotionCriterion(
        name="verified_backups",
        met=age_hours < 24,
        message=f"Newest backup is {age_hours:.0f}h old" if age_hours < 24 else f"Newest backup is {age_hours:.0f}h old (stale)",
        details={"newest_backup": str(newest), "age_hours": round(age_hours, 1)},
    )


def _check_yn_review() -> PromotionCriterion:
    """Check manual YN review is completed."""
    complete = is_yn_review_complete()
    return PromotionCriterion(
        name="yn_review_complete",
        met=complete,
        message="Manual YN review completed" if complete else "Manual YN review not completed",
    )


def _check_readiness_passing(config: ProductionConfig) -> PromotionCriterion:
    """Check that live readiness passes."""
    try:
        from src.live_readiness import run_readiness_checks
        report = run_readiness_checks(config, skip_network=True)
        passing = report.overall_status in ("ready", "ready_with_warnings")
        return PromotionCriterion(
            name="readiness_passing",
            met=passing,
            message=f"Readiness: {report.overall_status}",
            details={
                "pass": report.pass_count,
                "warn": report.warn_count,
                "fail": report.fail_count,
            },
        )
    except Exception as e:
        return PromotionCriterion(
            name="readiness_passing",
            met=False,
            message=f"Readiness check failed: {e}",
        )


def _check_shadow_active_days() -> PromotionCriterion:
    """Check that shadow mode has been active for at least 14 days."""
    start_str = get_shadow_start_date()
    if not start_str:
        return PromotionCriterion(
            name="shadow_active_days",
            met=False,
            message="Shadow start date not recorded",
        )

    try:
        start_dt = datetime.fromisoformat(start_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - start_dt).days
        return PromotionCriterion(
            name="shadow_active_days",
            met=days >= 14,
            message=f"Shadow mode active for {days} days" + (" (need 14)" if days < 14 else ""),
            details={"days": days, "start": start_str},
        )
    except (ValueError, TypeError):
        return PromotionCriterion(
            name="shadow_active_days",
            met=False,
            message="Invalid shadow start date",
        )


# ── Main evaluation ────────────────────────────────────────────────

def check_promotion_criteria(
    config: ProductionConfig | None = None,
) -> PromotionResult:
    """Evaluate all promotion criteria."""
    if config is None:
        config = load_config()

    criteria = [
        _check_consecutive_operational_days(config),
        _check_job_success_rate(config),
        _check_no_db_integrity_failures(config),
        _check_verified_backups(config),
        _check_yn_review(),
        _check_readiness_passing(config),
        _check_shadow_active_days(),
    ]

    met_count = sum(1 for c in criteria if c.met)

    # Get shadow active days for result
    start_str = get_shadow_start_date()
    days = 0
    if start_str:
        try:
            start_dt = datetime.fromisoformat(start_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - start_dt).days
        except (ValueError, TypeError):
            pass

    return PromotionResult(
        all_passed=met_count == len(criteria),
        met_count=met_count,
        total_count=len(criteria),
        criteria=criteria,
        days_shadow_active=days,
    )


def format_promotion_result(result: PromotionResult) -> str:
    """Format promotion result for display."""
    lines = [
        f"Promotion Criteria — {'ALL MET' if result.all_passed else 'NOT MET'}",
        f"  {result.met_count}/{result.total_count} criteria met",
        f"  Days shadow active: {result.days_shadow_active}",
        "",
    ]
    for c in result.criteria:
        icon = "PASS" if c.met else "FAIL"
        lines.append(f"  [{icon}] {c.name}: {c.message}")
    lines.append("")
    if result.all_passed:
        lines.append("  ALL CRITERIA MET. Shadow mode can be promoted.")
        lines.append("  Operator must manually disable shadow mode.")
    else:
        lines.append(f"  {result.total_count - result.met_count} criteria not yet met.")
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="promotion", description="Evaluate promotion criteria")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--mark-yn-review", action="store_true", help="Mark YN review complete")
    parser.add_argument("--mark-shadow-start", action="store_true", help="Record shadow start date")
    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="WARNING")

    if args.mark_yn_review:
        mark_yn_review_complete()
        print("YN review marked complete")
        return 0

    if args.mark_shadow_start:
        mark_shadow_start()
        print("Shadow start date recorded")
        return 0

    config = load_config()
    result = check_promotion_criteria(config)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_promotion_result(result))

    return 0 if result.all_passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
