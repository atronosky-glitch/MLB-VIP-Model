"""Database manager.

Centralises all database operations so the rest of the codebase never
touches raw SQL outside this module.

Supports both SQLite (local/tests) and PostgreSQL (production) via
the DATABASE_URL environment variable.
"""

import hashlib
import json
import os
import sqlite3
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from database.connection import DB, get_connection as _get_db_connection, get_database_url

load_dotenv()

logger = logging.getLogger(__name__)

_DB_PATH_ENV = os.environ.get("MLB_DB_PATH", "")
if _DB_PATH_ENV:
    DB_PATH = Path(_DB_PATH_ENV)
else:
    DB_PATH = Path(__file__).resolve().parent / "mlb_model.db"

REQUIRED_SCHEMA_TABLES = frozenset({
    "recommendation_lifecycle_events",
    "scan_runs",
    "games",
    "odds",
    "historical_recommendations",
    "closing_prices",
    "market_settlements",
})


def schema_diagnostic(conn: DB) -> dict:
    """Return safe dialect/database/schema/table diagnostics."""
    dialect = getattr(conn, "dialect", "sqlite")
    if dialect == "postgresql":
        identity = conn.execute(
            "SELECT current_database() AS database_name, current_schema() AS schema_name"
        ).fetchone() or {}
        rows = conn.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchall()
        database_name = identity.get("database_name") if isinstance(identity, dict) else None
        schema_name = identity.get("schema_name") if isinstance(identity, dict) else None
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        database_name = str(DB_PATH)
        schema_name = "main"
    present = sorted({row["name"] if isinstance(row, dict) else row[0] for row in rows})
    return {
        "dialect": dialect,
        "database_name": database_name,
        "schema_name": schema_name,
        "required_tables": sorted(REQUIRED_SCHEMA_TABLES),
        "present_tables": present,
        "missing_tables": sorted(REQUIRED_SCHEMA_TABLES - set(present)),
    }


def verify_required_schema(conn: DB) -> dict:
    """Verify all required tables exist, raising a safe actionable error."""
    diagnostic = schema_diagnostic(conn)
    missing = diagnostic["missing_tables"]
    if missing:
        raise RuntimeError(
            "Required database schema tables are missing: "
            f"{', '.join(missing)} "
            f"(dialect={diagnostic['dialect']}, "
            f"database={diagnostic['database_name']}, "
            f"schema={diagnostic['schema_name']})"
        )
    return diagnostic


def lifecycle_table_diagnostic(conn: DB) -> dict:
    """Inspect lifecycle table catalogs and transaction state safely."""
    dialect = getattr(conn, "dialect", "sqlite")
    if dialect == "postgresql":
        regclass = conn.execute(
            "SELECT to_regclass('public.recommendation_lifecycle_events') AS table_name"
        ).fetchone()
        info = conn.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'recommendation_lifecycle_events'"
        ).fetchone()
        table_name = regclass["table_name"] if regclass else None
        info_present = info is not None
        raw = getattr(conn, "_conn", None)
        transaction_status = getattr(raw, "status", None)
    else:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'recommendation_lifecycle_events'"
        ).fetchone()
        table_name = row["name"] if isinstance(row, dict) else row[0] if row else None
        info_present = row is not None
        transaction_status = "sqlite"
    return {
        "dialect": dialect,
        "to_regclass": table_name,
        "information_schema_present": info_present,
        "transaction_status": transaction_status,
        "present": table_name == "recommendation_lifecycle_events" and info_present,
    }


def create_player_identity_tables(conn: DB) -> None:
    """Create the canonical player-identity tables (see src/player_identity.py).

    A prop odds source that gives only a free-text player name (no stable
    ID) is resolved once against a real roster, then cached here so
    subsequent scans don't re-fetch rosters. Confidence is tracked
    explicitly — only HIGH/MEDIUM mappings should feed a recommendation.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_identities (
            canonical_player_id TEXT PRIMARY KEY,
            league               TEXT NOT NULL,
            display_name         TEXT NOT NULL,
            normalized_name       TEXT NOT NULL,
            team_id               TEXT,
            team_name             TEXT,
            created_at            TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pi_league_norm "
        "ON player_identities(league, normalized_name)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_identity_mappings (
            mapping_id           TEXT PRIMARY KEY,
            canonical_player_id  TEXT,
            league                TEXT NOT NULL,
            provider              TEXT NOT NULL,
            provider_player_id    TEXT,
            provider_name_raw     TEXT NOT NULL,
            mapping_confidence    TEXT NOT NULL,
            mapping_method        TEXT NOT NULL,
            verified_at           TEXT,
            created_at            TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_pim_provider_raw "
        "ON player_identity_mappings(league, provider, provider_name_raw)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pim_canonical "
        "ON player_identity_mappings(canonical_player_id)"
    )


def get_cached_player_identity_mapping(
    conn: DB, league: str, provider: str, provider_name_raw: str,
) -> dict | None:
    """Return a previously resolved identity mapping, or None if uncached."""
    row = conn.execute(
        "SELECT * FROM player_identity_mappings "
        "WHERE league = ? AND provider = ? AND provider_name_raw = ?",
        (league, provider, provider_name_raw),
    ).fetchone()
    return dict(row) if row else None


def save_player_identity_mapping(
    conn: DB,
    *,
    league: str,
    provider: str,
    provider_name_raw: str,
    canonical_player_id: str | None,
    display_name: str,
    mapping_confidence: str,
    mapping_method: str,
    team_id: str = "",
    team_name: str = "",
) -> None:
    """Persist a resolved (or explicitly unresolved) identity mapping.

    Idempotent per (league, provider, provider_name_raw): a repeated
    resolution updates the existing row rather than duplicating it.
    ``canonical_player_id`` is None for an UNRESOLVED mapping — cached so
    repeated scans don't keep re-attempting a roster fetch for a name
    that's genuinely unmatched, while still never becoming a recommendation.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        if canonical_player_id:
            conn.execute(
                """INSERT INTO player_identities
                   (canonical_player_id, league, display_name, normalized_name, team_id, team_name)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (canonical_player_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       team_id = excluded.team_id,
                       team_name = excluded.team_name,
                       updated_at = datetime('now')""",
                (canonical_player_id, league, display_name,
                 _normalize_for_storage(display_name), team_id, team_name),
            )
        mapping_id = f"{league}:{provider}:{provider_name_raw}"
        conn.execute(
            """INSERT INTO player_identity_mappings
               (mapping_id, canonical_player_id, league, provider, provider_name_raw,
                mapping_confidence, mapping_method, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (league, provider, provider_name_raw) DO UPDATE SET
                   canonical_player_id = excluded.canonical_player_id,
                   mapping_confidence = excluded.mapping_confidence,
                   mapping_method = excluded.mapping_method,
                   verified_at = excluded.verified_at,
                   updated_at = datetime('now')""",
            (mapping_id, canonical_player_id, league, provider, provider_name_raw,
             mapping_confidence, mapping_method, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _normalize_for_storage(display_name: str) -> str:
    from src.player_identity import normalize_name
    return normalize_name(display_name)


def create_recommendation_lifecycle_table(conn: DB) -> None:
    """Create the Phase 19A lifecycle table and indexes exactly once per init."""
    logger.info("LIFECYCLE DDL START table=recommendation_lifecycle_events")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recommendation_lifecycle_events (
            lifecycle_event_id       TEXT PRIMARY KEY,
            recommendation_id        TEXT NOT NULL,
            event_type               TEXT NOT NULL,
            event_key                TEXT NOT NULL UNIQUE,
            run_id                   TEXT,
            event_id                 TEXT,
            player_id                TEXT,
            player_name              TEXT,
            market_type              TEXT,
            side                     TEXT,
            line                     REAL,
            sportsbook               TEXT,
            offered_american_odds    INTEGER,
            offered_decimal_odds     REAL,
            implied_probability      REAL,
            model_fair_probability   REAL,
            model_edge               REAL,
            ev                       REAL,
            confidence_score         REAL,
            quality_score            REAL,
            pinnacle_reference_used  INTEGER,
            pinnacle_book            TEXT,
            pinnacle_line            REAL,
            pinnacle_over_odds       INTEGER,
            pinnacle_under_odds      INTEGER,
            pinnacle_fair_probability REAL,
            pinnacle_ev              REAL,
            pinnacle_probability_edge REAL,
            snapshot_kind            TEXT,
            closing_sportsbook       TEXT,
            closing_line             REAL,
            closing_american_odds    INTEGER,
            closing_decimal_odds     REAL,
            closing_implied_probability REAL,
            line_move_type           TEXT,
            closing_available        INTEGER,
            clv_probability          REAL,
            clv_price_diff           INTEGER,
            clv_available            INTEGER,
            result                   TEXT,
            final_stat_value         REAL,
            settlement_reason        TEXT,
            grader_version           TEXT,
            event_timestamp          TEXT NOT NULL,
            data_source              TEXT,
            source_run_id            TEXT,
            provenance_json          TEXT,
            created_at               TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rle_recommendation "
        "ON recommendation_lifecycle_events(recommendation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rle_event_type "
        "ON recommendation_lifecycle_events(event_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rle_run "
        "ON recommendation_lifecycle_events(run_id)"
    )
    state = lifecycle_table_diagnostic(conn)
    logger.info(
        "LIFECYCLE DDL EXECUTED table=recommendation_lifecycle_events "
        "to_regclass=%s information_schema=%s transaction_status=%s",
        state["to_regclass"], state["information_schema_present"],
        state["transaction_status"],
    )
    if not state["present"]:
        raise RuntimeError(
            "LIFECYCLE DDL EXECUTED but catalog verification failed: "
            f"to_regclass={state['to_regclass']} "
            f"information_schema={state['information_schema_present']}"
        )

# Column names added by schema migrations — for safe ALTER TABLE.
_ODDS_MIGRATIONS = [
    ("odd_id", "TEXT DEFAULT ''"),
    ("validation_status", "TEXT DEFAULT 'VALID'"),
    ("mapping_confidence", "TEXT DEFAULT 'NONE'"),
    ("mapping_method", "TEXT DEFAULT ''"),
    ("validation_reason", "TEXT DEFAULT ''"),
    ("league", "TEXT DEFAULT 'MLB'"),
]


def get_connection(db_path: str | None = None) -> DB:
    """Return a database connection.

    If DATABASE_URL is set, connects to PostgreSQL.
    Otherwise, connects to the local SQLite database.

    Parameters
    ----------
    db_path : str or None
        Explicit path to an SQLite database file.  Only used when
        ``DATABASE_URL`` is not set.  Falls back to ``DB_PATH`` if None.
    """
    db_url = get_database_url()
    if db_url:
        return _get_db_connection(url=db_url)
    path = db_path or str(DB_PATH)
    return _get_db_connection(db_path=path)


def _get_raw_sqlite_connection() -> sqlite3.Connection:
    """Return a raw sqlite3 connection (for SQLite-only operations like backup)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _safe_migrate_odds(conn) -> None:
    """Add columns to ``odds`` table if they don't exist yet.

    Safe to run against both new and existing databases.
    Never drops or modifies existing data.
    """
    _add_columns_if_missing(conn, "odds", _ODDS_MIGRATIONS)


def _create_audit_table(conn: DB) -> None:
    """Create ``odds_mapping_audit`` table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_mapping_audit (
            audit_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id            TEXT NOT NULL,
            odd_id              TEXT NOT NULL,
            sportsbook          TEXT NOT NULL,
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
        )
    """)


# Column names added by schema migrations — for safe ALTER TABLE on player_prop_odds.
_PLAYER_PROP_MIGRATIONS = [
    ("team_id", "TEXT DEFAULT ''"),
    ("team_name", "TEXT DEFAULT ''"),
    ("league", "TEXT DEFAULT 'MLB'"),
]


def _existing_columns(conn: DB, table_name: str) -> set[str]:
    """Return existing columns without issuing failing ALTER statements."""
    if getattr(conn, "dialect", "sqlite") == "postgresql":
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table_name,),
        ).fetchall()
        return {row["name"] for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] if isinstance(row, dict) else row[1] for row in rows}


def _add_columns_if_missing(conn: DB, table_name: str, migrations: list[tuple[str, str]]) -> None:
    """Apply additive migrations without rolling back prior DDL on PostgreSQL."""
    existing = _existing_columns(conn, table_name)
    for col_name, col_def in migrations:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
            logger.info("Added column '%s' to %s table", col_name, table_name)
            existing.add(col_name)


def _dedupe_running_worker_locks(conn: DB) -> None:
    """Resolve duplicate running worker-locks before the unique index is created.

    ``src/worker.py::_acquire_lock`` historically did a check-then-insert
    with no database-level constraint, so two concurrent callers could each
    observe no existing lock and both insert a 'running' row for the same
    ``job_type``. Creating ``idx_sj_running_lock`` (a partial unique index)
    would fail against a database that already holds such duplicates, so
    this keeps only the most recently started lock per job_type and marks
    the rest completed. A no-op on any database that never raced.
    """
    rows = conn.execute(
        "SELECT job_id, job_type, started_at FROM scheduled_jobs "
        "WHERE status = 'running' AND metadata = 'worker-lock'"
    ).fetchall()
    by_type: dict[str, list] = {}
    for row in rows:
        by_type.setdefault(row["job_type"], []).append(row)

    for job_type, locks in by_type.items():
        if len(locks) <= 1:
            continue
        locks.sort(key=lambda r: r["started_at"] or "", reverse=True)
        for stale in locks[1:]:
            conn.execute(
                "UPDATE scheduled_jobs SET status = 'completed', "
                "error_message = 'Deduplicated duplicate worker-lock during migration' "
                "WHERE job_id = ?",
                (stale["job_id"],),
            )
            logger.warning(
                "Deduplicated stale duplicate worker-lock job_id=%s job_type=%s",
                stale["job_id"], job_type,
            )


def _safe_migrate_player_prop(conn) -> None:
    """Add columns to ``player_prop_odds`` if they don't exist yet."""
    _add_columns_if_missing(conn, "player_prop_odds", _PLAYER_PROP_MIGRATIONS)


# Columns that must exist on official_picks for the dashboard/analytics to run.
# Phase 17C added variable-Kelly staking; older production databases may lack it.
_OFFICIAL_PICKS_MIGRATIONS = [
    ("risk_units", "REAL"),
]


def ensure_official_picks_schema() -> None:
    """Guarantee official_picks has every required column.

    Run at dashboard startup: unlike full ``init_db`` (worker-only), this is
    safe and cheap to call from the web app, and works on both SQLite and
    PostgreSQL (checks column existence before issuing ALTER TABLE).
    """
    conn = get_connection()
    try:
        dialect = getattr(conn, "dialect", "sqlite")
        if dialect == "postgresql":
            cols = conn.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = 'official_picks'"
            ).fetchall()
            existing = {row["name"] for row in cols}
        else:
            cursor = conn.execute("PRAGMA table_info(official_picks)")
            rows = cursor.fetchall()
            if rows and isinstance(rows[0], dict):
                existing = {row["name"] for row in rows}
            else:
                existing = {row[1] for row in rows}

        for col_name, col_def in _OFFICIAL_PICKS_MIGRATIONS:
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE official_picks ADD COLUMN {col_name} {col_def}"
                )
                logger.info("Added column '%s' to official_picks table", col_name)

        # Postgres uses autocommit=False: ALTER TABLE must be committed
        # explicitly or it is rolled back when the connection closes.
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _create_player_prop_audit_table(conn: DB) -> None:
    """Create ``player_prop_mapping_audit`` table if it doesn't exist."""
    conn.execute("""
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
        )
    """)


def init_db(db_path: str | None = None) -> None:
    """Create all tables if they don't exist yet.  Runs schema migrations."""
    conn = get_connection(db_path) if db_path else get_connection()
    conn.executescript("""
        -- Games / events
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

        -- Odds stored at the granularity of (game, sportsbook, market, selection)
        -- Schema columns may be extended by _safe_migrate_odds below.
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
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Raw API responses (for re-processing later)
        CREATE TABLE IF NOT EXISTS raw_responses (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint        TEXT NOT NULL,
            params          TEXT,
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now')),
            response_json   TEXT NOT NULL
        );

        -- Track each data pull cycle
        CREATE TABLE IF NOT EXISTS data_pulls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL REFERENCES games(event_id),
            pull_type       TEXT NOT NULL,
            pulled_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Bet results (graded after games finish)
        CREATE TABLE IF NOT EXISTS bet_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id        TEXT NOT NULL REFERENCES games(event_id),
            sportsbook      TEXT NOT NULL,
            market          TEXT NOT NULL,
            selection       TEXT NOT NULL,
            price           REAL,
            outcome         TEXT,
            units           REAL,
            profit          REAL,
            graded_at       TEXT
        );

        -- Player prop odds (pitcher strikeouts, etc.)
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
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_odds_event ON odds(event_id);
        CREATE INDEX IF NOT EXISTS idx_odds_sportsbook ON odds(sportsbook);
        CREATE INDEX IF NOT EXISTS idx_pulls_event ON data_pulls(event_id);

        CREATE INDEX IF NOT EXISTS idx_pp_event ON player_prop_odds(event_id);
        CREATE INDEX IF NOT EXISTS idx_pp_player ON player_prop_odds(player_id);
        CREATE INDEX IF NOT EXISTS idx_pp_group ON player_prop_odds(market_group_key);
        CREATE INDEX IF NOT EXISTS idx_pp_sportsbook ON player_prop_odds(sportsbook);
        CREATE INDEX IF NOT EXISTS idx_pp_validation ON player_prop_odds(validation_status);
        CREATE INDEX IF NOT EXISTS idx_pp_captured ON player_prop_odds(captured_at);

        -- Run tracking (auditability)
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

        -- Ingestion log (per-event ingestion tracking)
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

        CREATE INDEX IF NOT EXISTS idx_scan_runs_started ON scan_runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_scan_runs_type ON scan_runs(run_type);
        CREATE INDEX IF NOT EXISTS idx_ingestion_log_run ON ingestion_log(run_id);
        CREATE INDEX IF NOT EXISTS idx_ingestion_log_event ON ingestion_log(event_id);

        -- ================================================================
        -- Phase 6: Historical recommendations, results, grading, CLV
        -- ================================================================

        -- Frozen recommendation snapshots (immutable after insert)
        CREATE TABLE IF NOT EXISTS historical_recommendations (
            recommendation_id     TEXT PRIMARY KEY,
            fingerprint           TEXT NOT NULL,
            scan_run_id           TEXT,
            ingestion_run_id      TEXT,
            event_id              TEXT NOT NULL,
            event_start_time      TEXT,
            player_id             TEXT NOT NULL,
            player_name           TEXT,
            market_type           TEXT NOT NULL,
            market_form           TEXT NOT NULL,
            period                TEXT NOT NULL,
            line                  REAL,
            side                  TEXT NOT NULL,
            sportsbook            TEXT NOT NULL,
            offered_american_odds INTEGER NOT NULL,
            offered_decimal_odds  REAL NOT NULL,
            offered_implied_prob  REAL NOT NULL,
            fair_prob             REAL,
            fair_american_odds    INTEGER,
            ev_pct                REAL,
            yn_reference_prob     REAL,
            yn_reference_odds     INTEGER,
            yn_implied_prob_adv   REAL,
            yn_decimal_odds_adv   INTEGER,
            n_consensus_books     INTEGER,
            market_quality        TEXT,
            rec_status            TEXT NOT NULL,
            rec_eligible          INTEGER NOT NULL DEFAULT 0,
            data_source           TEXT,
            observation_timestamp TEXT,
            scan_timestamp        TEXT NOT NULL,
            freshness_status      TEXT,
            model_version         TEXT DEFAULT 'v1',
            created_at            TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_hr_fingerprint ON historical_recommendations(fingerprint);
        CREATE INDEX IF NOT EXISTS idx_hr_event ON historical_recommendations(event_id);
        CREATE INDEX IF NOT EXISTS idx_hr_player ON historical_recommendations(player_id);
        CREATE INDEX IF NOT EXISTS idx_hr_market ON historical_recommendations(market_type);
        CREATE INDEX IF NOT EXISTS idx_hr_rec_status ON historical_recommendations(rec_status);
        CREATE INDEX IF NOT EXISTS idx_hr_scan_run ON historical_recommendations(scan_run_id);

        -- Event results (game outcomes)
        CREATE TABLE IF NOT EXISTS event_results (
            event_id            TEXT PRIMARY KEY,
            final_status        TEXT NOT NULL DEFAULT 'UNRESOLVED',
            away_score          INTEGER,
            home_score          INTEGER,
            result_source       TEXT,
            source_observed_at  TEXT,
            result_detail       TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Player stat results (individual stat lines)
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

        CREATE INDEX IF NOT EXISTS idx_psr_event ON player_stat_results(event_id);
        CREATE INDEX IF NOT EXISTS idx_psr_player ON player_stat_results(player_id);
        CREATE INDEX IF NOT EXISTS idx_psr_market ON player_stat_results(market_type);

        -- Market settlements (graded bets)
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
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_rec ON market_settlements(recommendation_id);
        CREATE INDEX IF NOT EXISTS idx_ms_status ON market_settlements(settlement_status);

        -- Units tracking (per settled bet)
        CREATE TABLE IF NOT EXISTS bet_units (
            settlement_id       TEXT PRIMARY KEY,
            recommendation_id   TEXT NOT NULL,
            risk_units          REAL NOT NULL DEFAULT 1.0,
            profit_units        REAL NOT NULL DEFAULT 0.0,
            return_units        REAL NOT NULL DEFAULT 0.0,
            odds_at_settle      INTEGER,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Closing prices (CLV tracking)
        CREATE TABLE IF NOT EXISTS closing_prices (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id   TEXT NOT NULL,
            closing_american    INTEGER,
            closing_decimal     REAL,
            closing_implied_prob REAL,
            closing_line        REAL,
            closing_observed_at TEXT,
            closing_sportsbook  TEXT,
            line_move_type      TEXT,
            clv_probability     REAL,
            clv_price_diff      INTEGER,
            clv_available       INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_cp_rec ON closing_prices(recommendation_id);

        -- Manual override audit trail
        CREATE TABLE IF NOT EXISTS manual_override_audit (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id   TEXT NOT NULL,
            previous_status     TEXT,
            new_status          TEXT NOT NULL,
            override_reason     TEXT NOT NULL,
            override_by         TEXT DEFAULT 'cli',
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_moa_rec ON manual_override_audit(recommendation_id);
    """)

    create_recommendation_lifecycle_table(conn)
    logger.info("Phase 19A lifecycle table creation phase completed")

    create_player_identity_tables(conn)

    _safe_migrate_odds(conn)
    _create_audit_table(conn)
    _safe_migrate_player_prop(conn)
    _create_player_prop_audit_table(conn)

    # Phase 13: Add matchup, event_status columns
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("matchup", "TEXT"),
        ("event_status", "TEXT"),
    ])

    # Phase 14: Add model score columns
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("model_score", "REAL"),
        ("score_version", "TEXT DEFAULT 'model_score_v1'"),
        ("score_components", "TEXT"),
        ("score_cap", "REAL"),
        ("score_explanation", "TEXT"),
    ])

    # Phase 15: Add official pick qualification columns
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("recommendation_tier", "TEXT DEFAULT 'RESEARCH_ONLY'"),
        ("qualification_passed", "INTEGER DEFAULT 0"),
        ("qualification_reasons", "TEXT DEFAULT ''"),
        ("disqualification_reasons", "TEXT DEFAULT ''"),
        ("contributing_book_count", "INTEGER DEFAULT 0"),
        ("contributing_books", "TEXT DEFAULT ''"),
        ("applicable_edge_metric", "TEXT DEFAULT ''"),
        ("applicable_edge_threshold", "REAL DEFAULT 0.0"),
        ("model_score_threshold", "REAL DEFAULT 8.0"),
        ("qualification_rules_version", "TEXT DEFAULT ''"),
    ])

    # Phase 16: Add qualification_timestamp, official_rank columns
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("qualification_timestamp", "TEXT DEFAULT ''"),
        ("official_rank", "INTEGER"),
    ])

    # Phase 16A: Add score diagnostics columns
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("points_to_7", "REAL DEFAULT 0.0"),
        ("price_outlier_capped", "INTEGER DEFAULT 0"),
        ("true_ev_unavailable", "INTEGER DEFAULT 0"),
        ("one_sided_market", "INTEGER DEFAULT 0"),
        ("insufficient_books_failure", "INTEGER DEFAULT 0"),
        ("market_quality_score", "REAL DEFAULT 0.0"),
    ])

    # Phase 18/19: Persist Pinnacle and official-gate evidence for audits.
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("pinnacle_found", "INTEGER"),
        ("pinnacle_reference_used", "INTEGER"),
        ("pinnacle_approved", "INTEGER"),
        ("pinnacle_book", "TEXT"),
        ("pinnacle_line", "REAL"),
        ("pinnacle_over_price", "INTEGER"),
        ("pinnacle_under_price", "INTEGER"),
        ("pinnacle_fair_prob", "REAL"),
        ("pinnacle_ev", "REAL"),
        ("pinnacle_prob_edge", "REAL"),
        # Provenance: which real feed produced this Pinnacle quote —
        # "odds_api_pinnacle" or "direct_pinnapi" (2026-08-26, added
        # alongside The Odds API becoming a second, primary Pinnacle
        # source). NULL for rows saved before this column existed or
        # where pinnacle_found is False.
        ("pinnacle_source", "TEXT"),
        ("is_official", "INTEGER DEFAULT 0"),
        ("confidence_score", "REAL"),
        ("reliable_ev_checked", "INTEGER DEFAULT 0"),
        ("reliable_ev", "INTEGER"),
        ("reliable_ev_status", "TEXT"),
        ("reliable_ev_reasons", "TEXT"),
        ("reliable_ev_calculated_pct", "REAL"),
        ("reliable_ev_version", "TEXT"),
        ("challenger_expected_strikeouts", "REAL"),
        ("challenger_over_probability", "REAL"),
        ("challenger_under_probability", "REAL"),
        ("challenger_push_probability", "REAL"),
        ("challenger_fair_probability", "REAL"),
        ("challenger_version", "TEXT"),
    ])

    # Multi-league support: every recommendation carries its league and
    # sport explicitly rather than assuming MLB. Default 'MLB'/'baseball'
    # preserves all existing rows' meaning exactly (this project was
    # MLB-only until this migration).
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("league", "TEXT DEFAULT 'MLB'"),
        ("sport", "TEXT DEFAULT 'baseball'"),
    ])

    # Signed line for game-market spread settlement (grade_spread needs the
    # favorite/underdog direction; "line" is intentionally abs-valued for
    # O/U-style pairing/EV analysis — see src/game_settlement.py).
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("raw_line", "REAL"),
    ])

    # Confidence grade (A-F, from src.confidence.compute_confidence) alongside
    # the existing numeric confidence_score column, for display/filtering.
    _add_columns_if_missing(conn, "historical_recommendations", [
        ("confidence_grade", "TEXT"),
    ])

    # Phase 16: Official picks frozen snapshot table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS official_picks (
            recommendation_id TEXT PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'OFFICIAL_TRACKED',
            selected_at TEXT NOT NULL DEFAULT (datetime('now')),
            official_rank INTEGER,
            rules_version TEXT DEFAULT 'official_pick_rules_v1',
            outcome TEXT NOT NULL DEFAULT 'pending',
            graded_at TEXT,
            profit_units REAL,
            final_stat_value REAL,
            grader_version TEXT
        )
    """)

    # Non-destructive customer performance epoch. Historical records remain
    # available to learning/audit; customer-facing recordkeeping starts once.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance_baseline (
            baseline_id INTEGER PRIMARY KEY,
            baseline_at TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'production_baseline'
        )
    """)
    conn.execute(
        """INSERT INTO performance_baseline (baseline_id, baseline_at, reason)
           VALUES (1, datetime('now'), 'production_baseline')
           ON CONFLICT (baseline_id) DO NOTHING"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_op_tier ON official_picks(tier)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_op_outcome ON official_picks(outcome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_op_selected ON official_picks(selected_at)"
    )

    # Phase 17C: Variable staking - add risk_units to official_picks
    _add_columns_if_missing(conn, "official_picks", [("risk_units", "REAL")])

    # Multi-league support: league/sport tags on every remaining table that
    # carries per-event or per-recommendation data, so results, settlement,
    # closing-line, and lifecycle records can be filtered/reported per
    # league. Default 'MLB'/'baseball' preserves existing rows' meaning.
    _add_columns_if_missing(conn, "official_picks", [
        ("league", "TEXT DEFAULT 'MLB'"),
        ("sport", "TEXT DEFAULT 'baseball'"),
    ])
    _add_columns_if_missing(conn, "event_results", [("league", "TEXT DEFAULT 'MLB'")])
    # Line-movement-aware CLV: reported alongside price CLV when the line
    # moved and a same-line price comparison isn't available — see
    # src/grading.py::calculate_clv / classify_line_movement.
    _add_columns_if_missing(conn, "closing_prices", [("line_movement_direction", "TEXT")])

    # Duplicate-pick suppression: a scan re-running every few minutes must
    # not freeze a new official pick every time the price wiggles a cent.
    # bet_slot_key identifies "the same logical bet" (event+player+market+
    # side) across scans; pick_status distinguishes the one ACTIVE row per
    # slot from SUPERSEDED history when a materially different price/line
    # replaces it. See src/official_picks.py::classify_pick_update.
    _add_columns_if_missing(conn, "official_picks", [
        ("pick_status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
        ("bet_slot_key", "TEXT"),
        ("superseded_by", "TEXT"),
        ("superseded_at", "TEXT"),
        ("first_recommended_at", "TEXT"),
        ("best_american_odds", "INTEGER"),
        ("best_odds_at", "TEXT"),
        ("latest_american_odds", "INTEGER"),
        ("latest_odds_at", "TEXT"),
        ("update_count", "INTEGER NOT NULL DEFAULT 0"),
    ])
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_op_slot_status ON official_picks(bet_slot_key, pick_status)"
    )

    # The Odds API (WNBA's provider) returns real credit-remaining headers
    # on every response (x-requests-used/x-requests-remaining/x-requests-
    # last) — see src/odds_api_client.py. Persisting them is how the
    # platform stays credit-aware on a hard monthly-quota free tier
    # instead of guessing usage. See src/odds_api_credits.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_api_credits (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at         TEXT NOT NULL DEFAULT (datetime('now')),
            endpoint            TEXT NOT NULL,
            job_type            TEXT NOT NULL DEFAULT '',
            requests_used       INTEGER,
            requests_remaining  INTEGER,
            requests_last       INTEGER,
            cache_hit           INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_oac_recorded ON odds_api_credits(recorded_at)"
    )
    _add_columns_if_missing(conn, "player_stat_results", [("league", "TEXT DEFAULT 'MLB'")])
    _add_columns_if_missing(conn, "market_settlements", [("league", "TEXT DEFAULT 'MLB'")])
    _add_columns_if_missing(conn, "closing_prices", [("league", "TEXT DEFAULT 'MLB'")])
    _add_columns_if_missing(conn, "recommendation_lifecycle_events", [("league", "TEXT DEFAULT 'MLB'")])

    # Phase 16: Odds observations (append-only)
    conn.execute("""
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
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_po_pick ON pick_observations(official_pick_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_po_type ON pick_observations(observation_type)"
    )

    # Phase 16: Scheduled jobs for automation
    conn.execute("""
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
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sj_type_status ON scheduled_jobs(job_type, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sj_scheduled ON scheduled_jobs(scheduled_at)"
    )
    # A pre-existing database (created before this constraint) could already
    # hold duplicate running worker-locks from the old check-then-insert race;
    # the unique index below would fail to create against such rows.
    _dedupe_running_worker_locks(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sj_running_lock "
        "ON scheduled_jobs(job_type) "
        "WHERE status = 'running' AND metadata = 'worker-lock'"
    )

    # ================================================================
    # Phase 16B: Adaptive Learning and Model Calibration
    # ================================================================

    # Experiments: Champion/Challenger comparisons
    conn.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id     TEXT PRIMARY KEY,
            challenger_id     TEXT NOT NULL,
            champion_config   TEXT,
            challenger_config TEXT,
            created_at        TEXT NOT NULL,
            training_window   TEXT,
            validation_window TEXT,
            holdout_window    TEXT,
            champion_metrics  TEXT,
            challenger_metrics TEXT,
            conclusion        TEXT DEFAULT 'pending',
            approved          INTEGER DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exp_challenger ON experiments(challenger_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exp_conclusion ON experiments(conclusion)"
    )

    # Configuration versions: versioning and rollback
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config_versions (
            version_id                    TEXT PRIMARY KEY,
            scoring_version               TEXT NOT NULL,
            market_quality_version        TEXT NOT NULL,
            qualification_rules_version   TEXT NOT NULL,
            calibration_version           TEXT NOT NULL,
            activated_at                  TEXT NOT NULL,
            deactivated_at                TEXT DEFAULT '',
            reason                        TEXT DEFAULT '',
            experiment_id                 TEXT DEFAULT '',
            approver                      TEXT DEFAULT '',
            rollback_target               TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cv_active ON config_versions(deactivated_at)"
    )

    # Learning recommendations: advisory suggestions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS learning_recommendations (
            recommendation_id   TEXT PRIMARY KEY,
            category            TEXT NOT NULL,
            proposed_change     TEXT NOT NULL,
            current_value       TEXT,
            proposed_value      TEXT,
            reason              TEXT,
            sample_size         INTEGER DEFAULT 0,
            historical_roi_diff REAL DEFAULT 0.0,
            historical_clv_diff REAL DEFAULT 0.0,
            confidence_low      REAL,
            confidence_high     REAL,
            expected_volume     TEXT,
            overfitting_risk    TEXT DEFAULT 'UNKNOWN',
            status              TEXT DEFAULT 'INSUFFICIENT_DATA',
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lr_status ON learning_recommendations(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lr_category ON learning_recommendations(category)"
    )

    try:
        diagnostic = verify_required_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        logger.exception("Database schema initialization failed")
        raise
    conn.close()
    logger.info(
        "Database schema initialized: dialect=%s database=%s schema=%s required_tables=%d",
        diagnostic["dialect"], diagnostic["database_name"],
        diagnostic["schema_name"], len(diagnostic["required_tables"]),
    )
    diagnostic["lifecycle_helper_ran"] = True
    return diagnostic


def save_game(conn: DB, game: dict) -> None:
    """Insert or update a game record."""
    conn.execute("""
        INSERT INTO games (event_id, league, away_team, home_team, start_time, status, sport_id, league_id)
        VALUES (:event_id, :league, :away_team, :home_team, :start_time, :status, :sport_id, :league_id)
        ON CONFLICT(event_id) DO UPDATE SET
            status      = excluded.status,
            updated_at  = datetime('now')
    """, game)
    conn.commit()


def save_raw_response(conn: DB, endpoint: str, params: dict | None, data: dict) -> None:
    """Store the full raw API response for later reprocessing."""
    import json
    conn.execute(
        "INSERT INTO raw_responses (endpoint, params, response_json) VALUES (?, ?, ?)",
        (endpoint, json.dumps(params) if params else None, json.dumps(data, default=str)),
    )
    conn.commit()


def record_pull(conn: DB, event_id: str, pull_type: str) -> None:
    """Record that a data pull happened for a game."""
    conn.execute(
        "INSERT INTO data_pulls (event_id, pull_type) VALUES (?, ?)",
        (event_id, pull_type),
    )
    conn.commit()


_REQUIRED_ODDS_KEYS = frozenset({
    "event_id", "sportsbook", "market", "selection",
    "price", "points", "is_alt_line", "available",
    "odd_id", "validation_status", "mapping_confidence",
    "mapping_method", "validation_reason",
})


def save_odds_batch(
    conn: DB,
    odds_rows: list[dict],
    audit_rows: list[dict] | None = None,
) -> int:
    """Bulk-insert odds rows and mapping-audit rows in a single transaction.

    Parameters
    ----------
    conn : DB
        Open database connection.
    odds_rows : list[dict]
        Normal odds rows (must include all columns including validation fields).
    audit_rows : list[dict] or None
        Audit records for the ``odds_mapping_audit`` table.

    Returns
    -------
    int
        Number of odds rows inserted.

    Raises
    ------
    ValueError
        If any odds row is missing a required key.
    """
    if not odds_rows:
        return 0

    # Validate all rows before touching the DB
    for i, row in enumerate(odds_rows):
        missing = _REQUIRED_ODDS_KEYS - set(row)
        if missing:
            raise ValueError(
                f"Row {i} missing required keys: {sorted(missing)}"
            )

    try:
        conn.execute("BEGIN IMMEDIATE")

        odds_sql = """
            INSERT INTO odds
                (event_id, sportsbook, market, selection, price, points,
                 is_alt_line, available, odd_id, validation_status,
                 mapping_confidence, mapping_method, validation_reason)
            VALUES
                (:event_id, :sportsbook, :market, :selection, :price, :points,
                 :is_alt_line, :available, :odd_id, :validation_status,
                 :mapping_confidence, :mapping_method, :validation_reason)
        """
        conn.executemany(odds_sql, odds_rows)

        if audit_rows:
            audit_sql = """
                INSERT INTO odds_mapping_audit
                    (event_id, odd_id, sportsbook,
                     raw_participant_id, raw_participant_name,
                     matched_team_id, matched_team_name,
                     mapping_method, mapping_confidence,
                     validation_status, validation_reason, price)
                VALUES
                    (:event_id, :odd_id, :sportsbook,
                     :raw_participant_id, :raw_participant_name,
                     :matched_team_id, :matched_team_name,
                     :mapping_method, :mapping_confidence,
                     :validation_status, :validation_reason, :price)
            """
            conn.executemany(audit_sql, audit_rows)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return len(odds_rows)


_PP_REQUIRED_KEYS = frozenset({
    "event_id", "odd_id", "sportsbook", "player_id", "player_name",
    "market_type", "market_group_key", "side", "line", "price",
    "decimal_odds", "is_alt_line", "available", "validation_status",
    "mapping_confidence", "mapping_method", "validation_reason",
    "captured_at",
})


def save_player_prop_batch(
    conn: DB,
    rows: list[dict],
    audit_rows: list[dict] | None = None,
) -> int:
    """Bulk-insert player prop odds rows and audit rows in one transaction.

    Parameters
    ----------
    conn : DB
        Open database connection.
    rows : list[dict]
        Approved odds rows.
    audit_rows : list[dict] or None
        Audit records (including excluded ones).

    Returns
    -------
    int
        Number of rows inserted.

    Raises
    ------
    ValueError
        If any row is missing a required key.
    """
    if not rows:
        return 0

    for i, row in enumerate(rows):
        missing = _PP_REQUIRED_KEYS - set(row)
        if missing:
            raise ValueError(f"Player prop row {i} missing keys: {sorted(missing)}")

    try:
        conn.execute("BEGIN IMMEDIATE")

        sql = """
            INSERT INTO player_prop_odds
                (event_id, odd_id, sportsbook, player_id, player_name,
                 team_id, team_name, market_type, market_group_key,
                 side, line, price, decimal_odds, is_alt_line, available,
                 validation_status, mapping_confidence, mapping_method,
                 validation_reason, captured_at)
            VALUES
                (:event_id, :odd_id, :sportsbook, :player_id, :player_name,
                 :team_id, :team_name, :market_type, :market_group_key,
                 :side, :line, :price, :decimal_odds, :is_alt_line, :available,
                 :validation_status, :mapping_confidence, :mapping_method,
                 :validation_reason, :captured_at)
        """
        conn.executemany(sql, rows)

        if audit_rows:
            audit_sql = """
                INSERT INTO player_prop_mapping_audit
                    (event_id, odd_id, sportsbook,
                     player_id, player_name, team_id, team_name,
                     market_type, market_group_key, side, line,
                     price, decimal_odds, is_alt_line, available,
                     validation_status, mapping_confidence, mapping_method,
                     validation_reason, excluded, exclusion_reasons,
                     captured_at)
                VALUES
                    (:event_id, :odd_id, :sportsbook,
                     :player_id, :player_name, :team_id, :team_name,
                     :market_type, :market_group_key, :side, :line,
                     :price, :decimal_odds, :is_alt_line, :available,
                     :validation_status, :mapping_confidence, :mapping_method,
                     :validation_reason, :excluded, :exclusion_reasons,
                     :captured_at)
            """
            conn.executemany(audit_sql, audit_rows)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return len(rows)


# ==================================================================
# Run tracking
# ==================================================================

def create_run(
    conn: DB,
    run_type: str = "scan",
    mode: str | None = None,
    market_filter: str | None = None,
    form_filter: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Create a new scan run and return its UUID."""
    import json
    run_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO scan_runs (run_id, started_at, run_type, mode,
           market_filter, form_filter, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, datetime.now(timezone.utc).isoformat(), run_type,
         mode, market_filter, form_filter,
         json.dumps(metadata, default=str) if metadata else None),
    )
    conn.commit()
    logger.info("Created run %s (type=%s)", run_id[:8], run_type)
    return run_id


def finish_run(
    conn: DB,
    run_id: str,
    *,
    n_events: int = 0,
    n_markets: int = 0,
    n_opportunities: int = 0,
    n_yn_opps: int = 0,
    data_source: str = "",
    research_only: bool = False,
    error_message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Mark a scan run as finished with summary stats.

    *metadata*, when given, is JSON-merged into the row's existing
    ``metadata_json`` (set at ``create_run`` time, if any) rather than
    overwriting it outright — added 2026-08-23 so the scanner's own
    per-run Pinnacle/qualification-funnel diagnostics (total groups,
    insufficient-books/EV-threshold/no-edge counts, official_approved,
    stale-Pinnacle-skipped) become queryable per scan_run for ongoing
    funnel visibility, not just a printed log line.
    """
    import json
    merged_metadata = None
    if metadata is not None:
        existing = conn.execute(
            "SELECT metadata_json FROM scan_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        existing_meta = {}
        if existing and existing["metadata_json"]:
            try:
                existing_meta = json.loads(existing["metadata_json"])
            except (TypeError, ValueError):
                existing_meta = {}
        existing_meta.update(metadata)
        merged_metadata = json.dumps(existing_meta, default=str)

    if merged_metadata is not None:
        conn.execute(
            """UPDATE scan_runs SET
               finished_at = ?, n_events = ?, n_markets = ?,
               n_opportunities = ?, n_yn_opps = ?, data_source = ?,
               research_only = ?, error_message = ?, metadata_json = ?
               WHERE run_id = ?""",
            (datetime.now(timezone.utc).isoformat(), n_events, n_markets,
             n_opportunities, n_yn_opps, data_source,
             1 if research_only else 0, error_message, merged_metadata, run_id),
        )
    else:
        conn.execute(
            """UPDATE scan_runs SET
               finished_at = ?, n_events = ?, n_markets = ?,
               n_opportunities = ?, n_yn_opps = ?, data_source = ?,
               research_only = ?, error_message = ?
               WHERE run_id = ?""",
            (datetime.now(timezone.utc).isoformat(), n_events, n_markets,
             n_opportunities, n_yn_opps, data_source,
             1 if research_only else 0, error_message, run_id),
        )
    conn.commit()
    logger.info("Finished run %s: %d events, %d+%d opps",
                run_id[:8], n_events, n_opportunities, n_yn_opps)


def update_run_metadata(conn: DB, run_id: str, metadata: dict) -> None:
    """JSON-merge *metadata* into a scan_runs row's ``metadata_json``,
    touching no other column.

    ``finish_run(..., metadata=...)`` also merges, but it unconditionally
    rewrites ``n_events``/``n_markets``/``n_opportunities``/``n_yn_opps``/
    ``data_source``/``research_only``/``error_message`` to their call's
    values (defaulting to 0/""/False/None) — safe when called once at the
    natural end of a run, but a second, later caller (e.g. the freeze
    stage merging in save/tier/Pinnacle counts after the scan stage has
    already finished the row) would silently zero those out. This does
    only the metadata merge, safe to call any number of times.
    """
    import json
    existing = conn.execute(
        "SELECT metadata_json FROM scan_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    existing_meta = {}
    if existing and existing["metadata_json"]:
        try:
            existing_meta = json.loads(existing["metadata_json"])
        except (TypeError, ValueError):
            existing_meta = {}
    existing_meta.update(metadata)
    conn.execute(
        "UPDATE scan_runs SET metadata_json = ? WHERE run_id = ?",
        (json.dumps(existing_meta, default=str), run_id),
    )
    conn.commit()


def log_ingestion(
    conn: DB,
    run_id: str | None,
    event_id: str,
    odds_rows: int = 0,
    audit_rows: int = 0,
    error_message: str | None = None,
) -> None:
    """Record an ingestion event for auditability."""
    conn.execute(
        """INSERT INTO ingestion_log (run_id, event_id, odds_rows, audit_rows, error_message)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, event_id, odds_rows, audit_rows, error_message),
    )
    conn.commit()


def persist_scan_error(
    conn: DB,
    run_id: str | None,
    error_type: str,
    error_message: str,
    context: dict | None = None,
) -> None:
    """Persist a scan error to the ingestion_log with run_id for audit trail.

    Parameters
    ----------
    run_id : str or None
        The run ID to associate with the error.
    error_type : str
        Category of error (e.g. "api_failure", "parse_error", "config_error").
    error_message : str
        Human-readable error description.
    context : dict or None
        Additional context (e.g. endpoint, params).
    """
    import json
    event_id = f"_error_{error_type}"
    ctx_str = json.dumps(context, default=str) if context else None
    conn.execute(
        """INSERT INTO ingestion_log (run_id, event_id, odds_rows, audit_rows, error_message)
           VALUES (?, ?, 0, 0, ?)""",
        (run_id, event_id, f"[{error_type}] {error_message}" + (f" | {ctx_str}" if ctx_str else "")),
    )
    conn.commit()
    logger.error("Persisted error [%s]: %s", error_type, error_message)


# ==================================================================
# Phase 6: Historical recommendations, grading, CLV
# ==================================================================

# Fingerprint fields — the exact fields that define a unique recommendation snapshot.
# Changing any of these creates a new recommendation record.
FINGERPRINT_FIELDS = (
    "event_id", "player_id", "market_type", "market_form", "period",
    "line", "side", "sportsbook", "offered_american_odds",
    "rec_status", "observation_timestamp",
)


def compute_fingerprint(rec: dict) -> str:
    """Compute a deterministic SHA-256 fingerprint for a recommendation dict.

    The fingerprint changes when any material field changes (price, line,
    side, status, observation time). It is stable across identical snapshots.

    Fields used (from FINGERPRINT_FIELDS):
        event_id, player_id, market_type, market_form, period,
        line, side, sportsbook, offered_american_odds,
        rec_status, observation_timestamp.
    """
    parts = []
    for field in FINGERPRINT_FIELDS:
        val = rec.get(field)
        if val is None:
            parts.append("")
        else:
            parts.append(str(val))
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def generate_recommendation_id() -> str:
    """Generate a UUID-based recommendation ID."""
    return str(uuid.uuid4())


def record_lifecycle_event(
    conn: DB,
    event_type: str,
    event_key: str,
    *,
    recommendation: dict | None = None,
    run_id: str | None = None,
    snapshot_kind: str | None = None,
    closing: dict | None = None,
    line_move_type: str | None = None,
    closing_available: bool | None = None,
    clv_available: bool | None = None,
    result: str | None = None,
    final_stat_value: float | None = None,
    settlement_reason: str | None = None,
    grader_version: str | None = None,
    event_timestamp: str | None = None,
    provenance: dict | None = None,
) -> bool:
    """Append one idempotent recommendation lifecycle event.

    Lifecycle rows are never updated.  ``event_key`` is the stable idempotency
    key; a repeated event is ignored while a correction can use a new key.
    """
    rec = recommendation or {}
    closing = closing or {}
    rec_id = rec.get("recommendation_id")
    if not rec_id:
        return False

    reference_used = rec.get("pinnacle_reference_used")
    pinnacle_line = rec.get("pinnacle_line")
    if pinnacle_line is None and reference_used:
        pinnacle_line = rec.get("line")

    columns = [
        "lifecycle_event_id", "recommendation_id", "event_type", "event_key",
        "run_id", "event_id", "player_id", "player_name", "market_type", "side",
        "line", "sportsbook", "offered_american_odds", "offered_decimal_odds",
        "implied_probability", "model_fair_probability", "model_edge", "ev",
        "confidence_score", "quality_score", "pinnacle_reference_used",
        "pinnacle_book", "pinnacle_line", "pinnacle_over_odds",
        "pinnacle_under_odds", "pinnacle_fair_probability", "pinnacle_ev",
        "pinnacle_probability_edge", "snapshot_kind", "closing_sportsbook",
        "closing_line", "closing_american_odds", "closing_decimal_odds",
        "closing_implied_probability", "line_move_type", "closing_available",
        "clv_probability", "clv_price_diff", "clv_available",
        "result", "final_stat_value", "settlement_reason", "grader_version",
        "event_timestamp", "data_source", "source_run_id", "provenance_json",
    ]
    values = [
        str(uuid.uuid4()), rec_id, event_type, event_key,
        run_id or rec.get("scan_run_id"), rec.get("event_id"), rec.get("player_id"),
        rec.get("player_name"), rec.get("market_type"), rec.get("side"),
        rec.get("line"), rec.get("sportsbook"), rec.get("offered_american_odds"),
        rec.get("offered_decimal_odds"), rec.get("offered_implied_prob"),
        rec.get("fair_prob"),
        rec.get("model_edge", rec.get("ev_pct") if rec.get("ev_pct") is not None
                else rec.get("yn_implied_prob_adv")),
        rec.get("ev", rec.get("ev_pct")),
        rec.get("confidence_score", rec.get("confidence")),
        rec.get("quality_score", rec.get("market_quality_score")),
        1 if reference_used else 0 if reference_used is not None else None,
        rec.get("pinnacle_book"), pinnacle_line, rec.get("pinnacle_over_price"),
        rec.get("pinnacle_under_price"),
        rec.get("pinnacle_fair_probability", rec.get("pinnacle_fair_prob")),
        rec.get("pinnacle_ev"),
        rec.get("pinnacle_probability_edge", rec.get("pinnacle_prob_edge")),
        snapshot_kind, closing.get("sportsbook"), closing.get("line"),
        closing.get("american_odds", closing.get("price")),
        closing.get("decimal_odds"), closing.get("implied_probability"),
        line_move_type or closing.get("line_move_type"),
        1 if closing_available else 0 if closing_available is not None else None,
        closing.get("clv_probability"), closing.get("clv_price_diff"),
        1 if clv_available else 0 if clv_available is not None else None,
        result,
        final_stat_value, settlement_reason, grader_version,
        event_timestamp or rec.get("scan_timestamp") or datetime.now(timezone.utc).isoformat(),
        rec.get("data_source"), run_id or rec.get("scan_run_id"),
        json.dumps(provenance or {}, sort_keys=True, default=str),
    ]
    try:
        cursor = conn.execute(
            f"INSERT INTO recommendation_lifecycle_events "
            f"({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT (event_key) DO NOTHING",
            values,
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as exc:
        conn.rollback()
        logger.warning("Could not record lifecycle event %s: %s", event_key, exc)
        return False


def record_recommendation_created(conn: DB, rec: dict) -> int:
    """Record creation and creation-line evidence for a recommendation."""
    rec_id = rec.get("recommendation_id")
    if not rec_id:
        return 0
    recorded = 0
    if record_lifecycle_event(
        conn, "RECOMMENDATION_CREATED", f"created:{rec_id}",
        recommendation=rec, run_id=rec.get("scan_run_id"),
        event_timestamp=rec.get("scan_timestamp"),
        provenance={"source": rec.get("data_source"), "phase": "freeze"},
    ):
        recorded += 1
    if record_lifecycle_event(
        conn, "LINE_SNAPSHOT", f"line:{rec_id}:creation",
        recommendation=rec, run_id=rec.get("scan_run_id"),
        snapshot_kind="creation", event_timestamp=rec.get("observation_timestamp"),
        provenance={"source": rec.get("data_source"), "phase": "freeze"},
    ):
        recorded += 1
    return recorded


def record_settlement_lifecycle(
    conn: DB,
    recommendation_id: str,
    settlement_status: str,
    *,
    final_stat_value: float | None = None,
    settlement_reason: str | None = None,
    grader_version: str | None = None,
) -> bool:
    """Append a deterministic settlement event using the stored recommendation."""
    rec = get_recommendation_by_id(conn, recommendation_id)
    if not rec:
        return False
    stat_key = "" if final_stat_value is None else str(final_stat_value)
    key = f"settlement:{recommendation_id}:{settlement_status}:{stat_key}:{grader_version or ''}"
    return record_lifecycle_event(
        conn, "SETTLEMENT", key, recommendation=rec,
        run_id=rec.get("scan_run_id"), result=settlement_status,
        final_stat_value=final_stat_value, settlement_reason=settlement_reason,
        grader_version=grader_version, provenance={"phase": "grading"},
    )


def record_grading_completed(
    conn: DB,
    rec: dict,
    result: str,
    *,
    final_stat_value: float | None = None,
    grader_version: str | None = None,
) -> bool:
    """Append an idempotent grading-completed event."""
    rec_id = rec.get("recommendation_id")
    if not rec_id:
        return False
    key = f"grading:{rec_id}:{result}:{'' if final_stat_value is None else final_stat_value}:{grader_version or ''}"
    return record_lifecycle_event(
        conn, "GRADING_COMPLETED", key, recommendation=rec,
        run_id=rec.get("scan_run_id"), result=result,
        final_stat_value=final_stat_value, grader_version=grader_version,
        provenance={"phase": "grading"},
    )


def _bool_to_int_or_none(value) -> int | None:
    """Convert a real Python bool to the 0/1 an `integer` column expects,
    preserving None (genuinely unknown/not-applicable) rather than
    collapsing it to 0. Postgres does NOT implicitly cast a bound
    `boolean` parameter to `integer` in an UPDATE ... SET clause the way
    SQLite silently would — passing a raw True/False here raises
    ``psycopg2.errors.DatatypeMismatch``."""
    if value is None:
        return None
    return 1 if value else 0


def _persist_recommendation_evidence(conn: DB, recommendation_id: str, rec: dict) -> None:
    """Persist optional score/Pinnacle fields without breaking legacy schemas."""
    fields = {
        "market_quality_score": rec.get("market_quality_score"),
        # Real root cause of a 2026-08-22 to 2026-08-24 production outage
        # (traced 2026-08-24, reproduced against the real prod schema):
        # these three carry a genuine Python bool (True/False) whenever
        # Pinnacle was actually checked — not just None — and a bool
        # bound against an `integer` column raises DatatypeMismatch on
        # PostgreSQL, silently swallowed by save_recommendation's bare
        # `except Exception`. is_official/reliable_ev_checked/reliable_ev
        # below were already correctly cast; these three were missed.
        "pinnacle_found": _bool_to_int_or_none(rec.get("pinnacle_found")),
        "pinnacle_reference_used": _bool_to_int_or_none(rec.get("pinnacle_reference_used")),
        "pinnacle_approved": _bool_to_int_or_none(rec.get("pinnacle_approved")),
        "pinnacle_book": rec.get("pinnacle_book"),
        "pinnacle_line": rec.get("pinnacle_line"),
        "pinnacle_over_price": rec.get("pinnacle_over_price"),
        "pinnacle_under_price": rec.get("pinnacle_under_price"),
        "pinnacle_fair_prob": rec.get("pinnacle_fair_prob"),
        "pinnacle_ev": rec.get("pinnacle_ev"),
        "pinnacle_prob_edge": rec.get("pinnacle_prob_edge"),
        "pinnacle_source": rec.get("pinnacle_source"),
        "is_official": 1 if rec.get("is_official") else 0,
        "confidence_score": rec.get("confidence_score"),
        "reliable_ev_checked": 1 if rec.get("reliable_ev_checked") else 0,
        "reliable_ev": 1 if rec.get("reliable_ev") else 0,
        "reliable_ev_status": rec.get("reliable_ev_status"),
        "reliable_ev_reasons": "; ".join(rec.get("reliable_ev_reasons") or []),
        "reliable_ev_calculated_pct": rec.get("reliable_ev_calculated_pct"),
        "reliable_ev_version": rec.get("reliable_ev_version"),
        "challenger_expected_strikeouts": rec.get("challenger_expected_strikeouts"),
        "challenger_over_probability": rec.get("challenger_over_probability"),
        "challenger_under_probability": rec.get("challenger_under_probability"),
        "challenger_push_probability": rec.get("challenger_push_probability"),
        "challenger_fair_probability": rec.get("challenger_fair_probability"),
        "challenger_version": rec.get("challenger_version"),
    }
    existing = _existing_columns(conn, "historical_recommendations")
    fields = {name: value for name, value in fields.items() if name in existing}
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE historical_recommendations SET {assignments} WHERE recommendation_id = ?",
        [*fields.values(), recommendation_id],
    )


SAVE_STATUS_SAVED = "saved"
SAVE_STATUS_DUPLICATE = "duplicate"
SAVE_STATUS_ERROR = "error"


class SaveRecommendationResult:
    """Outcome of save_recommendation_result() — distinguishes a real
    persistence ERROR from a legitimate intentional DUPLICATE skip,
    added 2026-08-24 after a production outage (2026-08-22 to
    2026-08-24) where both cases returned an indistinguishable None and
    a real DatatypeMismatch was silently swallowed for three days. See
    save_recommendation_result's docstring."""

    __slots__ = ("recommendation_id", "status", "error_type", "error_message")

    def __init__(self, recommendation_id: str | None, status: str,
                 error_type: str | None = None, error_message: str | None = None):
        self.recommendation_id = recommendation_id
        self.status = status
        self.error_type = error_type
        self.error_message = error_message

    @property
    def ok(self) -> bool:
        """True for either a real save or a legitimate duplicate — i.e.
        "the pipeline doesn't need to treat this as a failure", as
        opposed to status == SAVE_STATUS_ERROR."""
        return self.status in (SAVE_STATUS_SAVED, SAVE_STATUS_DUPLICATE)


def _safe_rec_identifiers(rec: dict) -> dict:
    """The subset of a rec dict safe to put in logs/error messages —
    identifiers only, never odds/prices/scores (not secret, just noise)
    and never anything resembling a credential (a rec dict never
    contains one, but this stays intentionally narrow regardless)."""
    return {
        "event_id": rec.get("event_id"),
        "player_id": rec.get("player_id"),
        "market_type": rec.get("market_type"),
        "side": rec.get("side"),
        "sportsbook": rec.get("sportsbook"),
        "line": rec.get("line"),
        "league": rec.get("league"),
    }


def save_recommendation_result(conn: DB, rec: dict) -> SaveRecommendationResult:
    """Insert a recommendation snapshot and report exactly what happened.

    Added 2026-08-24 following a real production outage: save_recommendation's
    bare `except Exception: return None` made a genuine DATABASE ERROR
    (a real psycopg2.errors.DatatypeMismatch, see
    _persist_recommendation_evidence) completely indistinguishable from a
    legitimate DUPLICATE (fingerprint already exists) — both returned
    None, silently, with no log line anywhere. Every recommendation for
    three straight days was the former, not the latter, and nothing
    noticed. This function still does the exact same work as
    save_recommendation, but:

    - logs any real exception (type, message, safe identifiers,
      traceback) via logger.error before rolling back — never silently
      swallowed again.
    - returns a SaveRecommendationResult whose `.status` is exactly one
      of "saved" / "duplicate" / "error", so a caller (see
      daily_pipeline.py::_stage_freeze) can count and surface real
      failures separately from intentional skips, instead of treating
      both as an identical no-op.

    save_recommendation() (below) remains the plain str|None wrapper for
    every existing caller that only needs the id.
    """
    rec_id = rec.get("recommendation_id") or generate_recommendation_id()
    fingerprint = compute_fingerprint(rec)

    try:
        cursor = conn.execute(
            """INSERT INTO historical_recommendations
               (recommendation_id, fingerprint, scan_run_id, ingestion_run_id,
                event_id, event_start_time, player_id, player_name,
                market_type, market_form, period, line, side, sportsbook,
                offered_american_odds, offered_decimal_odds, offered_implied_prob,
                fair_prob, fair_american_odds, ev_pct,
                yn_reference_prob, yn_reference_odds,
                yn_implied_prob_adv, yn_decimal_odds_adv,
                n_consensus_books, market_quality,
                rec_status, rec_eligible, data_source,
                observation_timestamp, scan_timestamp,
                freshness_status, model_version, matchup, event_status,
                model_score, score_version, score_components, score_cap, score_explanation,
                recommendation_tier, qualification_passed,
                qualification_reasons, disqualification_reasons,
                contributing_book_count, contributing_books,
                applicable_edge_metric, applicable_edge_threshold,
                model_score_threshold, qualification_rules_version,
                league, sport, raw_line, confidence_score, confidence_grade)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (fingerprint) DO NOTHING""",
            (rec_id, fingerprint,
             rec.get("scan_run_id"), rec.get("ingestion_run_id"),
             rec["event_id"], rec.get("event_start_time"),
             rec["player_id"], rec.get("player_name"),
             rec["market_type"], rec.get("market_form", "ou"),
             rec.get("period", "game"), rec.get("line"),
             rec["side"], rec["sportsbook"],
             rec["offered_american_odds"], rec["offered_decimal_odds"],
             rec["offered_implied_prob"],
             rec.get("fair_prob"), rec.get("fair_american_odds"),
             rec.get("ev_pct"),
             rec.get("yn_reference_prob"), rec.get("yn_reference_odds"),
             rec.get("yn_implied_prob_adv"), rec.get("yn_decimal_odds_adv"),
             rec.get("n_consensus_books"), rec.get("market_quality"),
             rec["rec_status"], 1 if rec.get("rec_eligible") else 0,
             rec.get("data_source"),
             rec.get("observation_timestamp"), rec["scan_timestamp"],
             rec.get("freshness_status"), rec.get("model_version", "v1"),
             rec.get("matchup", ""), rec.get("event_status", ""),
             rec.get("model_score"), rec.get("score_version", "model_score_v1"),
             rec.get("score_components"), rec.get("score_cap"),
             rec.get("score_explanation"),
             rec.get("recommendation_tier", "RESEARCH_ONLY"),
             rec.get("qualification_passed", 0),
             rec.get("qualification_reasons", ""),
             rec.get("disqualification_reasons", ""),
             rec.get("contributing_book_count", 0),
             rec.get("contributing_books", ""),
             rec.get("applicable_edge_metric", ""),
             rec.get("applicable_edge_threshold", 0.0),
             rec.get("model_score_threshold", 8.0),
             rec.get("qualification_rules_version", ""),
             rec.get("league", "MLB"),
             rec.get("sport", "baseball"),
             rec.get("raw_line"),
             rec.get("confidence_score"),
             rec.get("confidence_grade"),
             ),
        )
        _persist_recommendation_evidence(conn, rec_id, rec)
        conn.commit()
        if cursor.rowcount == 0:
            existing = conn.execute(
                "SELECT recommendation_id FROM historical_recommendations WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing:
                rec_with_id = dict(rec)
                rec_with_id["recommendation_id"] = existing["recommendation_id"]
                record_recommendation_created(conn, rec_with_id)
                return SaveRecommendationResult(existing["recommendation_id"], SAVE_STATUS_DUPLICATE)
            # rowcount==0 with no matching fingerprint row is a genuine
            # anomaly (the ON CONFLICT target didn't fire, but nothing
            # was found either) — surface it as an error, not a silent
            # duplicate-shaped None, so it isn't mistaken for the
            # expected/common case.
            logger.error(
                "save_recommendation: INSERT affected 0 rows but no existing "
                "row matches fingerprint=%s — unexpected. identifiers=%s",
                fingerprint, _safe_rec_identifiers(rec),
            )
            return SaveRecommendationResult(
                None, SAVE_STATUS_ERROR, error_type="AnomalousZeroRowcount",
                error_message="INSERT affected 0 rows but no matching fingerprint was found",
            )
        rec_with_id = dict(rec)
        rec_with_id["recommendation_id"] = rec_id
        record_recommendation_created(conn, rec_with_id)
        return SaveRecommendationResult(rec_id, SAVE_STATUS_SAVED)
    except Exception as exc:
        conn.rollback()
        logger.error(
            "save_recommendation FAILED: %s: %s | identifiers=%s",
            type(exc).__name__, exc, _safe_rec_identifiers(rec),
            exc_info=True,
        )
        return SaveRecommendationResult(
            None, SAVE_STATUS_ERROR,
            error_type=type(exc).__name__, error_message=str(exc),
        )


def save_recommendation(conn: DB, rec: dict) -> str | None:
    """Insert a recommendation snapshot. Idempotent via fingerprint UNIQUE.

    Thin backward-compatible wrapper around save_recommendation_result()
    for every existing caller that only needs the id (or None for "did
    not insert", for any reason — duplicate or error, indistinguishably,
    exactly as before: a fresh insert returns its own new id, anything
    else returns None, even though save_recommendation_result's
    .recommendation_id is populated for the duplicate case too). Callers
    that need to tell a real persistence error apart from an intentional
    duplicate skip — chiefly daily_pipeline.py::_stage_freeze — should
    call save_recommendation_result() directly instead.
    """
    result = save_recommendation_result(conn, rec)
    return result.recommendation_id if result.status == SAVE_STATUS_SAVED else None


def freeze_official_pick(
    conn: DB,
    recommendation_id: str,
    tier: str = "OFFICIAL_TRACKED",
    official_rank: int | None = None,
    rules_version: str = "official_pick_rules_v1",
) -> bool:
    """Create an immutable frozen snapshot in official_picks.

    Returns True if inserted, False if already exists (idempotent).
    """
    try:
        cursor = conn.execute("""
            INSERT INTO official_picks (
                recommendation_id, tier, official_rank, rules_version,
                league, sport
            )
            VALUES (
                ?, ?, ?, ?,
                COALESCE((SELECT league FROM historical_recommendations WHERE recommendation_id = ?), 'MLB'),
                COALESCE((SELECT sport FROM historical_recommendations WHERE recommendation_id = ?), 'baseball')
            )
            ON CONFLICT (recommendation_id) DO NOTHING
        """, (recommendation_id, tier, official_rank, rules_version, recommendation_id, recommendation_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False


def _is_better_price(candidate_odds: int | None, current_odds: int | None) -> bool:
    """True if *candidate_odds* pays more per dollar risked than *current_odds*.

    Compared via implied probability (lower is better for the bettor) so
    the comparison is correct on both sides of the American-odds sign
    boundary, unlike a raw numeric comparison of the odds themselves.
    """
    if candidate_odds is None:
        return False
    if current_odds is None:
        return True
    from src.market_analysis import american_to_probability
    try:
        return american_to_probability(int(candidate_odds)) < american_to_probability(int(current_odds))
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def freeze_or_update_official_pick(
    conn: DB,
    rec: dict,
    tier: str = "OFFICIAL_TRACKED",
    official_rank: int | None = None,
    rules_version: str = "official_pick_rules_v2",
) -> dict:
    """Freeze *rec* as an official pick, suppressing duplicate re-observations
    of the same bet and superseding it only on a materially different price
    or line — see src/official_picks.py::classify_pick_update.

    Returns {"action": "frozen"|"duplicate"|"superseded"|"error",
             "recommendation_id": <the ACTIVE pick's recommendation_id>}.
    Idempotent: rerunning with the exact same rec is safe (ON CONFLICT DO
    NOTHING on insert; duplicate branch just refreshes tracking fields).
    """
    from src.official_picks import compute_bet_slot_key, classify_pick_update, UPDATE_DUPLICATE

    recommendation_id = rec.get("recommendation_id")
    if not recommendation_id:
        return {"action": "error", "reason": "missing recommendation_id"}

    slot_key = compute_bet_slot_key(rec)
    now = datetime.now(timezone.utc).isoformat()
    candidate_odds = rec.get("offered_american_odds")

    existing = conn.execute("""
        SELECT op.*, hr.line AS hr_line, hr.offered_american_odds AS hr_odds
        FROM official_picks op
        JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
        WHERE op.bet_slot_key = ? AND op.pick_status = 'ACTIVE'
    """, (slot_key,)).fetchone()

    if existing is None:
        try:
            conn.execute("""
                INSERT INTO official_picks (
                    recommendation_id, tier, official_rank, rules_version,
                    league, sport, pick_status, bet_slot_key,
                    first_recommended_at, best_american_odds, best_odds_at,
                    latest_american_odds, latest_odds_at, update_count
                ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT (recommendation_id) DO NOTHING
            """, (
                recommendation_id, tier, official_rank, rules_version,
                rec.get("league", "MLB"), rec.get("sport", "baseball"), slot_key,
                now, candidate_odds, now, candidate_odds, now,
            ))
            conn.commit()
            return {"action": "frozen", "recommendation_id": recommendation_id}
        except Exception:
            conn.rollback()
            return {"action": "error", "recommendation_id": recommendation_id}

    existing_dict = dict(existing)
    existing_recommendation_id = existing_dict["recommendation_id"]

    if existing_recommendation_id == recommendation_id:
        # Same recommendation snapshot re-selected on a later scan (e.g.
        # a stable price with no new opportunity to compare against) —
        # nothing to do, still the active pick.
        return {"action": "duplicate", "recommendation_id": existing_recommendation_id}

    update_kind = classify_pick_update(
        {"line": existing_dict.get("hr_line"), "offered_american_odds": existing_dict.get("hr_odds")},
        rec,
    )

    if update_kind == UPDATE_DUPLICATE:
        best_odds = existing_dict.get("best_american_odds")
        best_at = existing_dict.get("best_odds_at")
        if _is_better_price(candidate_odds, best_odds):
            best_odds, best_at = candidate_odds, now
        try:
            conn.execute("""
                UPDATE official_picks
                SET latest_american_odds = ?, latest_odds_at = ?,
                    best_american_odds = ?, best_odds_at = ?,
                    update_count = update_count + 1
                WHERE recommendation_id = ?
            """, (candidate_odds, now, best_odds, best_at, existing_recommendation_id))
            conn.commit()
        except Exception:
            conn.rollback()
        return {"action": "duplicate", "recommendation_id": existing_recommendation_id}

    # Material update: supersede the old ACTIVE pick, freeze the new one.
    try:
        conn.execute("""
            UPDATE official_picks SET pick_status = 'SUPERSEDED', superseded_by = ?, superseded_at = ?
            WHERE recommendation_id = ? AND pick_status = 'ACTIVE'
        """, (recommendation_id, now, existing_recommendation_id))
        conn.execute("""
            INSERT INTO official_picks (
                recommendation_id, tier, official_rank, rules_version,
                league, sport, pick_status, bet_slot_key,
                first_recommended_at, best_american_odds, best_odds_at,
                latest_american_odds, latest_odds_at, update_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recommendation_id) DO NOTHING
        """, (
            recommendation_id, tier, official_rank, rules_version,
            rec.get("league", "MLB"), rec.get("sport", "baseball"), slot_key,
            existing_dict.get("first_recommended_at") or now,
            candidate_odds, now, candidate_odds, now,
            (existing_dict.get("update_count") or 0) + 1,
        ))
        conn.commit()
        return {
            "action": "superseded", "recommendation_id": recommendation_id,
            "superseded_recommendation_id": existing_recommendation_id,
        }
    except Exception:
        conn.rollback()
        return {"action": "error", "recommendation_id": recommendation_id}


def get_official_picks_today(conn: DB) -> list[dict]:
    """Get today's official picks."""
    rows = conn.execute("""
        SELECT op.*, hr.player_name, hr.market_type, hr.market_form,
               hr.side, hr.line, hr.sportsbook, hr.offered_american_odds,
               hr.offered_decimal_odds, hr.ev_pct, hr.yn_implied_prob_adv,
               hr.n_consensus_books, hr.matchup, hr.event_status,
               hr.event_start_time, hr.model_score, hr.score_explanation
        FROM official_picks op
        JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
        WHERE date(op.selected_at) = date('now')
          AND op.pick_status = 'ACTIVE'
        ORDER BY op.official_rank
    """).fetchall()
    return [dict(r) for r in rows]


def get_performance_baseline(conn: DB) -> str | None:
    """Return the non-destructive customer performance epoch."""
    row = conn.execute(
        "SELECT baseline_at FROM performance_baseline WHERE baseline_id = 1"
    ).fetchone()
    return row["baseline_at"] if row else None


def get_research_picks_today(conn: DB) -> list[dict]:
    """Get today's research-only recommendations."""
    rows = conn.execute("""
        SELECT recommendation_id, event_id, player_name, market_type, market_form,
               side, line, sportsbook, offered_american_odds, ev_pct,
               yn_implied_prob_adv, n_consensus_books, market_quality,
               rec_status, freshness_status, matchup, event_status,
               event_start_time, model_score, score_explanation,
               recommendation_tier, disqualification_reasons
        FROM historical_recommendations
        WHERE date(scan_timestamp) = date('now')
        AND recommendation_tier = 'RESEARCH_ONLY'
        ORDER BY model_score DESC
    """).fetchall()
    return [dict(r) for r in rows]


def save_event_result(
    conn: DB,
    event_id: str,
    *,
    final_status: str = "UNRESOLVED",
    away_score: int | None = None,
    home_score: int | None = None,
    result_source: str | None = None,
    source_observed_at: str | None = None,
    result_detail: str | None = None,
) -> None:
    """Insert or update an event result."""
    conn.execute(
        """INSERT INTO event_results
           (event_id, final_status, away_score, home_score,
            result_source, source_observed_at, result_detail)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id) DO UPDATE SET
               final_status = excluded.final_status,
               away_score = excluded.away_score,
               home_score = excluded.home_score,
               result_source = excluded.result_source,
               source_observed_at = excluded.source_observed_at,
               result_detail = excluded.result_detail,
               updated_at = datetime('now')""",
        (event_id, final_status, away_score, home_score,
         result_source, source_observed_at, result_detail),
    )
    conn.commit()


def save_player_stat_result(
    conn: DB,
    event_id: str,
    player_id: str,
    market_type: str,
    *,
    player_name: str | None = None,
    final_stat_value: float | None = None,
    result_source: str | None = None,
    source_observed_at: str | None = None,
    result_status: str = "UNRESOLVED",
    result_detail: str | None = None,
) -> None:
    """Insert or update a player stat result. Idempotent via UNIQUE constraint."""
    conn.execute(
        """INSERT INTO player_stat_results
           (event_id, player_id, player_name, market_type,
            final_stat_value, result_source, source_observed_at,
            result_status, result_detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id, player_id, market_type) DO UPDATE SET
               player_name = excluded.player_name,
               final_stat_value = excluded.final_stat_value,
               result_source = excluded.result_source,
               source_observed_at = excluded.source_observed_at,
               result_status = excluded.result_status,
               result_detail = excluded.result_detail,
               updated_at = datetime('now')""",
        (event_id, player_id, player_name, market_type,
         final_stat_value, result_source, source_observed_at,
         result_status, result_detail),
    )
    conn.commit()


def settle_recommendation(
    conn: DB,
    recommendation_id: str,
    settlement_status: str,
    *,
    final_stat_value: float | None = None,
    settlement_reason: str | None = None,
    grader_version: str = "v1",
) -> bool:
    """Settle a recommendation. Idempotent — skips if already settled.

    Returns True if settled (or already settled), False on error.
    """
    # Check if already settled
    cur = conn.execute(
        "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
        (recommendation_id,),
    )
    existing = cur.fetchone()
    if existing and existing[0] != "UNRESOLVED":
        logger.debug("Recommendation %s already settled as %s", recommendation_id[:8], existing[0])
        return True

    settlement_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # market_settlements.league defaults to 'MLB' at the schema level (a
    # pre-existing column added for multi-league reporting) but was never
    # actually populated here — every WNBA/NFL settlement silently landed
    # labeled MLB. Look up the real league from the recommendation itself
    # rather than trusting the column default.
    league_row = conn.execute(
        "SELECT league FROM historical_recommendations WHERE recommendation_id = ?",
        (recommendation_id,),
    ).fetchone()
    league = (league_row["league"] if league_row and league_row["league"] else "MLB")

    try:
        if existing:
            # Update existing UNRESOLVED row
            conn.execute(
                """UPDATE market_settlements SET
                   settlement_status = ?, final_stat_value = ?,
                   settled_at = ?, settlement_reason = ?, grader_version = ?,
                   league = ?
                   WHERE recommendation_id = ?""",
                (settlement_status, final_stat_value, now,
                 settlement_reason, grader_version, league, recommendation_id),
            )
        else:
            # Insert new
            conn.execute(
                """INSERT INTO market_settlements
                   (settlement_id, recommendation_id, settlement_status,
                    final_stat_value, settled_at, settlement_reason, grader_version,
                    league)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (settlement_id, recommendation_id, settlement_status,
                 final_stat_value, now, settlement_reason, grader_version, league),
            )
        conn.commit()
        record_settlement_lifecycle(
            conn,
            recommendation_id,
            settlement_status,
            final_stat_value=final_stat_value,
            settlement_reason=settlement_reason,
            grader_version=grader_version,
        )
        return True
    except Exception as e:
        conn.rollback()
        logger.error("Failed to settle %s: %s", recommendation_id[:8], e)
        return False


def compute_units(settlement_status: str, american_odds: int) -> tuple[float, float, float]:
    """Compute risk, profit, and return units for a settled bet.

    Returns (risk_units, profit_units, return_units).
    """
    if settlement_status == "WIN":
        if american_odds > 0:
            profit = american_odds / 100.0
        else:
            profit = 100.0 / abs(american_odds)
        return (1.0, profit, 1.0 + profit)
    elif settlement_status == "LOSS":
        return (1.0, -1.0, 0.0)
    elif settlement_status in ("PUSH", "VOID", "CANCELLED"):
        return (1.0, 0.0, 1.0)
    else:
        # UNRESOLVED or unknown — exclude from settled performance
        return (0.0, 0.0, 0.0)


def save_bet_units(
    conn: DB,
    recommendation_id: str,
    settlement_status: str,
    american_odds: int,
    *,
    risk_units: float = 1.0,
) -> None:
    """Save units for a settled bet with an explicit stake size."""
    base_risk, base_profit, base_return = compute_units(settlement_status, american_odds)
    stake = max(0.0, float(risk_units))
    risk = base_risk * stake
    profit = base_profit * stake
    ret = base_return * stake
    existing = conn.execute(
        "SELECT settlement_id FROM bet_units WHERE recommendation_id = ? LIMIT 1",
        (recommendation_id,),
    ).fetchone()
    settlement_id = existing[0] if existing else str(uuid.uuid4())
    conn.execute(
        """INSERT INTO bet_units
           (settlement_id, recommendation_id, risk_units, profit_units,
            return_units, odds_at_settle)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (settlement_id) DO UPDATE SET
                recommendation_id = excluded.recommendation_id,
                risk_units = excluded.risk_units,
                profit_units = excluded.profit_units,
                return_units = excluded.return_units,
                odds_at_settle = excluded.odds_at_settle""",
         (settlement_id, recommendation_id, risk, profit, ret, american_odds),
    )
    try:
        conn.execute(
            """UPDATE official_picks SET outcome = ?, graded_at = datetime('now'),
               profit_units = ?, risk_units = ?, grader_version = ?
               WHERE recommendation_id = ?""",
            (settlement_status.lower(), profit, risk, "v1", recommendation_id),
        )
    except Exception:
        # Older isolated schemas may not include the dashboard projection.
        logger.debug("Official-pick projection unavailable for %s", recommendation_id[:8])
    conn.commit()


def save_closing_price(
    conn: DB,
    recommendation_id: str,
    *,
    closing_american: int | None = None,
    closing_decimal: float | None = None,
    closing_implied_prob: float | None = None,
    closing_line: float | None = None,
    closing_observed_at: str | None = None,
    closing_sportsbook: str | None = None,
    line_move_type: str | None = None,
    clv_probability: float | None = None,
    clv_price_diff: int | None = None,
    clv_available: bool = False,
    line_movement_direction: str | None = None,
) -> None:
    """Store closing price and CLV for a recommendation.

    ``line_movement_direction`` ("favorable"/"unfavorable"/"neutral"/
    "unknown") is set only when the line moved and price CLV isn't
    available at the same line — see src/grading.py::calculate_clv. It's
    reported alongside, never blended into, clv_probability.
    """
    conn.execute(
        """INSERT INTO closing_prices
           (recommendation_id, closing_american, closing_decimal,
            closing_implied_prob, closing_line, closing_observed_at,
            closing_sportsbook, line_move_type, clv_probability,
            clv_price_diff, clv_available, line_movement_direction)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recommendation_id, closing_american, closing_decimal,
         closing_implied_prob, closing_line, closing_observed_at,
         closing_sportsbook, line_move_type, clv_probability,
         clv_price_diff, 1 if clv_available else 0, line_movement_direction),
    )
    conn.commit()


def apply_manual_override(
    conn: DB,
    recommendation_id: str,
    new_status: str,
    override_reason: str,
    *,
    override_by: str = "cli",
) -> bool:
    """Apply a manual settlement override with full audit trail.

    Returns True on success, False if reason is blank or DB error.
    """
    if not override_reason or not override_reason.strip():
        logger.error("Manual override rejected: blank reason")
        return False

    # Get current status
    cur = conn.execute(
        "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
        (recommendation_id,),
    )
    row = cur.fetchone()
    previous_status = row[0] if row else "UNRESOLVED"

    now = datetime.now(timezone.utc).isoformat()

    try:
        # Record audit trail
        conn.execute(
            """INSERT INTO manual_override_audit
               (recommendation_id, previous_status, new_status,
                override_reason, override_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (recommendation_id, previous_status, new_status,
             override_reason, override_by, now),
        )

        # Update settlement
        conn.execute(
            """UPDATE market_settlements SET
               settlement_status = ?,
               manual_override = 1,
               override_reason = ?,
               override_previous = ?,
               settled_at = ?
               WHERE recommendation_id = ?""",
            (new_status, override_reason, previous_status, now,
             recommendation_id),
        )
        conn.commit()
        logger.info("Manual override %s: %s → %s", recommendation_id[:8],
                     previous_status, new_status)
        return True
    except Exception as e:
        conn.rollback()
        logger.error("Manual override failed for %s: %s", recommendation_id[:8], e)
        return False


# ==================================================================
# Query helpers for grading and performance
# ==================================================================

def get_unsettled_recommendations(conn: DB) -> list[dict]:
    """Return all recommendations without a settled status."""
    cur = conn.execute(
        """SELECT hr.*, ms.settlement_status
           FROM historical_recommendations hr
           LEFT JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
           WHERE ms.settlement_status IS NULL OR ms.settlement_status = 'UNRESOLVED'
           ORDER BY hr.created_at"""
    )
    return [dict(row) for row in cur.fetchall()]


def get_settled_recommendations(conn: DB) -> list[dict]:
    """Return all settled (non-UNRESOLVED) recommendations with units and CLV.

    Found live 2026-08-20: this previously omitted closing_prices, so
    every caller of performance_summary() fed by this function (e.g.
    src/grade_recommendations.py) always saw clv_probability/
    line_movement_direction as None, regardless of how much real CLV
    data had actually accumulated. The website (src/customer_view.py)
    has its own separate CLV-inclusive query and was unaffected.
    """
    cur = conn.execute(
        """SELECT hr.*, ms.settlement_status, ms.final_stat_value,
                  ms.settled_at, ms.settlement_reason,
                  bu.risk_units, bu.profit_units, bu.return_units,
                  cp.clv_probability, cp.line_movement_direction,
                  cp.closing_american, cp.closing_line
           FROM historical_recommendations hr
           JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
           LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
           LEFT JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
           WHERE ms.settlement_status != 'UNRESOLVED'
           ORDER BY ms.settled_at"""
    )
    return [dict(row) for row in cur.fetchall()]


def get_recommendation_by_id(conn: DB, rec_id: str) -> dict | None:
    """Return a single recommendation by ID."""
    cur = conn.execute(
        "SELECT * FROM historical_recommendations WHERE recommendation_id = ?",
        (rec_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_player_stat_result(
    conn: DB, event_id: str, player_id: str, market_type: str,
) -> dict | None:
    """Return a player stat result if it exists."""
    cur = conn.execute(
        """SELECT * FROM player_stat_results
           WHERE event_id = ? AND player_id = ? AND market_type = ?""",
        (event_id, player_id, market_type),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_event_result(conn: DB, event_id: str) -> dict | None:
    """Return an event's final result (scores/status) if it exists."""
    cur = conn.execute(
        "SELECT * FROM event_results WHERE event_id = ?", (event_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ==================================================================
# Phase 9: Closing price capture and analytics helpers
# ==================================================================

def capture_closing_prices(
    conn: DB,
    recommendations: list[dict],
    *,
    run_id: str | None = None,
    snapshot_kind: str = "final",
) -> int:
    """Capture closing prices for recommendations from current odds data.

    For each recommendation, looks up the latest odds for the same
    (event_id, player_id, market_type, side) in player_prop_odds and
    records an append-only lifecycle snapshot. ``final`` snapshots also
    populate the existing canonical closing_prices table.

    Returns the number of closing prices captured.
    """
    captured = 0
    for rec in recommendations:
        rec_id = rec.get("recommendation_id")
        if not rec_id:
            continue

        # The canonical closing_prices table has one final snapshot. Lifecycle
        # evidence can retain separate pregame and final snapshots.
        existing = None
        if snapshot_kind == "final":
            existing = conn.execute(
                "SELECT id FROM closing_prices WHERE recommendation_id = ?",
                (rec_id,),
            ).fetchone()
        if existing:
            continue

        # Look up latest odds for this market
        cur = conn.execute(
            """SELECT sportsbook, price, decimal_odds, line, captured_at
               FROM player_prop_odds
               WHERE event_id = ? AND player_id = ? AND market_type = ?
                  AND LOWER(side) = LOWER(?) AND available = 1
                  AND (? IS NULL OR captured_at <= ?)
               ORDER BY captured_at DESC
               LIMIT 1""",
            (rec.get("event_id"), rec.get("player_id"),
             rec.get("market_type"), rec.get("side", "").lower(),
             rec.get("event_start_time"), rec.get("event_start_time")),
        )
        closing_row = cur.fetchone()

        if not closing_row:
            record_lifecycle_event(
                conn, "CLOSING_SNAPSHOT", f"closing:{rec_id}:{snapshot_kind}",
                recommendation=rec, run_id=run_id,
                snapshot_kind=snapshot_kind,
                event_timestamp=rec.get("scan_timestamp"),
                line_move_type="no_close",
                closing_available=False,
                clv_available=False,
                provenance={"source": rec.get("data_source"), "available": False},
            )
            continue

        closing = dict(closing_row)
        bet_line = rec.get("line")
        closing_line = closing.get("line")
        bet_american = rec.get("offered_american_odds")

        from src.market_analysis import american_to_probability, american_to_decimal
        from src.grading import calculate_clv

        closing_american = closing.get("price")
        closing_decimal = closing.get("decimal_odds") or (
            american_to_decimal(closing_american) if closing_american else None
        )
        closing_implied = (
            american_to_probability(closing_american) if closing_american else None
        )

        # If the market's representative line moved away from our bet's
        # line, check whether any book still quotes our EXACT original
        # line as an alt line at close — that's a genuinely correct
        # same-line price comparison, not a fallback/approximation.
        # Restricted to the SAME capture batch as the representative
        # closing row (one scan run stamps all its rows with one
        # captured_at): otherwise this would just re-find the bet's own
        # original, long-stale snapshot at that line and wrongly treat
        # a book that no longer offers it as if it still did.
        exact_line_closing_american = None
        closing_captured_at = closing.get("captured_at")
        if (bet_line is not None and closing_line is not None
                and bet_line != closing_line and closing_captured_at is not None):
            exact_cur = conn.execute(
                """SELECT price FROM player_prop_odds
                   WHERE event_id = ? AND player_id = ? AND market_type = ?
                      AND LOWER(side) = LOWER(?) AND available = 1 AND line = ?
                      AND captured_at = ?
                   ORDER BY captured_at DESC LIMIT 1""",
                (rec.get("event_id"), rec.get("player_id"), rec.get("market_type"),
                 rec.get("side", "").lower(), bet_line, closing_captured_at),
            )
            exact_row = exact_cur.fetchone()
            if exact_row:
                exact_line_closing_american = exact_row["price"]

        clv_result = calculate_clv(
            bet_american, bet_line, closing_american, closing_line,
            market_type=rec.get("market_type"), side=rec.get("side"),
            raw_line=rec.get("raw_line"), closing_raw_line=None,
            exact_line_closing_american=exact_line_closing_american,
        )
        line_move_type = clv_result["line_move_type"] or "no_close"
        clv_prob = clv_result["clv_probability"]
        clv_price = clv_result["clv_price_diff"]
        clv_available = clv_result["clv_available"]
        line_movement_direction = clv_result["line_movement_direction"]

        if snapshot_kind == "final":
            save_closing_price(
                conn, rec_id,
                closing_american=closing_american,
                closing_decimal=closing_decimal,
                closing_implied_prob=closing_implied,
                closing_line=closing_line,
                closing_observed_at=closing.get("captured_at"),
                closing_sportsbook=closing.get("sportsbook"),
                line_move_type=line_move_type,
                clv_probability=clv_prob,
                clv_price_diff=clv_price,
                clv_available=clv_available,
                line_movement_direction=line_movement_direction,
            )
            captured += 1

        record_lifecycle_event(
            conn, "CLOSING_SNAPSHOT", f"closing:{rec_id}:{snapshot_kind}",
            recommendation=rec, run_id=run_id,
            snapshot_kind=snapshot_kind,
            closing={
                "sportsbook": closing.get("sportsbook"),
                "line": closing_line,
                "american_odds": closing_american,
                "decimal_odds": closing_decimal,
                "implied_probability": closing_implied,
                "line_move_type": line_move_type,
                "clv_probability": clv_prob,
                "clv_price_diff": clv_price,
                "line_movement_direction": line_movement_direction,
            },
            line_move_type=line_move_type,
            closing_available=True,
            clv_available=clv_available,
            event_timestamp=closing.get("captured_at") or rec.get("scan_timestamp"),
            provenance={"source": rec.get("data_source"), "available": True},
        )

    return captured


def get_all_recommendations_with_settlement(conn: DB) -> list[dict]:
    """Return all recommendations with settlement status and units for analytics."""
    cur = conn.execute("""
        SELECT hr.*, ms.settlement_status, ms.final_stat_value,
               bu.risk_units, bu.profit_units, bu.return_units,
               cp.clv_probability, cp.clv_available, cp.line_move_type
        FROM historical_recommendations hr
        LEFT JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id
        LEFT JOIN bet_units bu ON hr.recommendation_id = bu.recommendation_id
        LEFT JOIN closing_prices cp ON hr.recommendation_id = cp.recommendation_id
        ORDER BY hr.created_at
    """)
    return [dict(row) for row in cur.fetchall()]


def save_learning_recommendation(conn: DB, rec: dict) -> None:
    """Persist a learning recommendation (advisory only)."""
    ci = rec.get("confidence_interval")
    conn.execute("""
        INSERT INTO learning_recommendations
        (recommendation_id, category, proposed_change, current_value,
         proposed_value, reason, sample_size, historical_roi_diff,
         historical_clv_diff, confidence_low, confidence_high,
         expected_volume, overfitting_risk, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (recommendation_id) DO NOTHING
    """, (
        rec.get("recommendation_id"),
        rec.get("category"),
        rec.get("proposed_change"),
        str(rec.get("current_value", "")),
        str(rec.get("proposed_value", "")),
        rec.get("reason"),
        rec.get("sample_size", 0),
        rec.get("historical_roi_diff", 0.0),
        rec.get("historical_clv_diff", 0.0),
        ci[0] if ci else None,
        ci[1] if ci else None,
        rec.get("expected_volume_effect"),
        rec.get("overfitting_risk"),
        rec.get("status", "INSUFFICIENT_DATA"),
        rec.get("created_at"),
    ))
    conn.commit()


def get_learning_recommendations(
    conn: DB,
    status: str | None = None,
) -> list[dict]:
    """Retrieve learning recommendations, optionally filtered by status."""
    if status:
        rows = conn.execute(
            "SELECT * FROM learning_recommendations WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learning_recommendations ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_experiments(conn: DB, limit: int = 20) -> list[dict]:
    """Retrieve recent experiments."""
    rows = conn.execute(
        "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_config_version(conn: DB) -> dict | None:
    """Get the currently active configuration version."""
    row = conn.execute(
        "SELECT * FROM config_versions WHERE deactivated_at = '' ORDER BY activated_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
