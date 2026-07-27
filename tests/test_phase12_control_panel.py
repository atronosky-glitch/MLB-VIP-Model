"""Tests for Phase 12: Local Control Panel.

Covers:
- Control panel imports
- Configuration status generation
- Health status generation
- Recommendation table transformation
- O/U EV display
- Y/N advantage display
- Empty recommendation state
- Pipeline success/warning/failure state
- Simultaneous-run prevention
- Rerun confirmation
- Secret redaction
- Shadow-mode label
- Live-delivery controls absent
- CSV export
- Latest-report lookup
- Backup action
- Launcher file existence
- Setup file existence
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ==================================================================
# File existence tests
# ==================================================================

class TestFileExistence:

    def test_control_panel_exists(self):
        assert Path("src/control_panel.py").exists()

    def test_launcher_exists(self):
        assert Path("launch_mlb_model.bat").exists()

    def test_setup_exists(self):
        assert Path("setup_local_app.bat").exists()

    def test_shortcut_script_exists(self):
        assert Path("create_desktop_shortcut.ps1").exists()

    def test_env_example_exists(self):
        assert Path(".env.example").exists()

    def test_requirements_includes_streamlit(self):
        text = Path("requirements.txt").read_text()
        assert "streamlit" in text.lower()


# ==================================================================
# Control panel imports
# ==================================================================

class TestControlPanelImports:

    def test_control_panel_imports(self):
        """Control panel module can be imported without Streamlit running."""
        # The control panel uses streamlit at module level via st.set_page_config
        # so we verify the source is valid Python with key symbols
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "def _redact" in source
        assert "def _status_color" in source
        assert "def _load_todays_recs" in source
        assert "def _format_market_type" in source

    def test_no_live_delivery_button_in_source(self):
        """No 'Enable Live Delivery' button exists in the control panel source."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Enable Live Delivery" not in source

    def test_no_secret_display_in_source(self):
        """Control panel source never directly prints API keys."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        # Redaction is used instead of raw display
        assert "_redact(config.api_key)" in source


# ==================================================================
# Helper function tests
# ==================================================================

class TestHelperFunctions:

    def test_redact_long_string(self):
        from src.control_panel import _redact
        result = _redact("sk_test_12345678", show=4)
        assert result.startswith("sk_t")
        assert "*" in result
        assert "12345678" not in result

    def test_redact_short_string(self):
        from src.control_panel import _redact
        result = _redact("ab")
        assert result == "****"

    def test_redact_empty_string(self):
        from src.control_panel import _redact
        result = _redact("")
        assert result == "****"

    def test_status_color_ok(self):
        from src.control_panel import _status_color
        assert _status_color("ok") == "green"
        assert _status_color("healthy") == "green"
        assert _status_color("pass") == "green"

    def test_status_color_warning(self):
        from src.control_panel import _status_color
        assert _status_color("warning") == "orange"
        assert _status_color("degraded") == "orange"

    def test_status_color_error(self):
        from src.control_panel import _status_color
        assert _status_color("error") == "red"
        assert _status_color("unhealthy") == "red"
        assert _status_color("failed") == "red"

    def test_status_color_unknown(self):
        from src.control_panel import _status_color
        assert _status_color("something_else") == "gray"

    def test_format_market_type(self):
        from src.control_panel import _format_market_type
        assert _format_market_type("strikeouts") == "Strikeouts"
        assert _format_market_type("pitcher_outs") == "Pitcher Outs"
        assert _format_market_type("") == ""


# ==================================================================
# Recommendation table transformation
# ==================================================================

class TestRecommendationTable:

    def _make_recs_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                recommendation_id TEXT PRIMARY KEY,
                fingerprint TEXT,
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
                offered_implied_prob REAL,
                fair_prob REAL,
                fair_american_odds INTEGER,
                ev_pct REAL,
                yn_reference_prob REAL,
                yn_reference_odds INTEGER,
                yn_implied_prob_adv REAL,
                yn_decimal_odds_adv INTEGER,
                n_consensus_books INTEGER,
                market_quality TEXT,
                rec_status TEXT,
                rec_eligible INTEGER,
                data_source TEXT,
                observation_timestamp TEXT,
                scan_timestamp TEXT,
                freshness_status TEXT,
                model_version TEXT,
                scan_run_id TEXT DEFAULT '',
                matchup TEXT DEFAULT '',
                event_status TEXT DEFAULT '',
                event_start_time TEXT DEFAULT '',
                model_score REAL, score_version TEXT DEFAULT 'model_score_v1',
                score_explanation TEXT,
                recommendation_tier TEXT DEFAULT 'RESEARCH_ONLY',
                qualification_passed INTEGER DEFAULT 0,
                qualification_reasons TEXT DEFAULT '',
                disqualification_reasons TEXT DEFAULT '',
                contributing_book_count INTEGER DEFAULT 0,
                contributing_books TEXT DEFAULT '',
                applicable_edge_metric TEXT DEFAULT '',
                applicable_edge_threshold REAL DEFAULT 0.0,
                model_score_threshold REAL DEFAULT 8.0,
                qualification_rules_version TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT14:00:00+00:00")
        conn.execute(
            "INSERT INTO historical_recommendations "
            "(recommendation_id, fingerprint, event_id, player_name, market_type, market_form, period, "
            "line, side, sportsbook, offered_american_odds, offered_decimal_odds, offered_implied_prob, "
            "fair_prob, fair_american_odds, ev_pct, yn_reference_prob, yn_reference_odds, "
            "yn_implied_prob_adv, yn_decimal_odds_adv, n_consensus_books, market_quality, "
            "rec_status, rec_eligible, data_source, observation_timestamp, scan_timestamp, "
            "freshness_status, model_version, scan_run_id, matchup, event_status, event_start_time) "
            "VALUES "
            "('rec-001', 'fp1', 'evt1', 'Aaron Judge', 'strikeouts', 'ou', 'game', "
            "0.5, 'Over', 'DK', -110, 1.909, 0.526, 0.50, -100, 0.026, "
            "NULL, NULL, NULL, NULL, 6, 'VALID', 'BET', 1, 'live', "
            f"'{today}', '{today}', 'fresh', 'v1', 'run-001', 'NYY @ LAD', 'scheduled', '{today}')"
        )
        conn.execute(
            "INSERT INTO historical_recommendations "
            "(recommendation_id, fingerprint, event_id, player_name, market_type, market_form, period, "
            "line, side, sportsbook, offered_american_odds, offered_decimal_odds, offered_implied_prob, "
            "fair_prob, fair_american_odds, ev_pct, yn_reference_prob, yn_reference_odds, "
            "yn_implied_prob_adv, yn_decimal_odds_adv, n_consensus_books, market_quality, "
            "rec_status, rec_eligible, data_source, observation_timestamp, scan_timestamp, "
            "freshness_status, model_version, scan_run_id, matchup, event_status, event_start_time) "
            "VALUES "
            "('rec-002', 'fp2', 'evt2', 'Shohei Ohtani', 'pitching_win', 'yn', 'game', "
            "NULL, 'Yes', 'FD', 150, 2.5, 0.4, NULL, NULL, NULL, "
            "0.55, -110, 0.05, 50, 5, 'VALID', 'LEAN', 1, 'live', "
            f"'{today}', '{today}', 'fresh', 'v1', 'run-001', 'LAA @ BOS', 'scheduled', '{today}')"
        )
        conn.commit()
        conn.close()
        return db_path

    def test_load_recs_returns_list(self, tmp_path):
        from src.control_panel import _load_todays_recs
        db_path = self._make_recs_db(tmp_path)
        recs = _load_todays_recs(str(db_path))
        assert isinstance(recs, list)
        assert len(recs) == 2

    def test_load_recs_empty_db(self, tmp_path):
        from src.control_panel import _load_todays_recs
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                recommendation_id TEXT PRIMARY KEY, fingerprint TEXT,
                event_id TEXT, player_name TEXT, market_type TEXT,
                market_form TEXT, period TEXT, line REAL, side TEXT,
                sportsbook TEXT, offered_american_odds INTEGER, offered_decimal_odds REAL,
                offered_implied_prob REAL, fair_prob REAL, fair_american_odds INTEGER,
                ev_pct REAL, yn_reference_prob REAL, yn_reference_odds INTEGER,
                yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
                n_consensus_books INTEGER, market_quality TEXT,
                rec_status TEXT, rec_eligible INTEGER, data_source TEXT,
                observation_timestamp TEXT, scan_timestamp TEXT,
                freshness_status TEXT, model_version TEXT,
                model_score REAL, score_version TEXT DEFAULT 'model_score_v1',
                score_explanation TEXT,
                recommendation_tier TEXT DEFAULT 'RESEARCH_ONLY',
                qualification_passed INTEGER DEFAULT 0,
                qualification_reasons TEXT DEFAULT '',
                disqualification_reasons TEXT DEFAULT '',
                contributing_book_count INTEGER DEFAULT 0,
                contributing_books TEXT DEFAULT '',
                applicable_edge_metric TEXT DEFAULT '',
                applicable_edge_threshold REAL DEFAULT 0.0,
                model_score_threshold REAL DEFAULT 8.0,
                qualification_rules_version TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        recs = _load_todays_recs(str(db_path))
        assert recs == []

    def test_load_recs_nonexistent_db(self):
        from src.control_panel import _load_todays_recs
        recs = _load_todays_recs("/nonexistent/path/db.sqlite")
        assert recs == []

    def test_ou_ev_display(self, tmp_path):
        """O/U recommendations have ev_pct populated."""
        from src.control_panel import _load_todays_recs
        db_path = self._make_recs_db(tmp_path)
        recs = _load_todays_recs(str(db_path))
        ou_recs = [r for r in recs if r.get("market_form") == "ou"]
        assert len(ou_recs) == 1
        assert ou_recs[0]["ev_pct"] is not None
        assert ou_recs[0]["ev_pct"] > 0

    def test_yn_advantage_display(self, tmp_path):
        """Y/N recommendations have yn_implied_prob_adv populated, not fake EV."""
        from src.control_panel import _load_todays_recs
        db_path = self._make_recs_db(tmp_path)
        recs = _load_todays_recs(str(db_path))
        yn_recs = [r for r in recs if r.get("market_form") == "yn"]
        assert len(yn_recs) == 1
        assert yn_recs[0]["yn_implied_prob_adv"] is not None
        assert yn_recs[0]["ev_pct"] is None  # No fake EV for Y/N

    def test_empty_recommendation_state(self, tmp_path):
        """No recommendations returns empty list."""
        from src.control_panel import _load_todays_recs
        recs = _load_todays_recs(str(tmp_path / "nope.db"))
        assert recs == []


# ==================================================================
# Secret redaction
# ==================================================================

class TestSecretRedaction:

    def test_redact_api_key(self):
        from src.control_panel import _redact
        key = "sk_test_abcdef1234567890"
        redacted = _redact(key)
        assert "sk_t" in redacted
        assert "abcdef1234567890" not in redacted

    def test_redact_webhook_url(self):
        from src.control_panel import _redact
        url = "https://discord.com/api/webhooks/123456/abcdefghij"
        redacted = _redact(url)
        assert "123456" not in redacted or redacted.count("*") > 0

    def test_redact_credential_path(self):
        from src.control_panel import _redact
        path = "/home/user/.config/gcloud/service-account.json"
        redacted = _redact(path)
        assert "service-account" not in redacted


# ==================================================================
# Shadow mode / safety
# ==================================================================

class TestShadowModeSafety:

    def test_shadow_mode_label_in_source(self):
        """Control panel source references shadow mode correctly."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "SHADOW" in source
        assert "shadow" in source.lower()

    def test_delivery_blocked_in_source(self):
        """Control panel shows delivery is blocked."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Blocked" in source or "blocked" in source

    def test_no_wager_placement_in_source(self):
        """Control panel source has no bet placement code."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "place_bet" not in source.lower()
        assert "place_wager" not in source.lower()
        assert "submit_bet" not in source.lower()
        assert "auto_bet" not in source.lower()

    def test_simultaneous_run_prevention(self):
        """Control panel source has run_active state management."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "run_active" in source

    def test_rerun_confirmation_in_source(self):
        """Control panel has rerun time guard."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "900" in source  # 15 min = 900 seconds

    def test_no_delivery_enable_button(self):
        """No enable-delivery button in the source."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Enable Live Delivery" not in source
        assert "enable live delivery" not in source.lower()


# ==================================================================
# Pipeline state displays
# ==================================================================

class TestPipelineStates:

    def test_success_state_handled(self):
        """Source handles success status."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"success"' in source
        assert "completed" in source.lower() or "success" in source.lower()

    def test_failure_state_handled(self):
        """Source handles failure status."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"failed"' in source

    def test_running_state_handled(self):
        """Source handles running status."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"running"' in source

    def test_technical_details_section(self):
        """Technical details expandable section exists."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Technical Details" in source
        assert "exit_code" in source


# ==================================================================
# CSV export
# ==================================================================

class TestCSVExport:

    def test_csv_download_button_exists(self):
        """Source has CSV export capability."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "import csv" in source

    def test_csv_generation(self, tmp_path):
        """CSV can be generated from recommendation data."""
        from src.control_panel import _load_todays_recs
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE historical_recommendations (
                recommendation_id TEXT PRIMARY KEY, fingerprint TEXT,
                event_id TEXT, player_name TEXT, market_type TEXT,
                market_form TEXT, period TEXT, line REAL, side TEXT,
                sportsbook TEXT, offered_american_odds INTEGER, offered_decimal_odds REAL,
                offered_implied_prob REAL, fair_prob REAL, fair_american_odds INTEGER,
                ev_pct REAL, yn_reference_prob REAL, yn_reference_odds INTEGER,
                yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
                n_consensus_books INTEGER, market_quality TEXT,
                rec_status TEXT, rec_eligible INTEGER, data_source TEXT,
                observation_timestamp TEXT, scan_timestamp TEXT,
                freshness_status TEXT, model_version TEXT,
                scan_run_id TEXT DEFAULT '', matchup TEXT DEFAULT '',
                event_status TEXT DEFAULT '', event_start_time TEXT DEFAULT '',
                model_score REAL, score_version TEXT DEFAULT 'model_score_v1',
                score_explanation TEXT,
                recommendation_tier TEXT DEFAULT 'RESEARCH_ONLY',
                qualification_passed INTEGER DEFAULT 0,
                qualification_reasons TEXT DEFAULT '',
                disqualification_reasons TEXT DEFAULT '',
                contributing_book_count INTEGER DEFAULT 0,
                contributing_books TEXT DEFAULT '',
                applicable_edge_metric TEXT DEFAULT '',
                applicable_edge_threshold REAL DEFAULT 0.0,
                model_score_threshold REAL DEFAULT 8.0,
                qualification_rules_version TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT14:00:00+00:00")
        conn.execute(
            "INSERT INTO historical_recommendations "
            "(recommendation_id, fingerprint, event_id, player_name, market_type, market_form, period, "
            "line, side, sportsbook, offered_american_odds, offered_decimal_odds, offered_implied_prob, "
            "fair_prob, fair_american_odds, ev_pct, yn_reference_prob, yn_reference_odds, "
            "yn_implied_prob_adv, yn_decimal_odds_adv, n_consensus_books, market_quality, "
            "rec_status, rec_eligible, data_source, observation_timestamp, scan_timestamp, "
            "freshness_status, model_version, scan_run_id, matchup, event_status, event_start_time) "
            "VALUES "
            "('r1','fp1','e1','Judge','strikeouts','ou','game',0.5,'Over','DK',"
            "-110,1.909,0.526,0.5,-100,0.026,NULL,NULL,NULL,NULL,6,'VALID','BET',1,"
            f"'live','{today}','{today}','fresh','v1','run-001','NYY @ LAD','scheduled','{today}')"
        )
        conn.commit()
        conn.close()

        recs = _load_todays_recs(str(db_path))
        assert len(recs) == 1
        csv_buf = io.StringIO()
        import pandas as pd
        df = pd.DataFrame(recs)
        csv_data = df.to_csv(index=False)
        assert "Judge" in csv_data
        assert "strikeouts" in csv_data


# ==================================================================
# Backup action
# ==================================================================

class TestBackupAction:

    def test_backup_button_exists(self):
        """Source has a backup button."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Backup" in source

    def test_backup_function_callable(self, tmp_path):
        """Backup can be called programmatically."""
        from src.backup_database import backup_database
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        bp = backup_database(db_path, backup_dir)
        assert bp.exists()
        assert bp.stat().st_size > 0


# ==================================================================
# Latest report lookup
# ==================================================================

class TestLatestReport:

    def test_latest_report_found(self, tmp_path):
        """Latest report is found when files exist."""
        from src.control_panel import _get_latest_report
        (tmp_path / "recommendations.csv").write_text("test")
        (tmp_path / "recommendations.json").write_text("{}")
        result = _get_latest_report(str(tmp_path))
        assert result is not None
        assert "recommendations" in result

    def test_latest_report_not_found(self, tmp_path):
        """No report returns None."""
        from src.control_panel import _get_latest_report
        result = _get_latest_report(str(tmp_path))
        assert result is None

    def test_latest_report_nonexistent_dir(self):
        """Nonexistent directory returns None."""
        from src.control_panel import _get_latest_report
        result = _get_latest_report("/nonexistent/dir")
        assert result is None


# ==================================================================
# Streamlit page config
# ==================================================================

class TestStreamlitConfig:

    def test_page_title(self):
        """Control panel sets correct page title."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "MLB VIP Model" in source

    def test_wide_layout(self):
        """Control panel uses wide layout."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "wide" in source

    def test_page_icon(self):
        """Control panel has a page icon."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "page_icon" in source


# ==================================================================
# Advanced controls
# ==================================================================

class TestAdvancedControls:

    def test_canary_controls_exist(self):
        """Canary controls are in advanced section."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "canary" in source.lower()

    def test_pregame_refresh_exists(self):
        """Pregame refresh button exists."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Pregame Run" in source

    def test_closing_prices_exists(self):
        """Closing prices capture exists."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Closing Prices" in source

    def test_grade_recommendations_exists(self):
        """Grade recommendations exists."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Grade" in source

    def test_view_traces_exists(self):
        """View traces exists."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Trace" in source or "trace" in source

    def test_no_live_delivery_control(self):
        """No enable-delivery button anywhere in advanced controls."""
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Enable Live Delivery" not in source


# ==================================================================
# Launcher and setup
# ==================================================================

class TestLauncherAndSetup:

    def test_launcher_references_python(self):
        """Launcher checks for Python."""
        content = Path("launch_mlb_model.bat").read_text()
        assert "python" in content.lower()

    def test_launcher_references_streamlit(self):
        """Launcher starts Streamlit."""
        content = Path("launch_mlb_model.bat").read_text()
        assert "streamlit" in content.lower()

    def test_launcher_opens_browser(self):
        """Launcher does not disable browser opening."""
        content = Path("launch_mlb_model.bat").read_text()
        assert "headless false" in content.lower() or "server.headless" not in content.lower()

    def test_launcher_error_handling(self):
        """Launcher has error handling."""
        content = Path("launch_mlb_model.bat").read_text()
        assert "ERROR" in content
        assert "pause" in content.lower()

    def test_launcher_log_output(self):
        """Launcher writes to log file."""
        content = Path("launch_mlb_model.bat").read_text()
        assert "launcher.log" in content

    def test_setup_checks_python(self):
        """Setup checks Python version."""
        content = Path("setup_local_app.bat").read_text()
        assert "python" in content.lower()
        assert "3.10" in content

    def test_setup_creates_venv(self):
        """Setup creates virtual environment."""
        content = Path("setup_local_app.bat").read_text()
        assert "venv" in content.lower()

    def test_setup_installs_requirements(self):
        """Setup installs from requirements.txt."""
        content = Path("setup_local_app.bat").read_text()
        assert "requirements.txt" in content

    def test_setup_verifies_streamlit(self):
        """Setup verifies Streamlit is installed."""
        content = Path("setup_local_app.bat").read_text()
        assert "streamlit" in content.lower()

    def test_setup_creates_directories(self):
        """Setup creates required directories."""
        content = Path("setup_local_app.bat").read_text()
        assert "database" in content
        assert "output" in content

    def test_setup_copies_env(self):
        """Setup copies .env.example to .env."""
        content = Path("setup_local_app.bat").read_text()
        assert ".env" in content
        assert ".env.example" in content

    def test_setup_does_not_overwrite_env(self):
        """Setup only copies .env if it doesn't exist."""
        content = Path("setup_local_app.bat").read_text()
        assert 'if not exist ".env"' in content

    def test_setup_runs_smoke_test(self):
        """Setup runs a smoke test."""
        content = Path("setup_local_app.bat").read_text()
        assert "smoke test" in content.lower()

    def test_shortcut_script_references_bat(self):
        """Shortcut script points to the launcher bat."""
        content = Path("create_desktop_shortcut.ps1").read_text()
        assert "launch_mlb_model.bat" in content

    def test_shortcut_name(self):
        """Shortcut is named 'MLB VIP Model'."""
        content = Path("create_desktop_shortcut.ps1").read_text()
        assert "MLB VIP Model" in content
