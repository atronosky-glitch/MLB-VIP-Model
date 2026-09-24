"""Worker reliability additions (launch audit 2026-09-24): structured job
telemetry with memory, durable heartbeat (independent thread, current job,
last success/failure), and connection recovery."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import src.worker as worker
from database.connection import DB
from src.automation import create_job
from src.runtime_metrics import (
    HeartbeatThread, WorkerState, record_job_outcome, rss_mb, sport_for_job, summarize_result, write_heartbeat,
)

SECRET = "abcd1234efgh5678ijkl9012mnop3456qrst"


class TestRuntimeMetrics:
    def test_rss_is_a_positive_number(self):
        value = rss_mb()
        assert value is None or value > 1

    @pytest.mark.parametrize("job,sport", [
        ("morning-run", "MLB"), ("pregame-check", "MLB"), ("mlb-props-scan", "MLB"),
        ("morning-run-nfl", "NFL"), ("nfl-props-scan", "NFL"), ("morning-run-cfb", "NCAAF"),
        ("wnba-odds-scan", "WNBA"), ("wnba-props-scan", "WNBA"),
        ("grading", "ALL"), ("backup", "ALL"), ("customer-autobet-scan", "ALL"), ("arb-middle-scan", "ALL"),
    ])
    def test_every_job_maps_to_a_sport(self, job, sport):
        assert sport_for_job(job) == sport

    def test_result_summary_keeps_only_small_scalars(self):
        summary = summarize_result({
            "status": "success", "n": 3, "ok": True, "nested": {"a": 1}, "long": "x" * 500,
            "list": [1, 2], **{f"k{i}": i for i in range(20)},
        })
        assert summary["status"] == "success" and summary["n"] == 3 and summary["ok"] is True
        assert "nested" not in summary and "long" not in summary and "list" not in summary
        assert len(summary) <= 8


class TestHeartbeatRow:
    def test_beat_records_pid_uptime_rss_and_current_job(self, db_conn):
        state = WorkerState()
        state.start_job("morning-run-nfl")
        write_heartbeat(db_conn, state)
        row = dict(db_conn.execute("SELECT * FROM worker_heartbeat").fetchone())
        assert row["worker_pid"] and row["last_heartbeat"] and row["current_job"] == "morning-run-nfl"
        state.end_job()
        write_heartbeat(db_conn, state)
        assert dict(db_conn.execute("SELECT * FROM worker_heartbeat").fetchone())["current_job"] is None

    def test_last_success_and_last_failure_are_tracked_and_sanitized(self, db_conn):
        record_job_outcome(db_conn, "grading", True)
        record_job_outcome(db_conn, "morning-run-nfl", False, f"HTTPError apiKey={SECRET} failed")
        row = dict(db_conn.execute("SELECT * FROM worker_heartbeat").fetchone())
        assert row["last_success_job"] == "grading" and row["last_failure_job"] == "morning-run-nfl"
        assert SECRET not in str(row) and "<redacted>" in row["last_failure_message"]

    def test_single_row_only(self, db_conn):
        for _ in range(3):
            write_heartbeat(db_conn)
        assert db_conn.execute("SELECT COUNT(*) AS n FROM worker_heartbeat").fetchone()["n"] == 1

    def test_writing_never_raises_even_on_a_dead_connection(self):
        dead = mock.Mock()
        dead.execute.side_effect = RuntimeError("connection closed")
        write_heartbeat(dead)
        record_job_outcome(dead, "x", False, "y")


class TestHeartbeatThreadBeatsDuringALongJob:
    def test_heartbeat_advances_while_the_main_thread_is_busy(self, tmp_path):
        path = str(tmp_path / "hb.db")

        def connect():
            raw = sqlite3.connect(path, timeout=10)
            raw.row_factory = sqlite3.Row
            return DB(raw)

        connect().close()
        state = WorkerState()
        thread = HeartbeatThread(connect, interval_seconds=0.05, state=state)
        state.start_job("morning-run")
        thread.start()
        try:
            time.sleep(0.4)                      # a "long job" occupying the main thread
            reader = connect()
            first = dict(reader.execute("SELECT last_heartbeat, current_job FROM worker_heartbeat").fetchone())
            time.sleep(0.3)
            second = dict(reader.execute("SELECT last_heartbeat FROM worker_heartbeat").fetchone())
            reader.close()
        finally:
            thread.stop()
            thread.join(timeout=3)
        assert first["current_job"] == "morning-run"
        assert second["last_heartbeat"] > first["last_heartbeat"]
        assert not thread.is_alive()

    def test_thread_survives_connection_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            raise RuntimeError("db down")

        thread = HeartbeatThread(flaky, interval_seconds=0.02)
        thread.start()
        time.sleep(0.2)
        thread.stop()
        thread.join(timeout=3)
        assert calls["n"] >= 3


class TestJobTelemetry:
    def _due_job(self, db_conn, job_type="test-unknown"):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        return create_job(db_conn, job_type, scheduled_at=past)

    def test_job_start_and_end_are_logged_with_all_fields(self, db_conn, caplog):
        job_id = self._due_job(db_conn)
        with caplog.at_level(logging.INFO, logger="src.worker"):
            worker._process_pending_jobs(db_conn, None)
        text = caplog.text
        assert "JOB_START job=test-unknown" in text and f"run_id={job_id}" in text
        end = next(line for line in text.splitlines() if "JOB_END" in line)
        for field in ("sport=ALL", "status=", "duration_s=", "rss_before_mb=", "rss_after_mb=", "rss_delta_mb=", "summary="):
            assert field in end

    def test_a_crashing_job_is_logged_recorded_and_isolated(self, db_conn, caplog):
        bad = self._due_job(db_conn, "morning-run-nfl")
        good = self._due_job(db_conn, "grading")
        calls = []

        def fake_execute(job_type, conn, config, event_id=None):
            calls.append(job_type)
            if job_type == "morning-run-nfl":
                raise RuntimeError(f"boom apiKey={SECRET}")
            return {"status": "success", "graded": 4}

        with caplog.at_level(logging.INFO), mock.patch.object(worker, "_execute_job", side_effect=fake_execute):
            executed = worker._process_pending_jobs(db_conn, None)
        assert sorted(calls) == ["grading", "morning-run-nfl"]          # one failure did not stop the other
        assert executed == 1
        assert db_conn.execute("SELECT status FROM scheduled_jobs WHERE job_id = ?", (bad,)).fetchone()["status"] == "failed"
        assert db_conn.execute("SELECT status FROM scheduled_jobs WHERE job_id = ?", (good,)).fetchone()["status"] == "completed"
        assert SECRET not in caplog.text
        assert "sport=NFL" in caplog.text and "status=error" in caplog.text
        hb = dict(db_conn.execute("SELECT * FROM worker_heartbeat").fetchone())
        assert hb["last_failure_job"] == "morning-run-nfl" and hb["last_success_job"] == "grading"
        assert SECRET not in str(hb)
        assert worker.WORKER_STATE.snapshot() == (None, None)           # current job cleared after a crash

    def test_failure_error_message_persisted_on_the_job_row_is_sanitized(self, db_conn):
        job_id = self._due_job(db_conn, "grading")
        with mock.patch.object(worker, "_execute_job", side_effect=RuntimeError(f"url https://x.test/a?apiKey={SECRET}")):
            worker._process_pending_jobs(db_conn, None)
        msg = db_conn.execute("SELECT error_message FROM scheduled_jobs WHERE job_id = ?", (job_id,)).fetchone()["error_message"]
        assert SECRET not in msg and "?" not in msg


class TestConnectionRecovery:
    def test_healthy_connection_is_kept_after_a_rollback(self, db_conn):
        assert worker._recover_connection(db_conn, mock.Mock()) is db_conn

    def test_dead_connection_is_replaced(self):
        dead = mock.Mock()
        dead.rollback.side_effect = RuntimeError("closed")
        dead.execute.side_effect = RuntimeError("closed")
        fresh = mock.Mock()
        config = mock.Mock(database_path=":memory:")
        with mock.patch.object(worker, "get_connection", return_value=fresh):
            assert worker._recover_connection(dead, config) is fresh
        dead.close.assert_called()

    def test_failed_reconnect_keeps_the_old_handle_and_never_raises(self):
        dead = mock.Mock()
        dead.execute.side_effect = RuntimeError("closed")
        with mock.patch.object(worker, "get_connection", side_effect=RuntimeError("still down")):
            assert worker._recover_connection(dead, mock.Mock(database_path="x")) is dead

    def test_loop_error_handler_uses_recovery(self):
        import inspect
        src = inspect.getsource(worker.run_worker_persistent)
        assert "conn = _recover_connection(conn, config)" in src
        assert "HeartbeatThread(" in src and "heartbeat_thread.stop()" in src
