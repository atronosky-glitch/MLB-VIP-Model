"""Tests for src/customer_performance.py -- My Performance page
aggregation. Covers section 54's explicit scenario list: zero bets,
single win/loss, multiple bets, push/void, open vs settled, per-platform
filtering, PAPER/LIVE separation, date filters, ROI/wagered/P&L math,
graph data, and customer isolation."""

from __future__ import annotations

import uuid

from database.db_manager import save_autobet_execution
from src.customer_performance import get_customer_performance


def _execution(**overrides):
    base = {
        "execution_id": str(uuid.uuid4()), "account_id": "acct-1", "recommendation_id": "rec-1",
        "market_type": "game_moneyline", "matchup": "Away @ Home", "side": "YES",
        "model_ev_pct": 3.5, "price_at_detection": 0.60, "price_at_execution": 0.60,
        "stake_usd": 10.0, "filled_quantity": 16.6667, "avg_fill_price": 0.60,
        "status": "EXECUTED", "mode": "LIVE", "approval_mode": "AUTO", "platform": "polymarket_us",
    }
    base.update(overrides)
    return base


def _settle(conn, recommendation_id, status, settled_at="2026-09-20T00:00:00+00:00"):
    conn.execute(
        """INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at)
           VALUES (?, ?, ?, ?)""",
        (str(uuid.uuid4()), recommendation_id, status, settled_at),
    )
    conn.commit()


class TestEmptyAndBasicCases:
    def test_zero_bets_returns_clean_zeros(self, db_conn):
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 0
        assert perf["realized_pnl_usd"] == 0
        assert perf["roi_pct"] is None
        assert perf["win_rate_pct"] is None
        assert perf["cumulative_pnl_series"] == []

    def test_single_win(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0, filled_quantity=16.6667))
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["wins"] == 1
        assert perf["losses"] == 0
        # payout 16.6667 * 1.00 - 10.0 stake = 6.6667
        assert round(perf["realized_pnl_usd"], 2) == 6.67

    def test_single_loss(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0))
        _settle(db_conn, "rec-1", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["losses"] == 1
        assert perf["realized_pnl_usd"] == -10.0

    def test_push_refunds_with_zero_pnl(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0))
        _settle(db_conn, "rec-1", "PUSH")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pushes_voids"] == 1
        assert perf["realized_pnl_usd"] == 0
        assert perf["settled_wagered_usd"] == 10.0

    def test_void_refunds_with_zero_pnl(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0))
        _settle(db_conn, "rec-1", "VOID")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pushes_voids"] == 1
        assert perf["realized_pnl_usd"] == 0


class TestOpenVsSettled:
    def test_unresolved_recommendation_counts_as_open_not_settled(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0))
        # No market_settlements row at all -- never graded yet.
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["open_positions"] == 1
        assert perf["open_exposure_usd"] == 10.0
        assert perf["total_bets"] == 1
        assert perf["wins"] == 0
        assert perf["losses"] == 0
        assert perf["realized_pnl_usd"] == 0
        assert perf["roi_pct"] is None  # no settled stake to compute ROI against

    def test_open_position_never_treated_as_a_loss(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=50.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_usd"] == 0
        assert perf["losses"] == 0

    def test_unresolved_market_settlements_row_treated_as_open(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0))
        _settle(db_conn, "rec-1", "UNRESOLVED")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["open_positions"] == 1

    def test_mix_of_open_and_settled(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-win", stake_usd=10.0, filled_quantity=20.0))
        _settle(db_conn, "rec-win", "WIN")
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-open", stake_usd=15.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 2
        assert perf["wins"] == 1
        assert perf["open_positions"] == 1
        assert perf["open_exposure_usd"] == 15.0
        assert perf["realized_pnl_usd"] == 10.0  # 20 - 10


class TestSkippedAndFailedExcluded:
    def test_skipped_rows_never_counted_as_a_bet(self, db_conn):
        save_autobet_execution(db_conn, _execution(status="SKIPPED", stake_usd=None))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 0

    def test_failed_rows_never_counted_as_a_bet(self, db_conn):
        save_autobet_execution(db_conn, _execution(status="FAILED", stake_usd=None))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 0

    def test_partially_filled_is_counted(self, db_conn):
        save_autobet_execution(db_conn, _execution(status="PARTIALLY_FILLED", recommendation_id="rec-1", stake_usd=5.0))
        _settle(db_conn, "rec-1", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 1
        assert perf["losses"] == 1


class TestPlatformFiltering:
    def test_kalshi_only_filter(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-k", platform="kalshi", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-p", platform="polymarket_us", stake_usd=10.0, filled_quantity=20.0))
        _settle(db_conn, "rec-k", "WIN")
        _settle(db_conn, "rec-p", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="kalshi")
        assert perf["total_bets"] == 1
        assert perf["realized_pnl_usd"] == 10.0

    def test_polymarket_filter_includes_legacy_null_platform_rows(self, db_conn):
        row = _execution(recommendation_id="rec-legacy", stake_usd=10.0, filled_quantity=20.0)
        del row["platform"]
        save_autobet_execution(db_conn, row)
        _settle(db_conn, "rec-legacy", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="polymarket_us")
        assert perf["total_bets"] == 1

    def test_no_platform_filter_combines_both(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-k", platform="kalshi", stake_usd=10.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-p", platform="polymarket_us", stake_usd=10.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 2

    def test_realized_pnl_by_platform_breakdown(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-k", platform="kalshi", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-p", platform="polymarket_us", stake_usd=10.0))
        _settle(db_conn, "rec-k", "WIN")
        _settle(db_conn, "rec-p", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_by_platform"]["kalshi"] == 10.0
        assert perf["realized_pnl_by_platform"]["polymarket_us"] == -10.0


class TestPaperLiveSeparation:
    def test_paper_and_live_are_never_blended(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-live", mode="LIVE", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-paper", mode="PAPER", stake_usd=999.0, filled_quantity=2000.0))
        _settle(db_conn, "rec-live", "WIN")
        _settle(db_conn, "rec-paper", "WIN")
        live_perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        paper_perf = get_customer_performance(db_conn, "acct-1", mode="PAPER")
        assert live_perf["total_bets"] == 1
        assert paper_perf["total_bets"] == 1
        assert live_perf["realized_pnl_usd"] == 10.0
        assert paper_perf["realized_pnl_usd"] == 1001.0

    def test_invalid_mode_raises(self, db_conn):
        import pytest
        with pytest.raises(ValueError):
            get_customer_performance(db_conn, "acct-1", mode="BOTH")


class TestDateFilter:
    def test_days_filter_excludes_older_bets(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-old", stake_usd=10.0))
        db_conn.execute(
            "UPDATE customer_autobet_executions SET created_at = '2020-01-01T00:00:00+00:00' WHERE recommendation_id = 'rec-old'"
        )
        db_conn.commit()
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-new", stake_usd=5.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", days=7)
        assert perf["total_bets"] == 1


class TestROIMath:
    def test_roi_computed_against_settled_stake_only(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-win", stake_usd=10.0, filled_quantity=20.0))
        _settle(db_conn, "rec-win", "WIN")
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-open", stake_usd=1000.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        # ROI should use the $10 settled stake, not the $1000 still-open one.
        assert perf["settled_wagered_usd"] == 10.0
        assert round(perf["roi_pct"], 1) == 100.0

    def test_win_rate_excludes_pushes_from_denominator(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-w", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-l", stake_usd=10.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-push", stake_usd=10.0))
        _settle(db_conn, "rec-w", "WIN")
        _settle(db_conn, "rec-l", "LOSS")
        _settle(db_conn, "rec-push", "PUSH")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["win_rate_pct"] == 50.0  # 1 win / (1 win + 1 loss), push excluded


class TestGraphData:
    def test_cumulative_series_is_ordered_and_running(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-1", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-2", stake_usd=10.0))
        _settle(db_conn, "rec-1", "WIN", settled_at="2026-09-21T00:00:00+00:00")
        _settle(db_conn, "rec-2", "LOSS", settled_at="2026-09-22T00:00:00+00:00")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        series = perf["cumulative_pnl_series"]
        assert len(series) == 2
        assert series[0]["cumulative_pnl"] == 10.0
        assert series[1]["cumulative_pnl"] == 0.0  # 10 - 10

    def test_open_positions_never_appear_in_the_graph(self, db_conn):
        save_autobet_execution(db_conn, _execution(recommendation_id="rec-open", stake_usd=10.0))
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["cumulative_pnl_series"] == []


class TestCustomerIsolation:
    def test_one_customers_performance_never_includes_another(self, db_conn):
        save_autobet_execution(db_conn, _execution(account_id="acct-A", recommendation_id="rec-1", stake_usd=10.0, filled_quantity=20.0))
        save_autobet_execution(db_conn, _execution(account_id="acct-B", recommendation_id="rec-1", stake_usd=9999.0, filled_quantity=99999.0))
        _settle(db_conn, "rec-1", "WIN")
        perf_a = get_customer_performance(db_conn, "acct-A", mode="LIVE")
        assert perf_a["total_bets"] == 1
        assert perf_a["realized_pnl_usd"] == 10.0
