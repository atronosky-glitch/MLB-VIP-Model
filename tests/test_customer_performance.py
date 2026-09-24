"""Tests for src/customer_performance.py -- My Performance aggregation on
REAL EXECUTION ECONOMICS: cost basis is filled_quantity x avg_fill_price
(never the intended stake or the recommendation price), fees are netted
only when known (and the headline is labeled gross/estimated otherwise),
unfilled orders are not wagers, open positions stay unrealized, and
push/void outcomes are never booked as P&L."""

from __future__ import annotations

import uuid

import pytest

from database.db_manager import save_autobet_execution
from src.customer_performance import get_customer_performance


def _execution(**overrides):
    """A LIVE, fully-filled 20 contracts @ $0.50 = $10.00 cost. Intended
    stake and detection/recommendation prices are deliberately different
    from what was actually filled, so any calculation that leaks them
    shows up as a wrong number."""
    base = {
        "execution_id": str(uuid.uuid4()), "account_id": "acct-1", "recommendation_id": "rec-1",
        "market_type": "game_moneyline", "matchup": "Away @ Home", "side": "YES",
        "model_ev_pct": 3.5, "price_at_detection": 0.44, "price_at_execution": 0.50,
        "stake_usd": 25.0,                       # intended -- must never drive P&L
        "requested_quantity": 20.0, "filled_quantity": 20.0, "avg_fill_price": 0.50,
        "fees_usd": None, "fees_source": None, "fill_source": "ORDER_RESPONSE",
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


def _save(conn, **overrides):
    save_autobet_execution(conn, _execution(**overrides))


class TestEmptyAndBasicCases:
    def test_zero_bets_returns_clean_zeros(self, db_conn):
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 0
        assert perf["realized_pnl_usd"] == 0
        assert perf["roi_pct"] is None
        assert perf["win_rate_pct"] is None
        assert perf["cumulative_pnl_series"] == []

    def test_single_win_uses_actual_cost_not_intended_stake(self, db_conn):
        _save(db_conn)                                   # 20 @ 0.50, intended stake 25
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["wins"] == 1 and perf["losses"] == 0
        assert perf["total_wagered_usd"] == pytest.approx(10.0)      # not 25
        assert perf["realized_pnl_usd"] == pytest.approx(10.0)       # 20*1.00 - 10.00

    def test_single_loss_loses_only_what_was_actually_paid(self, db_conn):
        _save(db_conn)
        _settle(db_conn, "rec-1", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["losses"] == 1
        assert perf["realized_pnl_usd"] == pytest.approx(-10.0)      # not -25


class TestExecutionPriceDiffersFromRecommendationPrice:
    def test_pnl_uses_the_customers_real_fill_price(self, db_conn):
        # Model/detection saw 0.44; the customer actually paid 0.62.
        _save(db_conn, price_at_detection=0.44, price_at_execution=0.62,
              avg_fill_price=0.62, filled_quantity=10.0, requested_quantity=10.0)
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_usd"] == pytest.approx(3.80)       # 10 - 6.20
        # (a calculation from the 0.44 detection price would say +5.60)

    def test_a_worse_fill_can_turn_a_winning_side_into_less_profit(self, db_conn):
        _save(db_conn, avg_fill_price=0.90, filled_quantity=10.0, requested_quantity=10.0)
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_usd"] == pytest.approx(1.0)


class TestPartialAndUnfilledOrders:
    def test_partial_fill_counts_only_the_filled_contracts(self, db_conn):
        _save(db_conn, status="PARTIALLY_FILLED", requested_quantity=20.0,
              filled_quantity=8.0, avg_fill_price=0.50, stake_usd=10.0)
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_wagered_usd"] == pytest.approx(4.0)       # 8 * 0.50, not the $10 intent
        assert perf["realized_pnl_usd"] == pytest.approx(4.0)        # 8 - 4
        assert perf["partial_fills"] == 1
        assert perf["unfilled_contracts"] == pytest.approx(12.0)

    def test_partial_fill_loss_is_only_the_filled_cost(self, db_conn):
        _save(db_conn, status="PARTIALLY_FILLED", requested_quantity=20.0,
              filled_quantity=8.0, avg_fill_price=0.50)
        _settle(db_conn, "rec-1", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_usd"] == pytest.approx(-4.0)

    def test_unfilled_order_has_no_effect_on_realized_pnl_or_wagered(self, db_conn):
        _save(db_conn, recommendation_id="rec-unfilled", requested_quantity=20.0,
              filled_quantity=0.0, avg_fill_price=None, stake_usd=25.0, fill_source=None)
        _settle(db_conn, "rec-unfilled", "LOSS")
        _save(db_conn, recommendation_id="rec-filled")
        _settle(db_conn, "rec-filled", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["unfilled_orders"] == 1
        assert perf["total_bets"] == 1
        assert perf["losses"] == 0                                   # the unfilled "loss" never happened
        assert perf["total_wagered_usd"] == pytest.approx(10.0)
        assert perf["realized_pnl_usd"] == pytest.approx(10.0)
        assert perf["unfilled_contracts"] == pytest.approx(20.0)

    def test_quantity_without_a_readable_price_is_never_guessed(self, db_conn):
        _save(db_conn, avg_fill_price=None, filled_quantity=5.0)
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["unconfirmed_fill_orders"] == 1
        assert perf["total_bets"] == 0
        assert perf["realized_pnl_usd"] == 0


class TestFeesAndPnlLabeling:
    def test_platform_reported_fees_are_netted_and_labeled_net(self, db_conn):
        _save(db_conn, fees_usd=0.35, fees_source="PLATFORM", fill_source="PLATFORM_FILLS")
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pnl_basis"] == "NET"
        assert perf["realized_pnl_usd"] == pytest.approx(9.65)       # 10 - 0.35
        assert perf["gross_pnl_usd"] == pytest.approx(10.0)
        assert perf["fees_usd"] == pytest.approx(0.35)
        # ROI base = cost + fees
        assert perf["roi_pct"] == pytest.approx(9.65 / 10.35 * 100)

    def test_fees_make_a_loss_larger(self, db_conn):
        _save(db_conn, fees_usd=0.35, fees_source="PLATFORM")
        _settle(db_conn, "rec-1", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_usd"] == pytest.approx(-10.35)

    def test_estimated_fees_are_netted_but_labeled_as_estimated(self, db_conn):
        _save(db_conn, fees_usd=0.35, fees_source="ESTIMATED")
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pnl_basis"] == "NET_ESTIMATED_FEES"
        assert perf["realized_pnl_usd"] == pytest.approx(9.65)
        assert "estimated" in perf["pnl_label"].lower()

    def test_missing_fees_make_the_headline_gross_never_net(self, db_conn):
        _save(db_conn, recommendation_id="rec-a", fees_usd=0.35, fees_source="PLATFORM")
        _save(db_conn, recommendation_id="rec-b", fees_usd=None, fees_source=None)
        _settle(db_conn, "rec-a", "WIN")
        _settle(db_conn, "rec-b", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pnl_basis"] == "GROSS"
        assert perf["realized_pnl_usd"] == pytest.approx(20.0)       # fees excluded consistently
        assert "gross" in perf["pnl_label"].lower()
        assert perf["roi_pct"] == pytest.approx(100.0)               # 20 / 20 cost


class TestOpenVsSettled:
    def test_open_position_is_unrealized_exposure_not_pnl(self, db_conn):
        _save(db_conn, fees_usd=0.35, fees_source="PLATFORM")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")      # never graded
        assert perf["open_positions"] == 1
        assert perf["open_exposure_usd"] == pytest.approx(10.35)     # actual cost + fees, not $25
        assert perf["realized_pnl_usd"] == 0
        assert perf["wins"] == 0 and perf["losses"] == 0
        assert perf["roi_pct"] is None
        assert perf["cumulative_pnl_series"] == []

    def test_unresolved_settlement_row_is_still_open(self, db_conn):
        _save(db_conn)
        _settle(db_conn, "rec-1", "UNRESOLVED")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["open_positions"] == 1
        assert perf["realized_pnl_usd"] == 0

    def test_mix_of_open_and_settled(self, db_conn):
        _save(db_conn, recommendation_id="rec-win")
        _settle(db_conn, "rec-win", "WIN")
        _save(db_conn, recommendation_id="rec-open", filled_quantity=30.0, requested_quantity=30.0,
              avg_fill_price=0.50)
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["total_bets"] == 2
        assert perf["wins"] == 1
        assert perf["open_positions"] == 1
        assert perf["open_exposure_usd"] == pytest.approx(15.0)
        assert perf["realized_pnl_usd"] == pytest.approx(10.0)
        assert perf["settled_wagered_usd"] == pytest.approx(10.0)    # open cost not in the ROI base


class TestPushVoidNeverBookedAsPnl:
    @pytest.mark.parametrize("status", ["PUSH", "VOID", "CANCELLED"])
    def test_excluded_from_realized_pnl_and_roi_base(self, db_conn, status):
        _save(db_conn)
        _settle(db_conn, "rec-1", status)
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["pushes_voids"] == 1
        assert perf["realized_pnl_usd"] == 0
        assert perf["settled_wagered_usd"] == 0
        assert perf["refund_pending_usd"] == pytest.approx(10.0)
        assert perf["open_positions"] == 0
        assert perf["roi_pct"] is None


class TestSettledKalshiAndPolymarketExecutions:
    def test_settled_kalshi_execution_from_reconciled_fills(self, db_conn):
        _save(db_conn, platform="kalshi", recommendation_id="rec-k", side="NO",
              requested_quantity=30.0, filled_quantity=30.0, avg_fill_price=0.38,
              fees_usd=0.52, fees_source="PLATFORM", fill_source="PLATFORM_FILLS",
              price_at_detection=0.40, price_at_execution=0.38)
        _settle(db_conn, "rec-k", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="kalshi")
        # cost 30*0.38 = 11.40; payout 30.00; fees 0.52 => 18.08
        assert perf["realized_pnl_usd"] == pytest.approx(18.08)
        assert perf["pnl_basis"] == "NET"
        assert perf["fills_confirmed"] is True
        assert perf["realized_pnl_by_platform"]["kalshi"] == pytest.approx(18.08)

    def test_unreconciled_kalshi_limit_price_is_flagged_unconfirmed(self, db_conn):
        _save(db_conn, platform="kalshi", fill_source="ORDER_LIMIT_PRICE",
              fees_usd=0.20, fees_source="ESTIMATED")
        _settle(db_conn, "rec-1", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="kalshi")
        assert perf["fills_confirmed"] is False
        assert perf["unreconciled_orders"] == 1

    def test_settled_polymarket_execution_from_recorded_fill(self, db_conn):
        _save(db_conn, platform="polymarket_us", recommendation_id="rec-p",
              requested_quantity=16.0, filled_quantity=16.0, avg_fill_price=0.6125,
              fees_usd=0.24, fees_source="ESTIMATED", fill_source="ORDER_RESPONSE")
        _settle(db_conn, "rec-p", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="polymarket_us")
        assert perf["realized_pnl_usd"] == pytest.approx(-(16 * 0.6125 + 0.24))
        assert perf["pnl_basis"] == "NET_ESTIMATED_FEES"
        assert perf["fills_confirmed"] is True


class TestSkippedAndFailedExcluded:
    def test_skipped_rows_never_counted_as_a_bet(self, db_conn):
        _save(db_conn, status="SKIPPED", stake_usd=None)
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE")["total_bets"] == 0

    def test_failed_rows_never_counted_as_a_bet(self, db_conn):
        _save(db_conn, status="FAILED", stake_usd=None)
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE")["total_bets"] == 0


class TestPlatformFiltering:
    def test_kalshi_only_filter(self, db_conn):
        _save(db_conn, recommendation_id="rec-k", platform="kalshi")
        _save(db_conn, recommendation_id="rec-p", platform="polymarket_us")
        _settle(db_conn, "rec-k", "WIN")
        _settle(db_conn, "rec-p", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="kalshi")
        assert perf["total_bets"] == 1
        assert perf["realized_pnl_usd"] == pytest.approx(10.0)

    def test_polymarket_filter_includes_legacy_null_platform_rows(self, db_conn):
        row = _execution(recommendation_id="rec-legacy")
        del row["platform"]
        save_autobet_execution(db_conn, row)
        _settle(db_conn, "rec-legacy", "WIN")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="polymarket_us")
        assert perf["total_bets"] == 1

    def test_no_platform_filter_combines_both(self, db_conn):
        _save(db_conn, recommendation_id="rec-k", platform="kalshi")
        _save(db_conn, recommendation_id="rec-p", platform="polymarket_us")
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE")["total_bets"] == 2

    def test_realized_pnl_by_platform_breakdown(self, db_conn):
        _save(db_conn, recommendation_id="rec-k", platform="kalshi")
        _save(db_conn, recommendation_id="rec-p", platform="polymarket_us")
        _settle(db_conn, "rec-k", "WIN")
        _settle(db_conn, "rec-p", "LOSS")
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["realized_pnl_by_platform"]["kalshi"] == pytest.approx(10.0)
        assert perf["realized_pnl_by_platform"]["polymarket_us"] == pytest.approx(-10.0)


class TestPaperLiveSeparation:
    def test_paper_and_live_are_never_blended(self, db_conn):
        _save(db_conn, recommendation_id="rec-live", mode="LIVE")
        _save(db_conn, recommendation_id="rec-paper", mode="PAPER", fill_source="SIMULATED",
              filled_quantity=100.0, requested_quantity=100.0)
        _settle(db_conn, "rec-live", "WIN")
        _settle(db_conn, "rec-paper", "WIN")
        live = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        paper = get_customer_performance(db_conn, "acct-1", mode="PAPER")
        assert live["total_bets"] == 1 and paper["total_bets"] == 1
        assert live["realized_pnl_usd"] == pytest.approx(10.0)
        assert paper["realized_pnl_usd"] == pytest.approx(50.0)
        assert paper["simulated"] is True and live["simulated"] is False

    def test_invalid_mode_raises(self, db_conn):
        with pytest.raises(ValueError):
            get_customer_performance(db_conn, "acct-1", mode="BOTH")


class TestDateFilter:
    def test_days_filter_excludes_older_bets(self, db_conn):
        _save(db_conn, recommendation_id="rec-old")
        db_conn.execute(
            "UPDATE customer_autobet_executions SET created_at = '2020-01-01T00:00:00+00:00' "
            "WHERE recommendation_id = 'rec-old'"
        )
        db_conn.commit()
        _save(db_conn, recommendation_id="rec-new")
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE", days=7)["total_bets"] == 1


class TestROIAndWinRate:
    def test_roi_uses_settled_cost_only(self, db_conn):
        _save(db_conn, recommendation_id="rec-win")
        _settle(db_conn, "rec-win", "WIN")
        _save(db_conn, recommendation_id="rec-open", filled_quantity=2000.0, requested_quantity=2000.0)
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE")
        assert perf["settled_wagered_usd"] == pytest.approx(10.0)
        assert perf["roi_pct"] == pytest.approx(100.0)

    def test_win_rate_excludes_pushes_from_denominator(self, db_conn):
        _save(db_conn, recommendation_id="rec-w")
        _save(db_conn, recommendation_id="rec-l")
        _save(db_conn, recommendation_id="rec-push")
        _settle(db_conn, "rec-w", "WIN")
        _settle(db_conn, "rec-l", "LOSS")
        _settle(db_conn, "rec-push", "PUSH")
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE")["win_rate_pct"] == 50.0


class TestGraphData:
    def test_cumulative_series_is_ordered_running_and_matches_headline_basis(self, db_conn):
        _save(db_conn, recommendation_id="rec-1", fees_usd=0.5, fees_source="PLATFORM")
        _save(db_conn, recommendation_id="rec-2", fees_usd=0.5, fees_source="PLATFORM")
        _settle(db_conn, "rec-1", "WIN", settled_at="2026-09-21T00:00:00+00:00")
        _settle(db_conn, "rec-2", "LOSS", settled_at="2026-09-22T00:00:00+00:00")
        series = get_customer_performance(db_conn, "acct-1", mode="LIVE")["cumulative_pnl_series"]
        assert [round(p["cumulative_pnl"], 2) for p in series] == [9.5, -1.0]   # +9.50 then -10.50

    def test_open_positions_never_appear_in_the_graph(self, db_conn):
        _save(db_conn)
        assert get_customer_performance(db_conn, "acct-1", mode="LIVE")["cumulative_pnl_series"] == []


class TestCustomerIsolation:
    def test_one_customers_performance_never_includes_another(self, db_conn):
        _save(db_conn, account_id="acct-A", recommendation_id="rec-1")
        _save(db_conn, account_id="acct-B", recommendation_id="rec-1",
              filled_quantity=99999.0, requested_quantity=99999.0)
        _settle(db_conn, "rec-1", "WIN")
        perf_a = get_customer_performance(db_conn, "acct-A", mode="LIVE")
        assert perf_a["total_bets"] == 1
        assert perf_a["realized_pnl_usd"] == pytest.approx(10.0)
