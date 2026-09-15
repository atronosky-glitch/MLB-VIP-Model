"""Tests for src/execution/paper/portfolio.py against the db_conn
fixture. Covers bankroll arithmetic, exposure grouping (for
RiskContext), and daily/range stats computed on demand."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution.paper import portfolio, store
from src.execution.paper.models import PaperOrder, PaperOrderStatus, PaperPosition, PaperPositionStatus


def _order(**overrides) -> PaperOrder:
    now = datetime.now(timezone.utc)
    defaults = dict(
        paper_order_id=None, opportunity_id=1, recommendation_id="rec-1", provider="kalshi",
        provider_market_id="M1", league="MLB", event="Athletics @ Toronto Blue Jays", side="YES",
        sizing_mode="FLAT", model_probability=Decimal("0.72"), net_ev_pct=Decimal("3.5"),
        requested_units=Decimal("1"), requested_stake=Decimal("10"), approved_units=Decimal("1"),
        approved_stake=Decimal("10"), requested_quantity=Decimal("14"), limit_price=Decimal("0.69"),
        fingerprint="fp-1", status=PaperOrderStatus.FILLED, rejection_reason=None,
        limiting_constraint=None, submitted_at=now, created_at=now,
    )
    defaults.update(overrides)
    return PaperOrder(**defaults)


def _position(**overrides) -> PaperPosition:
    now = datetime.now(timezone.utc)
    defaults = dict(
        position_id=None, paper_order_id=1, recommendation_id="rec-1", provider="kalshi",
        provider_market_id="M1", event_id="event-A", league="MLB",
        side="YES", quantity=Decimal("14"), average_entry_price=Decimal("0.68"), entry_cost=Decimal("9.73"),
        fees_paid=Decimal("0.23"), model_probability_at_entry=Decimal("0.72"), net_ev_at_entry=Decimal("3.5"),
        fingerprint="fp-1", opened_at=now, status=PaperPositionStatus.OPEN,
        settled_at=None, settlement_value=None, realized_pnl=None,
    )
    defaults.update(overrides)
    return PaperPosition(**defaults)


class TestBankroll:
    def test_starting_bankroll_before_any_trade(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        bankroll = portfolio.get_bankroll(db_conn)
        assert bankroll.starting_bankroll == Decimal("1000")
        assert bankroll.cash == Decimal("1000")
        assert bankroll.equity == Decimal("1000")

    def test_cash_reduced_after_open_position(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("-9.73"))
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        bankroll = portfolio.get_bankroll(db_conn)
        assert bankroll.cash == Decimal("990.27")
        assert bankroll.open_position_cost == Decimal("9.73")

    def test_equity_unchanged_by_opening_a_position(self, db_conn):
        """Equity = cash + open_position_cost -- debiting cash to open a
        position at cost doesn't change total equity, only its shape."""
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("-9.73"))
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        bankroll = portfolio.get_bankroll(db_conn)
        assert bankroll.equity == Decimal("1000.00")

    def test_available_bankroll_is_cash(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("-100"))
        bankroll = portfolio.get_bankroll(db_conn)
        assert bankroll.available_bankroll == bankroll.cash

    def test_raises_for_unknown_account(self, db_conn):
        with pytest.raises(ValueError):
            portfolio.get_bankroll(db_conn, account_id="nonexistent")

    def test_equity_reflects_realized_pnl_after_settlement(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("14"), Decimal("4.27"))
        bankroll = portfolio.get_bankroll(db_conn)
        assert bankroll.realized_pnl == Decimal("4.27")
        assert bankroll.equity == Decimal("1014.00")


class TestRiskContextExposureGrouping:
    def test_multiple_positions_bucket_correctly_by_dimension(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(
            paper_order_id=order_id, event_id="event-A", provider="kalshi", league="MLB",
            entry_cost=Decimal("10"), fingerprint="fp-a",
        ))
        store.persist_paper_position(db_conn, _position(
            paper_order_id=order_id, event_id="event-B", provider="polymarket_us", league="NFL",
            entry_cost=Decimal("20"), fingerprint="fp-b",
        ))
        context = portfolio.get_risk_context(db_conn, "event-A", "kalshi", "MLB", "fp-new")
        assert context.event_exposure_usd == Decimal("10")
        assert context.provider_exposure_usd == Decimal("10")
        assert context.sport_exposure_usd == Decimal("10")
        assert context.total_open_exposure_usd == Decimal("30")
        assert context.open_positions_count == 2

    def test_duplicate_flags_reflect_existing_positions(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="fp-open"))
        context_open = portfolio.get_risk_context(db_conn, "event-A", "kalshi", "MLB", "fp-open")
        assert context_open.has_open_duplicate is True

        settled_position_id = store.persist_paper_position(
            db_conn, _position(paper_order_id=order_id, fingerprint="fp-settled"),
        )
        store.settle_paper_position(db_conn, settled_position_id, "WON", Decimal("14"), Decimal("4.27"))
        context_settled = portfolio.get_risk_context(db_conn, "event-A", "kalshi", "MLB", "fp-settled")
        assert context_settled.has_settled_duplicate is True
        assert context_settled.has_open_duplicate is False


class TestDailyStats:
    def test_no_trades_yields_zeroed_stats(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        stats = portfolio.compute_daily_stats(db_conn, Decimal("10"))
        assert stats["trades"] == 0
        assert stats["realized_pnl"] == Decimal("0")

    def test_zero_pnl_units_display_as_plain_zero_not_scientific_notation(self, db_conn):
        """Real bug found live 2026-09-14: Decimal division preserves the
        divisor's exponent, so 0 realized_pnl / a float-derived unit size
        (e.g. Decimal(str(10.0))) can internally be Decimal('0E+1'), which
        `str()`/a bare f-string renders as "0E+1" instead of "0" in
        `paper-stats --today`. The VALUE stays full precision (never
        rounded/quantized -- see test_units_won_lost_via_daily_stats in
        test_paper_broker.py, which needs 6 decimal places preserved
        exactly); only display must avoid scientific notation, via the
        ``:f`` format spec used in src/execution/paper_cli.py."""
        store.get_or_create_account(db_conn, Decimal("1000"))
        stats = portfolio.compute_daily_stats(db_conn, Decimal(str(10.0)))
        assert stats["units_won_lost"] == Decimal("0")
        assert "E" not in f"{stats['units_won_lost']:f}"
        assert "E" not in f"{stats['roi_pct']:f}"

    def test_one_winning_trade(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order(approved_stake=Decimal("10")))
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))

        stats = portfolio.compute_daily_stats(db_conn, Decimal("10"))
        assert stats["trades"] == 1
        assert stats["wins"] == 1
        assert stats["losses"] == 0
        assert stats["realized_pnl"] == Decimal("4.27")
        assert stats["units_won_lost"] == Decimal("0.427")
        assert stats["kalshi_pnl"] == Decimal("4.27")
        assert stats["polymarket_us_pnl"] == Decimal("0")

    def test_pending_position_counted_separately_from_settled(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id))
        stats = portfolio.compute_daily_stats(db_conn, Decimal("10"))
        assert stats["pending"] == 1
        assert stats["wins"] == 0

    def test_best_and_worst_trade(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order())
        p1 = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="a"))
        p2 = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="b"))
        store.settle_paper_position(db_conn, p1, "WON", Decimal("14"), Decimal("4.27"))
        store.settle_paper_position(db_conn, p2, "LOST", Decimal("0"), Decimal("-9.73"))
        stats = portfolio.compute_daily_stats(db_conn, Decimal("10"))
        assert stats["best_trade"] == Decimal("4.27")
        assert stats["worst_trade"] == Decimal("-9.73")

    def test_roi_pct_computed_against_amount_risked(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order(approved_stake=Decimal("10")))
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))
        stats = portfolio.compute_daily_stats(db_conn, Decimal("10"))
        assert stats["roi_pct"] == Decimal("4.27") / Decimal("10") * 100

    def test_range_stats_over_multiple_days_includes_today(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        order_id = store.persist_paper_order(db_conn, _order())
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))
        stats = portfolio.compute_stats_range(db_conn, Decimal("10"), days=7)
        assert stats["trades"] == 1
        assert stats["realized_pnl"] == Decimal("4.27")
