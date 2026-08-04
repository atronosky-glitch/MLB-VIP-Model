"""Tests for Phase 10 Part F: Health Check."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakePostgresConnection:
    def execute(self, sql, params=()):
        if "information_schema.tables" in sql:
            return _FakeResult([{"name": name} for name in (
                "games", "raw_responses", "odds", "historical_recommendations",
                "scan_runs", "scheduled_jobs", "worker_heartbeat",
            )])
        if "FROM scan_runs" in sql:
            return _FakeResult([{
                "finished_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
                "error_message": None,
            }])
        if "FROM scheduled_jobs" in sql and "COUNT(*)" not in sql:
            return _FakeResult([{
                "job_type": "pregame-check",
                "scheduled_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }])
        if "FROM worker_heartbeat" in sql:
            return _FakeResult([{
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "worker_pid": 123,
            }])
        if "COUNT(*) AS cnt FROM scheduled_jobs" in sql:
            return _FakeResult([{"cnt": 0}])
        return _FakeResult([])

    def close(self):
        pass


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE games (id TEXT)")
    conn.execute("CREATE TABLE raw_responses (id TEXT)")
    conn.execute("CREATE TABLE odds (id TEXT)")
    conn.execute("CREATE TABLE historical_recommendations (id TEXT)")
    conn.execute(
        "CREATE TABLE scan_runs (run_id TEXT, started_at TEXT, finished_at TEXT, "
        "run_type TEXT, error_message TEXT)"
    )
    conn.execute(
        "INSERT INTO scan_runs VALUES (?, ?, ?, ?, ?)",
        ("run-001", datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat(), "scan", None),
    )
    conn.execute(
        "CREATE TABLE scheduled_jobs (status TEXT, job_type TEXT, scheduled_at TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


class TestHealthCheck:

    def test_postgres_production_health_ignores_local_paths_and_optional_backup(self, tmp_path):
        from src.health_check import run_health_checks
        url = "postgresql://user:secret@example.invalid/db"
        with patch("src.health_check.get_database_url", return_value=url), \
             patch("src.health_check._open_connection", return_value=_FakePostgresConnection()):
            report = run_health_checks(
                db_path=tmp_path / "does-not-exist.db",
                api_key="sk_test_12345678",
                output_dir=tmp_path,
                environment="production",
                timezone_name="UTC",
                backup_dir=tmp_path / "missing-backups",
                scheduler_enabled=True,
            )
        assert report.overall_status == "healthy"
        checks = {check.name: check for check in report.checks}
        assert checks["persistent_storage"].status == "ok"
        assert checks["backup"].status == "ok"
        assert checks["data_freshness"].status == "ok"
        assert checks["worker_heartbeat"].status == "ok"
        assert checks["failed_jobs"].status == "ok"
        assert "secret" not in str(report.to_dict())

    def test_postgres_storage_does_not_require_local_file(self):
        from src.health_check import _check_persistent_storage
        with patch("src.health_check.get_database_url", return_value="postgresql://redacted"):
            check = _check_persistent_storage("/missing/local.db")
        assert check.status == "ok"
        assert "redacted" not in check.message
        assert check.details["storage_type"] == "managed_postgresql"

    def test_postgres_backup_directory_is_optional(self, tmp_path):
        from src.health_check import _check_backup_directory
        missing = tmp_path / "missing-backups"
        with patch("src.health_check.get_database_url", return_value="postgresql://redacted"):
            check = _check_backup_directory(missing)
        assert check.status == "ok"
        assert not missing.exists()
        assert "redacted" not in check.message

    def test_schedule_aware_freshness_allows_future_pregame_scan(self, tmp_path):
        from src.health_check import _check_data_freshness
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute("UPDATE scan_runs SET finished_at = ?", (old,))
        conn.execute(
            "INSERT INTO scheduled_jobs VALUES ('pending', 'pregame-check', ?)",
            (future,),
        )
        conn.commit()
        conn.close()
        check = _check_data_freshness(
            db_path,
            3600,
            scheduler_enabled=True,
            timezone_name="UTC",
        )
        assert check.status == "ok"
        assert check.details["schedule_aware"] is True
        assert check.details["pending_scheduled_scans"] == 1

    def test_schedule_aware_freshness_rejects_overdue_pregame_scan(self, tmp_path):
        from src.health_check import _check_data_freshness
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        overdue = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        conn.execute("UPDATE scan_runs SET finished_at = ?", (old,))
        conn.execute(
            "INSERT INTO scheduled_jobs VALUES ('pending', 'pregame-check', ?)",
            (overdue,),
        )
        conn.commit()
        conn.close()
        check = _check_data_freshness(
            db_path,
            3600,
            scheduler_enabled=True,
            timezone_name="UTC",
        )
        assert check.status == "error"
        assert check.details["overdue_pending_scans"] == 1

    def test_current_heartbeat_and_no_failed_jobs(self, tmp_path):
        from src.health_check import _check_failed_jobs, _check_worker_heartbeat
        db_path = _make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE worker_heartbeat (id INTEGER PRIMARY KEY, last_heartbeat TEXT, worker_pid INTEGER)"
        )
        conn.execute(
            "INSERT INTO worker_heartbeat VALUES (1, ?, 123)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()
        assert _check_worker_heartbeat(db_path).status == "ok"
        assert _check_failed_jobs(db_path).status == "ok"

    def test_database_ok(self, tmp_path):
        from src.health_check import _check_database
        check = _check_database(_make_db(tmp_path))
        assert check.status == "ok"
        assert "tables" in check.details

    def test_database_missing(self, tmp_path):
        from src.health_check import _check_database
        check = _check_database(tmp_path / "nonexistent.db")
        assert check.status == "error"

    def test_database_missing_tables(self, tmp_path):
        from src.health_check import _check_database
        db_path = tmp_path / "incomplete.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE games (id TEXT)")
        conn.commit()
        conn.close()
        check = _check_database(db_path)
        assert check.status == "error"
        assert "Missing tables" in check.message

    def test_disk_space_ok(self, tmp_path):
        from src.health_check import _check_disk_space
        check = _check_disk_space(tmp_path, min_mb=1)
        assert check.status == "ok"

    def test_disk_space_low(self, tmp_path):
        from src.health_check import _check_disk_space
        check = _check_disk_space(tmp_path, min_mb=999999999)
        assert check.status == "error"

    def test_data_freshness_ok(self, tmp_path):
        from src.health_check import _check_data_freshness
        check = _check_data_freshness(_make_db(tmp_path), 3600)
        assert check.status == "ok"

    def test_data_freshness_no_runs(self, tmp_path):
        from src.health_check import _check_data_freshness
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE scan_runs (started_at TEXT, finished_at TEXT, "
            "run_type TEXT, error_message TEXT)"
        )
        conn.commit()
        conn.close()
        check = _check_data_freshness(db_path, 3600)
        assert check.status == "warning"

    def test_data_freshness_db_error(self, tmp_path):
        from src.health_check import _check_data_freshness
        check = _check_data_freshness(tmp_path / "nosuch.db", 3600)
        assert check.status == "warning"

    def test_api_key_ok(self):
        from src.health_check import _check_api_key
        check = _check_api_key("sk_test_12345678")
        assert check.status == "ok"

    def test_api_key_missing(self):
        from src.health_check import _check_api_key
        check = _check_api_key("")
        assert check.status == "error"

    def test_api_key_short(self):
        from src.health_check import _check_api_key
        check = _check_api_key("short")
        assert check.status == "warning"

    def test_output_dir_ok(self, tmp_path):
        from src.health_check import _check_output_dir
        check = _check_output_dir(tmp_path / "output")
        assert check.status == "ok"

    def test_health_report_structure(self, tmp_path):
        from src.health_check import run_health_checks
        report = run_health_checks(
            db_path=_make_db(tmp_path),
            api_key="sk_test_12345678",
            output_dir=str(tmp_path),
        )
        assert report.overall_status in ("healthy", "degraded", "unhealthy")
        assert report.check_count > 0
        assert report.ok_count + report.warning_count + report.error_count == report.check_count

    def test_health_report_to_dict(self, tmp_path):
        from src.health_check import run_health_checks
        report = run_health_checks(
            db_path=_make_db(tmp_path),
            api_key="sk_test_12345678",
            output_dir=str(tmp_path),
        )
        d = report.to_dict()
        assert "overall_status" in d
        assert "checks" in d
        assert isinstance(d["checks"], list)

    def test_health_report_unhealthy_on_error(self, tmp_path):
        from src.health_check import run_health_checks
        report = run_health_checks(
            db_path=tmp_path / "nonexistent.db",
            api_key="",
            output_dir=str(tmp_path),
        )
        assert report.overall_status == "unhealthy"
        assert report.error_count > 0

    def test_health_report_healthy_when_all_ok(self, tmp_path):
        from src.health_check import run_health_checks
        report = run_health_checks(
            db_path=_make_db(tmp_path),
            api_key="sk_test_12345678",
            output_dir=str(tmp_path),
        )
        assert report.ok_count > 0

    def test_google_sheets_check_import_error(self):
        from src.health_check import _check_google_sheets
        with patch.dict("sys.modules", {"google.oauth2.credentials": None, "googleapiclient": None}):
            check = _check_google_sheets(Path("dummy"))
            assert check.status == "warning"

    def test_discord_check_ok(self):
        from src.health_check import _check_discord
        check = _check_discord()
        assert check.status == "ok"

    def test_report_overall_degraded(self):
        from src.health_check import HealthReport, HealthCheck
        report = HealthReport(overall_status="healthy", timestamp="test")
        report.add(HealthCheck(name="a", status="ok", message="fine"))
        report.add(HealthCheck(name="b", status="warning", message="hmm"))
        assert report.overall_status == "degraded"
        assert report.warning_count == 1

    def test_report_overall_unhealthy(self):
        from src.health_check import HealthReport, HealthCheck
        report = HealthReport(overall_status="healthy", timestamp="test")
        report.add(HealthCheck(name="a", status="ok", message="fine"))
        report.add(HealthCheck(name="b", status="error", message="bad"))
        assert report.overall_status == "unhealthy"
        assert report.error_count == 1
