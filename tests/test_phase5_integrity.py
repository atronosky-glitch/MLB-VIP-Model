"""Tests for Phase 5: Data Integrity and Operational Hardening.

Covers: run tracking, config validation, error persistence, database schema,
--min-ev YN rejection, --require-fresh flag, game filtering, no-data hint.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import database.db_manager as dbm
from database.db_manager import (
    create_run, finish_run, log_ingestion, persist_scan_error,
)
from src.player_prop_scanner import (
    run_scan, parse_args, display_results, main,
)
from src.player_prop_parser import ParsedPlayerPropResult


# ==================================================================
# Helpers
# ==================================================================

def _create_in_memory_db():
    """Create an in-memory SQLite database with all required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            run_id          TEXT PRIMARY KEY,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            run_type        TEXT NOT NULL DEFAULT 'scan',
            mode            TEXT,
            market_filter   TEXT,
            form_filter     TEXT,
            n_events        INTEGER DEFAULT 0,
            n_markets       INTEGER DEFAULT 0,
            n_opportunities INTEGER DEFAULT 0,
            n_yn_opps       INTEGER DEFAULT 0,
            data_source     TEXT,
            research_only   INTEGER DEFAULT 0,
            error_message   TEXT,
            metadata_json   TEXT
        );
        CREATE TABLE IF NOT EXISTS ingestion_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT,
            event_id        TEXT NOT NULL,
            ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
            odds_rows       INTEGER DEFAULT 0,
            audit_rows      INTEGER DEFAULT 0,
            error_message   TEXT,
            FOREIGN KEY (run_id) REFERENCES scan_runs(run_id)
        );
        CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY, league TEXT NOT NULL DEFAULT 'MLB',
            away_team TEXT, home_team TEXT, start_time TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            sport_id TEXT, league_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES games(event_id),
            sportsbook TEXT NOT NULL, market TEXT NOT NULL, selection TEXT,
            price REAL, points REAL, is_alt_line INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 1,
            pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
            odd_id TEXT DEFAULT '', validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT 'NONE',
            mapping_method TEXT DEFAULT '', validation_reason TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS raw_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL, params TEXT,
            pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
            response_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS data_pulls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, pull_type TEXT NOT NULL,
            pulled_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bet_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, sportsbook TEXT NOT NULL,
            market TEXT NOT NULL, selection TEXT NOT NULL, price REAL,
            outcome TEXT, units REAL, profit REAL, graded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS odds_mapping_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, raw_participant_id TEXT,
            raw_participant_name TEXT, matched_team_id TEXT,
            matched_team_name TEXT, mapping_method TEXT,
            mapping_confidence TEXT, validation_status TEXT,
            validation_reason TEXT, price REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, player_id TEXT NOT NULL,
            player_name TEXT, team_id TEXT DEFAULT '',
            team_name TEXT DEFAULT '', market_type TEXT NOT NULL,
            market_group_key TEXT NOT NULL, side TEXT NOT NULL,
            line REAL, price INTEGER, decimal_odds REAL,
            is_alt_line INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 1,
            validation_status TEXT NOT NULL DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT '',
            mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT '',
            captured_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_mapping_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL, player_id TEXT,
            player_name TEXT, team_id TEXT, team_name TEXT,
            market_type TEXT, market_group_key TEXT, side TEXT,
            line REAL, price INTEGER, decimal_odds REAL,
            is_alt_line INTEGER DEFAULT 0, available INTEGER DEFAULT 1,
            validation_status TEXT, mapping_confidence TEXT,
            mapping_method TEXT, validation_reason TEXT,
            excluded INTEGER DEFAULT 0, exclusion_reasons TEXT,
            captured_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    return conn


def _make_opp(ev_pct: float = 3.0, **overrides) -> dict:
    """Build a single O/U opportunity dict."""
    opp = {
        "event_id": "ev1",
        "away_team": "TeamA",
        "home_team": "TeamB",
        "start_time": "2026-07-20T23:00:00Z",
        "player_id": "PLAYER_1_MLB",
        "player_name": "Test Player",
        "market_type": "pitching_strikeouts_ou",
        "line": 5.5,
        "side": "OVER",
        "sportsbook": "testbook",
        "american_odds": -110,
        "decimal_odds": 1.9091,
        "n_consensus_books": 5,
        "fair_prob": 0.5,
        "ev_pct": ev_pct,
        "market_quality": "VALID_MARKET",
        "rec_eligible": True,
        "bet_status": "STRONG_EDGE",
        "validation_status": "VALID",
        "is_alt_line": 0,
    }
    opp.update(overrides)
    return opp


def _fake_result(opps=None, yn_opps=None, **overrides) -> dict:
    """Build a fake run_scan result dict."""
    result = {
        "opportunities": opps or [],
        "yn_opportunities": yn_opps or [],
        "n_events": 3,
        "n_markets": 2,
        "n_pitchers": 2,
        "n_approved_rows": 20,
        "n_excluded_rows": 0,
        "scan_start": "2026-07-20T12:00:00+00:00",
        "fetch_time": "2026-07-20T11:59:00+00:00",
        "data_source": "CACHE",
        "oldest_obs": "2026-07-20T11:50:00+00:00",
        "newest_obs": "2026-07-20T11:59:00+00:00",
        "age_seconds": 60,
        "stale_warning": False,
        "research_only": True,
        "scanner_title": "MLB PITCHER STRIKEOUTS EDGE SCANNER",
    }
    result.update(overrides)
    return result


def _make_game_event(event_id: str, away_name: str, home_name: str) -> dict:
    """Create a minimal event dict for game filter testing."""
    return {
        "eventID": event_id,
        "teams": {
            "away": {"names": {"long": away_name}, "teamID": "AWAY"},
            "home": {"names": {"long": home_name}, "teamID": "HOME"},
        },
        "status": {"startsAt": "2026-07-20T23:00:00Z"},
    }


def _make_ou_odds_pair(event_id: str, player_id: str = "P1",
                       line: float = 5.5) -> list[dict]:
    """Create a matched OVER + UNDER odds row pair for one event."""
    group_key = f"{event_id}_{player_id}_pitching_strikeouts_ou_{line}"
    base = {
        "event_id": event_id,
        "odd_id": "pitching_strikeouts-game-ou-over",
        "sportsbook": "book_a",
        "player_id": player_id,
        "player_name": "Test Pitcher",
        "market_type": "pitching_strikeouts_ou",
        "market_group_key": group_key,
        "side": "OVER",
        "line": line,
        "price": -110,
        "decimal_odds": 1.9091,
        "is_alt_line": 0,
        "available": 1,
        "validation_status": "VALID",
        "mapping_confidence": "HIGH",
        "mapping_method": "test",
        "validation_reason": "",
        "captured_at": "2026-07-20T11:59:00Z",
    }
    over_row = dict(base)
    under_row = dict(base)
    under_row["odd_id"] = "pitching_strikeouts-game-ou-under"
    under_row["sportsbook"] = "book_b"
    under_row["side"] = "UNDER"
    return [over_row, under_row]


def _run_scan_with_mocks(events: list[dict], odds_map: dict,
                         game_filter: str | None = None, *,
                         mode: str = "all") -> dict:
    """Run run_scan with fully mocked pipeline for game filter testing."""
    with mock.patch("src.player_prop_scanner.get_connection") as mock_gc, \
         mock.patch("src.player_prop_scanner.create_run",
                    return_value="test-run-id"), \
         mock.patch("src.player_prop_scanner.finish_run"), \
         mock.patch("src.player_prop_scanner.SportsGameOddsClient") as mock_cls, \
         mock.patch("src.player_prop_scanner.parse_player_props") as mock_parse, \
         mock.patch("src.player_prop_scanner.analyze_prop_group") as mock_analyze:

        mock_gc.return_value = mock.MagicMock()
        mock_cls.return_value.get_events.return_value = ({"data": events}, False)

        def _parse_side(event):
            eid = event.get("eventID", "")
            return ParsedPlayerPropResult(
                odds_rows=odds_map.get(eid, []), audit_rows=[],
            )
        mock_parse.side_effect = _parse_side

        mock_analyze.return_value = {
            "market_quality": "VALID_MARKET",
            "n_paired_books": 5,
            "books": [{
                "included": True,
                "side": "OVER",
                "sportsbook": "book_a",
                "american_odds": -110,
                "decimal_odds": 1.9091,
                "fair_prob": 0.5,
                "ev_pct": 5.0,
                "bet_status": "STRONG_EDGE",
                "validation_status": "VALID",
            }],
        }

        return run_scan(
            mode=mode, market="all", market_form="all", game=game_filter,
        )


def _init_db_to_temp() -> str:
    """Run init_db() against a temp file and return its path.

    Caller is responsible for deleting the file.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)

    orig_path = dbm.DB_PATH
    orig_get_conn = dbm.get_connection

    dbm.DB_PATH = Path(tmp_path)
    dbm.get_connection = lambda: sqlite3.connect(str(tmp_path))
    try:
        dbm.init_db()
    finally:
        dbm.DB_PATH = orig_path
        dbm.get_connection = orig_get_conn

    return tmp_path


# ==================================================================
# 1. Run tracking (5.1)
# ==================================================================

class TestRunTracking:
    def test_create_run_returns_uuid(self):
        conn = _create_in_memory_db()
        try:
            run_id = create_run(conn, run_type="scan")
            assert isinstance(run_id, str)
            assert len(run_id) > 0
            parts = run_id.split("-")
            assert len(parts) == 5
        finally:
            conn.close()

    def test_finish_run_updates_fields(self):
        conn = _create_in_memory_db()
        try:
            run_id = create_run(conn, run_type="scan")
            finish_run(
                conn, run_id,
                n_events=10, n_markets=5,
                n_opportunities=3, n_yn_opps=2,
                data_source="CACHE", research_only=True,
            )
            row = conn.execute(
                "SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert row is not None
            assert row["n_events"] == 10
            assert row["n_markets"] == 5
            assert row["n_opportunities"] == 3
            assert row["n_yn_opps"] == 2
            assert row["data_source"] == "CACHE"
            assert row["research_only"] == 1
            assert row["finished_at"] is not None
        finally:
            conn.close()

    def test_run_with_metadata(self):
        conn = _create_in_memory_db()
        try:
            metadata = {"endpoint": "/v2/events", "league": "MLB", "count": 42}
            run_id = create_run(conn, run_type="scan", metadata=metadata)
            row = conn.execute(
                "SELECT metadata_json FROM scan_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            stored = json.loads(row["metadata_json"])
            assert stored["endpoint"] == "/v2/events"
            assert stored["league"] == "MLB"
            assert stored["count"] == 42
        finally:
            conn.close()

    def test_ingestion_log_records_event(self):
        conn = _create_in_memory_db()
        try:
            run_id = create_run(conn, run_type="scan")
            log_ingestion(
                conn, run_id, event_id="EV_123",
                odds_rows=5, audit_rows=3,
            )
            row = conn.execute(
                "SELECT * FROM ingestion_log "
                "WHERE event_id = ? AND run_id = ?",
                ("EV_123", run_id),
            ).fetchone()
            assert row is not None
            assert row["event_id"] == "EV_123"
            assert row["run_id"] == run_id
            assert row["odds_rows"] == 5
            assert row["audit_rows"] == 3
        finally:
            conn.close()


# ==================================================================
# 2. Config validation (5.10)
# ==================================================================

class TestConfigValidation:
    def test_validate_config_valid(self):
        from src import prop_config as cfg
        errors = cfg.validate_config()
        assert errors == []

    def test_validate_config_strong_threshold_ordering(self):
        from src import prop_config as cfg
        orig_strong = cfg.STRONG_EDGE_THRESHOLD
        orig_pos = cfg.POSITIVE_EDGE_THRESHOLD
        try:
            cfg.STRONG_EDGE_THRESHOLD = 0.01
            cfg.POSITIVE_EDGE_THRESHOLD = 0.05
            errors = cfg.validate_config()
            assert any("STRONG_EDGE_THRESHOLD" in e for e in errors)
        finally:
            cfg.STRONG_EDGE_THRESHOLD = orig_strong
            cfg.POSITIVE_EDGE_THRESHOLD = orig_pos

    def test_validate_config_yn_threshold_ordering(self):
        from src import prop_config as cfg
        orig_strong = cfg.YN_STRONG_OUTLIER_THRESHOLD
        orig_outlier = cfg.YN_OUTLIER_THRESHOLD
        try:
            cfg.YN_STRONG_OUTLIER_THRESHOLD = 0.02
            cfg.YN_OUTLIER_THRESHOLD = 0.08
            errors = cfg.validate_config()
            assert any("YN_STRONG_OUTLIER_THRESHOLD" in e for e in errors)
        finally:
            cfg.YN_STRONG_OUTLIER_THRESHOLD = orig_strong
            cfg.YN_OUTLIER_THRESHOLD = orig_outlier

    def test_validate_config_duplicate_cli_names(self):
        from src import prop_config as cfg
        from src.prop_config import MarketConfig
        orig_registry = list(cfg.MARKET_REGISTRY)
        try:
            dupe = MarketConfig(
                cli_name="strikeouts",
                odd_id_stat_prefix="test_dup",
                market_type_ou="test_dup_ou",
                market_type_yn=None,
                display_name="Dup",
                short_label="D",
                period="game",
            )
            cfg.MARKET_REGISTRY.append(dupe)
            errors = cfg.validate_config()
            assert any("Duplicate CLI names" in e for e in errors)
        finally:
            cfg.MARKET_REGISTRY.clear()
            cfg.MARKET_REGISTRY.extend(orig_registry)

    def test_validate_config_empty_cli_name(self):
        from src import prop_config as cfg
        from src.prop_config import MarketConfig
        orig_registry = list(cfg.MARKET_REGISTRY)
        try:
            bad = MarketConfig(
                cli_name="",
                odd_id_stat_prefix="test_empty",
                market_type_ou="test_empty_ou",
                market_type_yn=None,
                display_name="Empty",
                short_label="E",
                period="game",
            )
            cfg.MARKET_REGISTRY.append(bad)
            errors = cfg.validate_config()
            assert any("empty cli_name" in e for e in errors)
        finally:
            cfg.MARKET_REGISTRY.clear()
            cfg.MARKET_REGISTRY.extend(orig_registry)


# ==================================================================
# 3. Error persistence (5.9)
# ==================================================================

class TestErrorPersistence:
    def test_persist_scan_error(self):
        conn = _create_in_memory_db()
        try:
            run_id = create_run(conn, run_type="scan")
            persist_scan_error(
                conn, run_id,
                error_type="api_failure",
                error_message="Connection timed out",
                context={"endpoint": "/v2/events", "status": 504},
            )
            row = conn.execute(
                "SELECT * FROM ingestion_log WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row is not None
            assert row["event_id"] == "_error_api_failure"
            assert "[api_failure]" in row["error_message"]
            assert "Connection timed out" in row["error_message"]
            assert row["odds_rows"] == 0
            assert row["audit_rows"] == 0
        finally:
            conn.close()


# ==================================================================
# 4. Database schema (5.2)
# ==================================================================

class TestDatabaseSchema:
    def test_scan_runs_table_exists(self):
        tmp_path = _init_db_to_temp()
        try:
            conn = sqlite3.connect(tmp_path)
            conn.row_factory = sqlite3.Row
            result = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='scan_runs'"
            ).fetchall()
            assert len(result) == 1
            assert result[0]["name"] == "scan_runs"
            conn.close()
        finally:
            os.unlink(tmp_path)

    def test_ingestion_log_table_exists(self):
        tmp_path = _init_db_to_temp()
        try:
            conn = sqlite3.connect(tmp_path)
            conn.row_factory = sqlite3.Row
            result = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='ingestion_log'"
            ).fetchall()
            assert len(result) == 1
            assert result[0]["name"] == "ingestion_log"
            conn.close()
        finally:
            os.unlink(tmp_path)


# ==================================================================
# 5. --min-ev YN rejection (Fix 4a)
# ==================================================================

class TestMinEvYNRejection:
    def test_min_ev_rejected_for_yn_form(self):
        with pytest.raises(SystemExit):
            main(["--min-ev", "0.05", "--market-form", "yn"])

    def test_min_ev_accepted_for_ou_form(self):
        with mock.patch("src.player_prop_scanner.run_scan") as mock_scan:
            mock_scan.return_value = _fake_result()
            old_stdout = sys.stdout
            sys.stdout = StringIO()
            try:
                main(["--min-ev", "0.05", "--market-form", "ou"])
            finally:
                sys.stdout = old_stdout
            mock_scan.assert_called_once()
            call_kwargs = mock_scan.call_args[1]
            assert call_kwargs["min_ev"] == 0.05
            assert call_kwargs["market_form"] == "ou"


# ==================================================================
# 6. --require-fresh flag (5.7)
# ==================================================================

class TestRequireFresh:
    def test_require_fresh_in_parser(self):
        args = parse_args(["--require-fresh"])
        assert args.require_fresh is True

    def test_require_fresh_default_false(self):
        args = parse_args([])
        assert args.require_fresh is False


# ==================================================================
# 7. Game filtering (Fix 4b)
# ==================================================================

class TestGameFilter:
    def test_game_filter_matches_away_team(self):
        event = _make_game_event("ev1", "Los Angeles Dodgers",
                                "San Francisco Giants")
        odds = _make_ou_odds_pair("ev1")
        result = _run_scan_with_mocks([event], {"ev1": odds},
                                      game_filter="Dodgers")
        assert len(result["opportunities"]) == 1

    def test_game_filter_matches_home_team(self):
        event = _make_game_event("ev1", "New York Yankees",
                                "Los Angeles Dodgers")
        odds = _make_ou_odds_pair("ev1")
        result = _run_scan_with_mocks([event], {"ev1": odds},
                                      game_filter="Dodgers")
        assert len(result["opportunities"]) == 1

    def test_game_filter_short_event_id_ignored(self):
        event = _make_game_event("abc", "Team A", "Team B")
        odds = _make_ou_odds_pair("abc")
        result = _run_scan_with_mocks([event], {"abc": odds},
                                      game_filter="abc")
        assert len(result["opportunities"]) == 0

    def test_game_filter_long_event_id_matched(self):
        event = _make_game_event("abcd1234", "Team A", "Team B")
        odds = _make_ou_odds_pair("abcd1234")
        result = _run_scan_with_mocks([event], {"abcd1234": odds},
                                      game_filter="abcd")
        assert len(result["opportunities"]) == 1


# ==================================================================
# 8. No-data hint (Fix 4c)
# ==================================================================

class TestNoDataHint:
    def test_display_results_shows_hint_when_no_approved(self):
        result = _fake_result(opps=[], yn_opps=[],
                              n_approved_rows=0, n_markets=0)
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "actionable")
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        assert "NO QUALIFYING OPPORTUNITIES" in output
        assert "Hint: No approved odds rows were found" in output
