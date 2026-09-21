"""Tests for the end-of-day Discord results summary (2026-09-21
operator request): for each category actually delivered to Discord (EV
picks, arbitrage, middles), today's and all-time record + profit in
units.

- database/db_manager.py::get_ev_pick_results_summary /
  get_arbitrage_results_summary / get_middle_results_summary
- src/message_formatter.py::format_daily_results_summary
- src/discord_delivery.py::deliver_daily_results_summary
- src/worker.py's 'daily-results-summary' job + scheduling
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

from database.db_manager import (
    get_ev_pick_results_summary, get_arbitrage_results_summary, get_middle_results_summary,
)
from src.message_formatter import format_daily_results_summary


def _insert_ev_pick(conn, rec_id, *, settlement_status, profit_units=0.0, settled_at="2026-09-21T10:00:00+00:00",
                     sent_at="2026-09-21T09:00:00+00:00"):
    conn.execute(
        "INSERT INTO discord_alerts_sent (alert_key, alert_type, sent_at) VALUES (?, 'ev_pick', ?)",
        (rec_id, sent_at),
    )
    conn.execute(
        """INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at)
           VALUES (?, ?, ?, ?)""",
        (f"settle-{rec_id}", rec_id, settlement_status, settled_at),
    )
    conn.execute(
        """INSERT INTO bet_units (settlement_id, recommendation_id, risk_units, profit_units, return_units)
           VALUES (?, ?, 1.0, ?, ?)""",
        (f"settle-{rec_id}", rec_id, profit_units, 1.0 + profit_units),
    )
    conn.commit()


def _insert_arb(conn, opp_id, *, profit_units, discord_sent=1, status="GRADED",
                 graded_at="2026-09-21T10:00:00+00:00"):
    conn.execute("""
        INSERT INTO arbitrage_opportunities (
            opportunity_id, league, event_id, side_a, side_a_sportsbook, side_a_price,
            side_a_decimal_odds, side_a_stake_pct, side_b, side_b_sportsbook, side_b_price,
            side_b_decimal_odds, side_b_stake_pct, guaranteed_roi_pct, status, discord_sent,
            discord_sent_at, outcome, profit_units, graded_at
        ) VALUES (?, 'MLB', 'E1', 'OVER', 'BookA', 110, 2.10, 0.5, 'UNDER', 'BookB', 130, 2.30, 0.5,
                  8.5, ?, ?, ?, 'WIN/LOSS', ?, ?)
    """, (opp_id, status, discord_sent, graded_at if discord_sent else None, profit_units, graded_at))
    conn.commit()


def _insert_middle(conn, opp_id, *, profit_units, discord_sent=1, status="GRADED", verdict="WORTH_IT",
                    graded_at="2026-09-21T10:00:00+00:00"):
    conn.execute("""
        INSERT INTO middle_opportunities (
            opportunity_id, league, event_id, over_line, over_sportsbook, over_price,
            over_decimal_odds, over_stake_pct, under_line, under_sportsbook, under_price,
            under_decimal_odds, under_stake_pct, window_width, worst_case_roi_pct,
            best_case_roi_pct, verdict, status, discord_sent, discord_sent_at, outcome,
            profit_units, graded_at
        ) VALUES (?, 'MLB', 'E1', 1.5, 'BookA', -110, 1.909, 0.5, 2.5, 'BookB', -110, 1.909, 0.5,
                  1.0, -2.0, 90.0, ?, ?, ?, ?, 'WIN/WIN', ?, ?)
    """, (opp_id, verdict, status, discord_sent, graded_at if discord_sent else None, profit_units, graded_at))
    conn.commit()


class TestGetEvPickResultsSummary:
    def test_no_alerted_picks_returns_zeros(self, db_conn):
        stats = get_ev_pick_results_summary(db_conn)
        assert stats == {
            "wins": 0, "losses": 0, "pushes": 0, "voids": 0, "graded_count": 0, "profit_units": 0.0,
        }

    def test_counts_by_settlement_status_and_sums_profit(self, db_conn):
        _insert_ev_pick(db_conn, "r1", settlement_status="WIN", profit_units=0.91)
        _insert_ev_pick(db_conn, "r2", settlement_status="LOSS", profit_units=-1.0)
        _insert_ev_pick(db_conn, "r3", settlement_status="PUSH", profit_units=0.0)
        _insert_ev_pick(db_conn, "r4", settlement_status="VOID", profit_units=0.0)
        stats = get_ev_pick_results_summary(db_conn)
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["pushes"] == 1
        assert stats["voids"] == 1
        assert stats["graded_count"] == 4
        assert stats["profit_units"] == -0.09

    def test_ignores_settlements_never_alerted_to_discord(self, db_conn):
        """A rec that settled but was never actually posted to the EV
        Discord channel doesn't count -- 'a bet made in that section'
        means literally posted there."""
        db_conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
            "VALUES ('s1', 'never-alerted', 'WIN', '2026-09-21T10:00:00+00:00')"
        )
        db_conn.commit()
        stats = get_ev_pick_results_summary(db_conn)
        assert stats["graded_count"] == 0

    def test_unresolved_settlements_are_excluded(self, db_conn):
        db_conn.execute(
            "INSERT INTO discord_alerts_sent (alert_key, alert_type, sent_at) VALUES ('r1', 'ev_pick', '2026-09-21T09:00:00+00:00')"
        )
        db_conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
            "VALUES ('s1', 'r1', 'UNRESOLVED', '2026-09-21T10:00:00+00:00')"
        )
        db_conn.commit()
        stats = get_ev_pick_results_summary(db_conn)
        assert stats["graded_count"] == 0

    def test_since_scopes_to_today(self, db_conn):
        _insert_ev_pick(db_conn, "old", settlement_status="WIN", profit_units=1.0,
                         settled_at="2026-09-19T10:00:00+00:00")
        _insert_ev_pick(db_conn, "today", settlement_status="WIN", profit_units=0.5,
                         settled_at="2026-09-21T10:00:00+00:00")
        today_stats = get_ev_pick_results_summary(db_conn, since="2026-09-21T00:00:00+00:00")
        all_time_stats = get_ev_pick_results_summary(db_conn)
        assert today_stats["wins"] == 1
        assert today_stats["profit_units"] == 0.5
        assert all_time_stats["wins"] == 2
        assert all_time_stats["profit_units"] == 1.5

    def test_missing_bet_units_row_treated_as_zero_profit_not_a_crash(self, db_conn):
        db_conn.execute(
            "INSERT INTO discord_alerts_sent (alert_key, alert_type, sent_at) VALUES ('r1', 'ev_pick', '2026-09-21T09:00:00+00:00')"
        )
        db_conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
            "VALUES ('s1', 'r1', 'WIN', '2026-09-21T10:00:00+00:00')"
        )
        db_conn.commit()
        stats = get_ev_pick_results_summary(db_conn)
        assert stats["wins"] == 1
        assert stats["profit_units"] == 0.0


class TestGetArbitrageResultsSummary:
    def test_no_delivered_opportunities_returns_zeros(self, db_conn):
        stats = get_arbitrage_results_summary(db_conn)
        assert stats == {"wins": 0, "losses": 0, "graded_count": 0, "profit_units": 0.0}

    def test_only_counts_delivered_and_graded(self, db_conn):
        _insert_arb(db_conn, "a1", profit_units=1.5, discord_sent=1, status="GRADED")
        _insert_arb(db_conn, "a2", profit_units=2.0, discord_sent=0, status="GRADED")  # never delivered
        _insert_arb(db_conn, "a3", profit_units=0.0, discord_sent=1, status="ACTIVE")  # not graded yet
        stats = get_arbitrage_results_summary(db_conn)
        assert stats["graded_count"] == 1
        assert stats["profit_units"] == 1.5

    def test_win_loss_split_by_profit_sign(self, db_conn):
        _insert_arb(db_conn, "a1", profit_units=1.5)
        _insert_arb(db_conn, "a2", profit_units=2.0)
        _insert_arb(db_conn, "a3", profit_units=-0.1)
        stats = get_arbitrage_results_summary(db_conn)
        assert stats["wins"] == 2
        assert stats["losses"] == 1
        assert round(stats["profit_units"], 2) == 3.4

    def test_since_scopes_by_graded_at(self, db_conn):
        _insert_arb(db_conn, "old", profit_units=1.0, graded_at="2026-09-19T10:00:00+00:00")
        _insert_arb(db_conn, "today", profit_units=0.5, graded_at="2026-09-21T10:00:00+00:00")
        today_stats = get_arbitrage_results_summary(db_conn, since="2026-09-21T00:00:00+00:00")
        assert today_stats["graded_count"] == 1
        assert today_stats["profit_units"] == 0.5


class TestGetMiddleResultsSummary:
    def test_only_counts_worth_it_delivered_graded(self, db_conn):
        _insert_middle(db_conn, "m1", profit_units=1.0, verdict="WORTH_IT")
        _insert_middle(db_conn, "m2", profit_units=2.0, verdict="NOT_WORTH_IT")  # filtered out
        _insert_middle(db_conn, "m3", profit_units=3.0, discord_sent=0)  # never delivered
        stats = get_middle_results_summary(db_conn)
        assert stats["graded_count"] == 1
        assert stats["profit_units"] == 1.0

    def test_win_loss_split_by_profit_sign(self, db_conn):
        _insert_middle(db_conn, "m1", profit_units=0.5)
        _insert_middle(db_conn, "m2", profit_units=-0.3)
        stats = get_middle_results_summary(db_conn)
        assert stats["wins"] == 1
        assert stats["losses"] == 1


class TestFormatDailyResultsSummary:
    def _stats(self, wins=1, losses=0, pushes=0, voids=0, profit=1.5):
        return {"wins": wins, "losses": losses, "pushes": pushes, "voids": voids, "profit_units": profit}

    def test_includes_all_three_categories_and_both_windows(self):
        text = format_daily_results_summary(
            self._stats(), self._stats(wins=10, profit=25.0),
            self._stats(losses=1, profit=0.0), self._stats(wins=5, profit=12.0),
            self._stats(profit=0.5), self._stats(wins=3, profit=8.0),
            date_label="2026-09-21",
        )
        assert "2026-09-21" in text
        assert "EV Picks" in text
        assert "Arbitrage" in text
        assert "Middles" in text
        assert "Today" in text and "All-Time" in text

    def test_ev_record_includes_pushes_arb_and_middle_do_not(self):
        text = format_daily_results_summary(
            self._stats(wins=12, losses=3, pushes=1, profit=4.25), self._stats(),
            self._stats(wins=3, losses=0, profit=1.85), self._stats(),
            self._stats(wins=2, losses=1, profit=0.95), self._stats(),
            date_label="2026-09-21",
        )
        assert "12-3-1" in text
        assert "3-0" in text
        assert "2-1" in text

    def test_profit_sign_is_explicit(self):
        text = format_daily_results_summary(
            self._stats(profit=4.25), self._stats(),
            self._stats(profit=-1.5), self._stats(),
            self._stats(profit=0.0), self._stats(),
            date_label="2026-09-21",
        )
        assert "+4.25u" in text
        assert "-1.50u" in text
        assert "+0.00u" in text


class TestDeliverDailyResultsSummary:
    def test_not_configured_returns_early(self, tmp_path):
        from src.discord_delivery import deliver_daily_results_summary
        from database.db_manager import init_db

        db_path = tmp_path / "results.db"
        init_db(str(db_path))

        class _Cfg:
            results_webhook_url = ""
            database_path = str(db_path)

        result = deliver_daily_results_summary(_Cfg())
        assert result == {"configured": False, "sent": 0, "errors": 0}

    def test_configured_sends_and_queries_all_three_categories(self, tmp_path):
        from src.discord_delivery import deliver_daily_results_summary
        from database.db_manager import init_db, get_connection

        db_path = tmp_path / "results.db"
        init_db(str(db_path))
        conn = get_connection(str(db_path))
        _insert_ev_pick(conn, "r1", settlement_status="WIN", profit_units=0.9)
        _insert_arb(conn, "a1", profit_units=1.2)
        _insert_middle(conn, "m1", profit_units=0.5)
        conn.close()

        class _Cfg:
            results_webhook_url = "https://discord.com/api/webhooks/results1"
            database_path = str(db_path)

        with mock.patch("src.discord_delivery._send_webhook_raw", return_value=True) as mocked:
            result = deliver_daily_results_summary(_Cfg(), date_label="2026-09-21")

        assert result["configured"] is True
        assert result["errors"] == 0
        mocked.assert_called()
        sent_text = mocked.call_args[0][1]["content"]
        assert "2026-09-21" in sent_text
        assert "0.90u" in sent_text or "+0.90u" in sent_text

    def test_never_logs_or_returns_the_webhook_url(self, tmp_path, caplog):
        from src.discord_delivery import deliver_daily_results_summary
        from database.db_manager import init_db

        db_path = tmp_path / "results.db"
        init_db(str(db_path))

        class _Cfg:
            results_webhook_url = "https://discord.com/api/webhooks/results1/secrettoken"
            database_path = str(db_path)

        with mock.patch("src.discord_delivery._send_webhook_raw", return_value=True):
            result = deliver_daily_results_summary(_Cfg())
        assert "secrettoken" not in str(result)
        for record in caplog.records:
            assert "secrettoken" not in record.getMessage()
