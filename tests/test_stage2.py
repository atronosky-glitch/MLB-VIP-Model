"""Verify Stage 2: odds parsing and database storage."""

import sys
import copy
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.odds_parser import parse_odds, parse_odd_id_components
from database.db_manager import save_odds_batch
from tests.fixture_data import tb_tor_event as _tb_tor_event


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def synthetic_events(all_events):
    """Use the shared deterministic event fixtures instead of API cache data."""
    return all_events


@pytest.fixture
def first_event():
    """Provide a deterministic full-game event for Stage 2 market checks."""
    event = copy.deepcopy(_tb_tor_event)
    event["odds"].update({
        "points-away-game-sp-away": {
            "statEntityID": "away",
            "marketName": "Tampa Bay Rays Spread",
            "sideID": "away",
            "opposingOddID": "points-home-game-sp-home",
            "byBookmaker": {
                "fanduel": {
                    "odds": "-110",
                    "spread": -1.5,
                    "altLines": [{"odds": "-105", "spread": -2.5}],
                },
            },
        },
        "points-home-game-sp-home": {
            "statEntityID": "home",
            "marketName": "Toronto Blue Jays Spread",
            "sideID": "home",
            "opposingOddID": "points-away-game-sp-away",
            "byBookmaker": {
                "fanduel": {"odds": "-110", "spread": 1.5},
            },
        },
    })
    return event


# ── oddID parsing tests ──────────────────────────────────────────


def test_parse_basic_odd_id():
    comps = parse_odd_id_components("points-away-game-ml-away")
    assert comps["stat_id"] == "points"
    assert comps["entity_id"] == "away"
    assert comps["period_id"] == "game"
    assert comps["bet_type"] == "ml"
    assert comps["side"] == "away"


def test_parse_player_prop_odd_id():
    comps = parse_odd_id_components(
        "batting_hits-KYLE_HIGASHIOKA_1_MLB-game-ou-over"
    )
    assert comps["stat_id"] == "batting_hits"
    assert comps["entity_id"] == "KYLE_HIGASHIOKA_1_MLB"
    assert comps["period_id"] == "game"
    assert comps["bet_type"] == "ou"
    assert comps["side"] == "over"


def test_parse_pitcher_prop_odd_id():
    comps = parse_odd_id_components(
        "pitching_strikeouts-JOE_RYAN_1_MLB-game-ou-over"
    )
    assert comps["stat_id"] == "pitching_strikeouts"
    assert comps["entity_id"] == "JOE_RYAN_1_MLB"
    assert comps["bet_type"] == "ou"
    assert comps["side"] == "over"


def test_parse_inning_market():
    comps = parse_odd_id_components("points-away-1i-sp-away")
    assert comps["period_id"] == "1i"
    assert comps["bet_type"] == "sp"
    assert comps["side"] == "away"


def test_parse_empty_string():
    comps = parse_odd_id_components("")
    assert comps["stat_id"] is None


def test_parse_malformed():
    comps = parse_odd_id_components("too-short")
    assert comps["stat_id"] is None


# ── Odds parsing tests ───────────────────────────────────────────


def _odds_rows(synthetic_events, index=0):
    result = parse_odds(synthetic_events[index])
    return result.odds_rows


def test_every_event_produces_odds(synthetic_events):
    """Every event should produce at least some odds rows."""
    for event in synthetic_events:
        result = parse_odds(event)
        assert len(result.odds_rows) > 0, f"Event {event.get('eventID')} produced 0 odds rows"


def test_moneyline_odds_present(first_event):
    """Moneyline odds exist for both home and away."""
    rows = parse_odds(first_event).odds_rows
    ml_rows = [r for r in rows if "-ml-" in r["market"]]
    assert len(ml_rows) >= 2, "Expected at least 2 moneyline odds entries (home + away)"

    # Should have both home and away moneyline for several books
    books = {r["sportsbook"] for r in ml_rows}
    assert len(books) >= 5, f"Expected >=5 books on moneyline, got {books}"


def test_spread_odds_have_point_values(first_event):
    """Spread odds should include a points (line) value."""
    rows = parse_odds(first_event).odds_rows
    sp_rows = [r for r in rows if "-sp-" in r["market"] and r["is_alt_line"] == 0]
    main_sp_rows = [r for r in sp_rows if r["points"] is not None]
    assert len(main_sp_rows) > 0, "Main spread odds missing point values"


def test_total_odds_have_point_values(first_event):
    """Over/under odds should include a points (total) value."""
    rows = parse_odds(first_event).odds_rows
    ou_rows = [r for r in rows if "-ou-" in r["market"] and r["is_alt_line"] == 0]
    main_ou_rows = [r for r in ou_rows if r["points"] is not None]
    assert len(main_ou_rows) > 0, "Over/under odds missing point values"


def test_alt_lines_are_marked(first_event):
    """Alternate lines should have is_alt_line = 1."""
    rows = parse_odds(first_event).odds_rows
    alt_rows = [r for r in rows if r["is_alt_line"] == 1]
    assert len(alt_rows) > 0, "No alternate lines found"


def test_max_nine_sportsbooks_per_game(first_event):
    """Each game should have odds from at most 9 sportsbooks (free tier)."""
    rows = parse_odds(first_event).odds_rows
    books = {r["sportsbook"] for r in rows}
    assert len(books) <= 9, f"Expected <=9 books, got {len(books)}: {books}"


def test_all_odds_have_sane_prices(first_event):
    """All odds prices should be non-zero integers (American odds)."""
    rows = parse_odds(first_event).odds_rows
    for r in rows:
        assert isinstance(r["price"], int | float), f"Price not numeric: {r}"
        assert r["price"] != 0, f"Zero price: {r}"


# ── Database integration tests (in-memory) ────────────────────────


def _setup_db():
    """Create an in-memory database with all tables."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE games (event_id TEXT PRIMARY KEY, league TEXT, away_team TEXT, home_team TEXT, status TEXT);

        CREATE TABLE odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            market TEXT NOT NULL,
            selection TEXT,
            price REAL,
            points REAL,
            is_alt_line INTEGER NOT NULL DEFAULT 0,
            available INTEGER NOT NULL DEFAULT 1,
            pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
            odd_id TEXT DEFAULT '',
            validation_status TEXT DEFAULT 'VALID',
            mapping_confidence TEXT DEFAULT 'NONE',
            mapping_method TEXT DEFAULT '',
            validation_reason TEXT DEFAULT ''
        );

        CREATE TABLE odds_mapping_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            odd_id TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            raw_participant_id TEXT,
            raw_participant_name TEXT,
            matched_team_id TEXT,
            matched_team_name TEXT,
            mapping_method TEXT,
            mapping_confidence TEXT,
            validation_status TEXT,
            validation_reason TEXT,
            price REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    return conn


def test_bulk_insert_and_query():
    """Bulk insert rows and verify count."""
    conn = _setup_db()
    conn.execute(
        "INSERT INTO games (event_id, league, away_team, home_team, status) "
        "VALUES ('TEST_EVENT_1', 'MLB', 'Test A', 'Test B', 'scheduled')"
    )
    conn.commit()

    test_rows = [
        {
            "event_id": "TEST_EVENT_1",
            "sportsbook": "testbook",
            "market": "points-away-game-ml-away",
            "selection": "points-away-game-ml-away",
            "price": -110,
            "points": None,
            "is_alt_line": 0,
            "available": 1,
            "odd_id": "points-away-game-ml-away",
            "validation_status": "VALID",
            "mapping_confidence": "HIGH",
            "mapping_method": "statEntityID",
            "validation_reason": "",
        },
        {
            "event_id": "TEST_EVENT_1",
            "sportsbook": "testbook",
            "market": "points-home-game-ml-home",
            "selection": "points-home-game-ml-home",
            "price": +120,
            "points": None,
            "is_alt_line": 0,
            "available": 1,
            "odd_id": "points-home-game-ml-home",
            "validation_status": "VALID",
            "mapping_confidence": "HIGH",
            "mapping_method": "statEntityID",
            "validation_reason": "",
        },
    ]
    cnt = save_odds_batch(conn, test_rows)
    assert cnt == 2

    # Query back
    rows = conn.execute(
        "SELECT * FROM odds WHERE event_id = 'TEST_EVENT_1'"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["sportsbook"] == "testbook"
    assert rows[0]["price"] == -110
    conn.close()


def test_save_odds_batch_missing_required_columns():
    """save_odds_batch must reject rows missing required validation fields."""
    conn = _setup_db()
    conn.execute(
        "INSERT INTO games (event_id) VALUES ('TEST_MISSING')"
    )
    conn.commit()

    bad_rows = [
        {
            "event_id": "TEST_MISSING",
            "sportsbook": "testbook",
            "market": "points-away-game-ml-away",
            "selection": "points-away-game-ml-away",
            "price": -110,
            "points": None,
            "is_alt_line": 0,
            "available": 1,
            # missing: odd_id, validation_status, etc.
        },
    ]
    with pytest.raises(ValueError):
        save_odds_batch(conn, bad_rows)
    conn.close()
