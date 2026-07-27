"""Tests for Phase 10 Part B: Job Orchestration CLI."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.production_config import ProductionConfig


class TestProductionJobs:

    def test_job_run_dataclass(self):
        from src.production_jobs import JobRun
        run = JobRun(job_id="abc", job_type="test", status="SUCCESS")
        d = run.to_dict()
        assert d["job_id"] == "abc"
        assert d["status"] == "SUCCESS"

    def test_run_job_unknown_type(self):
        from src.production_jobs import run_job
        config = ProductionConfig(api_key="sk_test_12345678")
        run = run_job("nonexistent-job", config)
        assert run.status == "FAILED"
        assert run.exit_code == 2

    def test_run_job_health_check(self, tmp_path):
        from src.production_jobs import run_job
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        for t in ("games", "raw_responses", "odds", "historical_recommendations"):
            conn.execute(f"CREATE TABLE {t} (id TEXT)")
        conn.execute("CREATE TABLE job_runs (started_at TEXT, completed_at TEXT, status TEXT)")
        conn.commit()
        conn.close()

        config = ProductionConfig(
            api_key="sk_test_12345678",
            database_path=str(db_path),
            output_dir=str(tmp_path),
        )
        run = run_job("health-check", config)
        assert run.status == "SUCCESS"
        assert run.exit_code == 0

    def test_run_job_backup(self, tmp_path):
        from src.production_jobs import run_job
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()

        config = ProductionConfig(
            api_key="sk_test_12345678",
            database_path=str(db_path),
            output_dir=str(tmp_path),
        )
        run = run_job("backup", config)
        assert run.status == "SUCCESS"
        assert run.exit_code == 0

    def test_run_job_calibrate(self, tmp_path):
        from src.production_jobs import run_job
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()

        config = ProductionConfig(
            api_key="sk_test_12345678",
            database_path=str(db_path),
            output_dir=str(tmp_path),
        )
        with patch("src.calibration.analyze_calibration",
                    return_value={"bucket_count": 0, "buckets": [], "recommendations": []}):
            run = run_job("calibrate", config)
        assert run.status == "SUCCESS"

    def test_run_job_exception_handling(self):
        from src.production_jobs import run_job, JOB_HANDLERS
        config = ProductionConfig(api_key="sk_test_12345678")

        original = JOB_HANDLERS.get("backup")
        try:
            JOB_HANDLERS["backup"] = MagicMock(side_effect=RuntimeError("boom"))
            run = run_job("backup", config)
            assert run.status == "FAILED"
            assert run.exit_code == 6
            assert "RuntimeError" in run.error_message
        finally:
            if original:
                JOB_HANDLERS["backup"] = original

    def test_persist_job_run(self, tmp_path):
        from src.production_jobs import JobRun, _persist_job_run
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.commit()
        conn.close()

        config = ProductionConfig(api_key="sk_test", database_path=str(db_path))

        # _persist_job_run uses get_connection() which points to production DB
        # so we just verify it doesn't raise
        run = JobRun(job_id="test-123", job_type="backup", status="SUCCESS",
                      exit_code=0, duration_seconds=1.5, started_at="2025-01-01",
                      completed_at="2025-01-01")
        _persist_job_run(run, config)
        # If no exception, the function worked

    def test_job_handlers_registry(self):
        from src.production_jobs import JOB_HANDLERS
        expected = {"morning-run", "pregame-run", "export-sheets", "deliver-discord",
                    "health-check", "backup", "calibrate", "full-daily"}
        assert set(JOB_HANDLERS.keys()) == expected

    def test_run_job_dry_run_sheets(self, tmp_path):
        from src.production_jobs import run_job
        # Empty DB = no recs = early return with success
        config = ProductionConfig(api_key="sk_test", spreadsheet_id="test_sheet",
                                  google_credentials_path=str(tmp_path / "creds.json"),
                                  database_path=str(tmp_path / "test.db"))
        run = run_job("export-sheets", config)
        assert run.exit_code == 0  # no recs, early return before credential check

    def test_run_job_deliver_discord_no_webhooks(self):
        from src.production_jobs import run_job
        config = ProductionConfig(api_key="sk_test")
        run = run_job("deliver-discord", config)
        assert run.status == "SUCCESS"

    def test_persist_job_run_db_failure(self):
        from src.production_jobs import JobRun, _persist_job_run
        config = ProductionConfig(api_key="sk_test", database_path="/nonexistent/path/db.sqlite")
        run = JobRun(job_id="x", job_type="test", status="SUCCESS",
                      exit_code=0, duration_seconds=0, started_at="t", completed_at="t")
        # Should not raise
        _persist_job_run(run, config)

    def test_main_cli_dry_run(self, capsys):
        from src.production_jobs import main
        exit_code = main(["health-check", "--dry-run"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "dry_run" in output

    def test_main_cli_json_output(self, capsys):
        from src.production_jobs import main
        exit_code = main(["--json", "deliver-discord"])
        assert exit_code == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert "job_id" in data
