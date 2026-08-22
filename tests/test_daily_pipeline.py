"""Phase 7 tests: Daily production pipeline.

All tests use in-memory databases, mocked API calls, and synthetic data.
No live API calls. No mutable cache. No production database access.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.daily_pipeline import (
    PipelineConfig,
    PipelineState,
    build_parser,
    main,
    run_pipeline,
    EXIT_SUCCESS,
    EXIT_SUCCESS_NO_RECS,
    EXIT_CONFIG_FAILURE,
    EXIT_API_FAILURE,
    EXIT_DB_FAILURE,
    EXIT_VALIDATION_FAILURE,
    EXIT_UNEXPECTED_FAILURE,
    _stage_validate_config,
    _stage_create_run,
    _stage_fetch_events,
    _stage_ingest,
    _stage_validate,
    _stage_scan,
    _stage_freeze,
    _stage_reports,
    _stage_summary,
    _build_run_summary,
    _build_pipeline_report,
    _parse_status,
    _write_completion_flag,
    _write_csv,
    _write_json,
    _write_text,
)
from tests.fixture_data import flaherty_event as _flaherty_event


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def db():
    """In-memory SQLite database with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
            away_team TEXT, home_team TEXT, start_time TEXT,
            status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, market TEXT NOT NULL, selection TEXT,
            price REAL, points REAL, is_alt_line INTEGER DEFAULT 0,
            available INTEGER DEFAULT 1, pulled_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            odd_id TEXT NOT NULL, sportsbook TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT, team_id TEXT DEFAULT '',
            team_name TEXT DEFAULT '', market_type TEXT NOT NULL,
            market_group_key TEXT NOT NULL, side TEXT NOT NULL, line REAL,
            price INTEGER, decimal_odds REAL, is_alt_line INTEGER DEFAULT 0,
            available INTEGER DEFAULT 1, validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT '', mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT '', captured_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
            finished_at TEXT, run_type TEXT DEFAULT 'scan', mode TEXT,
            market_filter TEXT, form_filter TEXT, n_events INTEGER DEFAULT 0,
            n_markets INTEGER DEFAULT 0, n_opportunities INTEGER DEFAULT 0,
            n_yn_opps INTEGER DEFAULT 0, data_source TEXT,
            research_only INTEGER DEFAULT 0, error_message TEXT,
            metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS ingestion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_id TEXT NOT NULL, ingested_at TEXT DEFAULT (datetime('now')),
            odds_rows INTEGER DEFAULT 0, audit_rows INTEGER DEFAULT 0,
            error_message TEXT
        );
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            scan_run_id TEXT, ingestion_run_id TEXT, event_id TEXT NOT NULL,
            event_start_time TEXT, player_id TEXT NOT NULL, player_name TEXT,
            market_type TEXT NOT NULL, market_form TEXT NOT NULL,
            period TEXT NOT NULL, line REAL, side TEXT NOT NULL,
            sportsbook TEXT NOT NULL, offered_american_odds INTEGER NOT NULL,
            offered_decimal_odds REAL NOT NULL, offered_implied_prob REAL NOT NULL,
            fair_prob REAL, fair_american_odds INTEGER, ev_pct REAL,
            yn_reference_prob REAL, yn_reference_odds INTEGER,
            yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
            n_consensus_books INTEGER, market_quality TEXT,
            rec_status TEXT NOT NULL, rec_eligible INTEGER DEFAULT 0,
            data_source TEXT, observation_timestamp TEXT,
            scan_timestamp TEXT NOT NULL, freshness_status TEXT,
            model_version TEXT DEFAULT 'v1',
            matchup TEXT DEFAULT '', event_status TEXT DEFAULT '',
            model_score REAL, score_version TEXT DEFAULT 'model_score_v1',
            score_components TEXT, score_cap REAL, score_explanation TEXT,
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
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_fingerprint ON historical_recommendations(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_hr_event ON historical_recommendations(event_id);
        CREATE INDEX IF NOT EXISTS idx_hr_player ON historical_recommendations(player_id);
        CREATE TABLE IF NOT EXISTS event_results (
            event_id TEXT PRIMARY KEY, final_status TEXT DEFAULT 'UNRESOLVED',
            away_score INTEGER, home_score INTEGER, result_source TEXT,
            source_observed_at TEXT, result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_stat_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
            player_id TEXT NOT NULL, player_name TEXT, market_type TEXT NOT NULL,
            final_stat_value REAL, result_source TEXT, source_observed_at TEXT,
            result_status TEXT DEFAULT 'UNRESOLVED', result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(event_id, player_id, market_type)
        );
        CREATE TABLE IF NOT EXISTS market_settlements (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            settlement_status TEXT DEFAULT 'UNRESOLVED', final_stat_value REAL,
            settled_at TEXT, settlement_reason TEXT,
            grader_version TEXT DEFAULT 'v1', manual_override INTEGER DEFAULT 0,
            override_reason TEXT, override_previous TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_rec ON market_settlements(recommendation_id);
        CREATE TABLE IF NOT EXISTS bet_units (
            settlement_id TEXT PRIMARY KEY, recommendation_id TEXT NOT NULL,
            risk_units REAL DEFAULT 1.0, profit_units REAL DEFAULT 0.0,
            return_units REAL DEFAULT 0.0, odds_at_settle INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS closing_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL, closing_american INTEGER,
            closing_decimal REAL, closing_implied_prob REAL,
            closing_line REAL, closing_observed_at TEXT,
            closing_sportsbook TEXT, line_move_type TEXT,
            clv_probability REAL, clv_price_diff INTEGER,
            clv_available INTEGER DEFAULT 0, line_movement_direction TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS manual_override_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT NOT NULL, previous_status TEXT,
            new_status TEXT NOT NULL, override_reason TEXT NOT NULL,
            override_by TEXT DEFAULT 'cli',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def sample_opps():
    """Sample O/U opportunities for testing."""
    return [
        {
            "event_id": "EVT_001",
            "away_team": "NYY",
            "home_team": "BOS",
            "start_time": "2026-07-23T19:00:00Z",
            "player_id": "PLAYER_001",
            "player_name": "Test Pitcher",
            "market_type": "pitching_strikeouts_ou",
            "line": 5.5,
            "side": "OVER",
            "sportsbook": "draftkings",
            "american_odds": -110,
            "decimal_odds": 1.909,
            "n_consensus_books": 5,
            "fair_prob": 0.52,
            "ev_pct": 3.5,
            "market_quality": "VALID",
            "rec_eligible": True,
            "bet_status": "POSITIVE_EDGE",
            "validation_status": "VALID",
            "is_alt_line": 0,
        },
        {
            "event_id": "EVT_001",
            "away_team": "NYY",
            "home_team": "BOS",
            "start_time": "2026-07-23T19:00:00Z",
            "player_id": "PLAYER_001",
            "player_name": "Test Pitcher",
            "market_type": "pitching_strikeouts_ou",
            "line": 5.5,
            "side": "UNDER",
            "sportsbook": "fanduel",
            "american_odds": 105,
            "decimal_odds": 2.05,
            "n_consensus_books": 5,
            "fair_prob": 0.48,
            "ev_pct": -1.2,
            "market_quality": "VALID",
            "rec_eligible": False,
            "bet_status": "NO_EDGE",
            "validation_status": "VALID",
            "is_alt_line": 0,
        },
    ]


@pytest.fixture
def sample_yn_opps():
    """Sample YN opportunities for testing."""
    return [
        {
            "event_id": "EVT_002",
            "away_team": "LAD",
            "home_team": "SF",
            "start_time": "2026-07-23T21:00:00Z",
            "player_id": "PLAYER_002",
            "player_name": "YN Pitcher",
            "market_type": "pitching_strikeouts_yn",
            "line": None,
            "side": "YES",
            "sportsbook": "draftkings",
            "american_odds": -180,
            "decimal_odds": 1.556,
            "n_consensus_books": 4,
            "price_advantage_pct": 5.2,
            "relative_payout_advantage_pct": 3.1,
            "decimal_odds_advantage": 2.5,
            "market_reference_probability": 0.62,
            "market_reference_odds": -163,
            "comparison_status": "PRICE_OUTLIER",
            "market_quality": "VALID",
            "rec_eligible": True,
            "validation_status": "VALID",
        },
    ]


# ── CLI tests ─────────────────────────────────────────────────────

class TestCLI:
    def test_build_parser_returns_argparse(self):
        parser = build_parser()
        assert parser is not None

    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.market == "all"
        assert args.market_form == "all"
        assert args.actionable_only is False
        assert args.positive_only is False
        assert args.all_markets is False
        assert args.live is False
        assert args.cache is False
        assert args.dry_run is False
        assert args.require_fresh is False
        assert args.debug is False
        assert args.output_dir == "output"

    def test_live_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--live"])
        assert args.live is True

    def test_cache_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--cache"])
        assert args.cache is True

    def test_auto_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--auto"])
        assert args.auto is True

    def test_market_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--market", "strikeouts"])
        assert args.market == "strikeouts"

    def test_market_form_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--market-form", "yn"])
        assert args.market_form == "yn"

    def test_actionable_only_default(self):
        parser = build_parser()
        args = parser.parse_args([])
        # Parser default is False; main() converts to True when no mode flag
        assert args.actionable_only is False

    def test_positive_only_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--positive-only"])
        assert args.positive_only is True
        assert args.actionable_only is False

    def test_all_markets_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--all-markets"])
        assert args.all_markets is True
        assert args.actionable_only is False

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_output_dir_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--output-dir", "my_output"])
        assert args.output_dir == "my_output"

    def test_require_fresh_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--require-fresh"])
        assert args.require_fresh is True

    def test_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--json"])
        assert args.as_json is True

    def test_csv_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--csv"])
        assert args.as_csv is True

    def test_debug_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--debug"])
        assert args.debug is True

    def test_market_choices(self):
        parser = build_parser()
        for market in ["strikeouts", "hits_allowed",
                       "walks_allowed", "home_runs", "all"]:
            args = parser.parse_args(["--market", market])
            assert args.market == market

    def test_market_form_choices(self):
        parser = build_parser()
        for form in ["ou", "yn", "all"]:
            args = parser.parse_args(["--market-form", form])
            assert args.market_form == form


# ── PipelineConfig tests ──────────────────────────────────────────

class TestPipelineConfig:
    def test_defaults(self):
        config = PipelineConfig()
        assert config.live is False
        assert config.use_cache is False
        assert config.auto is False
        assert config.output_dir == "output"
        assert config.market == "all"
        assert config.market_form == "all"
        assert config.actionable_only is True
        assert config.positive_only is False
        assert config.require_fresh is False
        assert config.dry_run is False
        assert config.as_json is False
        assert config.as_csv is False
        assert config.debug is False

    def test_custom_values(self):
        config = PipelineConfig(
            live=True, output_dir="test_output",
            market="strikeouts", market_form="ou",
            actionable_only=False, positive_only=True,
            require_fresh=True, dry_run=True,
        )
        assert config.live is True
        assert config.output_dir == "test_output"
        assert config.market == "strikeouts"
        assert config.market_form == "ou"
        assert config.actionable_only is False
        assert config.positive_only is True
        assert config.require_fresh is True
        assert config.dry_run is True


# ── PipelineState tests ───────────────────────────────────────────

class TestPipelineState:
    def test_defaults(self):
        state = PipelineState()
        assert state.pipeline_run_id == ""
        assert state.version == "1.0.0"
        assert state.n_events == 0
        assert state.status == "RUNNING"
        assert state.errors == []
        assert state.warnings == []
        assert state.stage_timings == {}

    def test_accumulation(self):
        state = PipelineState()
        state.errors.append("test error")
        state.warnings.append("test warning")
        state.n_events = 5
        assert len(state.errors) == 1
        assert len(state.warnings) == 1
        assert state.n_events == 5


# ── Exit code tests ───────────────────────────────────────────────

class TestExitCodes:
    def test_success_is_zero(self):
        assert EXIT_SUCCESS == 0

    def test_success_no_recs(self):
        assert EXIT_SUCCESS_NO_RECS == 1

    def test_config_failure(self):
        assert EXIT_CONFIG_FAILURE == 2

    def test_api_failure(self):
        assert EXIT_API_FAILURE == 3

    def test_db_failure(self):
        assert EXIT_DB_FAILURE == 4

    def test_validation_failure(self):
        assert EXIT_VALIDATION_FAILURE == 5

    def test_unexpected_failure(self):
        assert EXIT_UNEXPECTED_FAILURE == 6

    def test_all_unique(self):
        codes = [
            EXIT_SUCCESS, EXIT_SUCCESS_NO_RECS, EXIT_CONFIG_FAILURE,
            EXIT_API_FAILURE, EXIT_DB_FAILURE, EXIT_VALIDATION_FAILURE,
            EXIT_UNEXPECTED_FAILURE,
        ]
        assert len(codes) == len(set(codes))


# ── Stage 1: Validate config ─────────────────────────────────────

class TestStageValidateConfig:
    def test_valid_config(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        result = _stage_validate_config(config, state)
        assert result is True
        assert state.status == "RUNNING"
        assert len(state.errors) == 0

    def test_missing_api_key_non_dry_run(self):
        config = PipelineConfig(dry_run=False)
        state = PipelineState()
        old_key = os.environ.pop("SPORTSODDS_API_KEY", None)
        try:
            result = _stage_validate_config(config, state)
            assert result is False
            assert state.status == "CONFIG_FAILURE"
            assert any("SPORTSODDS_API_KEY" in e for e in state.errors)
        finally:
            if old_key:
                os.environ["SPORTSODDS_API_KEY"] = old_key

    def test_dry_run_skips_api_key_check(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        old_key = os.environ.pop("SPORTSODDS_API_KEY", None)
        try:
            result = _stage_validate_config(config, state)
            assert result is True
        finally:
            if old_key:
                os.environ["SPORTSODDS_API_KEY"] = old_key


# ── Stage 2: Create run ──────────────────────────────────────────

class TestStageCreateRun:
    def test_dry_run(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        result = _stage_create_run(config, state)
        assert result is True
        assert state.pipeline_run_id != ""
        assert state.execution_mode == "cache"

    def test_live_mode(self):
        config = PipelineConfig(live=True, dry_run=True)
        state = PipelineState()
        result = _stage_create_run(config, state)
        assert result is True
        assert state.execution_mode == "live"


# ── Stage 5: Validate ────────────────────────────────────────────

class TestStageValidate:
    def test_valid_data(self):
        config = PipelineConfig()
        state = PipelineState()
        state.n_approved_rows = 100
        result = _stage_validate(config, state)
        assert result is True

    def test_no_approved_rows_warning(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        state.n_approved_rows = 0
        result = _stage_validate(config, state)
        assert result is True
        assert state.n_warnings == 1

    def test_stale_data_reject(self):
        config = PipelineConfig(require_fresh=True, dry_run=True)
        state = PipelineState()
        state.n_approved_rows = 100
        state.stale_warning = True
        result = _stage_validate(config, state)
        assert result is False
        assert state.status == "VALIDATION_FAILURE"

    def test_stale_data_no_require_fresh(self):
        config = PipelineConfig(require_fresh=False, dry_run=True)
        state = PipelineState()
        state.n_approved_rows = 100
        state.stale_warning = True
        result = _stage_validate(config, state)
        assert result is True


# ── Report builders ───────────────────────────────────────────────

class TestReportBuilders:
    def test_build_run_summary(self):
        state = PipelineState()
        state.n_events = 10
        state.n_markets = 5
        state.n_recommendations_saved = 3
        summary = _build_run_summary(state)
        assert summary["pipeline_run_id"] == state.pipeline_run_id
        assert summary["metrics"]["n_events"] == 10
        assert summary["metrics"]["n_recommendations_saved"] == 3
        assert summary["status"] == "RUNNING"

    def test_build_pipeline_report(self):
        state = PipelineState()
        state.n_events = 10
        state.n_recommendations_saved = 3
        report = _build_pipeline_report(state)
        assert "MLB Sportsbook Analysis Pipeline Report" in report
        assert "Events:              10" in report
        assert "Recommendations:     3" in report

    def test_report_includes_warnings(self):
        state = PipelineState()
        state.warnings.append("test warning")
        report = _build_pipeline_report(state)
        assert "test warning" in report

    def test_report_includes_errors(self):
        state = PipelineState()
        state.errors.append("test error")
        report = _build_pipeline_report(state)
        assert "test error" in report

    def test_report_includes_timings(self):
        state = PipelineState()
        state.stage_timings["validate"] = 0.123
        report = _build_pipeline_report(state)
        assert "validate" in report
        assert "0.123" in report

    def test_completion_flag_uses_pipeline_run_id(self, tmp_path):
        import src.daily_pipeline as pipeline

        flag_path = tmp_path / ".pipeline_completed"
        state = PipelineState(pipeline_run_id="run-123")
        state.n_recommendations_saved = 1
        with patch.object(pipeline, "_PIPELINE_COMPLETION_FILE", flag_path):
            _write_completion_flag(PipelineConfig(), state)

        flag = json.loads(flag_path.read_text(encoding="utf-8"))
        assert flag["run_id"] == "run-123"
        assert flag["n_recommendations"] == 1


# ── File writers ──────────────────────────────────────────────────

class TestFileWriters:
    def test_write_csv(self, tmp_path):
        opps = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        path = tmp_path / "test.csv"
        _write_csv(path, opps, dry_run=False)
        assert path.exists()
        content = path.read_text()
        assert "a,b" in content
        assert "1,2" in content

    def test_write_csv_dry_run(self, tmp_path):
        opps = [{"a": 1}]
        path = tmp_path / "test.csv"
        _write_csv(path, opps, dry_run=True)
        assert not path.exists()

    def test_write_csv_empty(self, tmp_path):
        path = tmp_path / "test.csv"
        _write_csv(path, [], dry_run=False)
        assert not path.exists()

    def test_write_json(self, tmp_path):
        data = [{"a": 1, "b": "test"}]
        path = tmp_path / "test.json"
        _write_json(path, data, dry_run=False)
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded[0]["a"] == 1

    def test_write_json_dry_run(self, tmp_path):
        path = tmp_path / "test.json"
        _write_json(path, [{"a": 1}], dry_run=True)
        assert not path.exists()

    def test_write_text(self, tmp_path):
        path = tmp_path / "test.txt"
        _write_text(path, "hello world", dry_run=False)
        assert path.exists()
        assert path.read_text() == "hello world"

    def test_write_text_dry_run(self, tmp_path):
        path = tmp_path / "test.txt"
        _write_text(path, "hello", dry_run=True)
        assert not path.exists()


# ── parse_status ──────────────────────────────────────────────────

class TestParseStatus:
    def test_string_passthrough(self):
        assert _parse_status("live") == "live"

    def test_dict_state(self):
        assert _parse_status({"state": "in_progress"}) == "in_progress"

    def test_dict_no_state(self):
        assert _parse_status({}) == "scheduled"

    def test_empty_string(self):
        assert _parse_status("") == "scheduled"

    # ── Real SportsGameOdds status shape (verified live 2026-08-20 —
    # boolean flags, NO "state" key at all, for both MLB and NFL). The
    # dict_state test above covers a hypothetical/legacy shape that isn't
    # what the live API actually returns; these cover the real one.

    def test_real_shape_upcoming_game(self):
        status = {
            "started": False, "completed": False, "cancelled": False,
            "ended": False, "live": False, "finalized": False,
        }
        assert _parse_status(status) == "scheduled"

    def test_real_shape_live_game(self):
        status = {
            "started": True, "completed": False, "cancelled": False,
            "ended": False, "live": True, "finalized": False,
        }
        assert _parse_status(status) == "live"

    def test_real_shape_started_but_not_flagged_live(self):
        """Some real responses show started=True with live=False in a
        brief pre-live window — must still count as "not scheduled"."""
        status = {"started": True, "completed": False, "cancelled": False,
                   "ended": False, "live": False, "finalized": False}
        assert _parse_status(status) == "live"

    def test_real_shape_completed_game(self):
        status = {
            "started": True, "completed": True, "cancelled": False,
            "ended": True, "live": False, "finalized": True,
            "displayLong": "Final",
        }
        assert _parse_status(status) == "completed"

    def test_real_shape_cancelled_game(self):
        status = {
            "started": False, "completed": False, "cancelled": True,
            "ended": False, "live": False, "finalized": False,
        }
        assert _parse_status(status) == "cancelled"

    def test_real_shape_cancelled_takes_priority_over_other_flags(self):
        """A cancelled game might still carry started=True from before it
        was called off — cancelled must win, not "live" or "completed"."""
        status = {
            "started": True, "completed": False, "cancelled": True,
            "ended": False, "live": True, "finalized": False,
        }
        assert _parse_status(status) == "cancelled"


# ── Full pipeline (dry run) ──────────────────────────────────────

class TestFullPipelineDryRun:
    def test_dry_run_no_events(self, tmp_path):
        config = PipelineConfig(
            dry_run=True, output_dir=str(tmp_path / "output"),
        )
        empty_scan = {
            "opportunities": [], "yn_opportunities": [],
            "n_events": 0, "n_markets": 0, "n_pitchers": 0,
            "n_approved_rows": 0, "n_excluded_rows": 0,
            "scan_start": "", "fetch_time": "",
            "data_source": "CACHE", "oldest_obs": "", "newest_obs": "",
            "age_seconds": 0, "stale_warning": False,
            "research_only": True, "scanner_title": "TEST", "run_id": "",
        }
        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.daily_pipeline.run_scan", return_value=empty_scan):
            mock_instance = MagicMock()
            mock_instance.get_events.return_value = ({"data": []}, True)
            MockClient.return_value = mock_instance

            with patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
                code = run_pipeline(config)
        # No events = no recommendations
        assert code == EXIT_SUCCESS_NO_RECS

    def test_dry_run_with_events(self, tmp_path, sample_opps):
        config = PipelineConfig(
            dry_run=True, output_dir=str(tmp_path / "output"),
        )
        scan_result = {
            "opportunities": sample_opps,
            "yn_opportunities": [],
            "n_events": 1,
            "n_markets": 1,
            "n_pitchers": 1,
            "n_approved_rows": 10,
            "n_excluded_rows": 2,
            "scan_start": "2026-07-23T19:00:00Z",
            "fetch_time": "2026-07-23T19:00:01Z",
            "data_source": "CACHE",
            "oldest_obs": "2026-07-23T18:50:00Z",
            "newest_obs": "2026-07-23T19:00:00Z",
            "age_seconds": 1,
            "stale_warning": False,
            "research_only": True,
            "scanner_title": "MLB PLAYER PROP EDGE SCANNER",
            "run_id": "",
            "_raw_events": [{"eventID": "EVT_001", "odds": {}, "teams": {}, "status": {}}],
        }

        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.daily_pipeline.run_scan", return_value=scan_result), \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.return_value = ({"data": []}, True)
            MockClient.return_value = mock_instance

            code = run_pipeline(config)
        # Dry run with events should succeed
        assert code in (EXIT_SUCCESS, EXIT_SUCCESS_NO_RECS)

    def test_dry_run_produces_no_files(self, tmp_path):
        output_dir = tmp_path / "output"
        config = PipelineConfig(
            dry_run=True, output_dir=str(output_dir),
        )
        scan_result = {
            "opportunities": [{"a": 1}],
            "yn_opportunities": [],
            "n_events": 1, "n_markets": 1, "n_pitchers": 1,
            "n_approved_rows": 10, "n_excluded_rows": 0,
            "scan_start": "2026-07-23T19:00:00Z",
            "fetch_time": "2026-07-23T19:00:01Z",
            "data_source": "CACHE",
            "oldest_obs": "", "newest_obs": "",
            "age_seconds": 0, "stale_warning": False,
            "research_only": True, "scanner_title": "TEST", "run_id": "",
            "_raw_events": [],
        }

        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.daily_pipeline.run_scan", return_value=scan_result), \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.return_value = ({"data": []}, True)
            MockClient.return_value = mock_instance
            run_pipeline(config)

        # No files should be created in dry-run
        assert not output_dir.exists()


# ── Configuration failure ─────────────────────────────────────────

class TestConfigFailure:
    def test_missing_api_key_exits_config_failure(self, tmp_path):
        config = PipelineConfig(
            dry_run=False, output_dir=str(tmp_path / "output"),
        )
        old_key = os.environ.pop("SPORTSODDS_API_KEY", None)
        try:
            code = run_pipeline(config)
            assert code == EXIT_CONFIG_FAILURE
        finally:
            if old_key:
                os.environ["SPORTSODDS_API_KEY"] = old_key


# ── API failure ───────────────────────────────────────────────────

class TestAPIFailure:
    def test_api_exception_exits_api_failure(self, tmp_path):
        config = PipelineConfig(
            dry_run=False, output_dir=str(tmp_path / "output"),
        )
        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.side_effect = RuntimeError("API down")
            MockClient.return_value = mock_instance
            code = run_pipeline(config)
        assert code == EXIT_API_FAILURE


# ── SportsGameOdds quota exhaustion -> The Odds API fallback ───────
# Real production case, 2026-08-22: SportsGameOdds's free-tier monthly
# object quota was genuinely exhausted (verified live: 2,501/2,500
# entities used). A plain RuntimeError (above) must still hit
# EXIT_API_FAILURE unchanged — only a real HTTP 429 with a registered
# per-league fallback should recover instead of failing the whole run.

class TestSGOFallback:
    def _http_429(self):
        import requests
        resp = MagicMock()
        resp.status_code = 429
        err = requests.exceptions.HTTPError("429 rate limit exceeded")
        err.response = resp
        return err

    def test_429_recovers_via_fallback_instead_of_api_failure(self, tmp_path):
        empty_scan = {
            "opportunities": [], "yn_opportunities": [],
            "n_events": 0, "n_markets": 0, "n_pitchers": 0,
            "n_approved_rows": 0, "n_excluded_rows": 0,
            "scan_start": "2026-08-22T17:00:00Z",
            "fetch_time": "2026-08-22T17:00:01Z",
            "data_source": "CACHE",
            "oldest_obs": "", "newest_obs": "",
            "age_seconds": 0, "stale_warning": False,
            "research_only": True, "scanner_title": "TEST", "run_id": "",
            "_raw_events": [],
        }
        fake_normalized_events = [{
            "id": "game-1", "eventID": "game-1",
            "teams": {"home": {"name": "New York Yankees"}, "away": {"name": "Toronto Blue Jays"}},
            "status": {"startsAt": "2026-08-22T17:36:00Z"},
        }]

        config = PipelineConfig(dry_run=True, live=True, output_dir=str(tmp_path / "output"), league="MLB")

        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.daily_pipeline.get_connection", return_value=MagicMock()), \
             patch("src.daily_pipeline.run_scan", return_value=empty_scan), \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.side_effect = self._http_429()
            MockClient.return_value = mock_instance

            from src.sports import mlb as mlb_mod
            with patch.object(
                mlb_mod, "fetch_game_odds_via_odds_api",
                return_value=([], [], fake_normalized_events, False),
            ) as mock_fallback:
                code = run_pipeline(config)

        assert code == EXIT_SUCCESS_NO_RECS  # not EXIT_API_FAILURE
        mock_fallback.assert_called_once()

    def test_429_without_league_fallback_still_exits_api_failure(self, tmp_path):
        """A league with no fetch_game_odds_via_odds_api registered must
        still fail the run on a 429 rather than silently doing nothing.
        Uses a synthetic fake league rather than a real one (e.g. NFL) —
        both MLB and NFL now have this fallback registered, so a
        real-league-name test would go stale/break the moment a third
        league gets it too (this happened once already during this same
        session — see test_mlb_sgo_fallback.py's equivalent fix)."""
        config = PipelineConfig(dry_run=False, live=True, output_dir=str(tmp_path / "output"), league="FAKE")

        fake_league = MagicMock(spec=["get_market_registry", "AVAILABLE", "SPORT"])
        fake_league.AVAILABLE = True
        fake_league.SPORT = "fake"
        del fake_league.fetch_game_odds_via_odds_api

        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.sports.get_league", return_value=fake_league), \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.side_effect = self._http_429()
            MockClient.return_value = mock_instance
            code = run_pipeline(config)

        assert code == EXIT_API_FAILURE


# ── Empty slate (no opportunities) ────────────────────────────────

class TestEmptySlate:
    def test_no_opportunities_exits_success_no_recs(self, tmp_path):
        config = PipelineConfig(
            dry_run=True, output_dir=str(tmp_path / "output"),
        )
        empty_scan = {
            "opportunities": [], "yn_opportunities": [],
            "n_events": 1, "n_markets": 0, "n_pitchers": 0,
            "n_approved_rows": 10, "n_excluded_rows": 0,
            "scan_start": "2026-07-23T19:00:00Z",
            "fetch_time": "2026-07-23T19:00:01Z",
            "data_source": "CACHE",
            "oldest_obs": "", "newest_obs": "",
            "age_seconds": 0, "stale_warning": False,
            "research_only": True, "scanner_title": "TEST", "run_id": "",
            "_raw_events": [],
        }

        with patch("src.daily_pipeline.SportsGameOddsClient") as MockClient, \
             patch("src.daily_pipeline.run_scan", return_value=empty_scan), \
             patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            mock_instance = MagicMock()
            mock_instance.get_events.return_value = ({"data": []}, True)
            MockClient.return_value = mock_instance
            code = run_pipeline(config)
        assert code == EXIT_SUCCESS_NO_RECS


# ── CSV and JSON generation ──────────────────────────────────────

class TestReportGeneration:
    def test_csv_generated(self, tmp_path, sample_opps):
        output_dir = tmp_path / "output"
        config = PipelineConfig(
            dry_run=True, output_dir=str(output_dir),
        )
        state = PipelineState()
        state.scan_result = {"opportunities": sample_opps, "yn_opportunities": []}
        _stage_reports(config, state)
        # Dry run doesn't write files
        assert not (output_dir / "recommendations.csv").exists()

    def test_reports_dry_run_no_files(self, tmp_path, sample_opps):
        output_dir = tmp_path / "output"
        config = PipelineConfig(dry_run=True, output_dir=str(output_dir))
        state = PipelineState()
        state.scan_result = {"opportunities": sample_opps, "yn_opportunities": []}
        _stage_reports(config, state)
        assert not output_dir.exists()

    def test_reports_live_creates_files(self, tmp_path, sample_opps):
        output_dir = tmp_path / "output"
        config = PipelineConfig(dry_run=False, output_dir=str(output_dir))
        state = PipelineState()
        state.scan_result = {"opportunities": sample_opps, "yn_opportunities": []}
        _stage_reports(config, state)
        assert (output_dir / "recommendations.csv").exists()
        assert (output_dir / "recommendations.json").exists()
        assert (output_dir / "run_summary.json").exists()
        assert (output_dir / "pipeline_report.txt").exists()


class TestStageFreezeNonSportsGameOddsProvider:
    """A league on its own odds provider (WNBA) never gets a `games` table
    row from _stage_ingest (that only runs for the SportsGameOdds path —
    see _stage_fetch_events's early-SKIP branch). Found live 2026-08-20:
    without a fallback to the opportunity's own away_team/home_team/
    start_time, this left `matchup` permanently blank on the website AND
    silently disabled the live/already-started safety check for that
    league (event_status/start_time both empty -> _is_game_skippable
    never skips anything, regardless of whether the game already
    started)."""

    def test_matchup_falls_back_to_opportunity_fields_when_games_table_empty(
        self, tmp_path, sample_opps,
    ):
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "freeze_nogameTable.db"
        init_db(str(db_path))
        # Deliberately do NOT insert into `games` — this is the WNBA case.

        # sample_opps' fixed start_time is fixed at authoring time and can
        # drift into the past as the suite ages — force it safely into the
        # future so this test exercises "matchup fallback for an upcoming
        # game", not an unrelated (correct!) already-started skip.
        future_opps = [{**opp, "start_time": "2099-01-01T19:00:00Z"} for opp in sample_opps]

        config = PipelineConfig(dry_run=False, output_dir=str(tmp_path / "output"), league="WNBA")
        state = PipelineState()
        state.scan_run_id = "run-freeze-nogt"
        state.scan_result = {"opportunities": future_opps, "yn_opportunities": []}

        with patch("src.daily_pipeline.get_connection",
                   lambda *a, **kw: get_connection(str(db_path))):
            assert _stage_freeze(config, state) is True

        conn = get_connection(str(db_path))
        try:
            rows = conn.execute(
                "SELECT matchup, event_start_time FROM historical_recommendations "
                "WHERE scan_run_id = 'run-freeze-nogt'"
            ).fetchall()
        finally:
            conn.close()

        assert rows, "expected recommendations to be saved"
        for row in rows:
            assert row["matchup"], "matchup must not be blank when the opportunity carries team names"
            assert "NYY" in row["matchup"] and "BOS" in row["matchup"]

    def test_already_started_game_is_skipped_via_opportunity_start_time_fallback(
        self, tmp_path,
    ):
        """The safety-critical half of the same bug: with no `games` row,
        start_time must still come from the opportunity so an
        already-started game is correctly excluded, not silently scanned."""
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "freeze_started.db"
        init_db(str(db_path))

        past_opp = {
            "event_id": "EVT_STARTED", "away_team": "SEA", "home_team": "LVA",
            "start_time": "2020-01-01T00:00:00Z",  # unambiguously in the past
            "player_id": "PLAYER_777", "player_name": "Test Player",
            "market_type": "game_moneyline", "line": None, "side": "AWAY",
            "sportsbook": "fanduel", "american_odds": -110, "decimal_odds": 1.909,
            "n_consensus_books": 5, "fair_prob": 0.52, "ev_pct": 3.0,
            "market_quality": "VALID", "rec_eligible": True,
            "bet_status": "POSITIVE_EDGE", "validation_status": "VALID", "is_alt_line": 0,
        }

        config = PipelineConfig(dry_run=False, output_dir=str(tmp_path / "output"), league="WNBA")
        state = PipelineState()
        state.scan_run_id = "run-freeze-started"
        state.scan_result = {"opportunities": [past_opp], "yn_opportunities": []}

        with patch("src.daily_pipeline.get_connection",
                   lambda *a, **kw: get_connection(str(db_path))):
            assert _stage_freeze(config, state) is True

        conn = get_connection(str(db_path))
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM historical_recommendations WHERE scan_run_id = 'run-freeze-started'"
            ).fetchone()["c"]
        finally:
            conn.close()

        assert count == 0, "an already-started game must be skipped, not turned into a recommendation"
        assert state.n_games_skipped == 1


class TestStageIngestLeagueTagging:
    def test_game_record_uses_configured_league_not_hardcoded_mlb(self, tmp_path):
        """Found live 2026-08-20: _stage_ingest hardcoded 'league': 'MLB'
        into every games-table row and raw-response snapshot, regardless
        of config.league — meaning an NFL run's games would be silently
        mislabeled MLB in the database."""
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "ingest_league.db"
        init_db(str(db_path))

        config = PipelineConfig(dry_run=False, league="NFL")
        state = PipelineState()
        state.scan_result = {
            "_raw_events": [{
                "eventID": "NFL_EVT_1",
                "teams": {
                    "home": {"names": {"long": "Buffalo Bills"}},
                    "away": {"names": {"long": "Kansas City Chiefs"}},
                },
                "status": {"startsAt": "2099-01-01T18:00:00Z"},
                "sportID": "FOOTBALL", "leagueID": "NFL",
                "odds": {},
            }],
        }

        with patch("src.daily_pipeline.get_connection",
                   lambda *a, **kw: get_connection(str(db_path))):
            assert _stage_ingest(config, state) is True

        conn = get_connection(str(db_path))
        try:
            row = conn.execute(
                "SELECT league FROM games WHERE event_id = 'NFL_EVT_1'"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["league"] == "NFL"


class TestStageFreezeConfidence:
    """A player prop can never become an official recommendation on an
    uncertain identity mapping (Priority 1 of this session's mandate) —
    confidence_score/confidence_grade must actually be computed and
    persisted by _stage_freeze, not just exist as unused DB columns."""

    def test_confidence_score_and_grade_persisted(self, tmp_path, sample_opps):
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "freeze.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        conn.execute(
            """INSERT INTO games (event_id, away_team, home_team, start_time, status)
               VALUES ('EVT_001', 'NYY', 'BOS', '2099-01-01T19:00:00Z', 'scheduled')"""
        )
        conn.commit()
        conn.close()

        config = PipelineConfig(dry_run=False, output_dir=str(tmp_path / "output"))
        state = PipelineState()
        state.scan_run_id = "run-freeze-1"
        state.scan_result = {"opportunities": sample_opps, "yn_opportunities": []}

        with patch("src.daily_pipeline.get_connection",
                   lambda *a, **kw: get_connection(str(db_path))):
            assert _stage_freeze(config, state) is True

        conn = get_connection(str(db_path))
        try:
            rows = conn.execute(
                "SELECT confidence_score, confidence_grade "
                "FROM historical_recommendations WHERE scan_run_id = 'run-freeze-1'"
            ).fetchall()
        finally:
            conn.close()

        assert rows, "expected recommendations to be saved"
        for row in rows:
            assert row["confidence_score"] is not None
            assert 0.0 <= row["confidence_score"] <= 100.0
            assert row["confidence_grade"] in {"A", "B", "C", "D", "F"}

    def test_mapping_confidence_defaults_to_high_for_markets_without_identity_resolution(
        self, tmp_path, sample_opps,
    ):
        """MLB/NFL props carry provider-stable IDs with no ambiguous
        string-matching involved, so the confidence component that exists
        to catch uncertain player-identity mapping (WNBA today) must not
        silently penalize sports that never populate mapping_confidence."""
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "freeze2.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        conn.execute(
            """INSERT INTO games (event_id, away_team, home_team, start_time, status)
               VALUES ('EVT_001', 'NYY', 'BOS', '2099-01-01T19:00:00Z', 'scheduled')"""
        )
        conn.commit()
        conn.close()

        assert "mapping_confidence" not in sample_opps[0]

        config = PipelineConfig(dry_run=False, output_dir=str(tmp_path / "output"))
        state = PipelineState()
        state.scan_run_id = "run-freeze-2"
        state.scan_result = {"opportunities": sample_opps, "yn_opportunities": []}

        with patch("src.daily_pipeline.get_connection",
                   lambda *a, **kw: get_connection(str(db_path))):
            assert _stage_freeze(config, state) is True

        conn = get_connection(str(db_path))
        try:
            row = conn.execute(
                "SELECT confidence_score FROM historical_recommendations "
                "WHERE scan_run_id = 'run-freeze-2' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row["confidence_score"] is not None


# ── Pipeline summary ──────────────────────────────────────────────

class TestPipelineSummary:
    def test_summary_prints(self, capsys):
        config = PipelineConfig()
        state = PipelineState()
        state.n_events = 5
        state.n_recommendations_saved = 3
        _stage_summary(config, state)
        captured = capsys.readouterr()
        assert "Pipeline Status" in captured.out
        assert "Events Processed" in captured.out

    def test_summary_shows_warnings(self, capsys):
        state = PipelineState()
        state.warnings.append("test warning msg")
        config = PipelineConfig()
        _stage_summary(config, state)
        captured = capsys.readouterr()
        assert "test warning msg" in captured.out

    def test_summary_shows_errors(self, capsys):
        state = PipelineState()
        state.errors.append("test error msg")
        config = PipelineConfig()
        _stage_summary(config, state)
        captured = capsys.readouterr()
        assert "test error msg" in captured.out


# ── Stage timings ─────────────────────────────────────────────────

class TestStageTimings:
    def test_validate_config_records_timing(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        _stage_validate_config(config, state)
        assert "validate_config" in state.stage_timings
        assert state.stage_timings["validate_config"] >= 0

    def test_create_run_records_timing(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        _stage_create_run(config, state)
        assert "create_run" in state.stage_timings

    def test_validate_records_timing(self):
        config = PipelineConfig(dry_run=True)
        state = PipelineState()
        state.n_approved_rows = 10
        _stage_validate(config, state)
        assert "validate" in state.stage_timings

    def test_reports_records_timing(self, tmp_path):
        config = PipelineConfig(dry_run=True, output_dir=str(tmp_path / "out"))
        state = PipelineState()
        state.scan_result = {"opportunities": [], "yn_opportunities": []}
        _stage_reports(config, state)
        assert "reports" in state.stage_timings


# ── main() integration ───────────────────────────────────────────

class TestMainIntegration:
    def test_main_returns_int(self):
        with patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test"}):
            with patch("src.daily_pipeline.run_pipeline", return_value=0):
                code = main([])
        assert isinstance(code, int)

    def test_main_passes_config(self):
        with patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test"}), \
             patch("src.daily_pipeline.run_pipeline") as mock_rp:
            mock_rp.return_value = 0
            main(["--live", "--market", "strikeouts", "--dry-run"])
            config = mock_rp.call_args[0][0]
            assert config.live is True
            assert config.market == "strikeouts"
            assert config.dry_run is True


# ── Unexpected failure ────────────────────────────────────────────

class TestUnexpectedFailure:
    def test_unexpected_exception_returns_exit_code(self, tmp_path):
        config = PipelineConfig(
            dry_run=True, output_dir=str(tmp_path / "output"),
        )
        with patch("src.daily_pipeline._stage_validate_config",
                    side_effect=RuntimeError("boom")):
            code = run_pipeline(config)
        assert code == EXIT_UNEXPECTED_FAILURE
