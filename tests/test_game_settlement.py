"""Tests for the shared game-level settlement framework
(src/game_settlement.py) — moneyline/spread/total, shared across every
league. Also covers grade_available_game_recommendations' idempotency and
the postponed/cancelled/suspended VOID path.
"""

from __future__ import annotations

from src.game_settlement import (
    classify_event_status,
    grade_game_recommendation,
    grade_moneyline,
    grade_spread,
    grade_total,
)
from src.grading import (
    SETTLEMENT_LOSS,
    SETTLEMENT_NEEDS_REVIEW,
    SETTLEMENT_PUSH,
    SETTLEMENT_UNRESOLVED,
    SETTLEMENT_VOID,
    SETTLEMENT_WIN,
)


class TestGradeMoneyline:
    def test_win_and_loss(self):
        assert grade_moneyline("AWAY", 110, 100) == SETTLEMENT_WIN
        assert grade_moneyline("HOME", 100, 110) == SETTLEMENT_LOSS

    def test_exact_tie_is_push(self):
        """NFL regular-season games can end in a tie after one overtime."""
        assert grade_moneyline("AWAY", 20, 20) == SETTLEMENT_PUSH

    def test_missing_scores_unresolved(self):
        assert grade_moneyline("AWAY", None, 100) == SETTLEMENT_UNRESOLVED

    def test_invalid_side_unresolved(self):
        assert grade_moneyline("OVER", 110, 100) == SETTLEMENT_UNRESOLVED


class TestGradeSpread:
    def test_favorite_covers(self):
        """Home favored by 13.5 (raw_line=-13.5), wins by 20 -> covers."""
        assert grade_spread("HOME", 90, 70, -13.5) == SETTLEMENT_WIN

    def test_favorite_does_not_cover(self):
        """Home favored by 13.5, wins by only 10 -> doesn't cover."""
        assert grade_spread("HOME", 80, 70, -13.5) == SETTLEMENT_LOSS

    def test_underdog_covers_by_losing_close(self):
        """Away +13.5 (raw_line=+13.5), loses by 10 -> still covers."""
        assert grade_spread("AWAY", 70, 80, 13.5) == SETTLEMENT_WIN

    def test_underdog_does_not_cover(self):
        """Away +13.5, loses by 20 -> doesn't cover."""
        assert grade_spread("AWAY", 70, 90, 13.5) == SETTLEMENT_LOSS

    def test_exact_push_on_whole_line(self):
        """Home favored by exactly 3, wins by exactly 3 -> push."""
        assert grade_spread("HOME", 23, 20, -3.0) == SETTLEMENT_PUSH

    def test_missing_raw_line_is_needs_review_not_guessed(self):
        """This is the whole point of storing raw_line — never reconstruct
        the favorite/underdog direction from context."""
        assert grade_spread("HOME", 90, 70, None) == SETTLEMENT_NEEDS_REVIEW

    def test_missing_scores_unresolved(self):
        assert grade_spread("HOME", None, 70, -13.5) == SETTLEMENT_UNRESOLVED


class TestGradeTotal:
    def test_over_wins(self):
        assert grade_total("OVER", 90, 80, 168.5) == SETTLEMENT_WIN

    def test_under_wins(self):
        assert grade_total("UNDER", 90, 70, 168.5) == SETTLEMENT_WIN

    def test_push_on_whole_line(self):
        assert grade_total("OVER", 85, 85, 170.0) == SETTLEMENT_PUSH

    def test_missing_scores_unresolved(self):
        assert grade_total("OVER", None, 80, 168.5) == SETTLEMENT_UNRESOLVED


class TestClassifyEventStatus:
    def test_final_statuses(self):
        assert classify_event_status("FINAL") == "final"
        assert classify_event_status("STATUS_FINAL") == "final"

    def test_void_statuses(self):
        for status in ("POSTPONED", "CANCELLED", "CANCELED", "SUSPENDED",
                       "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED"):
            assert classify_event_status(status) == "void", status

    def test_unrecognized_status_is_pending_not_guessed(self):
        assert classify_event_status("SOME_NEW_STATUS_ESPN_ADDS_LATER") == "pending"
        assert classify_event_status(None) == "pending"
        assert classify_event_status("") == "pending"


class TestGradeGameRecommendation:
    def test_moneyline_end_to_end(self):
        rec = {"market_type": "game_moneyline", "side": "HOME", "line": None,
               "raw_line": None, "event_id": "evt-1"}
        event_result = {"final_status": "FINAL", "away_score": 70, "home_score": 78}
        status, detail = grade_game_recommendation(rec, event_result)
        assert status == SETTLEMENT_WIN
        assert "78" in detail["reason"] or detail["away_score"] == 70

    def test_spread_end_to_end(self):
        rec = {"market_type": "game_spread_ou", "side": "AWAY", "line": 13.5,
               "raw_line": 13.5, "event_id": "evt-1"}
        event_result = {"final_status": "FINAL", "away_score": 70, "home_score": 78}
        status, _ = grade_game_recommendation(rec, event_result)
        assert status == SETTLEMENT_WIN  # away +13.5, lost by only 8

    def test_total_end_to_end(self):
        rec = {"market_type": "game_total_ou", "side": "OVER", "line": 140.5,
               "raw_line": 140.5, "event_id": "evt-1"}
        event_result = {"final_status": "FINAL", "away_score": 70, "home_score": 78}
        status, _ = grade_game_recommendation(rec, event_result)
        assert status == SETTLEMENT_WIN  # 148 > 140.5

    def test_no_event_result_yet_is_unresolved(self):
        rec = {"market_type": "game_moneyline", "side": "HOME", "event_id": "evt-1"}
        status, detail = grade_game_recommendation(rec, None)
        assert status == SETTLEMENT_UNRESOLVED
        assert detail["reason"] == "event_not_final_yet"

    def test_postponed_game_is_void(self):
        rec = {"market_type": "game_moneyline", "side": "HOME", "event_id": "evt-1"}
        event_result = {"final_status": "POSTPONED", "away_score": None, "home_score": None}
        status, detail = grade_game_recommendation(rec, event_result)
        assert status == SETTLEMENT_VOID
        assert "POSTPONED" in detail["reason"]

    def test_suspended_game_is_void(self):
        rec = {"market_type": "game_total_ou", "side": "OVER", "event_id": "evt-1"}
        event_result = {"final_status": "STATUS_SUSPENDED"}
        status, _ = grade_game_recommendation(rec, event_result)
        assert status == SETTLEMENT_VOID

    def test_non_game_market_type_is_unresolved(self):
        rec = {"market_type": "player_points_ou", "side": "OVER", "event_id": "evt-1"}
        status, detail = grade_game_recommendation(rec, {"final_status": "FINAL"})
        assert status == SETTLEMENT_UNRESOLVED
        assert detail["reason"] == "not_a_game_market"


class TestGradeAvailableGameRecommendationsIdempotency:
    def test_repeated_settlement_does_not_duplicate_or_change(self, tmp_path):
        from database.db_manager import (
            init_db, get_connection, save_recommendation, save_event_result,
        )
        from src.automatic_grading import grade_available_game_recommendations

        db_path = tmp_path / "idempotent_game_settle.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec_id = save_recommendation(conn, {
            "event_id": "evt-idem-1", "player_id": "GAME", "player_name": "Moneyline",
            "market_type": "game_moneyline", "side": "HOME", "sportsbook": "draftkings",
            "offered_american_odds": -150, "offered_decimal_odds": 1.667,
            "offered_implied_prob": 0.6, "rec_status": "BET",
            "scan_timestamp": "2026-08-17T20:00:00Z", "league": "WNBA", "sport": "basketball",
        })
        assert rec_id is not None
        save_event_result(conn, "evt-idem-1", final_status="FINAL", away_score=70, home_score=78)

        result1 = grade_available_game_recommendations(conn)
        assert result1["graded"] == 1

        row_after_first = conn.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row_after_first["settlement_status"] == "WIN"

        settlement_count_before = conn.execute(
            "SELECT COUNT(*) AS c FROM market_settlements"
        ).fetchone()["c"]

        # Running it again must not duplicate or change the settled row.
        result2 = grade_available_game_recommendations(conn)
        assert result2["examined"] == 0  # already settled, no longer "unsettled"

        settlement_count_after = conn.execute(
            "SELECT COUNT(*) AS c FROM market_settlements"
        ).fetchone()["c"]
        assert settlement_count_after == settlement_count_before

        row_after_second = conn.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row_after_second["settlement_status"] == "WIN"

    def test_needs_review_when_spread_missing_raw_line(self, tmp_path):
        from database.db_manager import (
            init_db, get_connection, save_recommendation, save_event_result,
        )
        from src.automatic_grading import grade_available_game_recommendations

        db_path = tmp_path / "needs_review.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec_id = save_recommendation(conn, {
            "event_id": "evt-nr-1", "player_id": "GAME", "player_name": "Spread",
            "market_type": "game_spread_ou", "side": "AWAY", "line": 3.5,
            "sportsbook": "draftkings", "offered_american_odds": -110,
            "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
            "rec_status": "BET", "scan_timestamp": "2026-08-17T20:00:00Z",
            "league": "NFL", "sport": "football",
            # raw_line intentionally omitted
        })
        save_event_result(conn, "evt-nr-1", final_status="FINAL", away_score=20, home_score=17)

        result = grade_available_game_recommendations(conn)
        assert result["needs_review"] == 1
        row = conn.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert row["settlement_status"] == "NEEDS_REVIEW"

    def test_postponed_game_settles_as_void_with_zero_units(self, tmp_path):
        from database.db_manager import (
            init_db, get_connection, save_recommendation, save_event_result,
        )
        from src.automatic_grading import grade_available_game_recommendations

        db_path = tmp_path / "void_game.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        rec_id = save_recommendation(conn, {
            "event_id": "evt-void-1", "player_id": "GAME", "player_name": "Total",
            "market_type": "game_total_ou", "side": "OVER", "line": 45.5,
            "sportsbook": "fanduel", "offered_american_odds": -110,
            "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
            "rec_status": "BET", "scan_timestamp": "2026-08-17T20:00:00Z",
            "league": "NFL", "sport": "football",
        })
        save_event_result(conn, "evt-void-1", final_status="POSTPONED")

        result = grade_available_game_recommendations(conn)
        assert result["graded"] == 1
        settlement = conn.execute(
            "SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
            (rec_id,),
        ).fetchone()
        assert settlement["settlement_status"] == "VOID"
        units = conn.execute(
            "SELECT profit_units FROM bet_units WHERE recommendation_id = ?", (rec_id,),
        ).fetchone()
        assert units["profit_units"] == 0.0

    def test_player_prop_recommendation_is_ignored_by_game_grader(self, tmp_path):
        """Player props must be graded by grade_available_recommendations,
        not this game-level grader — confirms the market_type filter works."""
        from database.db_manager import init_db, get_connection, save_recommendation
        from src.automatic_grading import grade_available_game_recommendations

        db_path = tmp_path / "prop_ignored.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))

        save_recommendation(conn, {
            "event_id": "evt-prop-1", "player_id": "ESPN_WNBA_1", "player_name": "Test Player",
            "market_type": "player_points_ou", "side": "OVER", "line": 14.5,
            "sportsbook": "fanduel", "offered_american_odds": -110,
            "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
            "rec_status": "BET", "scan_timestamp": "2026-08-17T20:00:00Z",
            "league": "WNBA", "sport": "basketball",
        })

        result = grade_available_game_recommendations(conn)
        assert result["examined"] == 0
