"""src/production_readiness.py: read-only PASS/WARN/FAIL report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.production_readiness as pr
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key
from src.runtime_metrics import record_job_outcome, write_heartbeat


def _cfg(**over):
    base = dict(live_trading_enabled=False, kalshi_live_enabled=False, polymarket_us_live_enabled=False,
                kalshi_autobet_server_max_order_usd=100.0, polymarket_us_autobet_server_max_order_usd=100.0,
                discord_webhook_urls="", discord_webhook_urls_arb_middle="", discord_webhook_urls_middle="",
                validate=lambda: [])
    base.update(over)
    return SimpleNamespace(**base)


def _by(checks):
    return {c.subsystem: c for c in checks}


class TestChecks:
    def test_no_heartbeat_is_a_failure(self, db_conn):
        assert pr.check_worker_heartbeat(db_conn).status == pr.FAIL

    def test_fresh_heartbeat_passes_and_stale_fails(self, db_conn):
        write_heartbeat(db_conn)
        assert pr.check_worker_heartbeat(db_conn).status == pr.PASS
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        db_conn.execute("UPDATE worker_heartbeat SET last_heartbeat = ?", (old,))
        db_conn.commit()
        assert pr.check_worker_heartbeat(db_conn).status == pr.FAIL

    def test_heartbeat_details_include_last_failure(self, db_conn):
        write_heartbeat(db_conn)
        record_job_outcome(db_conn, "morning-run-nfl", False, "boom")
        assert pr.check_worker_heartbeat(db_conn).details["last_failure_job"] == "morning-run-nfl"

    def test_live_flags_on_warn_and_off_pass(self, db_conn):
        assert pr.check_autobet(db_conn, _cfg()).status == pr.PASS
        c = pr.check_autobet(db_conn, _cfg(live_trading_enabled=True, kalshi_live_enabled=True))
        assert c.status == pr.WARN and "LIVE_TRADING_ENABLED" in c.message

    def test_production_on_sqlite_fails(self, db_conn, monkeypatch):
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert pr.check_database(db_conn).status == pr.FAIL

    def test_database_backend_never_shows_credentials(self, db_conn, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:TOPSECRETPW@dpg-x.example.com/mydb")
        c = pr.check_database(db_conn)
        assert "TOPSECRETPW" not in str(c) and "user" not in c.message.split("host=")[-1].split()[0]
        assert "dpg-x.example.com" in c.message

    def test_encryption_key_check(self, monkeypatch):
        monkeypatch.delenv(ENCRYPTION_KEY_ENV_VAR, raising=False)
        assert pr.check_credential_encryption().status == pr.FAIL
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())
        assert pr.check_credential_encryption().status == pr.PASS

    def test_production_without_admin_password_fails(self, db_conn, monkeypatch):
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")
        monkeypatch.delenv("MLB_ADMIN_PASSWORD", raising=False)
        assert pr.check_auth(db_conn).status == pr.FAIL
        monkeypatch.setenv("MLB_ADMIN_PASSWORD", "x")
        assert pr.check_auth(db_conn).status in (pr.PASS, pr.WARN)

    def test_missing_email_config_warns(self, db_conn, monkeypatch):
        monkeypatch.setenv("MLB_ADMIN_PASSWORD", "x")
        for k in ("SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL", "SITE_BASE_URL"):
            monkeypatch.delenv(k, raising=False)
        c = pr.check_auth(db_conn)
        assert c.status == pr.WARN and "NOT send" in c.message

    def test_discord_counts_only_never_urls(self):
        secret = "https://discord.com/api/webhooks/123456/AbCdEfGhIjKlMnOpQrStUv"
        c = pr.check_discord(_cfg(discord_webhook_urls=secret + "," + secret))
        assert c.status == pr.PASS and c.details["ev_webhooks"] == 2 and "AbCdEf" not in str(c)
        assert pr.check_discord(_cfg()).status == pr.WARN

    def test_backup_check_is_honest_about_postgres(self, db_conn, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
        c = pr.check_backups(db_conn)
        assert c.status == pr.WARN and "NOT verifiable" in c.message

    def test_schema_check_passes_on_a_real_init_db_schema(self, tmp_path):
        import sqlite3
        from database.connection import DB
        from database.db_manager import init_db
        path = str(tmp_path / "s.db")
        init_db(path)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        assert pr.check_schema(DB(raw)).status == pr.PASS

    def test_schema_check_fails_when_launch_migrations_are_missing(self, tmp_path):
        import sqlite3
        from database.connection import DB
        from database.db_manager import init_db
        path = str(tmp_path / "s2.db")
        init_db(path)
        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        raw.execute("ALTER TABLE customer_accounts DROP COLUMN disabled")
        c = pr.check_schema(DB(raw))
        assert c.status == pr.FAIL and "customer_accounts" in c.message

    def test_a_check_never_raises_on_a_broken_connection(self):
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("down")

            def rollback(self):
                pass

        for fn in (pr.check_worker_heartbeat, pr.check_model_runs, pr.check_recommendations_fresh,
                   pr.check_odds_data, pr.check_database, pr.check_schema):
            assert fn(Broken()).status in (pr.FAIL, pr.WARN)


class TestRunnerAndSafety:
    def test_offline_run_returns_every_subsystem(self, db_conn):
        names = {c.subsystem for c in pr.run_checks(db_conn, _cfg(), skip_network=True)}
        assert {"config", "database", "schema", "worker_heartbeat", "model_runs", "official_picks", "odds_data",
                "customer_auth", "credential_encryption", "autobet", "discord", "backups", "providers"} <= names

    def test_report_verdict_wording(self):
        assert "NOT READY" in pr.format_report([pr.Check("a", pr.FAIL, "x")])
        assert "READY WITH WARNINGS" in pr.format_report([pr.Check("a", pr.WARN, "x")])
        assert pr.format_report([pr.Check("a", pr.PASS, "x")]).rstrip().endswith("READY")

    def test_module_can_never_place_an_order(self):
        src = Path("src/production_readiness.py").read_text(encoding="utf-8")
        for forbidden in ("_submit_authorized_order", "execute_authorized", "place_order", "cancel_order",
                          "approval.approve", "prepare_order", ".post(", "INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert forbidden not in src, forbidden

    def test_exit_code_reflects_failures(self, db_conn, monkeypatch):
        import database.db_manager as dbm

        class _Keep:
            def __init__(self, inner):
                self._inner = inner

            def close(self):
                pass

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(dbm, "get_connection", lambda *a, **k: _Keep(db_conn))
        assert pr.main(["--skip-network"]) == 1          # no worker heartbeat in a fresh test DB


class TestOddsApiCreditsReporting:
    """The credits figure must be the provider's header verbatim, for whichever key the process holds."""

    @pytest.mark.parametrize("header", ["1431", "1", "0", "18569"])
    def test_reports_header_value_verbatim(self, monkeypatch, header):
        monkeypatch.setenv("THE_ODDS_API_KEY", "k" * 32)
        monkeypatch.delenv("SPORTSODDS_API_KEY", raising=False)

        def fake_get(url, **kwargs):
            return SimpleNamespace(status_code=200, headers={"x-requests-remaining": header}, json=lambda: {})

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        check = next(c for c in pr.check_providers(skip_network=False) if c.subsystem == "provider_odds_api")
        assert check.message.endswith(f"credits remaining {header}")
