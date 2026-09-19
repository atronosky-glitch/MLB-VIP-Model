"""Tests for the verified CFB ESPN result adapter.

Fixtures are synthetic but shaped exactly like the real ESPN
college-football scoreboard response verified live 2026-09-19 (same
schema as ESPN NFL's — see src/cfb_results.py docstring).

Much smaller than tests/test_nfl_results.py: CFB has no player props
(see src/sports/cfb.py), so there's no boxscore/athlete-stat extraction
to test here at all -- only event/score matching and event_results
persistence.
"""

from src.cfb_results import ESPNCFBClient, ingest_results_for_recommendations, normalize_name


def _scoreboard_event(completed=True):
    """Shaped like one entry in GET /scoreboard?dates=... events[]."""
    return {
        "id": "401756876",
        "date": "2026-09-19T23:30Z",
        "status": {"type": {"completed": completed, "name": "STATUS_FINAL"}},
        "competitions": [{
            "competitors": [
                {"team": {"displayName": "Toledo Rockets"}, "homeAway": "home", "score": "31"},
                {"team": {"displayName": "Temple Owls"}, "homeAway": "away", "score": "17"},
            ],
        }],
    }


class FakeClient(ESPNCFBClient):
    def __init__(self, scoreboard=None):
        self._scoreboard = scoreboard if scoreboard is not None else [_scoreboard_event()]

    def fetch_scoreboard(self, date_value):
        return self._scoreboard


def test_normalize_name():
    assert normalize_name("Ohio State  Buckeyes") == "ohio state buckeyes"


def _rec(**overrides):
    row = {
        "event_id": "cfb-evt-1", "market_type": "game_moneyline", "side": "HOME",
        "away_team": "Temple Owls", "home_team": "Toledo Rockets",
        "event_start_time": "2026-09-19T23:30:00Z",
    }
    row.update(overrides)
    return row


class TestIngestion:
    def test_ingestion_persists_final_event_result(self, db_conn):
        result = ingest_results_for_recommendations(db_conn, [_rec()], client=FakeClient())
        assert result["games_final"] == 1
        row = db_conn.execute(
            "SELECT * FROM event_results WHERE event_id = 'cfb-evt-1'"
        ).fetchone()
        assert row["final_status"] == "FINAL"
        assert row["away_score"] == 17
        assert row["home_score"] == 31
        assert row["result_source"] == "ESPN CFB"

    def test_game_not_final_is_unresolved(self, db_conn):
        result = ingest_results_for_recommendations(
            db_conn, [_rec()], client=FakeClient(scoreboard=[_scoreboard_event(completed=False)]),
        )
        assert result["unresolved_reasons"]["game_not_final"] == 1
        assert db_conn.execute("SELECT * FROM event_results WHERE event_id = 'cfb-evt-1'").fetchone() is None

    def test_postponed_game_is_voided(self, db_conn):
        postponed = _scoreboard_event(completed=False)
        postponed["status"]["type"]["name"] = "STATUS_POSTPONED"
        result = ingest_results_for_recommendations(db_conn, [_rec()], client=FakeClient(scoreboard=[postponed]))
        assert result["games_final"] == 1
        row = db_conn.execute("SELECT * FROM event_results WHERE event_id = 'cfb-evt-1'").fetchone()
        assert row["final_status"] == "STATUS_POSTPONED"

    def test_missing_start_time_is_unresolved(self, db_conn):
        result = ingest_results_for_recommendations(db_conn, [_rec(event_start_time=None)], client=FakeClient())
        assert result["unresolved_reasons"]["missing_start_time"] == 1

    def test_missing_matchup_is_unresolved(self, db_conn):
        result = ingest_results_for_recommendations(
            db_conn, [_rec(away_team="", home_team="", matchup="")], client=FakeClient(),
        )
        assert result["unresolved_reasons"]["missing_matchup"] == 1

    def test_no_scoreboard_match_is_unresolved(self, db_conn):
        result = ingest_results_for_recommendations(
            db_conn, [_rec(away_team="Nobody", home_team="Nowhere")], client=FakeClient(),
        )
        assert result["unresolved_reasons"]["game_matching_failure"] == 1

    def test_scoreboard_fetch_error_is_recorded(self, db_conn):
        class ErrorClient(ESPNCFBClient):
            def fetch_scoreboard(self, date_value):
                raise RuntimeError("boom")

        result = ingest_results_for_recommendations(db_conn, [_rec()], client=ErrorClient())
        assert result["errors"] == 1
        assert result["unresolved_reasons"]["scoreboard_fetch_error"] == 1

    def test_matchup_fallback_when_away_home_fields_missing(self, db_conn):
        """away_team/home_team blank but matchup carries 'Away @ Home' --
        same fallback nfl_results.py already relies on."""
        result = ingest_results_for_recommendations(
            db_conn,
            [_rec(away_team="", home_team="", matchup="Temple Owls @ Toledo Rockets")],
            client=FakeClient(),
        )
        assert result["games_final"] == 1

    def test_second_recommendation_for_same_event_reuses_the_match(self, db_conn):
        """Two recommendations (e.g. moneyline + spread) for the same
        event must only match/save once, not duplicate work or error on
        a second event_results write."""
        recs = [_rec(market_type="game_moneyline"), _rec(market_type="game_spread_ou")]
        result = ingest_results_for_recommendations(db_conn, recs, client=FakeClient())
        assert result["games_final"] == 1
        rows = db_conn.execute("SELECT * FROM event_results WHERE event_id = 'cfb-evt-1'").fetchall()
        assert len(rows) == 1
