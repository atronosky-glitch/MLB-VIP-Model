"""Phase 19A recommendation lifecycle and CLV evidence tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from database.connection import DB
from database.db_manager import (
    capture_closing_prices,
    record_grading_completed,
    record_lifecycle_event,
    record_recommendation_created,
)
from src.grading import calculate_clv


def _recommendation() -> dict:
    return {
        "recommendation_id": "rec-19a",
        "scan_run_id": "run-19a",
        "event_id": "event-19a",
        "player_id": "player-19a",
        "player_name": "Test Pitcher",
        "market_type": "pitching_strikeouts_ou",
        "side": "Over",
        "line": 6.5,
        "sportsbook": "draftkings",
        "offered_american_odds": -110,
        "offered_decimal_odds": 1.9091,
        "offered_implied_prob": 0.52381,
        "fair_prob": 0.56,
        "ev_pct": 6.91,
        "model_score": 7.4,
        "market_quality_score": 8.1,
        "pinnacle_reference_used": True,
        "pinnacle_book": "pinnacle",
        "pinnacle_line": 6.5,
        "pinnacle_over_price": -120,
        "pinnacle_under_price": 100,
        "pinnacle_fair_prob": 0.54545,
        "pinnacle_ev": 3.09,
        "pinnacle_prob_edge": 2.16,
        "data_source": "live",
        "observation_timestamp": "2026-08-04T12:00:00+00:00",
        "scan_timestamp": "2026-08-04T12:00:00+00:00",
    }


def _lifecycle_table(conn):
    conn.executescript(
        """
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
        """
    )


def test_creation_and_line_events_are_append_only(db_conn):
    _lifecycle_table(db_conn)
    rec = _recommendation()

    assert record_recommendation_created(db_conn, rec) == 2
    assert record_recommendation_created(db_conn, rec) == 0
    rows = db_conn.execute(
        "SELECT event_type, line, data_source, provenance_json "
        "FROM recommendation_lifecycle_events ORDER BY event_type"
    ).fetchall()
    assert [row["event_type"] for row in rows] == ["LINE_SNAPSHOT", "RECOMMENDATION_CREATED"]
    assert all(row["line"] == 6.5 for row in rows)
    assert all(row["data_source"] == "live" for row in rows)
    assert '"phase": "freeze"' in rows[0]["provenance_json"]

    # A repeated event key cannot overwrite the original evidence.
    assert record_lifecycle_event(
        db_conn,
        "LINE_SNAPSHOT",
        "line:rec-19a:creation",
        recommendation={**rec, "line": 7.5},
    ) is False
    assert db_conn.execute(
        "SELECT line FROM recommendation_lifecycle_events "
        "WHERE event_key = 'line:rec-19a:creation'"
    ).fetchone()["line"] == 6.5


def test_freeze_persists_quality_and_pinnacle_evidence(tmp_path):
    import database.db_manager as dbm

    db_path = tmp_path / "evidence.db"
    dbm.init_db(str(db_path))
    conn = dbm.get_connection(str(db_path))
    rec = _recommendation()
    rec.update({
        "market_form": "ou",
        "period": "game",
        "fair_american_odds": -127,
        "market_quality": "VALID_MARKET",
        "rec_status": "POSITIVE_EDGE",
        "rec_eligible": True,
        "freshness_status": "FRESH",
        "model_version": "v1",
        "scan_timestamp": "2026-08-04T12:00:00+00:00",
    })
    assert dbm.save_recommendation(conn, rec) == "rec-19a"
    row = conn.execute(
        "SELECT market_quality_score, pinnacle_found, pinnacle_reference_used, "
        "pinnacle_book, pinnacle_fair_prob, pinnacle_ev FROM historical_recommendations "
        "WHERE recommendation_id = ?", ("rec-19a",)
    ).fetchone()
    assert row["market_quality_score"] == 8.1
    assert row["pinnacle_found"] is None
    assert row["pinnacle_reference_used"] == 1
    assert row["pinnacle_book"] == "pinnacle"
    assert row["pinnacle_fair_prob"] == 0.54545
    assert row["pinnacle_ev"] == 3.09
    conn.close()


def test_clv_formula_and_closing_lifecycle_snapshot():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _lifecycle_table(conn)
    conn.executescript(
        """
        CREATE TABLE player_prop_odds (
            event_id TEXT, player_id TEXT, market_type TEXT, side TEXT,
            sportsbook TEXT, price INTEGER, decimal_odds REAL, line REAL,
            available INTEGER, captured_at TEXT
        );
        CREATE TABLE closing_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT, closing_american INTEGER,
            closing_decimal REAL, closing_implied_prob REAL, closing_line REAL,
            closing_observed_at TEXT, closing_sportsbook TEXT,
            line_move_type TEXT, clv_probability REAL, clv_price_diff INTEGER,
            clv_available INTEGER, line_movement_direction TEXT
        );
        """
    )
    rec = _recommendation()
    conn.execute(
        "INSERT INTO player_prop_odds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("event-19a", "player-19a", "pitching_strikeouts_ou", "over",
         "fanduel", -105, 1.9524, 6.5, 1, "2026-08-04T13:00:00+00:00"),
    )
    conn.commit()

    assert capture_closing_prices(
        conn, [rec], run_id="run-final", snapshot_kind="final"
    ) == 1
    assert capture_closing_prices(
        conn, [rec], run_id="run-final", snapshot_kind="final"
    ) == 0
    close = conn.execute("SELECT * FROM closing_prices").fetchone()
    event = conn.execute(
        "SELECT * FROM recommendation_lifecycle_events "
        "WHERE event_type = 'CLOSING_SNAPSHOT'"
    ).fetchone()
    expected = calculate_clv(-110, 6.5, -105, 6.5)
    assert close["clv_probability"] == expected["clv_probability"]
    assert close["clv_price_diff"] == expected["clv_price_diff"]
    assert event["clv_probability"] == expected["clv_probability"]
    assert event["closing_sportsbook"] == "fanduel"
    assert event["line_move_type"] == "same_line"
    assert event["closing_available"] == 1
    assert event["clv_available"] == 1
    assert event["source_run_id"] == "run-final"

    changed = {**rec, "recommendation_id": "rec-line-changed"}
    conn.execute(
        "INSERT INTO player_prop_odds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("event-19a", "player-19a", "pitching_strikeouts_ou", "over",
         "fanduel", -105, 1.9524, 7.0, 1, "2026-08-04T13:01:00+00:00"),
    )
    conn.commit()
    assert capture_closing_prices(conn, [changed], snapshot_kind="final") == 1
    changed_event = conn.execute(
        "SELECT line_move_type, closing_available, clv_probability, clv_available "
        "FROM recommendation_lifecycle_events WHERE recommendation_id = ?",
        ("rec-line-changed",),
    ).fetchone()
    assert changed_event["line_move_type"] == "line_changed"
    assert changed_event["closing_available"] == 1
    assert changed_event["clv_probability"] is None
    assert changed_event["clv_available"] == 0


def test_missing_closing_data_is_audited_without_fabricating_clv():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _lifecycle_table(conn)
    conn.executescript(
        """
        CREATE TABLE player_prop_odds (
            event_id TEXT, player_id TEXT, market_type TEXT, side TEXT,
            sportsbook TEXT, price INTEGER, decimal_odds REAL, line REAL,
            available INTEGER, captured_at TEXT
        );
        CREATE TABLE closing_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id TEXT, closing_american INTEGER,
            closing_decimal REAL, closing_implied_prob REAL, closing_line REAL,
            closing_observed_at TEXT, closing_sportsbook TEXT,
            line_move_type TEXT, clv_probability REAL, clv_price_diff INTEGER,
            clv_available INTEGER, line_movement_direction TEXT
        );
        """
    )
    rec = _recommendation()
    assert capture_closing_prices(conn, [rec], snapshot_kind="pregame") == 0
    event = conn.execute(
        "SELECT closing_american_odds, line_move_type, closing_available, "
        "clv_probability, clv_price_diff, clv_available, snapshot_kind "
        "FROM recommendation_lifecycle_events"
    ).fetchone()
    assert event["closing_american_odds"] is None
    assert event["line_move_type"] == "no_close"
    assert event["closing_available"] == 0
    assert event["clv_probability"] is None
    assert event["clv_price_diff"] is None
    assert event["clv_available"] == 0
    assert event["snapshot_kind"] == "pregame"


def test_settlement_and_grading_events_preserve_void_and_push_results(db_conn):
    _lifecycle_table(db_conn)
    rec = _recommendation()
    for result in ("PUSH", "VOID"):
        assert record_lifecycle_event(
            db_conn,
            "SETTLEMENT",
            f"settlement:rec-19a:{result}",
            recommendation=rec,
            result=result,
            settlement_reason="manual verified result",
            grader_version="grader_19a",
            final_stat_value=6.5,
        ) is True
        assert record_grading_completed(
            db_conn, rec, result, final_stat_value=6.5, grader_version="grader_19a"
        ) is True
        assert record_grading_completed(
            db_conn, rec, result, final_stat_value=6.5, grader_version="grader_19a"
        ) is False
    rows = db_conn.execute(
        "SELECT event_type, result, grader_version FROM recommendation_lifecycle_events "
        "WHERE result IN ('PUSH', 'VOID') ORDER BY event_type, result"
    ).fetchall()
    assert {(row["event_type"], row["result"]) for row in rows} == {
        ("GRADING_COMPLETED", "PUSH"),
        ("GRADING_COMPLETED", "VOID"),
        ("SETTLEMENT", "PUSH"),
        ("SETTLEMENT", "VOID"),
    }
    assert all(row["grader_version"] == "grader_19a" for row in rows)


class _FakeCursor:
    rowcount = 1

    def execute(self, sql, params):
        self.sql = sql
        self.params = params


class _FakeRawConnection:
    def __init__(self):
        self.cursor_obj = _FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def rollback(self):
        pass


def test_lifecycle_insert_uses_postgresql_placeholders():
    raw = _FakeRawConnection()
    conn = DB(raw, dialect="postgresql")
    assert record_lifecycle_event(
        conn,
        "RECOMMENDATION_CREATED",
        "created:postgres-rec",
        recommendation={**_recommendation(), "recommendation_id": "postgres-rec"},
    ) is True
    assert "%s" in raw.cursor_obj.sql
    assert "?" not in raw.cursor_obj.sql
    assert "ON CONFLICT (event_key) DO NOTHING" in raw.cursor_obj.sql
