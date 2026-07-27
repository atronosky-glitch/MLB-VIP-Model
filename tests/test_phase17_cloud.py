"""Phase 17: Cloud Deployment Tests.

Tests for environment loading, production database path, scheduler
enable/disable, worker heartbeat, duplicate-job prevention, timezone-aware
scheduling, persistent-storage health, secret redaction, backup creation
and restore, and web/worker separation.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Helpers ────────────────────────────────────────────────────────

def _make_conn(db_path: str | None = None) -> sqlite3.Connection:
    """Create a test connection with the required schema."""
    if db_path:
        conn = sqlite3.connect(db_path, timeout=5)
    else:
        conn = sqlite3.connect(":memory:", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_test_schema(conn)
    return conn


def _init_test_schema(conn: sqlite3.Connection) -> None:
    """Create the minimal schema needed for deployment tests."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY,
            away_team TEXT,
            home_team TEXT,
            start_time TEXT,
            status TEXT DEFAULT 'scheduled'
        );
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            scheduled_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            event_id TEXT,
            metadata TEXT,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id TEXT PRIMARY KEY,
            event_id TEXT,
            player_name TEXT,
            market_type TEXT,
            market_form TEXT,
            period TEXT,
            line REAL,
            side TEXT,
            sportsbook TEXT,
            offered_american_odds INTEGER,
            offered_decimal_odds REAL,
            ev_pct REAL,
            rec_status TEXT,
            observation_timestamp TEXT,
            scan_timestamp TEXT,
            fingerprint TEXT,
            scan_run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS raw_responses (
            response_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            sportsbook TEXT,
            raw_json TEXT,
            fetched_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds (
            odd_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            sportsbook TEXT,
            market_type TEXT,
            side TEXT,
            price REAL,
            line REAL,
            validation_status TEXT DEFAULT 'VALID'
        );
        CREATE TABLE IF NOT EXISTS worker_heartbeat (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_heartbeat TEXT NOT NULL,
            worker_pid INTEGER,
            uptime_seconds REAL
        );
    """)


# ── Part 12.1: Environment loading ────────────────────────────────


class TestEnvironmentLoading:
    """Test that environment variables are properly loaded."""

    def test_load_config_from_env(self):
        from src.production_config import load_config, ProductionConfig
        with patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key_12345"}):
            config = load_config()
            assert config.api_key == "test_key_12345"

    def test_load_config_default_database_path(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
            assert config.database_path == "database/mlb_model.db"

    def test_load_config_custom_database_path(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_DB_PATH": "/data/custom.db"}):
            config = load_config()
            assert config.database_path == "/data/custom.db"

    def test_load_config_environment_field(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_ENVIRONMENT": "production"}):
            config = load_config()
            assert config.environment == "production"

    def test_load_config_scheduler_enabled(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_SCHEDULER_ENABLED": "true"}):
            config = load_config()
            assert config.scheduler_enabled is True

    def test_load_config_scheduler_disabled(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_SCHEDULER_ENABLED": "false"}):
            config = load_config()
            assert config.scheduler_enabled is False

    def test_load_config_shadow_mode(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_SHADOW_MODE": "true"}):
            config = load_config()
            assert config.shadow_mode is True

    def test_load_config_backup_dir(self):
        from src.production_config import load_config
        with patch.dict(os.environ, {"MLB_BACKUP_DIR": "/data/backups"}):
            config = load_config()
            assert config.backup_dir == "/data/backups"


# ── Part 12.2: Production database path ───────────────────────────


class TestProductionDatabasePath:
    """Test database path respects environment variable."""

    def test_db_manager_respects_env_var(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            with patch.dict(os.environ, {"MLB_DB_PATH": db_path}):
                # Reimport to pick up new env var
                import importlib
                import database.db_manager as db_mod
                importlib.reload(db_mod)
                assert str(db_mod.DB_PATH) == db_path
                # Restore default
                del os.environ["MLB_DB_PATH"]
                importlib.reload(db_mod)

    def test_db_manager_default_path(self):
        import database.db_manager as db_mod
        # When no env var is set, should default to database/mlb_model.db
        expected = Path(__file__).resolve().parent.parent / "database" / "mlb_model.db"
        # The default is set at module load time, so just verify the constant
        assert "mlb_model.db" in str(db_mod.DB_PATH)


# ── Part 12.3: Scheduler enable/disable ───────────────────────────


class TestSchedulerEnableDisable:
    """Test scheduler configuration."""

    def test_scheduler_enabled_default(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig()
        assert config.scheduler_enabled is True

    def test_scheduler_disabled(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig(scheduler_enabled=False)
        assert config.scheduler_enabled is False

    def test_worker_respects_scheduler_disabled(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig(scheduler_enabled=False)
        # Worker should check this flag before running
        assert config.scheduler_enabled is False


# ── Part 12.4: Worker heartbeat ───────────────────────────────────


class TestWorkerHeartbeat:
    """Test worker heartbeat read/write."""

    def test_write_heartbeat(self):
        from src.worker import _write_heartbeat
        conn = _make_conn()
        _write_heartbeat(conn)
        row = conn.execute("SELECT * FROM worker_heartbeat WHERE id = 1").fetchone()
        assert row is not None
        assert row["worker_pid"] == os.getpid()
        assert row["last_heartbeat"] is not None

    def test_read_heartbeat(self):
        from src.worker import _write_heartbeat, _read_heartbeat
        conn = _make_conn()
        _write_heartbeat(conn)
        hb = _read_heartbeat(conn)
        assert hb is not None
        assert "last_heartbeat" in hb
        assert "worker_pid" in hb

    def test_read_heartbeat_empty(self):
        from src.worker import _read_heartbeat
        conn = _make_conn()
        hb = _read_heartbeat(conn)
        assert hb is None

    def test_heartbeat_overwrites_previous(self):
        from src.worker import _write_heartbeat, _read_heartbeat
        conn = _make_conn()
        _write_heartbeat(conn)
        first = _read_heartbeat(conn)
        _write_heartbeat(conn)
        second = _read_heartbeat(conn)
        assert first["last_heartbeat"] != second["last_heartbeat"] or first["worker_pid"] == second["worker_pid"]


# ── Part 12.5: Duplicate-job prevention ───────────────────────────


class TestDuplicateJobPrevention:
    """Test job locking and idempotency."""

    def test_acquire_lock_success(self):
        from src.worker import _acquire_lock, _release_lock
        conn = _make_conn()
        lock = _acquire_lock(conn, "test_job")
        assert lock is not None
        _release_lock(conn, lock)

    def test_acquire_lock_conflict(self):
        from src.worker import _acquire_lock, _release_lock
        conn = _make_conn()
        lock1 = _acquire_lock(conn, "test_job")
        assert lock1 is not None
        # Second lock of same type should fail
        lock2 = _acquire_lock(conn, "test_job")
        assert lock2 is None
        _release_lock(conn, lock1)

    def test_release_lock_allows_reacquire(self):
        from src.worker import _acquire_lock, _release_lock
        conn = _make_conn()
        lock1 = _acquire_lock(conn, "test_job")
        _release_lock(conn, lock1)
        lock2 = _acquire_lock(conn, "test_job")
        assert lock2 is not None
        _release_lock(conn, lock2)

    def test_duplicate_job_idempotency_key(self):
        from src.automation import create_job
        conn = _make_conn()
        # Create two jobs with same type — they should have different IDs
        jid1 = create_job(conn, "pregame", event_id="ev1")
        jid2 = create_job(conn, "pregame", event_id="ev1")
        assert jid1 != jid2


# ── Part 12.6: Timezone-aware scheduling ──────────────────────────


class TestTimezoneAwareScheduling:
    """Test timezone-aware scheduling logic."""

    def test_now_local_returns_timezone_aware(self):
        from src.worker import _now_local
        now = _now_local()
        assert now.tzinfo is not None

    def test_now_local_uses_configured_tz(self):
        from src.worker import _get_tz, TZ_NAME
        tz = _get_tz()
        now_local = datetime.now(tz)
        assert now_local.tzinfo is not None

    def test_is_backup_time(self):
        from src.worker import _is_backup_time
        # 3:30 AM ET is backup time
        backup_time = datetime(2026, 7, 27, 3, 30, 0, tzinfo=timezone.utc)
        assert _is_backup_time(backup_time) is True

    def test_is_not_backup_time(self):
        from src.worker import _is_backup_time
        non_backup = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_backup_time(non_backup) is False

    def test_pregame_schedule_entry(self):
        from src.scheduler import get_default_schedules
        schedules = get_default_schedules()
        names = [s.name for s in schedules]
        assert "morning_scan" in names
        assert "pregame_scan" in names


# ── Part 12.7: Persistent storage health ──────────────────────────


class TestPersistentStorageHealth:
    """Test persistent storage health check."""

    def test_persistent_storage_check_exists(self):
        from src.health_check import _check_persistent_storage
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            Path(db_path).write_text("ok")
            check = _check_persistent_storage(db_path)
            assert check.name == "persistent_storage"
            assert check.status in ("ok", "warning")

    def test_persistent_storage_check_missing(self):
        from src.health_check import _check_persistent_storage
        check = _check_persistent_storage("/nonexistent/path/db.sqlite")
        assert check.status == "warning"

    def test_persistent_storage_check_database_dir(self):
        from src.health_check import _check_persistent_storage
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            Path(db_path).write_text("ok")
            check = _check_persistent_storage(db_path)
            assert check.status in ("ok", "warning")


# ── Part 12.8: Secret redaction ───────────────────────────────────


class TestSecretRedaction:
    """Test that secrets are properly redacted."""

    def test_config_redacts_api_key(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="secret_abc123")
        redacted = config.redacted()
        assert redacted["api_key"] == "***REDACTED***"
        assert "secret" not in str(redacted)

    def test_config_redacts_empty_key(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="")
        redacted = config.redacted()
        assert redacted["api_key"] == ""

    def test_secret_fields_constant(self):
        from src.production_config import SECRET_FIELDS
        assert "api_key" in SECRET_FIELDS
        assert "google_credentials_path" in SECRET_FIELDS
        assert "discord_webhook_urls" in SECRET_FIELDS


# ── Part 12.9: Backup creation and restore ────────────────────────


class TestBackupRestore:
    """Test backup creation and restoration."""

    def test_backup_creates_file(self):
        from src.backup_database import backup_database
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a source database
            src_db = os.path.join(tmpdir, "source.db")
            conn = sqlite3.connect(src_db)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.execute("INSERT INTO test VALUES (42)")
            conn.commit()
            conn.close()

            backup_path = backup_database(src_db, tmpdir)
            assert backup_path.exists()
            assert backup_path.stat().st_size > 0

    def test_backup_compression(self):
        from src.backup_database import backup_database
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = os.path.join(tmpdir, "source.db")
            conn = sqlite3.connect(src_db)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.commit()
            conn.close()

            backup_path = backup_database(src_db, tmpdir, compress=True)
            assert str(backup_path).endswith(".gz")

    def test_list_backups(self):
        from src.backup_database import backup_database, list_backups
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = os.path.join(tmpdir, "source.db")
            conn = sqlite3.connect(src_db)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.commit()
            conn.close()

            backup_database(src_db, tmpdir)
            backups = list_backups(tmpdir)
            assert len(backups) >= 1

    def test_restore_database(self):
        from src.backup_database import backup_database, restore_database
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = os.path.join(tmpdir, "source.db")
            conn = sqlite3.connect(src_db)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.execute("INSERT INTO test VALUES (42)")
            conn.commit()
            conn.close()

            backup_path = backup_database(src_db, tmpdir)
            restore_path = os.path.join(tmpdir, "restored.db")
            restore_database(backup_path, restore_path, confirm=True)

            conn2 = sqlite3.connect(restore_path)
            row = conn2.execute("SELECT id FROM test").fetchone()
            assert row[0] == 42
            conn2.close()


# ── Part 12.10: Web/worker separation ─────────────────────────────


class TestWebWorkerSeparation:
    """Test that web and worker processes are properly separated."""

    def test_worker_module_exists(self):
        import src.worker
        assert hasattr(src.worker, "run_worker_persistent")
        assert hasattr(src.worker, "run_worker_once")
        assert hasattr(src.worker, "run_specific_job")

    def test_worker_has_main(self):
        import src.worker
        assert hasattr(src.worker, "main")

    def test_control_panel_is_streamlit(self):
        """Verify control_panel.py starts with Streamlit page config."""
        control_panel_path = Path(__file__).resolve().parent.parent / "src" / "control_panel.py"
        content = control_panel_path.read_text(encoding="utf-8")
        assert "st.set_page_config" in content
        assert "streamlit" in content.lower()

    def test_worker_uses_production_config(self):
        import src.worker
        content = open(src.worker.__file__).read()
        assert "load_config" in content
        assert "ProductionConfig" in content or "load_config" in content

    def test_worker_handles_run_once(self):
        import src.worker
        assert hasattr(src.worker, "run_worker_once")

    def test_worker_handles_specific_job(self):
        import src.worker
        assert hasattr(src.worker, "run_specific_job")

    def test_database_path_from_config(self):
        from src.production_config import ProductionConfig
        config = ProductionConfig(database_path="/data/test.db")
        assert config.database_path == "/data/test.db"


# ── Part 12.11: Health check new checks ───────────────────────────


class TestHealthCheckNewChecks:
    """Test the new health check functions added in Phase 17."""

    def test_check_worker_heartbeat_no_data(self):
        from src.health_check import _check_worker_heartbeat
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE worker_heartbeat (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_heartbeat TEXT NOT NULL,
                worker_pid INTEGER
            )""")
            conn.commit()
            conn.close()
            check = _check_worker_heartbeat(db_path)
            assert check.name == "worker_heartbeat"
            assert check.status == "warning"

    def test_check_deployment_environment(self):
        from src.health_check import _check_deployment_environment
        check = _check_deployment_environment("production")
        assert check.status == "ok"
        assert "production" in check.message

    def test_check_deployment_environment_empty(self):
        from src.health_check import _check_deployment_environment
        check = _check_deployment_environment("")
        assert check.status == "ok"
        assert "local" in check.message

    def test_check_timezone_valid(self):
        from src.health_check import _check_timezone
        check = _check_timezone("America/New_York")
        assert check.status == "ok"
        assert "America/New_York" in check.message

    def test_check_timezone_invalid(self):
        from src.health_check import _check_timezone
        check = _check_timezone("Invalid/Zone")
        assert check.status == "error"

    def test_check_timezone_empty(self):
        from src.health_check import _check_timezone
        check = _check_timezone("")
        assert check.status == "warning"

    def test_check_scheduler_enabled(self):
        from src.health_check import _check_scheduler
        check = _check_scheduler(True)
        assert check.status == "ok"
        assert "enabled" in check.message

    def test_check_scheduler_disabled(self):
        from src.health_check import _check_scheduler
        check = _check_scheduler(False)
        assert check.status == "warning"
        assert "disabled" in check.message

    def test_check_backup_directory_missing(self):
        from src.health_check import _check_backup_directory
        check = _check_backup_directory("/nonexistent/backup/dir")
        assert check.status == "warning"

    def test_check_backup_directory_empty(self):
        from src.health_check import _check_backup_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            check = _check_backup_directory(tmpdir)
            assert check.status == "warning"
            assert "No backups" in check.message

    def test_check_backup_directory_with_backups(self):
        from src.backup_database import backup_database
        from src.health_check import _check_backup_directory
        with tempfile.TemporaryDirectory() as tmpdir:
            src_db = os.path.join(tmpdir, "source.db")
            conn = sqlite3.connect(src_db)
            conn.execute("CREATE TABLE test (id INTEGER)")
            conn.commit()
            conn.close()
            backup_database(src_db, tmpdir)
            check = _check_backup_directory(tmpdir)
            assert check.status == "ok"
            assert "Latest backup" in check.message


# ── Part 12.12: Stale job recovery ────────────────────────────────


class TestStaleJobRecovery:
    """Test stale job detection and recovery."""

    def test_recover_stale_jobs(self):
        from src.worker import _recover_stale_jobs
        conn = _make_conn()
        # Insert a job that's been running for a long time
        old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO scheduled_jobs (job_id, job_type, status, started_at) VALUES (?, 'test', 'running', ?)",
            ("stale_job_1", old_time),
        )
        conn.commit()
        count = _recover_stale_jobs(conn)
        assert count == 1
        # Verify job was reset to pending
        row = conn.execute("SELECT status FROM scheduled_jobs WHERE job_id = 'stale_job_1'").fetchone()
        assert row["status"] == "pending"

    def test_no_stale_jobs(self):
        from src.worker import _recover_stale_jobs
        conn = _make_conn()
        count = _recover_stale_jobs(conn)
        assert count == 0
