"""Tests for the multi-league schema migration (league/sport columns).

Covers:
- Migration is idempotent and preserves existing data on a database that
  predates the league/sport columns (simulates a real production upgrade).
- save_recommendation() actually persists an explicit league/sport instead
  of silently falling back to the column default.
- freeze_official_pick() copies league/sport from the source recommendation.
"""

from __future__ import annotations

import sqlite3

import database.db_manager as dbm
from database.db_manager import (
    freeze_official_pick,
    get_connection,
    init_db,
    save_recommendation,
)


def _make_rec(**overrides) -> dict:
    rec = {
        "event_id": "nfl-evt-1",
        "player_id": "JARED_GOFF_1_NFL",
        "player_name": "Jared Goff",
        "market_type": "passing_yards_ou",
        "side": "OVER",
        "sportsbook": "draftkings",
        "offered_american_odds": -110,
        "offered_decimal_odds": 1.909,
        "offered_implied_prob": 0.524,
        "rec_status": "BET",
        "scan_timestamp": "2026-09-07T12:00:00Z",
        "league": "NFL",
        "sport": "football",
    }
    rec.update(overrides)
    return rec


class TestMigrationIdempotentAndSafe:
    def test_upgrading_a_pre_migration_database_preserves_data(self, tmp_path):
        """Simulates a real production database created before this migration:
        the full current schema, minus the new league/sport columns, with a
        real row already in it (SQLite 3.35+ supports DROP COLUMN, so this
        is a more realistic pre-migration snapshot than hand-authoring a
        second copy of the whole schema)."""
        db_path = tmp_path / "pre_migration.db"
        init_db(str(db_path))
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO historical_recommendations (recommendation_id, fingerprint, "
            "event_id, player_id, market_type, market_form, period, side, sportsbook, "
            "offered_american_odds, offered_decimal_odds, offered_implied_prob, "
            "rec_status, scan_timestamp) "
            "VALUES ('pre-mig-1', 'fp-1', 'ev-1', 'p-1', 'pitching_strikeouts_ou', 'ou', 'game', "
            "'OVER', 'draftkings', -110, 1.909, 0.524, 'BET', '2026-01-01T00:00:00Z')"
        )
        conn.execute("ALTER TABLE historical_recommendations DROP COLUMN league")
        conn.execute("ALTER TABLE historical_recommendations DROP COLUMN sport")
        conn.commit()
        conn.close()

        # Running the real migration must not fail, and must not lose the row.
        init_db(str(db_path))

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT recommendation_id, league, sport FROM historical_recommendations "
            "WHERE recommendation_id = 'pre-mig-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["recommendation_id"] == "pre-mig-1"
        # Pre-existing rows default to MLB/baseball — this project was
        # MLB-only before this migration, so that's the correct backfill.
        assert row["league"] == "MLB"
        assert row["sport"] == "baseball"

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent.db"
        init_db(str(db_path))
        init_db(str(db_path))  # must not raise on a second run
        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(historical_recommendations)")}
        conn.close()
        assert "league" in cols
        assert "sport" in cols


class TestSaveRecommendationPersistsLeague:
    def test_explicit_league_and_sport_are_persisted(self, tmp_path):
        db_path = tmp_path / "nfl_rec.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec_id = save_recommendation(conn, _make_rec())
        assert rec_id is not None

        row = conn.execute(
            "SELECT league, sport, market_type FROM historical_recommendations WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["league"] == "NFL"
        assert row["sport"] == "football"
        assert row["market_type"] == "passing_yards_ou"

    def test_omitted_league_defaults_to_mlb(self, tmp_path):
        """Every existing MLB call site omits league/sport — must keep working."""
        db_path = tmp_path / "mlb_default.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec = _make_rec(event_id="mlb-evt-1", market_type="pitching_strikeouts_ou")
        del rec["league"]
        del rec["sport"]
        rec_id = save_recommendation(conn, rec)
        assert rec_id is not None

        row = conn.execute(
            "SELECT league, sport FROM historical_recommendations WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["league"] == "MLB"
        assert row["sport"] == "baseball"


class TestFreezeOfficialPickCopiesLeague:
    def test_freeze_copies_league_and_sport_from_recommendation(self, tmp_path):
        db_path = tmp_path / "freeze_nfl.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec_id = save_recommendation(conn, _make_rec())
        assert rec_id is not None

        assert freeze_official_pick(conn, rec_id, tier="OFFICIAL_TRACKED", official_rank=1) is True

        row = conn.execute(
            "SELECT league, sport FROM official_picks WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["league"] == "NFL"
        assert row["sport"] == "football"

    def test_freeze_with_no_matching_recommendation_still_inserts(self, tmp_path):
        """freeze_official_pick has never required the recommendation to
        exist first (some callers freeze before the row is visible in this
        connection); it must keep working and default sensibly."""
        db_path = tmp_path / "freeze_orphan.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        assert freeze_official_pick(conn, "no-such-rec", tier="OFFICIAL_TRACKED") is True
        row = conn.execute(
            "SELECT league, sport FROM official_picks WHERE recommendation_id = 'no-such-rec'"
        ).fetchone()
        assert row["league"] == "MLB"
        assert row["sport"] == "baseball"
