"""Tests for the verified MLB StatsAPI result adapter."""

from datetime import datetime, timezone

from src.mlb_results import MLBStatsClient, extract_stat_fact, ingest_results_for_recommendations


def _feed():
    return {
        "gameData": {"status": {"abstractGameState": "Final"}},
        "liveData": {
            "decisions": {"winner": {"id": 123}},
            "boxscore": {"teams": {
                "away": {"teamStats": {"batting": {"runs": 3}}, "players": {
                    "ID123": {"person": {"id": 123, "fullName": "Test Pitcher"},
                              "stats": {"pitching": {"strikeOuts": 7, "wins": 1}}},
                }},
                "home": {"teamStats": {"batting": {"runs": 2}}, "players": {}},
            }},
        },
    }


class FakeClient(MLBStatsClient):
    def fetch_schedule(self, date_value):
        return [{
            "gamePk": 99, "gameDate": "2026-08-06T19:00:00Z",
            "teams": {"away": {"team": {"name": "Away Team"}},
                      "home": {"team": {"name": "Home Team"}}},
        }]

    def fetch_game_feed(self, game_pk):
        return _feed()


def test_extracts_verified_pitching_stat_and_win():
    rec = {"player_name": "Test Pitcher", "market_type": "pitching_strikeouts_ou"}
    assert extract_stat_fact(_feed(), rec)["value"] == 7
    win = extract_stat_fact(_feed(), {"player_name": "Test Pitcher", "market_type": "pitching_win_yn"})
    assert win["value"] == 1


def test_extracts_verified_atomic_batter_and_pitcher_fields():
    feed = _feed()
    feed["liveData"]["boxscore"]["teams"]["home"]["players"]["ID456"] = {
        "person": {"id": 456, "fullName": "Test Batter"},
        "stats": {"batting": {
            "hits": 2, "doubles": 1, "triples": 0,
            "homeRuns": 0, "rbi": 3,
        }},
    }
    assert extract_stat_fact(feed, {
        "player_name": "Test Batter", "market_type": "batting_RBI_ou",
    })["value"] == 3
    assert extract_stat_fact(feed, {
        "player_name": "Test Batter", "market_type": "batting_singles_ou",
    })["value"] == 1


def test_missing_player_stats_are_unresolved():
    rec = {"player_name": "Missing", "market_type": "pitching_strikeouts_ou"}
    assert extract_stat_fact(_feed(), rec) is None


def test_ingestion_persists_final_fact(db_conn):
    from tests.test_automatic_grading import _seed
    _seed(db_conn, "result-1")
    db_conn.execute("""CREATE TABLE IF NOT EXISTS event_results (
        event_id TEXT PRIMARY KEY, final_status TEXT, away_score INTEGER,
        home_score INTEGER, result_source TEXT, source_observed_at TEXT,
        result_detail TEXT, updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db_conn.execute("UPDATE historical_recommendations SET matchup = ?, event_start_time = ? WHERE recommendation_id = ?",
                    ("Away Team @ Home Team", "2026-08-06T19:00:00Z", "result-1"))
    db_conn.commit()
    result = ingest_results_for_recommendations(
        db_conn,
        [{"recommendation_id": "result-1", "event_id": "E-AUTO", "player_id": "P-AUTO",
          "player_name": "Test Pitcher", "market_type": "pitching_strikeouts", "event_start_time": "2026-08-06T19:00:00Z",
          "matchup": "Away Team @ Home Team"}],
        client=FakeClient(),
    )
    assert result["facts_saved"] == 1
    row = db_conn.execute("SELECT final_stat_value, result_source FROM player_stat_results").fetchone()
    assert row[0] == 7
    assert row[1] == "MLB StatsAPI"


def test_ingestion_reports_unsupported_market_reason(db_conn):
    result = ingest_results_for_recommendations(
        db_conn,
        [{"market_type": "batting_hits+runs+rbi_ou"}],
        client=FakeClient(),
    )
    assert result["unresolved_reasons"]["unsupported_or_research_market"] == 1


def test_ingestion_persists_postponed_game_as_void_status(db_conn):
    """A postponed game must not stay UNRESOLVED forever — its status
    should be persisted (detailedState) so src/game_settlement.py can
    void it, instead of the recommendation waiting forever."""
    db_conn.execute("""CREATE TABLE IF NOT EXISTS event_results (
        event_id TEXT PRIMARY KEY, final_status TEXT, away_score INTEGER,
        home_score INTEGER, result_source TEXT, source_observed_at TEXT,
        result_detail TEXT, updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db_conn.commit()

    class PostponedClient(FakeClient):
        def fetch_game_feed(self, game_pk):
            return {"gameData": {"status": {
                "abstractGameState": "Preview", "detailedState": "Postponed",
            }}}

    result = ingest_results_for_recommendations(
        db_conn,
        [{"event_id": "E-POSTPONED", "player_id": "P-AUTO",
          "player_name": "Test Pitcher", "market_type": "pitching_strikeouts",
          "event_start_time": "2026-08-06T19:00:00Z",
          "matchup": "Away Team @ Home Team"}],
        client=PostponedClient(),
    )
    assert result["unresolved_reasons"]["game_not_final"] == 0
    row = db_conn.execute(
        "SELECT final_status FROM event_results WHERE event_id = ?", ("E-POSTPONED",),
    ).fetchone()
    assert row["final_status"] == "POSTPONED"
