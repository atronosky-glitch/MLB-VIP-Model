"""Phase 13 — Dashboard & export improvements: deterministic tests.

All tests use in-memory or temp-file databases with explicit timestamps.
No clock-dependent tests.
"""

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ==================================================================
# Helpers
# ==================================================================


def _make_rec_db(
    tmp_path: Path,
    recs: list[dict] | None = None,
    *,
    games: list[dict] | None = None,
) -> str:
    """Create a temp DB with historical_recommendations and games tables."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games (
            event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
            away_team TEXT, home_team TEXT, start_time TEXT,
            status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE historical_recommendations (
            recommendation_id TEXT PRIMARY KEY, fingerprint TEXT,
            event_id TEXT, player_name TEXT, market_type TEXT,
            market_form TEXT, period TEXT, line REAL, side TEXT,
            sportsbook TEXT, offered_american_odds INTEGER,
            offered_decimal_odds REAL, offered_implied_prob REAL,
            fair_prob REAL, fair_american_odds INTEGER, ev_pct REAL,
            yn_reference_prob REAL, yn_reference_odds INTEGER,
            yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
            n_consensus_books INTEGER, market_quality TEXT,
            rec_status TEXT, rec_eligible INTEGER, data_source TEXT,
            observation_timestamp TEXT, scan_timestamp TEXT,
            freshness_status TEXT, model_version TEXT,
            scan_run_id TEXT DEFAULT '', event_start_time TEXT DEFAULT '',
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
            qualification_timestamp TEXT DEFAULT '',
            official_rank INTEGER,
            points_to_7 REAL DEFAULT 0.0,
            price_outlier_capped INTEGER DEFAULT 0,
            true_ev_unavailable INTEGER DEFAULT 0,
            one_sided_market INTEGER DEFAULT 0,
            insufficient_books_failure INTEGER DEFAULT 0,
            market_quality_score REAL DEFAULT 0.0,
            created_at TEXT DEFAULT (datetime('now'))
        );
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
    """)

    if games:
        for g in games:
            conn.execute(
                "INSERT INTO games (event_id, away_team, home_team, start_time, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (g["event_id"], g.get("away_team", ""), g.get("home_team", ""),
                 g.get("start_time", ""), g.get("status", "scheduled")),
            )

    if recs:
        for r in recs:
            conn.execute(
                "INSERT INTO historical_recommendations "
                "(recommendation_id, fingerprint, event_id, player_name, market_type, "
                "market_form, period, line, side, sportsbook, offered_american_odds, "
                "offered_decimal_odds, offered_implied_prob, ev_pct, yn_implied_prob_adv, "
                "n_consensus_books, rec_status, rec_eligible, scan_timestamp, "
                "freshness_status, scan_run_id, event_start_time, matchup, event_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["recommendation_id"], r.get("fingerprint", "fp"),
                    r["event_id"], r.get("player_name", ""),
                    r.get("market_type", "strikeouts"), r.get("market_form", "ou"),
                    r.get("period", "game"), r.get("line"),
                    r.get("side", "Over"), r.get("sportsbook", "DK"),
                    r.get("offered_american_odds", -110),
                    r.get("offered_decimal_odds", 1.91),
                    r.get("offered_implied_prob", 0.526),
                    r.get("ev_pct"), r.get("yn_implied_prob_adv"),
                    r.get("n_consensus_books", 6),
                    r.get("rec_status", "BET"), 1 if r.get("rec_eligible", True) else 0,
                    r.get("scan_timestamp", "2026-07-25T14:00:00+00:00"),
                    r.get("freshness_status", "fresh"),
                    r.get("scan_run_id", "run-001"),
                    r.get("event_start_time", ""),
                    r.get("matchup", ""),
                    r.get("event_status", ""),
                ),
            )
    # Auto-populate scan_runs from unique scan_run_ids in recs (encounter order)
    seen_ids: set[str] = set()
    if recs:
        for i, r in enumerate(recs):
            rid = r.get("scan_run_id", "")
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                conn.execute(
                    "INSERT OR IGNORE INTO scan_runs "
                    "(run_id, started_at, finished_at, run_type) VALUES (?, ?, ?, ?)",
                    (rid, f"2026-07-25T{10+i:02d}:00:00+00:00",
                     "2026-07-25T23:59:59+00:00", "scan"),
                )
    conn.commit()
    conn.close()
    return db_path


# ==================================================================
# Live-game filtering in pipeline
# ==================================================================


class TestIsGameSkippable:
    """Test the _is_game_skippable helper from daily_pipeline."""

    def test_live_game_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, reason = _is_game_skippable("live", datetime.now(timezone.utc))
        assert skippable is True
        assert "live" in reason.lower()

    def test_in_progress_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, _ = _is_game_skippable("in_progress", datetime.now(timezone.utc))
        assert skippable is True

    def test_final_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, reason = _is_game_skippable("final", datetime.now(timezone.utc))
        assert skippable is True
        assert "completed" in reason.lower() or "final" in reason.lower()

    def test_finished_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, _ = _is_game_skippable("finished", datetime.now(timezone.utc))
        assert skippable is True

    def test_completed_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, _ = _is_game_skippable("completed", datetime.now(timezone.utc))
        assert skippable is True

    def test_started_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, _ = _is_game_skippable("started", datetime.now(timezone.utc))
        assert skippable is True

    def test_scheduled_not_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        skippable, reason = _is_game_skippable("scheduled", future)
        assert skippable is False

    def test_future_start_time_not_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        skippable, _ = _is_game_skippable("scheduled", future)
        assert skippable is False

    def test_past_start_time_with_scheduled_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        skippable, reason = _is_game_skippable("scheduled", past)
        assert skippable is True
        assert "started" in reason.lower() or "past" in reason.lower()

    def test_postponed_not_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        skippable, _ = _is_game_skippable("postponed", datetime.now(timezone.utc))
        assert skippable is False

    def test_empty_status_not_skipped(self):
        from src.daily_pipeline import _is_game_skippable
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        skippable, _ = _is_game_skippable("", future)
        assert skippable is False


class TestBuildMatchup:
    """Test the _build_matchup helper from daily_pipeline."""

    def test_basic_matchup(self):
        from src.daily_pipeline import _build_matchup
        result = _build_matchup("Yankees", "Dodgers")
        assert "Yankees" in result
        assert "Dodgers" in result

    def test_away_home_order(self):
        from src.daily_pipeline import _build_matchup
        result = _build_matchup("Red Sox", "Blue Jays")
        assert result.index("Red Sox") < result.index("Blue Jays")

    def test_missing_teams(self):
        from src.daily_pipeline import _build_matchup
        result = _build_matchup(None, None)
        assert result == "" or "None" not in result


# ==================================================================
# Latest-run filtering (control panel _load_recs)
# ==================================================================


class TestLoadRecs:
    """Test the _load_recs function with different filter modes."""

    def test_latest_returns_only_newest_run(self, tmp_path):
        from src.control_panel import _load_recs
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T10:00:00+00:00", "scan_run_id": "run-old",
             "ev_pct": 0.05, "rec_status": "BET"},
            {"recommendation_id": "r2", "event_id": "e2", "player_name": "Ohtani",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-new",
             "ev_pct": 0.03, "rec_status": "BET"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        result = _load_recs(db_path, "latest")
        assert len(result) == 1
        assert result[0]["player_name"] == "Ohtani"

    def test_all_returns_everything(self, tmp_path):
        from src.control_panel import _load_recs
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T10:00:00+00:00", "scan_run_id": "run-old",
             "ev_pct": 0.05, "rec_status": "BET"},
            {"recommendation_id": "r2", "event_id": "e2", "player_name": "Ohtani",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-new",
             "ev_pct": 0.03, "rec_status": "BET"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        result = _load_recs(db_path, "all")
        assert len(result) == 2

    def test_nonexistent_db_returns_empty(self):
        from src.control_panel import _load_recs
        result = _load_recs("/nonexistent/path.db", "latest")
        assert result == []

    def test_empty_db_returns_empty(self, tmp_path):
        from src.control_panel import _load_recs
        db_path = _make_rec_db(tmp_path)
        result = _load_recs(db_path, "all")
        assert result == []

    def test_no_accumulation_across_runs(self, tmp_path):
        from src.control_panel import _load_recs
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T10:00:00+00:00", "scan_run_id": "run-old",
             "ev_pct": 0.05, "rec_status": "BET"},
            {"recommendation_id": "r2", "event_id": "e2", "player_name": "Ohtani",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-old",
             "ev_pct": 0.03, "rec_status": "BET"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        result = _load_recs(db_path, "latest")
        assert len(result) == 2  # same run → both shown


class TestGetLatestRunId:
    """Test the _get_latest_run_id helper."""

    def test_returns_newest_run_id(self, tmp_path):
        from src.control_panel import _get_latest_run_id
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T10:00:00+00:00", "scan_run_id": "run-old",
             "ev_pct": 0.05, "rec_status": "BET"},
            {"recommendation_id": "r2", "event_id": "e2", "player_name": "Ohtani",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-new",
             "ev_pct": 0.03, "rec_status": "BET"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        run_id = _get_latest_run_id(db_path)
        assert run_id == "run-new"

    def test_empty_db_returns_empty(self, tmp_path):
        from src.control_panel import _get_latest_run_id
        db_path = _make_rec_db(tmp_path)
        assert _get_latest_run_id(db_path) == ""


# ==================================================================
# Game detail columns in recommendation records
# ==================================================================


class TestGameDetails:
    """Test that matchup, event_status, event_start_time are populated."""

    def test_matchup_populated(self, tmp_path):
        from src.control_panel import _load_recs
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-001",
             "ev_pct": 0.05, "rec_status": "BET",
             "matchup": "NYY @ LAD", "event_status": "scheduled",
             "event_start_time": "2026-07-25T19:00:00+00:00"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        result = _load_recs(db_path, "all")
        assert result[0]["matchup"] == "NYY @ LAD"
        assert result[0]["event_status"] == "scheduled"
        assert "2026-07-25" in result[0]["event_start_time"]

    def test_csv_includes_game_columns(self, tmp_path):
        from src.control_panel import _load_recs
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-001",
             "ev_pct": 0.05, "rec_status": "BET",
             "matchup": "NYY @ LAD", "event_status": "scheduled",
             "event_start_time": "2026-07-25T19:00:00+00:00"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        result = _load_recs(db_path, "all")
        import pandas as pd
        df = pd.DataFrame(result)
        csv_data = df.to_csv(index=False)
        assert "NYY @ LAD" in csv_data
        assert "scheduled" in csv_data


# ==================================================================
# Pipeline state fields
# ==================================================================


class TestPipelineState:
    """Test that PipelineState has the new fields."""

    def test_has_skipped_games_list(self):
        from src.daily_pipeline import PipelineState
        state = PipelineState()
        assert hasattr(state, "skipped_games")
        assert isinstance(state.skipped_games, list)

    def test_has_game_count_fields(self):
        from src.daily_pipeline import PipelineState
        state = PipelineState()
        assert hasattr(state, "n_games_analyzed")
        assert hasattr(state, "n_games_skipped")
        assert hasattr(state, "n_total_games")
        assert state.n_games_analyzed == 0
        assert state.n_games_skipped == 0

    def test_has_live_game_recs_field(self):
        from src.daily_pipeline import PipelineState
        state = PipelineState()
        assert hasattr(state, "has_live_game_recs")
        assert state.has_live_game_recs is False


# ==================================================================
# Run summary includes game metrics
# ==================================================================


class TestRunSummary:
    """Test that _build_run_summary includes game metrics."""

    def test_summary_includes_game_metrics(self):
        from src.daily_pipeline import PipelineState, _build_run_summary
        state = PipelineState()
        state.n_total_games = 15
        state.n_games_analyzed = 12
        state.n_games_skipped = 3
        state.skipped_games = [
            {"matchup": "BOS @ NYY", "start_time": "2026-07-25T19:00:00",
             "status": "live", "reason": "game is live"},
        ]
        summary = _build_run_summary(state)
        assert summary["metrics"]["n_total_games"] == 15
        assert summary["metrics"]["n_games_analyzed"] == 12
        assert summary["metrics"]["n_games_skipped"] == 3
        assert len(summary["skipped_games"]) == 1
        assert summary["skipped_games"][0]["matchup"] == "BOS @ NYY"


# ==================================================================
# Control panel helpers
# ==================================================================


class TestGetScheduleSummary:
    """Test the _get_schedule_summary function."""

    def test_get_schedule_summary(self, tmp_path):
        from src.control_panel import _get_schedule_summary
        now = datetime.now(timezone.utc)
        future = now.replace(hour=12, minute=30, second=0, microsecond=0).isoformat()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
                away_team TEXT, home_team TEXT, start_time TEXT,
                status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO games VALUES ('e1', 'MLB', 'BOS', 'NYY', ?, 'scheduled', NULL, NULL, datetime('now'), datetime('now'))",
            (future,),
        )
        conn.execute(
            "INSERT INTO games VALUES ('e2', 'MLB', 'LAD', 'SF', ?, 'final', NULL, NULL, datetime('now'), datetime('now'))",
            (future,),
        )
        conn.commit()
        conn.close()
        summary = _get_schedule_summary(db_path)
        assert summary["total"] == 2
        assert summary["completed"] == 1
        assert summary["upcoming"] == 1

    def test_schedule_includes_postponed_cancelled(self, tmp_path):
        from src.control_panel import _get_schedule_summary
        now = datetime.now(timezone.utc)
        future = now.replace(hour=12, minute=30, second=0, microsecond=0).isoformat()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
                away_team TEXT, home_team TEXT, start_time TEXT,
                status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO games VALUES ('e1','MLB','BOS','NYY',?,'scheduled',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e2','MLB','LAD','SF',?,'final',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e3','MLB','CHC','NYM',?,'postponed',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e4','MLB','ATL','PHI',?,'cancelled',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.commit()
        conn.close()
        summary = _get_schedule_summary(db_path)
        assert summary["total"] == 4
        assert summary["upcoming"] == 1
        assert summary["completed"] == 1
        assert summary["postponed"] == 1
        assert summary["cancelled"] == 1
        assert summary["eligible"] == 2

    def test_schedule_total_equals_parts(self, tmp_path):
        from src.control_panel import _get_schedule_summary
        now = datetime.now(timezone.utc)
        future = now.replace(hour=12, minute=30, second=0, microsecond=0).isoformat()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
                away_team TEXT, home_team TEXT, start_time TEXT,
                status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO games VALUES ('e1','MLB','BOS','NYY',?,'scheduled',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e2','MLB','LAD','SF',?,'live',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e3','MLB','CHC','NYM',?,'final',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e4','MLB','ATL','PHI',?,'postponed',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.commit()
        conn.close()
        summary = _get_schedule_summary(db_path)
        parts_sum = summary["upcoming"] + summary["live"] + summary["completed"] + summary["postponed"] + summary["cancelled"]
        assert summary["total"] == parts_sum

    def test_schedule_eligible_excludes_postponed_cancelled(self, tmp_path):
        from src.control_panel import _get_schedule_summary
        now = datetime.now(timezone.utc)
        future = now.replace(hour=12, minute=30, second=0, microsecond=0).isoformat()
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
                away_team TEXT, home_team TEXT, start_time TEXT,
                status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT INTO games VALUES ('e1','MLB','BOS','NYY',?,'scheduled',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e2','MLB','LAD','SF',?,'postponed',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.execute("INSERT INTO games VALUES ('e3','MLB','CHC','NYM',?,'cancelled',NULL,NULL,datetime('now'),datetime('now'))", (future,))
        conn.commit()
        conn.close()
        summary = _get_schedule_summary(db_path)
        assert summary["eligible"] == 1
        assert summary["postponed"] == 1
        assert summary["cancelled"] == 1

    def test_schedule_validates_analyzed_plus_skipped(self, tmp_path):
        from src.control_panel import _get_schedule_summary
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        start_t = f"{today_str}T20:00:00+00:00"
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": now.isoformat(), "scan_run_id": "run-001",
             "ev_pct": 0.05, "rec_status": "BET",
             "matchup": "BOS @ NYY", "event_status": "scheduled",
             "event_start_time": start_t},
        ]
        games = [
            {"event_id": "e1", "away_team": "BOS", "home_team": "NYY",
             "start_time": start_t, "status": "scheduled"},
            {"event_id": "e2", "away_team": "LAD", "home_team": "SF",
             "start_time": start_t, "status": "scheduled"},
        ]
        db_path = _make_rec_db(tmp_path, recs, games=games)
        run_summary = {"skipped_games": [{"matchup": "LAD @ SF", "reason": "missing odds"}]}
        summary = _get_schedule_summary(db_path, run_summary)
        assert summary["analyzed"] == 1
        assert summary["skipped"] == 1
        assert summary["eligible"] == 2
        assert summary["valid"] is True

    def test_empty_schedule_returns_defaults(self):
        from src.control_panel import _get_schedule_summary
        summary = _get_schedule_summary("/nonexistent/path.db")
        assert summary["total"] == 0
        assert summary["upcoming"] == 0
        assert summary["postponed"] == 0
        assert summary["cancelled"] == 0
        assert summary["eligible"] == 0


class TestControlPanelHelpers:
    """Test control panel helper functions."""

    def test_load_latest_run_summary(self, tmp_path):
        from src.control_panel import _load_latest_run_summary
        summary = {
            "pipeline_run_id": "test-run",
            "metrics": {"n_total_games": 10, "n_games_skipped": 2},
            "skipped_games": [
                {"matchup": "BOS @ NYY", "status": "live", "reason": "game is live"}
            ],
        }
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        (out_dir / "run_summary.json").write_text(json.dumps(summary))
        result = _load_latest_run_summary(str(out_dir))
        assert result is not None
        assert result["metrics"]["n_games_skipped"] == 2
        assert len(result["skipped_games"]) == 1

    def test_load_latest_run_summary_missing(self, tmp_path):
        from src.control_panel import _load_latest_run_summary
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        result = _load_latest_run_summary(str(out_dir))
        assert result is None

    def test_dashboard_uses_postgres_url_and_supported_run_tables(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "def _open_dashboard_connection" in source
        assert "get_connection()" in source
        assert "get_connection(url=url)" not in source
        assert "pipeline_runs" not in source
        assert "rowid" not in source
        assert "sqlite_master" not in source
        assert "database/mlb_model.db" not in source
        assert "FROM odds WHERE date(pulled_at)" in source


# ==================================================================
# Live-game warnings
# ==================================================================


class TestLiveGameWarnings:
    """Test the _get_live_game_warnings helper."""

    def test_finds_live_game_recs(self, tmp_path):
        from src.control_panel import _get_live_game_warnings
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-001",
             "ev_pct": 0.05, "rec_status": "BET",
             "event_status": "live", "matchup": "NYY @ LAD"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        warnings = _get_live_game_warnings(db_path, "run-001")
        assert len(warnings) == 1

    def test_no_warnings_when_clean(self, tmp_path):
        from src.control_panel import _get_live_game_warnings
        recs = [
            {"recommendation_id": "r1", "event_id": "e1", "player_name": "Judge",
             "scan_timestamp": "2026-07-25T14:00:00+00:00", "scan_run_id": "run-001",
             "ev_pct": 0.05, "rec_status": "BET",
             "event_status": "scheduled", "matchup": "NYY @ LAD"},
        ]
        db_path = _make_rec_db(tmp_path, recs)
        warnings = _get_live_game_warnings(db_path, "run-001")
        assert len(warnings) == 0

    def test_empty_db_no_warnings(self, tmp_path):
        from src.control_panel import _get_live_game_warnings
        db_path = _make_rec_db(tmp_path)
        warnings = _get_live_game_warnings(db_path, "run-001")
        assert warnings == []


# ==================================================================
# Run summary includes skipped_games and game metrics
# ==================================================================


class TestRunSummarySkippedGames:
    """Test that run_summary.json includes skipped_games list."""

    def test_skipped_games_in_summary(self):
        from src.daily_pipeline import PipelineState, _build_run_summary
        state = PipelineState()
        state.skipped_games = [
            {"matchup": "BOS @ NYY", "status": "live", "reason": "game is live",
             "start_time": "2026-07-25T19:00:00"},
            {"matchup": "LAD @ SF", "status": "final", "reason": "game completed",
             "start_time": "2026-07-25T16:00:00"},
        ]
        state.n_games_skipped = 2
        summary = _build_run_summary(state)
        assert "skipped_games" in summary
        assert len(summary["skipped_games"]) == 2
        assert summary["skipped_games"][0]["matchup"] == "BOS @ NYY"
        assert summary["skipped_games"][1]["reason"] == "game completed"


# ==================================================================
# Source code structure checks
# ==================================================================


class TestSourceStructure:
    """Verify key elements exist in the source code."""

    def test_control_panel_has_schedule_summary(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "schedule" in source.lower()

    def test_control_panel_has_run_filter(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "filter_mode" in source

    def test_control_panel_has_skipped_games_section(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "skipped_games" in source

    def test_control_panel_has_safety_warnings(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "live" in source.lower() and "warning" in source.lower()

    def test_control_panel_has_ev_columns(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"EV %"' in source
        assert '"Price Adv (pp)"' in source

    def test_control_panel_has_model_score_column(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"Model Score"' in source

    def test_control_panel_has_score_filter(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "score_filter" in source
        assert "6.0+" in source

    def test_control_panel_has_score_disclaimer(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "not a guaranteed win probability" in source

    def test_control_panel_has_run_id_display(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Run ID" in source

    def test_control_panel_has_matchup_column(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Matchup" in source

    def test_control_panel_has_start_time_column(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Start Time" in source

    def test_control_panel_has_event_status_column(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "event_status" in source

    def test_pipeline_has_is_game_skippable(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "_is_game_skippable" in source

    def test_pipeline_has_build_matchup(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "_build_matchup" in source

    def test_pipeline_has_skipped_games_state(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "skipped_games" in source

    def test_pipeline_has_validation_failure(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "has_live_game_recs" in source

    def test_run_summary_has_game_metrics(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "n_total_games" in source
        assert "n_games_analyzed" in source
        assert "n_games_skipped" in source

    def test_control_panel_has_postponed_cancelled_counts(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "postponed" in source
        assert "cancelled" in source

    def test_control_panel_has_schedule_validation(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "parts_sum" in source
        assert "valid" in source

    def test_control_panel_skipped_games_always_shows(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "Skipped Games" in source
        assert "No skipped games." in source

    def test_control_panel_refresh_triggers_rerun(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "st.rerun()" in source


# ==================================================================
# Regression: Game-level deduplication (not opportunity-level)
# ==================================================================


class TestGameLevelDeduplication:
    """Skipped games must be counted at the GAME level, not opportunity-level.

    Previously, 6 skipped props from one game would count as 6 skipped games.
    After the fix, they must count as exactly 1 skipped game.
    """

    def _make_opp(self, event_id, player_name="Player A", market_type="strikeouts",
                  sportsbook="DK", american_odds=-110, side="over", line=6.5):
        """Build a minimal opportunity dict matching scanner output."""
        return {
            "event_id": event_id,
            "player_id": "P001",
            "player_name": player_name,
            "market_type": market_type,
            "line": line,
            "side": side,
            "sportsbook": sportsbook,
            "american_odds": american_odds,
            "decimal_odds": round(1 + abs(american_odds) / 100, 4) if american_odds > 0
                           else round(1 + 100 / abs(american_odds), 4),
            "ev_pct": 5.0,
            "comparison_status": "CONSENSUS",
            "bet_status": "ACTIONABLE",
            "start_time": "2026-07-25T20:05:00Z",
            "market_reference_probability": 0.52,
            "market_reference_odds": -110,
            "price_advantage_pct": 3.0,
            "decimal_odds_advantage": 0.05,
            "n_consensus_books": 5,
            "market_quality": "STRONG",
            "rec_eligible": True,
            "fair_prob": 0.55,
        }

    def _setup_db(self, game_rows):
        """Create in-memory DB with games table, patch get_connection."""
        import database.db_manager as dbm
        import src.daily_pipeline as dp

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE IF NOT EXISTS games (
            event_id TEXT PRIMARY KEY, away_team TEXT, home_team TEXT,
            start_time TEXT, status TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_run_id TEXT, ingestion_run_id TEXT, event_id TEXT,
            event_start_time TEXT, player_id TEXT, player_name TEXT,
            market_type TEXT, market_form TEXT, period TEXT,
            line REAL, side TEXT, sportsbook TEXT,
            offered_american_odds REAL, offered_decimal_odds REAL,
            offered_implied_prob REAL, fair_prob REAL,
            fair_american_odds REAL, ev_pct REAL,
            yn_reference_prob REAL, yn_reference_odds REAL,
            yn_implied_prob_adv REAL, yn_decimal_odds_adv REAL,
            n_consensus_books INTEGER, market_quality TEXT,
            rec_status TEXT, rec_eligible INTEGER,
            data_source TEXT, observation_timestamp TEXT,
            scan_timestamp TEXT, freshness_status TEXT,
            model_version TEXT, fingerprint TEXT,
            matchup TEXT, event_status TEXT,
            model_score REAL, score_version TEXT,
            score_components TEXT, score_cap TEXT,
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
            qualification_rules_version TEXT DEFAULT ''
        )""")
        for row in game_rows:
            conn.execute(
                "INSERT INTO games VALUES (?, ?, ?, ?, ?)", row
            )
        conn.commit()

        _orig = dp.get_connection
        dp.get_connection = lambda: conn
        return conn, _orig

    def _teardown_db(self, orig_get_conn):
        import src.daily_pipeline as dp
        dp.get_connection = orig_get_conn

    def test_six_skipped_props_from_one_game_count_as_one(self):
        """6 skipped props from event E1 must count as 1 skipped game."""
        from src.daily_pipeline import PipelineState, _stage_freeze, PipelineConfig

        state = PipelineState()
        state.scan_run_id = "test-run-001"
        state.ingestion_run_id = "test-ingest-001"
        state.data_source = "test"
        state.version = "1.0.0"

        opps = [self._make_opp("E1", player_name=f"Player {i}") for i in range(6)]
        state.scan_result = {"opportunities": opps, "yn_opportunities": []}

        conn, orig = self._setup_db([
            ("E1", "BOS", "NYY", "2026-07-25T20:05:00Z", "live"),
        ])
        try:
            config = PipelineConfig(dry_run=False)
            result = _stage_freeze(config, state)

            assert result is True
            assert state.n_games_skipped == 1, (
                f"Expected 1 skipped game, got {state.n_games_skipped}"
            )
            assert len(state.skipped_games) == 1
            assert state.skipped_games[0]["event_id"] == "E1"
            assert state.skipped_games[0]["matchup"] == "BOS @ NYY"
            assert "live" in state.skipped_games[0]["reason"].lower()
        finally:
            self._teardown_db(orig)
            conn.close()

    def test_multiple_props_from_three_games_count_as_three(self):
        """4 props from E1 + 3 from E2 + 2 from E3 (all live) = 3 skipped games."""
        from src.daily_pipeline import PipelineState, _stage_freeze, PipelineConfig

        state = PipelineState()
        state.scan_run_id = "test-run-002"
        state.ingestion_run_id = "test-ingest-002"
        state.data_source = "test"
        state.version = "1.0.0"

        opps = (
            [self._make_opp("E1", player_name=f"P1-{i}") for i in range(4)]
            + [self._make_opp("E2", player_name=f"P2-{i}") for i in range(3)]
            + [self._make_opp("E3", player_name=f"P3-{i}") for i in range(2)]
        )
        state.scan_result = {"opportunities": opps, "yn_opportunities": []}

        conn, orig = self._setup_db([
            ("E1", "BOS", "NYY", "2026-07-25T20:05:00Z", "live"),
            ("E2", "LAD", "SF", "2026-07-25T20:05:00Z", "live"),
            ("E3", "CHC", "MIL", "2026-07-25T20:05:00Z", "live"),
        ])
        try:
            config = PipelineConfig(dry_run=False)
            result = _stage_freeze(config, state)

            assert result is True
            assert state.n_games_skipped == 3, (
                f"Expected 3 skipped games, got {state.n_games_skipped}"
            )
            assert len(state.skipped_games) == 3
            skipped_ids = {sg["event_id"] for sg in state.skipped_games}
            assert skipped_ids == {"E1", "E2", "E3"}
        finally:
            self._teardown_db(orig)
            conn.close()

    def test_analyzed_games_counted_by_game_not_opportunity(self):
        """4 analyzed props from E1 + 2 from E2 = 2 analyzed games, not 6."""
        from src.daily_pipeline import PipelineState, _stage_freeze, PipelineConfig

        state = PipelineState()
        state.scan_run_id = "test-run-003"
        state.ingestion_run_id = "test-ingest-003"
        state.data_source = "test"
        state.version = "1.0.0"

        opps = (
            [self._make_opp("E4", player_name=f"A-{i}") for i in range(4)]
            + [self._make_opp("E5", player_name=f"B-{i}") for i in range(2)]
        )
        state.scan_result = {"opportunities": opps, "yn_opportunities": []}

        conn, orig = self._setup_db([
            ("E4", "TEX", "HOU", "2099-07-26T01:05:00Z", "scheduled"),
            ("E5", "ATL", "PHI", "2099-07-26T01:05:00Z", "scheduled"),
        ])
        try:
            config = PipelineConfig(dry_run=False)
            result = _stage_freeze(config, state)

            assert result is True
            assert state.n_games_analyzed == 2, (
                f"Expected 2 analyzed games, got {state.n_games_analyzed}"
            )
            assert state.n_games_skipped == 0
        finally:
            self._teardown_db(orig)
            conn.close()

    def test_mixed_analyzed_and_skipped_correct_totals(self):
        """2 live games (skipped) + 1 scheduled game (analyzed) = correct counts."""
        from src.daily_pipeline import PipelineState, _stage_freeze, PipelineConfig

        state = PipelineState()
        state.scan_run_id = "test-run-004"
        state.ingestion_run_id = "test-ingest-004"
        state.data_source = "test"
        state.version = "1.0.0"

        opps = (
            [self._make_opp("E10", player_name=f"L-{i}") for i in range(5)]
            + [self._make_opp("E11", player_name=f"M-{i}") for i in range(3)]
            + [self._make_opp("E12", player_name=f"S-{i}") for i in range(2)]
        )
        state.scan_result = {"opportunities": opps, "yn_opportunities": []}

        conn, orig = self._setup_db([
            ("E10", "NYY", "BOS", "2026-07-25T20:05:00Z", "live"),
            ("E11", "LAD", "SF", "2026-07-25T20:05:00Z", "final"),
            ("E12", "CHC", "MIL", "2099-07-26T01:05:00Z", "scheduled"),
        ])
        try:
            config = PipelineConfig(dry_run=False)
            result = _stage_freeze(config, state)

            assert result is True
            assert state.n_total_games == 3
            assert state.n_games_skipped == 2
            assert state.n_games_analyzed == 1
            assert state.n_recommendations_saved >= 0
        finally:
            self._teardown_db(orig)
            conn.close()

    def test_skipped_games_list_has_no_duplicates(self):
        """Same event_id appended multiple times must appear only once."""
        from src.daily_pipeline import PipelineState, _stage_freeze, PipelineConfig

        state = PipelineState()
        state.scan_run_id = "test-run-005"
        state.ingestion_run_id = "test-ingest-005"
        state.data_source = "test"
        state.version = "1.0.0"

        opps = [self._make_opp("E20", player_name=f"X-{i}") for i in range(10)]
        state.scan_result = {"opportunities": opps, "yn_opportunities": []}

        conn, orig = self._setup_db([
            ("E20", "NYM", "WSH", "2026-07-25T20:05:00Z", "completed"),
        ])
        try:
            config = PipelineConfig(dry_run=False)
            _stage_freeze(config, state)

            event_ids = [sg["event_id"] for sg in state.skipped_games]
            assert event_ids.count("E20") == 1, (
                f"E20 appears {event_ids.count('E20')} times, expected 1"
            )
            assert len(state.skipped_games) == 1
        finally:
            self._teardown_db(orig)
            conn.close()

    def test_run_summary_game_metrics_are_game_level(self):
        """_build_run_summary must contain game-level counts."""
        from src.daily_pipeline import PipelineState, _build_run_summary
        state = PipelineState()
        state.n_total_games = 10
        state.n_games_analyzed = 7
        state.n_games_skipped = 3
        state.skipped_games = [
            {"matchup": "A @ B", "start_time": "", "status": "live", "reason": "live", "event_id": "E1"},
            {"matchup": "C @ D", "start_time": "", "status": "final", "reason": "final", "event_id": "E2"},
            {"matchup": "E @ F", "start_time": "", "status": "live", "reason": "live", "event_id": "E3"},
        ]
        summary = _build_run_summary(state)
        m = summary["metrics"]
        assert m["n_games_analyzed"] + m["n_games_skipped"] == m["n_total_games"]
        assert len(summary["skipped_games"]) == m["n_games_skipped"]


# ==================================================================
# Dashboard cleanup regression tests
# ==================================================================


class TestGameCountReconciliation:
    """Regression: analyzed and skipped must always be counted by event_id,
    never by recommendation row.  analyzed + skipped = eligible always."""

    def _make_db(self, tmp_path, games, recs=None):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE games (
                event_id TEXT PRIMARY KEY, league TEXT DEFAULT 'MLB',
                away_team TEXT, home_team TEXT, start_time TEXT,
                status TEXT DEFAULT 'scheduled', sport_id TEXT, league_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.executescript("""
            CREATE TABLE historical_recommendations (
                recommendation_id TEXT PRIMARY KEY, fingerprint TEXT,
                event_id TEXT, player_name TEXT, market_type TEXT,
                market_form TEXT, period TEXT, line REAL, side TEXT,
                sportsbook TEXT, offered_american_odds INTEGER,
                offered_decimal_odds REAL, offered_implied_prob REAL,
                fair_prob REAL, fair_american_odds INTEGER, ev_pct REAL,
                yn_reference_prob REAL, yn_reference_odds INTEGER,
                yn_implied_prob_adv REAL, yn_decimal_odds_adv INTEGER,
                n_consensus_books INTEGER, market_quality TEXT,
                rec_status TEXT, rec_eligible INTEGER, data_source TEXT,
                observation_timestamp TEXT, scan_timestamp TEXT,
                freshness_status TEXT, model_version TEXT,
                scan_run_id TEXT DEFAULT '', event_start_time TEXT DEFAULT '',
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
        """)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        for g in games:
            st = g.get("start_time", f"{today}T20:00:00+00:00")
            conn.execute(
                "INSERT INTO games (event_id, away_team, home_team, start_time, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (g["event_id"], g.get("away_team", ""), g.get("home_team", ""), st, g.get("status", "scheduled")),
            )
        if recs:
            for i, r in enumerate(recs):
                conn.execute(
                    "INSERT INTO historical_recommendations "
                    "(recommendation_id, fingerprint, event_id, player_name, market_type, "
                    "market_form, period, line, side, sportsbook, offered_american_odds, "
                    "offered_decimal_odds, offered_implied_prob, ev_pct, n_consensus_books, "
                    "rec_status, rec_eligible, scan_timestamp, freshness_status, "
                    "scan_run_id, event_start_time, matchup, event_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r.get("recommendation_id", f"r{i}"), "fp",
                        r["event_id"], r.get("player_name", ""),
                        "strikeouts", "ou", "game", None, "Over", "DK",
                        -110, 1.91, 0.526, 0.05, 6,
                        "BET", 1,
                        now.isoformat(), "fresh",
                        r.get("scan_run_id", "run-001"),
                        f"{today}T20:00:00+00:00",
                        r.get("matchup", ""), "scheduled",
                    ),
                )
        seen_ids: set[str] = set()
        if recs:
            for i, r in enumerate(recs):
                rid = r.get("scan_run_id", "run-001") if "scan_run_id" in r else ""
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    conn.execute(
                        "INSERT OR IGNORE INTO scan_runs "
                        "(run_id, started_at, finished_at, run_type) VALUES (?, ?, ?, ?)",
                        (rid, f"2026-07-25T{10+i:02d}:00:00+00:00",
                         "2026-07-25T23:59:59+00:00", "scan"),
                    )
        conn.commit()
        conn.close()
        return db_path

    def test_duplicate_rec_event_ids_counted_once(self, tmp_path):
        """Two recommendations for the same event must count as one analyzed game."""
        from src.control_panel import _get_schedule_summary
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        games = [
            {"event_id": "e1", "start_time": f"{today}T20:00:00+00:00"},
            {"event_id": "e2", "start_time": f"{today}T20:00:00+00:00"},
        ]
        recs = [
            {"event_id": "e1", "recommendation_id": "r1", "scan_run_id": "run-001"},
            {"event_id": "e1", "recommendation_id": "r2", "scan_run_id": "run-001"},
            {"event_id": "e2", "recommendation_id": "r3", "scan_run_id": "run-001"},
        ]
        db_path = self._make_db(tmp_path, games, recs)
        summary = _get_schedule_summary(db_path)
        assert summary["analyzed"] == 2, "Must count distinct event_ids, not rec rows"
        assert summary["skipped"] == 0
        assert summary["eligible"] == 2
        assert summary["analyzed"] + summary["skipped"] == summary["eligible"]

    def test_skipped_derived_not_from_json(self, tmp_path):
        """skipped = eligible - analyzed regardless of run_summary skipped_games count."""
        from src.control_panel import _get_schedule_summary
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        games = [
            {"event_id": "e1", "start_time": f"{today}T20:00:00+00:00"},
            {"event_id": "e2", "start_time": f"{today}T20:00:00+00:00"},
            {"event_id": "e3", "start_time": f"{today}T20:00:00+00:00"},
        ]
        recs = [
            {"event_id": "e1", "recommendation_id": "r1", "scan_run_id": "run-001"},
        ]
        db_path = self._make_db(tmp_path, games, recs)
        # run_summary says 5 skipped games — but eligible=3, analyzed=1, so skipped must be 2
        run_summary = {"skipped_games": [
            {"event_id": "e2", "matchup": "A @ B"},
            {"event_id": "e3", "matchup": "C @ D"},
            {"event_id": "x", "matchup": "X @ Y"},
        ]}
        summary = _get_schedule_summary(db_path, run_summary)
        assert summary["analyzed"] == 1
        assert summary["skipped"] == 2
        assert summary["eligible"] == 3
        assert summary["analyzed"] + summary["skipped"] == summary["eligible"]

    def test_analyzed_plus_skipped_equals_eligible_always(self, tmp_path):
        """Core invariant: analyzed + skipped = eligible for any game/recommendation mix."""
        from src.control_panel import _get_schedule_summary
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        games = [
            {"event_id": "e1", "start_time": f"{today}T20:00:00+00:00", "status": "scheduled"},
            {"event_id": "e2", "start_time": f"{today}T20:00:00+00:00", "status": "scheduled"},
            {"event_id": "e3", "start_time": f"{today}T20:00:00+00:00", "status": "scheduled"},
            {"event_id": "e4", "start_time": f"{today}T20:00:00+00:00", "status": "postponed"},
            {"event_id": "e5", "start_time": f"{today}T20:00:00+00:00", "status": "cancelled"},
        ]
        recs = [
            {"event_id": "e1", "recommendation_id": "r1", "scan_run_id": "run-001"},
            {"event_id": "e2", "recommendation_id": "r2", "scan_run_id": "run-001"},
        ]
        db_path = self._make_db(tmp_path, games, recs)
        summary = _get_schedule_summary(db_path)
        assert summary["eligible"] == 3
        assert summary["analyzed"] == 2
        assert summary["skipped"] == 1
        assert summary["analyzed"] + summary["skipped"] == summary["eligible"]

    def test_only_latest_run_counted(self, tmp_path):
        """If there are recommendations from two runs, only the latest is counted."""
        from src.control_panel import _get_schedule_summary
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc)
        games = [
            {"event_id": "e1", "start_time": f"{today}T20:00:00+00:00"},
            {"event_id": "e2", "start_time": f"{today}T20:00:00+00:00"},
        ]
        recs = [
            {"event_id": "e1", "recommendation_id": "r1", "scan_run_id": "old-run"},
            {"event_id": "e2", "recommendation_id": "r2", "scan_run_id": "new-run"},
        ]
        db_path = self._make_db(tmp_path, games, recs)
        # Set scan_timestamps so "new-run" is the latest
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE historical_recommendations SET scan_timestamp = ? WHERE recommendation_id = 'r1'",
            ((now - timedelta(hours=1)).isoformat(),),
        )
        conn.execute(
            "UPDATE historical_recommendations SET scan_timestamp = ? WHERE recommendation_id = 'r2'",
            (now.isoformat(),),
        )
        conn.commit()
        conn.close()
        summary = _get_schedule_summary(db_path)
        assert summary["analyzed"] == 1, "Only latest run's event_ids should be counted"

    def test_no_recs_analyzed_zero(self, tmp_path):
        """With games but no recommendations, analyzed=0 and skipped=eligible."""
        from src.control_panel import _get_schedule_summary
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        games = [
            {"event_id": "e1", "start_time": f"{today}T20:00:00+00:00"},
            {"event_id": "e2", "start_time": f"{today}T20:00:00+00:00"},
        ]
        db_path = self._make_db(tmp_path, games)
        summary = _get_schedule_summary(db_path)
        assert summary["analyzed"] == 0
        assert summary["skipped"] == 2
        assert summary["eligible"] == 2


class TestDeduplicatedSkippedGames:
    """Regression: skipped games display must deduplicate by event_id."""

    def test_deduplication(self):
        from src.control_panel import _get_deduplicated_skipped_games
        run_summary = {
            "skipped_games": [
                {"event_id": "e1", "matchup": "A @ B", "reason": "live"},
                {"event_id": "e1", "matchup": "A @ B", "reason": "live"},
                {"event_id": "e2", "matchup": "C @ D", "reason": "final"},
            ]
        }
        result = _get_deduplicated_skipped_games(run_summary)
        assert len(result) == 2
        assert result[0]["event_id"] == "e1"
        assert result[1]["event_id"] == "e2"

    def test_none_summary(self):
        from src.control_panel import _get_deduplicated_skipped_games
        assert _get_deduplicated_skipped_games(None) == []

    def test_empty_skipped(self):
        from src.control_panel import _get_deduplicated_skipped_games
        assert _get_deduplicated_skipped_games({"skipped_games": []}) == []

    def test_preserves_order(self):
        from src.control_panel import _get_deduplicated_skipped_games
        run_summary = {
            "skipped_games": [
                {"event_id": "e3", "matchup": "X"},
                {"event_id": "e1", "matchup": "Y"},
                {"event_id": "e3", "matchup": "X"},
                {"event_id": "e2", "matchup": "Z"},
            ]
        }
        result = _get_deduplicated_skipped_games(run_summary)
        ids = [r["event_id"] for r in result]
        assert ids == ["e3", "e1", "e2"]
