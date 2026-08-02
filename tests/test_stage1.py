"""Verify Stage 1: project structure, API connectivity, and database."""

import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_project_structure():
    """All required directories and files exist."""
    required_dirs = ["database", "scripts", "src", "tests"]
    for d in required_dirs:
        assert (PROJECT_ROOT / d).is_dir(), f"Missing directory: {d}"

    required_files = ["main.py", "requirements.txt", ".env.example", ".gitignore"]
    for f in required_files:
        assert (PROJECT_ROOT / f).is_file(), f"Missing file: {f}"


def test_env_file_has_api_key():
    """The environment template documents the required API key variable."""
    env_path = PROJECT_ROOT / ".env.example"
    content = env_path.read_text(encoding="utf-8")
    assert "SPORTSODDS_API_KEY" in content
    assert "SPORTSODDS_API_KEY=your_api_key_here" in content


def test_database_initialised(db_conn):
    """Database has expected tables after init."""
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]

    expected = {"games", "odds", "raw_responses", "data_pulls", "bet_results",
                "odds_mapping_audit", "player_prop_odds", "player_prop_mapping_audit"}
    for t in expected:
        assert t in tables, f"Missing table: {t}"


def test_db_init_idempotent(db_conn):
    """Running init_db multiple times must not fail."""
    # Already initialised via db_conn fixture.  Run it twice more.
    from database.db_manager import init_db
    init_db()
    init_db()
    # Still queryable
    cursor = db_conn.execute("SELECT COUNT(*) FROM games")
    assert cursor.fetchone()[0] == 0


def test_games_stored(db_conn):
    """Can insert and retrieve a game from the database."""
    import sqlite3
    db_conn.execute(
        "INSERT INTO games (event_id, league, away_team, home_team, status) "
        "VALUES ('test_ev', 'MLB', 'Away', 'Home', 'scheduled')"
    )
    db_conn.commit()
    count = db_conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert count == 1


def test_raw_response_stored(db_conn):
    """Can store and retrieve a raw API response."""
    import json
    db_conn.execute(
        "INSERT INTO raw_responses (endpoint, params, response_json) VALUES (?, ?, ?)",
        ("events", None, json.dumps({"test": "data"})),
    )
    db_conn.commit()
    count = db_conn.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
    assert count == 1


def test_api_response_structure(all_events):
    """Verify the deterministic event fixture has the API event shape."""
    assert isinstance(all_events, list)
    assert all_events

    event = all_events[0]
    assert "eventID" in event
    assert "teams" in event
    assert "home" in event["teams"]
    assert "away" in event["teams"]
    assert "odds" in event
