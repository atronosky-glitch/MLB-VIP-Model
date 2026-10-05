"""CFB 1st-quarter / 1st-half markets: matching, result ingest and grading.

These markets settle on the score THROUGH a period (Q1, or Q1+Q2), not the final
score. Verified against ESPN's live college-football scoreboard 2026-10-05:
  * each competitor carries ``linescores: [{"period": 1, "value": 7.0}, ...]``
    and the quarters sum to the final score (Vanderbilt at Georgia, 14-38);
  * the scoreboard is keyed by the US Eastern game day, so Saturday-night games
    (UTC start on the next day) are listed under the Saturday date.
"""

import pytest

from database.db_manager import (
    get_event_period_scores, get_event_result, save_event_period_score, save_event_result,
    save_recommendation,
)
from src import cfb_results
from src.automatic_grading import grade_available_game_recommendations
from src.cfb_results import ESPNCFBClient, _period_scores, ingest_results_for_recommendations
from src.game_settlement import (
    GAME_MARKET_TYPES, PERIOD_MARKET_TYPES, SETTLEMENT_LOSS, SETTLEMENT_NEEDS_REVIEW,
    SETTLEMENT_UNRESOLVED, SETTLEMENT_VOID, SETTLEMENT_WIN, _score_through_period,
    grade_game_recommendation,
)
from src.sports.base import match_ou_market
from src.sports.cfb import MARKET_REGISTRY

FINAL = {"final_status": "FINAL", "away_score": 14, "home_score": 38}
# Vanderbilt (away) 7-7-0-0 = 14, Georgia (home) 7-14-7-10 = 38 -- the real game used to verify linescores.
Q = {1: {"away_score": 7, "home_score": 7}, 2: {"away_score": 7, "home_score": 14},
     3: {"away_score": 0, "home_score": 7}, 4: {"away_score": 0, "home_score": 10}}


def _rec(market_type, side, line=None, raw_line=None):
    return {"market_type": market_type, "side": side, "line": line, "raw_line": raw_line, "event_id": "E1"}


# ── market registry and matching ────────────────────────────────────────────
class TestRegistry:
    def test_period_markets_are_registered_and_all_game_level(self):
        types = {m.market_type_ou for m in MARKET_REGISTRY}
        assert {"game_moneyline_1q", "game_spread_1q_ou", "game_total_1q_ou",
                "game_moneyline_1h", "game_spread_1h_ou", "game_total_1h_ou"} <= types
        assert len(MARKET_REGISTRY) == 11 and all(m.game_level for m in MARKET_REGISTRY)

    def test_every_registered_market_is_settleable(self):
        for m in MARKET_REGISTRY:
            assert m.market_type_ou in GAME_MARKET_TYPES, m.market_type_ou

    @pytest.mark.parametrize("odd_id,expected", [
        ("points-home-1q-ml-home", "game_moneyline_1q"),
        ("points-away-1q-sp-away", "game_spread_1q_ou"),
        ("points-all-1q-ou-over", "game_total_1q_ou"),
        ("points-home-1h-ml-home", "game_moneyline_1h"),
        ("points-home-1h-sp-home", "game_spread_1h_ou"),
        ("points-all-1h-ou-under", "game_total_1h_ou"),
        # full-game ids must still map to the full-game markets, never a period market
        ("points-home-game-ml-home", "game_moneyline"),
        ("points-all-game-ou-over", "game_total_ou"),
    ])
    def test_provider_odd_ids_map_to_the_right_market(self, odd_id, expected):
        assert match_ou_market(MARKET_REGISTRY, odd_id).market_type_ou == expected

    @pytest.mark.parametrize("odd_id", ["points-all-2q-ou-over", "points-all-2h-ou-over", "points-all-3q-ou-over"])
    def test_unregistered_periods_do_not_match(self, odd_id):
        assert match_ou_market(MARKET_REGISTRY, odd_id) is None


# ── grading ─────────────────────────────────────────────────────────────────
class TestPeriodGrading:
    def test_1q_total_uses_only_the_first_quarter(self):
        # Q1 total is 14; the final total is 52 -- grading against the final would flip these.
        assert grade_game_recommendation(_rec("game_total_1q_ou", "UNDER", 14.5), FINAL, Q)[0] == SETTLEMENT_WIN
        assert grade_game_recommendation(_rec("game_total_1q_ou", "OVER", 14.5), FINAL, Q)[0] == SETTLEMENT_LOSS

    def test_1h_total_sums_quarters_one_and_two(self):
        # first half: away 14, home 21 = 35
        assert grade_game_recommendation(_rec("game_total_1h_ou", "OVER", 34.5), FINAL, Q)[0] == SETTLEMENT_WIN
        assert grade_game_recommendation(_rec("game_total_1h_ou", "UNDER", 34.5), FINAL, Q)[0] == SETTLEMENT_LOSS

    def test_1h_spread_uses_signed_raw_line_and_the_half_margin(self):
        # half margin: home 21 - away 14 = home +7
        assert grade_game_recommendation(_rec("game_spread_1h_ou", "HOME", 6.5, -6.5), FINAL, Q)[0] == SETTLEMENT_WIN
        assert grade_game_recommendation(_rec("game_spread_1h_ou", "HOME", 7.5, -7.5), FINAL, Q)[0] == SETTLEMENT_LOSS
        assert grade_game_recommendation(_rec("game_spread_1h_ou", "AWAY", 7.5, 7.5), FINAL, Q)[0] == SETTLEMENT_WIN

    def test_1q_moneyline_decided_quarter(self):
        quarters = {1: {"away_score": 3, "home_score": 10}}
        assert grade_game_recommendation(_rec("game_moneyline_1q", "HOME"), FINAL, quarters)[0] == SETTLEMENT_WIN
        assert grade_game_recommendation(_rec("game_moneyline_1q", "AWAY"), FINAL, quarters)[0] == SETTLEMENT_LOSS

    @pytest.mark.parametrize("market,through", [("game_moneyline_1q", 1), ("game_moneyline_1h", 2)])
    def test_tied_period_moneyline_is_flagged_not_guessed_as_a_push(self, market, through):
        """7-7 after Q1 is common and book rules for a two-way period moneyline differ, so it is
        never settled as a PUSH/WIN/LOSS automatically."""
        quarters = {1: {"away_score": 7, "home_score": 7}, 2: {"away_score": 0, "home_score": 0}}
        status, detail = grade_game_recommendation(_rec(market, "HOME"), FINAL, quarters)
        assert status == SETTLEMENT_NEEDS_REVIEW and "tied" in detail["reason"]

    def test_full_game_moneyline_tie_is_unchanged(self):
        status, _ = grade_game_recommendation(_rec("game_moneyline", "HOME"),
                                              {"final_status": "FINAL", "away_score": 20, "home_score": 20})
        assert status == "PUSH"

    @pytest.mark.parametrize("period_scores", [
        None, {}, {2: {"away_score": 7, "home_score": 14}},                      # no Q1
        {1: {"away_score": 7, "home_score": None}},                               # half-populated
    ])
    def test_missing_period_scores_stay_unresolved(self, period_scores):
        status, detail = grade_game_recommendation(_rec("game_total_1q_ou", "OVER", 10.5), FINAL, period_scores)
        assert status == SETTLEMENT_UNRESOLVED and "not yet available" in detail["reason"]

    def test_a_half_never_settles_from_only_one_quarter(self):
        only_q1 = {1: {"away_score": 7, "home_score": 7}}
        assert _score_through_period(only_q1, 2) == (None, None)
        assert grade_game_recommendation(_rec("game_total_1h_ou", "OVER", 5.5), FINAL, only_q1)[0] == SETTLEMENT_UNRESOLVED

    def test_unfinished_game_stays_unresolved_even_with_period_scores(self):
        live = {"final_status": "STATUS_IN_PROGRESS"}
        assert grade_game_recommendation(_rec("game_total_1q_ou", "OVER", 10.5), live, Q)[0] == SETTLEMENT_UNRESOLVED

    def test_postponed_game_voids_period_markets(self):
        post = {"final_status": "POSTPONED"}
        assert grade_game_recommendation(_rec("game_total_1h_ou", "OVER", 10.5), post, Q)[0] == SETTLEMENT_VOID

    def test_full_game_markets_ignore_period_scores(self):
        # final total 52; passing period scores must not change a full-game total
        assert grade_game_recommendation(_rec("game_total_ou", "OVER", 50.5), FINAL, Q)[0] == SETTLEMENT_WIN
        assert grade_game_recommendation(_rec("game_total_ou", "OVER", 50.5), FINAL, None)[0] == SETTLEMENT_WIN

    def test_period_market_set_matches_the_registry(self):
        registered = {m.market_type_ou for m in MARKET_REGISTRY if m.period in ("1q", "1h")}
        assert PERIOD_MARKET_TYPES == registered


# ── result ingest ───────────────────────────────────────────────────────────
def _event(away=(7, 7, 0, 0), home=(7, 14, 7, 10), completed=True, when="2026-10-03T16:45Z", name="STATUS_FINAL"):
    def lines(values):
        return [{"value": float(v), "displayValue": str(v), "period": i + 1} for i, v in enumerate(values)]
    return {
        "id": "401", "date": when,
        "status": {"type": {"completed": completed, "name": name}},
        "competitions": [{"competitors": [
            {"homeAway": "home", "team": {"displayName": "Georgia Bulldogs"}, "score": str(sum(home)), "linescores": lines(home)},
            {"homeAway": "away", "team": {"displayName": "Vanderbilt Commodores"}, "score": str(sum(away)), "linescores": lines(away)},
        ]}],
    }


class _Client(ESPNCFBClient):
    def __init__(self, boards):
        self.boards, self.requested = boards, []

    def fetch_scoreboard(self, date_value):
        self.requested.append(date_value)
        return self.boards.get(date_value, [])


def _ingest_rec(**over):
    row = {"event_id": "cfb-1", "market_type": "game_total_1h_ou", "side": "OVER", "line": 34.5, "raw_line": 34.5,
           "away_team": "Vanderbilt Commodores", "home_team": "Georgia Bulldogs",
           "event_start_time": "2026-10-03T16:45:00Z"}
    row.update(over)
    return row


class TestResultIngest:
    def test_period_scores_are_saved_and_sum_to_the_final(self, db_conn):
        client = _Client({"2026-10-03": [_event()]})
        ingest_results_for_recommendations(db_conn, [_ingest_rec()], client=client)
        saved = get_event_period_scores(db_conn, "cfb-1")
        assert saved == Q
        result = get_event_result(db_conn, "cfb-1")
        assert sum(p["away_score"] for p in saved.values()) == result["away_score"] == 14
        assert sum(p["home_score"] for p in saved.values()) == result["home_score"] == 38

    def test_rerun_is_idempotent(self, db_conn):
        client = _Client({"2026-10-03": [_event()]})
        for _ in range(3):
            ingest_results_for_recommendations(db_conn, [_ingest_rec()], client=client)
        assert db_conn.execute("SELECT COUNT(*) FROM event_period_scores").fetchone()[0] == 4
        assert db_conn.execute("SELECT COUNT(*) FROM event_results").fetchone()[0] == 1

    def test_unfinished_game_saves_no_period_scores(self, db_conn):
        live = _event(completed=False, name="STATUS_IN_PROGRESS")
        ingest_results_for_recommendations(db_conn, [_ingest_rec()], client=_Client({"2026-10-03": [live]}))
        assert get_event_period_scores(db_conn, "cfb-1") == {}
        assert get_event_result(db_conn, "cfb-1") is None

    def test_overtime_periods_are_stored_but_do_not_affect_1q_or_1h(self, db_conn):
        ot = _event(away=(7, 7, 0, 0, 6), home=(7, 14, 7, 10, 0))
        ingest_results_for_recommendations(db_conn, [_ingest_rec()], client=_Client({"2026-10-03": [ot]}))
        saved = get_event_period_scores(db_conn, "cfb-1")
        assert 5 in saved and _score_through_period(saved, 2) == (14, 21)

    def test_missing_linescores_still_saves_the_final_but_no_periods(self, db_conn):
        event = _event()
        for c in event["competitions"][0]["competitors"]:
            c.pop("linescores")
        ingest_results_for_recommendations(db_conn, [_ingest_rec()], client=_Client({"2026-10-03": [event]}))
        assert get_event_result(db_conn, "cfb-1")["final_status"] == "FINAL"
        assert get_event_period_scores(db_conn, "cfb-1") == {}

    def test_only_periods_both_teams_reported_are_kept(self):
        competitors = _event()["competitions"][0]["competitors"]
        competitors[1]["linescores"] = competitors[1]["linescores"][:2]          # away reports Q1-Q2 only
        assert [p for p, _, _ in _period_scores(competitors)] == [1, 2]
        competitors[0]["linescores"][0]["value"] = None                           # home Q1 not numeric
        assert [p for p, _, _ in _period_scores(competitors)] == [2]

    def test_evening_game_is_looked_up_under_the_eastern_day(self, db_conn):
        # 00:00Z on 10-04 is 8pm ET on Saturday 10-03 -- real Indiana at Rutgers start
        rec = _ingest_rec(event_start_time="2026-10-04T00:00:00Z")
        client = _Client({"2026-10-03": [_event(when="2026-10-04T00:00Z")]})
        stats = ingest_results_for_recommendations(db_conn, [rec], client=client)
        assert client.requested == ["2026-10-03"] and stats["games_final"] == 1

    def test_utc_date_alone_would_have_missed_it(self, db_conn):
        rec = _ingest_rec(event_start_time="2026-10-04T00:00:00Z")
        client = _Client({"2026-10-04": [_event(when="2026-10-04T00:00Z")]})
        stats = ingest_results_for_recommendations(db_conn, [rec], client=client)
        assert client.requested == ["2026-10-03"] and stats["games_final"] == 0
        assert get_event_result(db_conn, "cfb-1") is None


# ── end to end: stored scores -> settled recommendation ─────────────────────
def _save_rec(conn, market_type, side, line, raw_line, event_id="cfb-e2e"):
    return save_recommendation(conn, {
        "event_id": event_id, "player_id": "GAME", "player_name": "Game",
        "market_type": market_type, "side": side, "line": line, "raw_line": raw_line,
        "sportsbook": "draftkings", "offered_american_odds": -110, "offered_decimal_odds": 1.909,
        "offered_implied_prob": 0.52, "rec_status": "BET", "scan_timestamp": "2026-10-03T12:00:00Z",
        "league": "NCAAF", "sport": "football",
    })


def _status(conn, rec_id):
    row = conn.execute("SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?", (rec_id,)).fetchone()
    return row["settlement_status"] if row else None


class TestEndToEndGrading:
    def test_first_half_total_settles_from_stored_period_scores(self, db_conn):
        rec = _save_rec(db_conn, "game_total_1h_ou", "OVER", 34.5, 34.5)
        save_event_result(db_conn, "cfb-e2e", final_status="FINAL", away_score=14, home_score=38)
        for period, scores in Q.items():
            save_event_period_score(db_conn, "cfb-e2e", period, **scores)
        assert grade_available_game_recommendations(db_conn)["graded"] == 1
        assert _status(db_conn, rec) == "WIN"                                      # 35 > 34.5

    def test_without_period_scores_the_recommendation_stays_unsettled(self, db_conn):
        rec = _save_rec(db_conn, "game_total_1h_ou", "OVER", 34.5, 34.5)
        save_event_result(db_conn, "cfb-e2e", final_status="FINAL", away_score=14, home_score=38)
        result = grade_available_game_recommendations(db_conn)
        assert result["graded"] == 0 and _status(db_conn, rec) is None             # never graded from the final

    def test_tied_first_quarter_moneyline_is_settled_for_review_not_won_or_lost(self, db_conn):
        rec = _save_rec(db_conn, "game_moneyline_1q", "HOME", None, None)
        save_event_result(db_conn, "cfb-e2e", final_status="FINAL", away_score=14, home_score=38)
        save_event_period_score(db_conn, "cfb-e2e", 1, away_score=7, home_score=7)
        result = grade_available_game_recommendations(db_conn)
        assert result["needs_review"] == 1 and _status(db_conn, rec) == "NEEDS_REVIEW"

    def test_rerun_does_not_regrade_or_duplicate(self, db_conn):
        rec = _save_rec(db_conn, "game_total_1q_ou", "UNDER", 14.5, 14.5)
        save_event_result(db_conn, "cfb-e2e", final_status="FINAL", away_score=14, home_score=38)
        save_event_period_score(db_conn, "cfb-e2e", 1, away_score=7, home_score=7)
        grade_available_game_recommendations(db_conn)
        assert grade_available_game_recommendations(db_conn)["examined"] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM market_settlements WHERE recommendation_id = ?", (rec,)).fetchone()[0] == 1
        assert _status(db_conn, rec) == "WIN"

    def test_full_game_recommendation_still_grades_from_the_final(self, db_conn):
        rec = _save_rec(db_conn, "game_total_ou", "OVER", 50.5, 50.5)
        save_event_result(db_conn, "cfb-e2e", final_status="FINAL", away_score=14, home_score=38)
        assert grade_available_game_recommendations(db_conn)["graded"] == 1 and _status(db_conn, rec) == "WIN"
