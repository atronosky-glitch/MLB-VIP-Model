"""Tests for arbitrage/middle opportunity persistence and grading
(database/db_manager.py's sync_*/get_active_*/grade_* functions)."""

from database.db_manager import (
    sync_arbitrage_opportunities, sync_middle_opportunities,
    get_active_arbitrage_opportunities, get_active_middle_opportunities,
    get_graded_arbitrage_opportunities, get_graded_middle_opportunities,
    grade_arbitrage_opportunities, grade_middle_opportunities,
    save_player_stat_result, save_event_result,
)


def _arb_opp(group_key="E1|P1|k|6.5", roi=8.5):
    return {
        "group_key": group_key, "event_id": "E1", "matchup": "Away @ Home",
        "event_start_time": "2026-09-09T20:00:00+00:00",
        "player_id": "P1", "player_name": "Test Pitcher",
        "market_type": "pitching_strikeouts_ou", "line": 6.5,
        "side_a": "OVER", "side_a_book": "BookA", "side_a_price": 110,
        "side_a_decimal_odds": 2.10, "side_a_stake_pct": 0.523,
        "side_b": "UNDER", "side_b_book": "BookB", "side_b_price": 130,
        "side_b_decimal_odds": 2.30, "side_b_stake_pct": 0.477,
        "guaranteed_roi_pct": roi,
    }


def _mid_opp():
    return {
        "event_id": "E1", "matchup": "Away @ Home",
        "event_start_time": "2026-09-09T20:00:00+00:00",
        "player_id": "P1", "player_name": "Test Batter",
        "market_type": "batting_totalBases_ou",
        "over_line": 1.5, "over_sportsbook": "BookA", "over_price": -110,
        "over_decimal_odds": 1.909, "over_stake_pct": 0.5,
        "under_line": 2.5, "under_sportsbook": "BookB", "under_price": -110,
        "under_decimal_odds": 1.909, "under_stake_pct": 0.5,
        "window_width": 1.0, "worst_case_roi_pct": -2.0, "best_case_roi_pct": 90.0,
    }


class TestSyncArbitrageOpportunities:
    def test_first_sync_inserts_as_active(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["opportunity_id"] == "E1|P1|k|6.5"
        assert active[0]["guaranteed_roi_pct"] == 8.5

    def test_resync_updates_the_same_row_not_a_duplicate(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp(roi=8.5)])
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp(roi=6.0)])
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["guaranteed_roi_pct"] == 6.0

    def test_first_sync_reports_it_as_new(self, db_conn):
        result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        assert result["new_ids"] == ["E1|P1|k|6.5"]

    def test_resync_of_a_still_active_opportunity_is_not_new(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp(roi=8.5)])
        result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp(roi=6.0)])
        assert result["new_ids"] == []

    def test_stamps_opportunity_id_onto_the_passed_in_dict(self, db_conn):
        opp = _arb_opp()
        sync_arbitrage_opportunities(db_conn, "MLB", [opp])
        assert opp["opportunity_id"] == "E1|P1|k|6.5"

    def test_an_expired_opportunity_that_reappears_is_new_again(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        sync_arbitrage_opportunities(db_conn, "MLB", [])  # expires it
        result = sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])  # reappears
        assert result["new_ids"] == ["E1|P1|k|6.5"]

    def test_works_against_postgres_shaped_dict_only_rows(self, db_conn_dict_rows):
        """Regression test for a real production bug: the previously_active
        lookup used r[0] (positional), which works against sqlite3.Row in
        every other test here but raises against production's actual
        RealDictCursor rows -- silently breaking this function in prod for
        two days. See tests/conftest.py's db_conn_dict_rows docstring."""
        sync_arbitrage_opportunities(db_conn_dict_rows, "MLB", [_arb_opp(roi=8.5)])
        result = sync_arbitrage_opportunities(db_conn_dict_rows, "MLB", [_arb_opp(roi=6.0)])
        assert result["new_ids"] == []
        assert result["active"] == 1

    def test_opportunity_missing_from_a_later_sync_is_expired(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        sync_arbitrage_opportunities(db_conn, "MLB", [])  # price moved, no longer arbitrage
        assert get_active_arbitrage_opportunities(db_conn, "MLB") == []
        row = db_conn.execute(
            "SELECT status FROM arbitrage_opportunities WHERE opportunity_id = ?",
            ("E1|P1|k|6.5",),
        ).fetchone()
        assert row["status"] == "EXPIRED"

    def test_scoped_by_league(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        sync_arbitrage_opportunities(db_conn, "NFL", [_arb_opp(group_key="E2|P2|k|3.5")])
        assert len(get_active_arbitrage_opportunities(db_conn, "MLB")) == 1
        assert len(get_active_arbitrage_opportunities(db_conn, "NFL")) == 1
        assert len(get_active_arbitrage_opportunities(db_conn)) == 2


class TestSyncMiddleOpportunities:
    def test_first_sync_inserts_as_active(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        active = get_active_middle_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["over_line"] == 1.5
        assert active[0]["under_line"] == 2.5

    def test_missing_from_later_sync_is_expired(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        sync_middle_opportunities(db_conn, "MLB", [])
        assert get_active_middle_opportunities(db_conn, "MLB") == []

    def test_first_sync_reports_it_as_new(self, db_conn):
        result = sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        assert result["new_ids"] == ["E1|P1|batting_totalBases_ou|1.5|2.5"]

    def test_resync_of_a_still_active_opportunity_is_not_new(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        result = sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        assert result["new_ids"] == []

    def test_stamps_opportunity_id_onto_the_passed_in_dict(self, db_conn):
        opp = _mid_opp()
        sync_middle_opportunities(db_conn, "MLB", [opp])
        assert opp["opportunity_id"] == "E1|P1|batting_totalBases_ou|1.5|2.5"

    def test_works_against_postgres_shaped_dict_only_rows(self, db_conn_dict_rows):
        """See TestSyncArbitrageOpportunities's version of this test --
        same bug, same fix, for middles."""
        sync_middle_opportunities(db_conn_dict_rows, "MLB", [_mid_opp()])
        result = sync_middle_opportunities(db_conn_dict_rows, "MLB", [_mid_opp()])
        assert result["new_ids"] == []
        assert result["active"] == 1


class TestGradeArbitrageOpportunities:
    def test_ungraded_while_stat_result_unresolved(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        result = grade_arbitrage_opportunities(db_conn)
        assert result["graded"] == 0
        assert get_active_arbitrage_opportunities(db_conn, "MLB")[0]["opportunity_id"] == "E1|P1|k|6.5"

    def test_grades_a_true_win_win_arbitrage(self, db_conn):
        """Both legs actually won -- true arbitrage should always show a
        real, guaranteed positive profit once graded."""
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        # Final stat 6.5 exactly: OVER 6.5 needs >6.5 to win. Use 8 so
        # OVER wins and UNDER 6.5 loses -- still must show guaranteed
        # profit since it's a real arbitrage regardless of which side hits.
        save_player_stat_result(
            db_conn, "E1", "P1", "pitching_strikeouts_ou",
            final_stat_value=8, result_status="FINAL",
        )
        result = grade_arbitrage_opportunities(db_conn)
        assert result["graded"] == 1
        row = db_conn.execute(
            "SELECT outcome, profit_units, status FROM arbitrage_opportunities WHERE opportunity_id = ?",
            ("E1|P1|k|6.5",),
        ).fetchone()
        assert row["status"] == "GRADED"
        assert row["outcome"] == "WIN/LOSS"
        assert row["profit_units"] > 0  # arbitrage always profits regardless of outcome

    def test_grades_the_opposite_outcome_still_profitably(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        save_player_stat_result(
            db_conn, "E1", "P1", "pitching_strikeouts_ou",
            final_stat_value=3, result_status="FINAL",  # UNDER 6.5 wins
        )
        grade_arbitrage_opportunities(db_conn)
        row = db_conn.execute(
            "SELECT outcome, profit_units FROM arbitrage_opportunities WHERE opportunity_id = ?",
            ("E1|P1|k|6.5",),
        ).fetchone()
        assert row["outcome"] == "LOSS/WIN"
        assert row["profit_units"] > 0

    def test_grades_a_moneyline_arbitrage_from_event_results(self, db_conn):
        opp = _arb_opp(group_key="E9|ml")
        opp.update({
            "player_id": None, "player_name": None, "market_type": "game_moneyline", "line": None,
            "side_a": "HOME", "side_b": "AWAY",
        })
        sync_arbitrage_opportunities(db_conn, "MLB", [opp])
        save_event_result(db_conn, "E1", final_status="FINAL", away_score=2, home_score=5)
        # opportunity's event_id is E1 via _arb_opp() defaults
        result = grade_arbitrage_opportunities(db_conn)
        assert result["graded"] == 1
        row = db_conn.execute(
            "SELECT outcome, profit_units FROM arbitrage_opportunities WHERE opportunity_id = ?",
            ("E9|ml",),
        ).fetchone()
        assert row["outcome"] == "WIN/LOSS"  # HOME (5) beat AWAY (2)
        assert row["profit_units"] > 0

    def test_graded_opportunities_are_queryable(self, db_conn):
        sync_arbitrage_opportunities(db_conn, "MLB", [_arb_opp()])
        save_player_stat_result(
            db_conn, "E1", "P1", "pitching_strikeouts_ou",
            final_stat_value=8, result_status="FINAL",
        )
        grade_arbitrage_opportunities(db_conn)
        graded = get_graded_arbitrage_opportunities(db_conn, "MLB")
        assert len(graded) == 1
        assert get_active_arbitrage_opportunities(db_conn, "MLB") == []


class TestGradeMiddleOpportunities:
    def test_grades_both_legs_winning_the_middle(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        # 2 total bases: strictly between 1.5 and 2.5 -- both legs win.
        save_player_stat_result(
            db_conn, "E1", "P1", "batting_totalBases_ou",
            final_stat_value=2, result_status="FINAL",
        )
        result = grade_middle_opportunities(db_conn)
        assert result["graded"] == 1
        row = db_conn.execute(
            "SELECT outcome, profit_units FROM middle_opportunities "
            "WHERE event_id='E1' AND player_id='P1'"
        ).fetchone()
        assert row["outcome"] == "WIN/WIN"
        assert row["profit_units"] > 0

    def test_grades_the_worst_case_split_result(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        # 1 total base: below both lines -- OVER 1.5 loses, UNDER 2.5 wins.
        save_player_stat_result(
            db_conn, "E1", "P1", "batting_totalBases_ou",
            final_stat_value=1, result_status="FINAL",
        )
        grade_middle_opportunities(db_conn)
        row = db_conn.execute(
            "SELECT outcome, profit_units FROM middle_opportunities "
            "WHERE event_id='E1' AND player_id='P1'"
        ).fetchone()
        assert row["outcome"] == "LOSS/WIN"
        # Matches the worst_case_roi_pct this was flagged with (small loss).
        assert row["profit_units"] < 0

    def test_graded_middles_are_queryable(self, db_conn):
        sync_middle_opportunities(db_conn, "MLB", [_mid_opp()])
        save_player_stat_result(
            db_conn, "E1", "P1", "batting_totalBases_ou",
            final_stat_value=2, result_status="FINAL",
        )
        grade_middle_opportunities(db_conn)
        assert len(get_graded_middle_opportunities(db_conn, "MLB")) == 1
        assert get_active_middle_opportunities(db_conn, "MLB") == []
