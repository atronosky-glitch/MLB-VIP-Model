"""Tests for the verified NFL ESPN result adapter.

Fixtures are synthetic but shaped exactly like the real ESPN NFL
scoreboard/summary responses verified live 2026-08-19 against event
401873272 (CIN 16 - DET 14) — see src/nfl_results.py docstring.
"""

from src.nfl_results import (
    ESPNNFLClient,
    extract_stat_fact,
    ingest_results_for_recommendations,
    normalize_name,
)


def _summary():
    """Shaped like GET /summary?event=... — verified field names/positions."""
    return {
        "boxscore": {
            "players": [
                {
                    "team": {"displayName": "Detroit Lions"},
                    "statistics": [
                        {
                            "name": "passing",
                            "labels": ["C/ATT", "YDS", "AVG", "TD", "INT", "SACKS", "RTG"],
                            "athletes": [
                                {"athlete": {"id": "4597679", "displayName": "Luke Altmyer"},
                                 "stats": ["13/22", "130", "5.9", "1", "2", "1-12", "53.2"]},
                            ],
                        },
                        {
                            "name": "rushing",
                            "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
                            "athletes": [
                                {"athlete": {"id": "5001", "displayName": "Jacob Saylors"},
                                 "stats": ["8", "55", "6.9", "1", "27"]},
                            ],
                        },
                    ],
                },
                {
                    "team": {"displayName": "Cincinnati Bengals"},
                    "statistics": [
                        {
                            "name": "receiving",
                            "labels": ["REC", "YDS", "AVG", "TD", "LONG", "TGTS"],
                            "athletes": [
                                {"athlete": {"id": "6002", "displayName": "Test Receiver"},
                                 "stats": ["6", "88", "14.7", "1", "30", "9"]},
                            ],
                        },
                        {
                            "name": "kicking",
                            "labels": ["FG", "PCT", "LONG", "XP", "PTS"],
                            "athletes": [
                                {"athlete": {"id": "7003", "displayName": "Test Kicker"},
                                 "stats": ["2/3", "66.7", "48", "1/1", "7"]},
                            ],
                        },
                        {
                            "name": "kickReturns",
                            "labels": ["NO", "YDS", "AVG", "LONG", "TD"],
                            "athletes": [
                                {"athlete": {"id": "6002", "displayName": "Test Receiver"},
                                 "stats": ["1", "25", "25.0", "25", "1"]},
                            ],
                        },
                    ],
                },
            ],
        },
    }


def _scoreboard_event(completed=True):
    """Shaped like one entry in GET /scoreboard?dates=... events[]."""
    return {
        "id": "401873272",
        "date": "2026-08-13T23:00Z",
        "status": {"type": {"completed": completed}},
        "competitions": [{
            "competitors": [
                {"team": {"displayName": "Cincinnati Bengals"}, "homeAway": "home", "score": "16"},
                {"team": {"displayName": "Detroit Lions"}, "homeAway": "away", "score": "14"},
            ],
        }],
    }


class FakeClient(ESPNNFLClient):
    def __init__(self, scoreboard=None, summary=None):
        self._scoreboard = scoreboard if scoreboard is not None else [_scoreboard_event()]
        self._summary = summary if summary is not None else _summary()

    def fetch_scoreboard(self, date_value):
        return self._scoreboard

    def fetch_summary(self, event_id):
        return self._summary


def test_normalize_name():
    assert normalize_name("Luke  Altmyer Jr.") == "luke altmyer jr"


class TestExtractStatFact:
    def test_simple_stat_field(self):
        rec = {"player_name": "Luke Altmyer", "market_type": "passing_yards_ou"}
        fact = extract_stat_fact(_summary(), rec)
        assert fact["value"] == 130.0
        assert fact["player_id"] == "4597679"
        assert fact["source"] == "ESPN NFL"

    def test_passing_touchdowns_and_interceptions(self):
        summary = _summary()
        td = extract_stat_fact(summary, {"player_name": "Luke Altmyer", "market_type": "passing_touchdowns_ou"})
        interceptions = extract_stat_fact(summary, {"player_name": "Luke Altmyer", "market_type": "passing_interceptions_yn"})
        assert td["value"] == 1.0
        assert interceptions["value"] == 2.0

    def test_split_field_field_goals_made(self):
        rec = {"player_name": "Test Kicker", "market_type": "field_goals_made_ou"}
        fact = extract_stat_fact(_summary(), rec)
        assert fact["value"] == 2.0  # "2/3" -> made=2

    def test_anytime_touchdown_sums_across_categories(self):
        """Test Receiver scored 1 receiving TD + 1 kick-return TD = 2 total."""
        rec = {"player_name": "Test Receiver", "market_type": "anytime_touchdown_yn"}
        fact = extract_stat_fact(_summary(), rec)
        assert fact["value"] == 2.0

    def test_anytime_touchdown_zero_when_no_score(self):
        rec = {"player_name": "Jacob Saylors", "market_type": "anytime_touchdown_yn"}
        # Jacob Saylors only appears in "rushing" (TD=1), so this should be 1.
        fact = extract_stat_fact(_summary(), rec)
        assert fact["value"] == 1.0

    def test_missing_player_is_unresolved(self):
        rec = {"player_name": "Nobody Here", "market_type": "passing_yards_ou"}
        assert extract_stat_fact(_summary(), rec) is None

    def test_ambiguous_player_across_teams_is_unresolved(self):
        summary = _summary()
        # Duplicate the same display name onto a second team's athlete list.
        summary["boxscore"]["players"][1]["statistics"].append({
            "name": "passing",
            "labels": ["C/ATT", "YDS", "AVG", "TD", "INT", "SACKS", "RTG"],
            "athletes": [
                {"athlete": {"id": "9999", "displayName": "Luke Altmyer"},
                 "stats": ["1/1", "10", "10.0", "0", "0", "0-0", "80.0"]},
            ],
        })
        rec = {"player_name": "Luke Altmyer", "market_type": "passing_yards_ou"}
        assert extract_stat_fact(summary, rec) is None

    def test_unsupported_market_returns_none(self):
        rec = {"player_name": "Luke Altmyer", "market_type": "game_moneyline"}
        assert extract_stat_fact(_summary(), rec) is None


def _create_result_tables(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS event_results (
        event_id TEXT PRIMARY KEY, final_status TEXT, away_score INTEGER,
        home_score INTEGER, result_source TEXT, source_observed_at TEXT,
        result_detail TEXT, updated_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS player_stat_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL, player_id TEXT NOT NULL,
        player_name TEXT, market_type TEXT NOT NULL,
        final_stat_value REAL, result_source TEXT,
        source_observed_at TEXT, result_status TEXT NOT NULL DEFAULT 'UNRESOLVED',
        result_detail TEXT, created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(event_id, player_id, market_type)
    )""")
    conn.commit()


class TestIngestion:
    def test_ingestion_persists_final_facts_and_event_result(self, db_conn):
        _create_result_tables(db_conn)

        result = ingest_results_for_recommendations(
            db_conn,
            [{
                "event_id": "nfl-evt-1", "player_id": "ESPN-UNRESOLVED",
                "player_name": "Luke Altmyer", "market_type": "passing_yards_ou",
                "away_team": "Detroit Lions", "home_team": "Cincinnati Bengals",
                "event_start_time": "2026-08-13T23:00:00Z",
            }],
            client=FakeClient(),
        )
        assert result["facts_saved"] == 1
        assert result["games_final"] == 1

        stat_row = db_conn.execute(
            "SELECT final_stat_value, result_source FROM player_stat_results"
        ).fetchone()
        assert stat_row[0] == 130.0
        assert stat_row[1] == "ESPN NFL"

        event_row = db_conn.execute(
            "SELECT away_score, home_score, final_status FROM event_results WHERE event_id = ?",
            ("nfl-evt-1",),
        ).fetchone()
        assert event_row[0] == 14
        assert event_row[1] == 16
        assert event_row[2] == "FINAL"

    def test_ingestion_reports_unsupported_market(self, db_conn):
        result = ingest_results_for_recommendations(
            db_conn, [{"market_type": "defense_sacks_ou"}], client=FakeClient(),
        )
        assert result["unresolved_reasons"]["unsupported_or_research_market"] == 1

    def test_ingestion_reports_game_not_final(self, db_conn):
        _create_result_tables(db_conn)
        result = ingest_results_for_recommendations(
            db_conn,
            [{
                "event_id": "nfl-evt-2", "player_id": "P", "player_name": "Luke Altmyer",
                "market_type": "passing_yards_ou", "away_team": "Detroit Lions",
                "home_team": "Cincinnati Bengals", "event_start_time": "2026-08-13T23:00:00Z",
            }],
            client=FakeClient(scoreboard=[_scoreboard_event(completed=False)]),
        )
        assert result["unresolved_reasons"]["game_not_final"] == 1
        assert result["facts_saved"] == 0

    def test_ingestion_persists_postponed_game_as_void_status(self, db_conn):
        """A postponed game must not stay UNRESOLVED forever — its status
        should be persisted so src/game_settlement.py can void it."""
        _create_result_tables(db_conn)
        postponed_event = _scoreboard_event(completed=False)
        postponed_event["status"] = {"type": {"completed": False, "name": "STATUS_POSTPONED"}}
        result = ingest_results_for_recommendations(
            db_conn,
            [{
                "event_id": "nfl-evt-postponed", "player_id": "P", "player_name": "Luke Altmyer",
                "market_type": "passing_yards_ou", "away_team": "Detroit Lions",
                "home_team": "Cincinnati Bengals", "event_start_time": "2026-08-13T23:00:00Z",
            }],
            client=FakeClient(scoreboard=[postponed_event]),
        )
        assert result["unresolved_reasons"]["game_not_final"] == 0
        event_row = db_conn.execute(
            "SELECT final_status FROM event_results WHERE event_id = ?",
            ("nfl-evt-postponed",),
        ).fetchone()
        assert event_row["final_status"] == "STATUS_POSTPONED"

    def test_ingestion_reports_game_matching_failure(self, db_conn):
        result = ingest_results_for_recommendations(
            db_conn,
            [{
                "event_id": "nfl-evt-3", "player_id": "P", "player_name": "Luke Altmyer",
                "market_type": "passing_yards_ou", "away_team": "Nonexistent Team",
                "home_team": "Also Nonexistent", "event_start_time": "2026-08-13T23:00:00Z",
            }],
            client=FakeClient(),
        )
        assert result["unresolved_reasons"]["game_matching_failure"] == 1
