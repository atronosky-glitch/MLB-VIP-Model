"""Shared fixtures for all test modules.

Provides isolated database connections and synthetic API data
(deterministic, never cache-dependent).
"""

import os
import sqlite3
from pathlib import Path

import pytest

# Production imports fail fast without an API key.  Tests use a non-secret
# placeholder so collection is independent of module order and local .env files.
os.environ.setdefault("SPORTSODDS_API_KEY", "test_api_key_1234567890")

from tests.fixture_data import (
    TB_TOR_EVENT_ID,
    SF_KC_EVENT_ID,
    tb_tor_event as _tb_tor_event,
    sf_kc_event as _sf_kc_event,
    flaherty_event as _flaherty_event,
    all_synthetic_events,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ── Synthetic API data ────────────────────────────────────────────


@pytest.fixture(scope="session")
def api_data() -> dict:
    return {"data": all_synthetic_events}


@pytest.fixture(scope="session")
def all_events(api_data) -> list[dict]:
    return api_data.get("data", api_data.get("events", []))


@pytest.fixture
def tb_tor_event():
    return dict(_tb_tor_event)


@pytest.fixture
def sf_kc_event():
    return dict(_sf_kc_event)


@pytest.fixture
def flaherty_event():
    return dict(_flaherty_event)


# ── Isolated database ─────────────────────────────────────────────


@pytest.fixture
def db_conn():
    """Create an in-memory database with all tables via init_db.

    Uses the full init_db() pipeline for realism, but on an
    isolated in-memory database.
    """
    # Patch DB_PATH temporarily to use :memory: — init_db uses it
    import database.db_manager as dbm

    _orig_path = dbm.DB_PATH
    dbm.DB_PATH = Path(":memory:")

    # Monkey-patch get_connection to return our :memory: connection
    _orig_get_conn = dbm.get_connection

    def _memory_conn():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    dbm.get_connection = _memory_conn
    dbm.init_db()

    conn = _memory_conn()  # fresh connection to the same :memory: db
    # Re-create tables in this specific connection via init_db's logic
    # Actually init_db above already created tables in a separate conn.
    # Let's just manually run schema here to be safe.
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            event_id        TEXT PRIMARY KEY,
            league          TEXT NOT NULL DEFAULT 'MLB',
            away_team       TEXT,
            home_team       TEXT,
            start_time      TEXT,
            status          TEXT NOT NULL DEFAULT 'scheduled',
            sport_id        TEXT,
            league_id       TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL REFERENCES games(event_id),
            sportsbook      TEXT NOT NULL,
            market          TEXT NOT NULL,
            selection       TEXT,
            price           REAL,
            points          REAL,
            is_alt_line     INTEGER NOT NULL DEFAULT 0,
            available       INTEGER NOT NULL DEFAULT 1,
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now')),
            odd_id          TEXT DEFAULT '',
            validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT 'NONE',
            mapping_method   TEXT DEFAULT '',
            validation_reason TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS raw_responses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint        TEXT NOT NULL,
            params          TEXT,
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now')),
            response_json   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS data_pulls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL,
            pull_type       TEXT NOT NULL,
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bet_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL,
            sportsbook      TEXT NOT NULL,
            market          TEXT NOT NULL,
            selection       TEXT NOT NULL,
            price           REAL,
            outcome         TEXT,
            units           REAL,
            profit          REAL,
            graded_at       TEXT
        );
        CREATE TABLE IF NOT EXISTS odds_mapping_audit (
            audit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL,
            odd_id          TEXT NOT NULL,
            sportsbook      TEXT NOT NULL,
            raw_participant_id  TEXT,
            raw_participant_name TEXT,
            matched_team_id     TEXT,
            matched_team_name   TEXT,
            mapping_method      TEXT,
            mapping_confidence  TEXT,
            validation_status   TEXT,
            validation_reason   TEXT,
            price               REAL,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS player_prop_odds (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            TEXT NOT NULL,
            odd_id              TEXT NOT NULL,
            sportsbook          TEXT NOT NULL,
            player_id           TEXT NOT NULL,
            player_name         TEXT,
            team_id             TEXT DEFAULT '',
            team_name           TEXT DEFAULT '',
            market_type         TEXT NOT NULL,
            market_group_key    TEXT NOT NULL,
            side                TEXT NOT NULL,
            line                REAL,
            price               INTEGER,
            decimal_odds        REAL,
            is_alt_line         INTEGER NOT NULL DEFAULT 0,
            available           INTEGER NOT NULL DEFAULT 1,
            validation_status   TEXT NOT NULL DEFAULT 'VALID',
            mapping_confidence  TEXT DEFAULT '',
            mapping_method      TEXT DEFAULT '',
            validation_reason   TEXT DEFAULT '',
            captured_at         TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            league              TEXT DEFAULT 'MLB'
        );
        CREATE TABLE IF NOT EXISTS player_prop_mapping_audit (
            audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            TEXT NOT NULL,
            odd_id              TEXT NOT NULL,
            sportsbook          TEXT NOT NULL,
            player_id           TEXT,
            player_name         TEXT,
            team_id             TEXT,
            team_name           TEXT,
            market_type         TEXT,
            market_group_key    TEXT,
            side                TEXT,
            line                REAL,
            price               INTEGER,
            decimal_odds        REAL,
            is_alt_line         INTEGER DEFAULT 0,
            available           INTEGER DEFAULT 1,
            validation_status   TEXT,
            mapping_confidence  TEXT,
            mapping_method      TEXT,
            validation_reason   TEXT,
            excluded            INTEGER DEFAULT 0,
            exclusion_reasons   TEXT,
            captured_at         TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id     TEXT PRIMARY KEY,
            fingerprint           TEXT NOT NULL DEFAULT '',
            scan_run_id           TEXT,
            ingestion_run_id      TEXT,
            event_id              TEXT NOT NULL,
            event_start_time      TEXT,
            player_id             TEXT NOT NULL,
            player_name           TEXT,
            market_type           TEXT NOT NULL,
            market_form           TEXT NOT NULL DEFAULT 'ou',
            period                TEXT NOT NULL DEFAULT 'game',
            line                  REAL,
            side                  TEXT NOT NULL,
            sportsbook            TEXT NOT NULL,
            offered_american_odds INTEGER NOT NULL DEFAULT 0,
            offered_decimal_odds  REAL NOT NULL DEFAULT 0,
            offered_implied_prob  REAL NOT NULL DEFAULT 0,
            fair_prob             REAL,
            fair_american_odds    INTEGER,
            ev_pct                REAL,
            yn_reference_prob     REAL,
            yn_reference_odds     INTEGER,
            yn_implied_prob_adv   REAL,
            yn_decimal_odds_adv   INTEGER,
            n_consensus_books     INTEGER,
            market_quality        TEXT,
            rec_status            TEXT NOT NULL DEFAULT 'OPPORTUNITY',
            rec_eligible          INTEGER NOT NULL DEFAULT 0,
            data_source           TEXT,
            observation_timestamp TEXT,
            scan_timestamp        TEXT NOT NULL DEFAULT '',
            freshness_status      TEXT DEFAULT '',
            model_version         TEXT DEFAULT 'v1',
            matchup               TEXT DEFAULT '',
            event_status          TEXT DEFAULT '',
            model_score           REAL,
            score_version         TEXT DEFAULT 'model_score_v1',
            score_components      TEXT,
            score_cap             REAL,
            score_explanation     TEXT,
            recommendation_tier   TEXT DEFAULT 'RESEARCH_ONLY',
            qualification_passed  INTEGER DEFAULT 0,
            qualification_reasons TEXT DEFAULT '',
            disqualification_reasons TEXT DEFAULT '',
            contributing_book_count INTEGER DEFAULT 0,
            contributing_books    TEXT DEFAULT '',
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
            league                TEXT DEFAULT 'MLB',
            sport                 TEXT DEFAULT 'baseball',
            raw_line              REAL,
            confidence_score      REAL,
            confidence_grade      TEXT,
            created_at            TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_fingerprint ON historical_recommendations(fingerprint);
        CREATE TABLE IF NOT EXISTS closing_prices (
            recommendation_id     TEXT PRIMARY KEY,
            closing_american_odds INTEGER,
            closing_decimal_odds  REAL,
            closing_implied_prob  REAL,
            closing_fair_prob     REAL,
            closing_ev_pct        REAL,
            captured_at           TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (recommendation_id) REFERENCES historical_recommendations(recommendation_id)
        );
        CREATE TABLE IF NOT EXISTS recommendation_lifecycle_events (
            lifecycle_event_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            run_id TEXT, event_id TEXT, player_id TEXT, player_name TEXT,
            market_type TEXT, side TEXT, line REAL, sportsbook TEXT,
            offered_american_odds INTEGER, offered_decimal_odds REAL,
            implied_probability REAL, model_fair_probability REAL,
            model_edge REAL, ev REAL, confidence_score REAL, quality_score REAL,
            pinnacle_reference_used INTEGER, pinnacle_book TEXT, pinnacle_line REAL,
            pinnacle_over_odds INTEGER, pinnacle_under_odds INTEGER,
            pinnacle_fair_probability REAL, pinnacle_ev REAL,
            pinnacle_probability_edge REAL, snapshot_kind TEXT,
            closing_sportsbook TEXT, closing_line REAL,
            closing_american_odds INTEGER, closing_decimal_odds REAL,
            closing_implied_probability REAL, line_move_type TEXT,
            closing_available INTEGER, clv_probability REAL,
            clv_price_diff INTEGER, clv_available INTEGER,
            result TEXT, final_stat_value REAL,
            settlement_reason TEXT, grader_version TEXT, event_timestamp TEXT NOT NULL,
            data_source TEXT, source_run_id TEXT, provenance_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS market_settlements (
            settlement_id       TEXT PRIMARY KEY,
            recommendation_id   TEXT NOT NULL,
            settlement_status   TEXT NOT NULL DEFAULT 'UNRESOLVED',
            final_stat_value    REAL,
            settled_at          TEXT,
            settlement_reason   TEXT,
            grader_version      TEXT DEFAULT 'v1',
            manual_override     INTEGER NOT NULL DEFAULT 0,
            override_reason     TEXT,
            override_previous   TEXT,
            league              TEXT DEFAULT 'MLB',
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_rec ON market_settlements(recommendation_id);
        CREATE TABLE IF NOT EXISTS event_results (
            event_id TEXT PRIMARY KEY, final_status TEXT DEFAULT 'UNRESOLVED',
            away_score INTEGER, home_score INTEGER, result_source TEXT,
            source_observed_at TEXT, result_detail TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS official_picks (
            recommendation_id TEXT PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'OFFICIAL_TRACKED',
            selected_at TEXT NOT NULL DEFAULT (datetime('now')),
            official_rank INTEGER,
            rules_version TEXT DEFAULT 'official_pick_rules_v1',
            outcome TEXT NOT NULL DEFAULT 'pending',
            graded_at TEXT,
            profit_units REAL,
            risk_units REAL,
            final_stat_value REAL,
            grader_version TEXT,
            league TEXT DEFAULT 'MLB',
            sport TEXT DEFAULT 'baseball',
            pick_status TEXT NOT NULL DEFAULT 'ACTIVE',
            bet_slot_key TEXT,
            superseded_by TEXT,
            superseded_at TEXT,
            first_recommended_at TEXT,
            best_american_odds INTEGER,
            best_odds_at TEXT,
            latest_american_odds INTEGER,
            latest_odds_at TEXT,
            update_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pick_observations (
            observation_id TEXT PRIMARY KEY,
            official_pick_id TEXT NOT NULL,
            observation_type TEXT NOT NULL,
            sportsbook TEXT,
            american_odds INTEGER,
            decimal_odds REAL,
            implied_prob REAL,
            line REAL,
            consensus_prob REAL,
            avg_other_prob REAL,
            median_other_prob REAL,
            unique_book_count INTEGER,
            freshness_status TEXT,
            source_run_id TEXT,
            market_status TEXT,
            observed_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (official_pick_id) REFERENCES official_picks(recommendation_id)
        );
        CREATE TABLE IF NOT EXISTS player_stat_results (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            TEXT NOT NULL,
            player_id           TEXT NOT NULL,
            player_name         TEXT,
            market_type         TEXT NOT NULL,
            final_stat_value    REAL,
            result_source       TEXT,
            source_observed_at  TEXT,
            result_status       TEXT NOT NULL DEFAULT 'UNRESOLVED',
            result_detail       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(event_id, player_id, market_type)
        );
        CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
            opportunity_id TEXT PRIMARY KEY,
            league TEXT NOT NULL DEFAULT 'MLB',
            sport TEXT DEFAULT 'baseball',
            event_id TEXT,
            matchup TEXT,
            event_start_time TEXT,
            player_id TEXT,
            player_name TEXT,
            market_type TEXT,
            line REAL,
            side_a TEXT,
            side_a_sportsbook TEXT,
            side_a_price INTEGER,
            side_a_decimal_odds REAL,
            side_a_stake_pct REAL,
            side_b TEXT,
            side_b_sportsbook TEXT,
            side_b_price INTEGER,
            side_b_decimal_odds REAL,
            side_b_stake_pct REAL,
            guaranteed_roi_pct REAL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            outcome TEXT,
            profit_units REAL,
            graded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS middle_opportunities (
            opportunity_id TEXT PRIMARY KEY,
            league TEXT NOT NULL DEFAULT 'MLB',
            sport TEXT DEFAULT 'baseball',
            event_id TEXT,
            matchup TEXT,
            event_start_time TEXT,
            player_id TEXT,
            player_name TEXT,
            market_type TEXT,
            over_line REAL,
            over_sportsbook TEXT,
            over_price INTEGER,
            over_decimal_odds REAL,
            over_stake_pct REAL,
            under_line REAL,
            under_sportsbook TEXT,
            under_price INTEGER,
            under_decimal_odds REAL,
            under_stake_pct REAL,
            window_width REAL,
            worst_case_roi_pct REAL,
            best_case_roi_pct REAL,
            detected_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            outcome TEXT,
            profit_units REAL,
            graded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS discord_alerts_sent (
            alert_key  TEXT NOT NULL,
            alert_type TEXT NOT NULL DEFAULT 'ev_pick',
            sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (alert_type, alert_key)
        );
        CREATE TABLE IF NOT EXISTS market_matches (
            match_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id   TEXT NOT NULL,
            provider            TEXT NOT NULL,
            provider_market_id  TEXT NOT NULL,
            league              TEXT NOT NULL,
            market_type         TEXT NOT NULL,
            confidence          REAL NOT NULL,
            match_status        TEXT NOT NULL,
            team_score          REAL,
            market_type_score   REAL,
            line_score          REAL,
            date_score          REAL,
            matcher_version     TEXT NOT NULL,
            provider_title      TEXT,
            matched_at          TEXT NOT NULL DEFAULT (datetime('now')),
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_matches_rec_provider
            ON market_matches(recommendation_id, provider, matcher_version);
        CREATE INDEX IF NOT EXISTS idx_market_matches_rec ON market_matches(recommendation_id);
        CREATE INDEX IF NOT EXISTS idx_market_matches_provider_market
            ON market_matches(provider, provider_market_id);
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            scheduled_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            event_id TEXT,
            error_message TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY,
            challenger_id TEXT NOT NULL,
            champion_config TEXT,
            challenger_config TEXT,
            created_at TEXT NOT NULL,
            training_window TEXT,
            validation_window TEXT,
            holdout_window TEXT,
            champion_metrics TEXT,
            challenger_metrics TEXT,
            conclusion TEXT DEFAULT 'pending',
            approved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS config_versions (
            version_id TEXT PRIMARY KEY,
            scoring_version TEXT NOT NULL,
            market_quality_version TEXT NOT NULL,
            qualification_rules_version TEXT NOT NULL,
            calibration_version TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            deactivated_at TEXT DEFAULT '',
            reason TEXT DEFAULT '',
            experiment_id TEXT DEFAULT '',
            approver TEXT DEFAULT '',
            rollback_target TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS learning_recommendations (
            recommendation_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            current_value TEXT,
            proposed_value TEXT,
            reason TEXT,
            sample_size INTEGER DEFAULT 0,
            historical_roi_diff REAL DEFAULT 0.0,
            historical_clv_diff REAL DEFAULT 0.0,
            confidence_low REAL,
            confidence_high REAL,
            expected_volume TEXT,
            overfitting_risk TEXT DEFAULT 'UNKNOWN',
            status TEXT DEFAULT 'INSUFFICIENT_DATA',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS odds_api_credits (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at         TEXT NOT NULL DEFAULT (datetime('now')),
            endpoint            TEXT NOT NULL,
            job_type            TEXT NOT NULL DEFAULT '',
            requests_used       INTEGER,
            requests_remaining  INTEGER,
            requests_last       INTEGER,
            cache_hit           INTEGER NOT NULL DEFAULT 0
        );
    """)

    # Restore globals
    dbm.DB_PATH = _orig_path
    dbm.get_connection = _orig_get_conn

    yield conn
    conn.close()


# ── Postgres-shaped rows (catches the r[0] bug class) ──────────────
#
# Production uses psycopg2.extras.RealDictCursor (see
# database/connection.py), whose rows are RealDictRow -- a dict
# subclass with NO positional int access. sqlite3.Row (what db_conn
# above uses) supports both r["col"] and r[0], so code that
# accidentally does r[0] instead of r["col"] passes every test here
# but throws KeyError/TypeError against real production Postgres.
# This silently broke sync_arbitrage_opportunities/
# sync_middle_opportunities in production for two days (2026-09-10 to
# 2026-09-12) before being caught -- see database/db_manager.py's
# history. Use db_conn_dict_rows instead of db_conn for any new
# function whose SQL results get indexed, to catch this bug class
# before it ships.


class _NoPositionalRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            raise TypeError(
                "tuple indices must be strings, not int -- RealDictRow "
                "(production's actual row type) has no positional access"
            )
        return super().__getitem__(key)


class _DictRowCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def fetchall(self):
        return [_NoPositionalRow(dict(r)) for r in self._cursor.fetchall()]

    def fetchone(self):
        r = self._cursor.fetchone()
        return _NoPositionalRow(dict(r)) if r is not None else None


class _DictRowConn:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *args, **kwargs):
        return _DictRowCursor(self._conn.execute(*args, **kwargs))


@pytest.fixture
def db_conn_dict_rows(db_conn):
    """Same schema as db_conn, but every query result behaves like
    production's RealDictCursor rows -- dict-only, no r[0]. See the
    module comment above for why this fixture exists."""
    return _DictRowConn(db_conn)
