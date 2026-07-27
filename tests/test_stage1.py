"""Verify Stage 1: project structure, API connectivity, and database."""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_project_structure():
    """All required directories and files exist."""
    required_dirs = ["database", "scripts", "logs", "src", "tests"]
    for d in required_dirs:
        assert (PROJECT_ROOT / d).is_dir(), f"Missing directory: {d}"

    required_files = ["main.py", "requirements.txt", ".env", ".gitignore"]
    for f in required_files:
        assert (PROJECT_ROOT / f).is_file(), f"Missing file: {f}"


def test_env_file_has_api_key():
    """.env contains the API key."""
    env_path = PROJECT_ROOT / ".env"
    content = env_path.read_text(encoding="utf-8")
    assert "SPORTSODDS_API_KEY" in content
    assert "c97f504cbfdb901a9ba011d5e60c1ca4" in content


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


def test_api_response_structure():
    """Verify the cached API response has the expected fields."""
    import json
    cache_dir = PROJECT_ROOT / "data" / "_api_cache"
    # Find the events cache file specifically
    events_files = [f for f in cache_dir.glob("*.json") if "events" in f.name]
    assert len(events_files) > 0, "No cached events API response"
    # Might also have account/usage cache — pick the events one
    cache_file = events_files[0]

    with open(cache_file, encoding="utf-8") as f:
        data = json.load(f)

    assert "success" in data
    assert data["success"] is True
    assert "data" in data
    events_list = data["data"]
    assert isinstance(events_list, list)
    assert len(events_list) > 0

    event = events_list[0]
    assert "eventID" in event
    assert "teams" in event
    assert "home" in event["teams"]
    assert "away" in event["teams"]
    assert "odds" in event
