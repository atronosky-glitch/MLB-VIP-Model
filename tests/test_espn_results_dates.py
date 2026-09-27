"""NFL / WNBA result ingest: Eastern-date lookup and game-only events.

Found in production 2026-09-26:
  * ESPN's scoreboard is keyed by the US Eastern game day.  Both adapters grouped by the
    UTC date, so an evening game (UTC start rolls into the next day) asked ESPN for the
    wrong day and never matched (WNBA: 20 of 20 game events; NFL: 4 of 14).
  * NFL only fetched games that had a supported player-prop recommendation, so game-only
    totals/spreads never produced a final score for game_settlement to grade.
"""

import pytest

from database.db_manager import get_event_result, get_player_stat_result
from src.game_settlement import grade_game_recommendation
from src import nfl_results, wnba_results


def _event(event_id, away, home, when, away_score, home_score, completed=True, status="STATUS_FINAL"):
    return {
        "id": event_id, "date": when,
        "status": {"type": {"completed": completed, "name": status}},
        "competitions": [{"competitors": [
            {"homeAway": "away", "team": {"displayName": away}, "score": str(away_score)},
            {"homeAway": "home", "team": {"displayName": home}, "score": str(home_score)},
        ]}],
    }


def _nfl_summary():
    return {"boxscore": {"players": [{"statistics": [{
        "name": "rushing", "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
        "athletes": [{"athlete": {"id": "5001", "displayName": "José Rusher"},
                      "stats": ["8", "55", "6.9", "1", "27"]}]}]}]}}


def _wnba_summary():
    return {"boxscore": {"players": [{"statistics": [{
        "labels": ["MIN", "PTS"],
        "athletes": [{"athlete": {"id": "9", "displayName": "Some Player"}, "stats": ["30", "22"]}]}]}]}}


class FakeClient:
    """Serves scoreboards keyed by the exact date string requested; records every request."""

    def __init__(self, boards, summary):
        self.boards, self.summary, self.requested = boards, summary, []

    def fetch_scoreboard(self, date_value):
        self.requested.append(date_value)
        return self.boards.get(date_value, [])

    def fetch_summary(self, event_id):
        return self.summary


def _rec(**over):
    base = {"event_id": "EV1", "player_id": "P1", "player_name": "José Rusher", "market_type": "rushing_yards_ou",
            "matchup": "Away Team @ Home Team", "event_start_time": "2026-09-15T00:15:00Z",
            "side": "OVER", "line": 40.5}
    base.update(over)
    return base


def _game_rec(market="game_total_ou", **over):
    return _rec(player_id="GAME", player_name="Game Total", market_type=market, **over)


# ── NFL ─────────────────────────────────────────────────────────────────────
class TestNflEasternDate:
    def test_evening_game_is_looked_up_under_the_eastern_day(self, db_conn):
        # 00:15Z on 9/15 is 8:15pm ET on 9/14
        client = FakeClient({"2026-09-14": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]},
                            _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(db_conn, [_rec()], client=client)
        assert client.requested == ["2026-09-14"]
        assert stats["facts_saved"] == 1 and stats["unresolved_reasons"]["game_matching_failure"] == 0
        assert get_player_stat_result(db_conn, "EV1", "P1", "rushing_yards_ou")["final_stat_value"] == 55.0

    def test_utc_date_alone_would_have_missed_it(self, db_conn):
        client = FakeClient({"2026-09-15": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]},
                            _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(db_conn, [_rec()], client=client)
        assert client.requested == ["2026-09-14"]                   # asks the Eastern day only
        assert stats["facts_saved"] == 0 and stats["unresolved_reasons"]["game_matching_failure"] == 1
        assert get_event_result(db_conn, "EV1") is None


class TestNflGameOnlyEvents:
    def test_game_only_event_stores_final_score_and_is_gradable(self, db_conn):
        client = FakeClient({"2026-09-14": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]},
                            _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(db_conn, [_game_rec()], client=client)
        result = get_event_result(db_conn, "EV1")
        assert result["final_status"] == "FINAL" and (result["away_score"], result["home_score"]) == (14, 30)
        assert stats["games_final"] == 1 and stats["facts_saved"] == 0
        assert stats["unresolved_reasons"]["unsupported_or_research_market"] == 0
        assert stats["unresolved_reasons"]["player_fact_missing_or_ambiguous"] == 0   # not a player fact
        status, _ = grade_game_recommendation(_game_rec(side="OVER", line=40.5), result)
        assert status == "WIN"                                       # 44 total > 40.5
        status, _ = grade_game_recommendation(_game_rec(side="UNDER", line=40.5), result)
        assert status == "LOSS"

    @pytest.mark.parametrize("market", ["game_moneyline", "game_spread_ou", "game_total_ou"])
    def test_all_three_game_markets_trigger_a_fetch(self, db_conn, market):
        client = FakeClient({"2026-09-14": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]},
                            _nfl_summary())
        nfl_results.ingest_results_for_recommendations(db_conn, [_game_rec(market)], client=client)
        assert get_event_result(db_conn, "EV1")["final_status"] == "FINAL"

    def test_unsupported_market_still_never_calls_the_provider(self, db_conn):
        client = FakeClient({}, _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(
            db_conn, [_rec(market_type="punting_yards_ou")], client=client)
        assert client.requested == [] and stats["unresolved_reasons"]["unsupported_or_research_market"] == 1

    def test_game_not_final_stores_nothing(self, db_conn):
        board = [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 7, 3, completed=False,
                        status="STATUS_IN_PROGRESS")]
        stats = nfl_results.ingest_results_for_recommendations(
            db_conn, [_game_rec()], client=FakeClient({"2026-09-14": board}, _nfl_summary()))
        assert get_event_result(db_conn, "EV1") is None and stats["unresolved_reasons"]["game_not_final"] == 1

    def test_postponed_game_is_stored_as_void_status(self, db_conn):
        board = [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 0, 0, completed=False,
                        status="STATUS_POSTPONED")]
        nfl_results.ingest_results_for_recommendations(
            db_conn, [_game_rec()], client=FakeClient({"2026-09-14": board}, _nfl_summary()))
        assert get_event_result(db_conn, "EV1")["final_status"] == "STATUS_POSTPONED"


class TestNflExistingBehaviorAndSafety:
    def test_player_prop_and_game_market_in_one_event_both_work(self, db_conn):
        client = FakeClient({"2026-09-14": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]},
                            _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(db_conn, [_rec(), _game_rec()], client=client)
        assert stats["facts_saved"] == 1 and stats["games_final"] == 1
        assert get_event_result(db_conn, "EV1")["home_score"] == 30

    def test_rerun_is_idempotent(self, db_conn):
        board = {"2026-09-14": [_event("E9", "Away Team", "Home Team", "2026-09-15T00:15Z", 14, 30)]}
        recs = [_rec(), _game_rec(event_id="EV2")]
        for _ in range(3):
            nfl_results.ingest_results_for_recommendations(db_conn, recs, client=FakeClient(board, _nfl_summary()))
        assert db_conn.execute("SELECT COUNT(*) FROM player_stat_results").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM event_results").fetchone()[0] == 2

    def test_two_candidate_games_fail_closed(self, db_conn):
        board = [_event("E1", "Away Team", "Home Team", "2026-09-15T00:15Z", 1, 2),
                 _event("E2", "Away Team", "Home Team", "2026-09-15T03:00Z", 3, 4)]
        stats = nfl_results.ingest_results_for_recommendations(
            db_conn, [_game_rec()], client=FakeClient({"2026-09-14": board}, _nfl_summary()))
        assert get_event_result(db_conn, "EV1") is None
        assert stats["unresolved_reasons"]["game_matching_failure"] == 1

    def test_wrong_teams_fail_closed(self, db_conn):
        board = [_event("E1", "Other Team", "Home Team", "2026-09-15T00:15Z", 1, 2)]
        stats = nfl_results.ingest_results_for_recommendations(
            db_conn, [_game_rec()], client=FakeClient({"2026-09-14": board}, _nfl_summary()))
        assert get_event_result(db_conn, "EV1") is None and stats["unresolved_reasons"]["game_matching_failure"] == 1

    def test_missing_start_time_is_never_fetched(self, db_conn):
        client = FakeClient({}, _nfl_summary())
        stats = nfl_results.ingest_results_for_recommendations(
            db_conn, [_game_rec(event_start_time=None)], client=client)
        assert client.requested == [] and stats["unresolved_reasons"]["missing_start_time"] == 1


# ── WNBA ────────────────────────────────────────────────────────────────────
class TestWnbaEasternDate:
    def test_evening_game_is_looked_up_under_the_eastern_day(self, db_conn):
        # 02:00Z on 9/18 is 10pm ET on 9/17
        client = FakeClient({"2026-09-17": [_event("W1", "Away Team", "Home Team", "2026-09-18T02:00Z", 70, 82)]},
                            _wnba_summary())
        stats = wnba_results.ingest_results_for_recommendations(
            db_conn, [_game_rec(event_start_time="2026-09-18T02:00:00Z")], client=client)
        assert client.requested == ["2026-09-17"]
        result = get_event_result(db_conn, "EV1")
        assert (result["away_score"], result["home_score"]) == (70, 82) and stats["games_final"] == 1
        status, _ = grade_game_recommendation(
            _game_rec(side="OVER", line=140.5, event_start_time="2026-09-18T02:00:00Z"), result)
        assert status == "WIN"                                       # 70+82=152 > 140.5

    def test_utc_date_alone_would_have_missed_it(self, db_conn):
        client = FakeClient({"2026-09-18": [_event("W1", "Away Team", "Home Team", "2026-09-18T02:00Z", 70, 82)]},
                            _wnba_summary())
        stats = wnba_results.ingest_results_for_recommendations(
            db_conn, [_game_rec(event_start_time="2026-09-18T02:00:00Z")], client=client)
        assert client.requested == ["2026-09-17"]
        assert get_event_result(db_conn, "EV1") is None and stats["unresolved_reasons"]["game_matching_failure"] == 1

    def test_player_prop_still_settles_and_reruns_are_idempotent(self, db_conn):
        board = {"2026-09-17": [_event("W1", "Away Team", "Home Team", "2026-09-18T02:00Z", 70, 82)]}
        rec = _rec(player_name="Some Player", market_type="player_points_ou",
                   event_start_time="2026-09-18T02:00:00Z")
        for _ in range(2):
            stats = wnba_results.ingest_results_for_recommendations(
                db_conn, [rec], client=FakeClient(board, _wnba_summary()))
        assert stats["facts_saved"] == 1
        assert get_player_stat_result(db_conn, "EV1", "P1", "player_points_ou")["final_stat_value"] == 22.0
        assert db_conn.execute("SELECT COUNT(*) FROM player_stat_results").fetchone()[0] == 1

    def test_two_candidate_games_fail_closed(self, db_conn):
        board = [_event("W1", "Away Team", "Home Team", "2026-09-18T02:00Z", 1, 2),
                 _event("W2", "Away Team", "Home Team", "2026-09-18T05:00Z", 3, 4)]
        stats = wnba_results.ingest_results_for_recommendations(
            db_conn, [_game_rec(event_start_time="2026-09-18T02:00:00Z")],
            client=FakeClient({"2026-09-17": board}, _wnba_summary()))
        assert get_event_result(db_conn, "EV1") is None and stats["unresolved_reasons"]["game_matching_failure"] == 1
