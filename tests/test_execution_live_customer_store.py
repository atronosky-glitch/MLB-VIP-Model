"""Tests for src/execution/live/customer_store.py -- per-customer-
scoped queries against the live-execution tables, and the account_id
parameter on build_live_risk_context (src/execution/live/
revalidation.py). Core guarantee: two different customers' exposure/
positions/trades never mix, and the operator's own manual trading
(account_id IS NULL) is never affected by, or counted toward, any
customer's limits."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import pytest

from src.execution.live import customer_store
from src.execution.live.revalidation import build_live_risk_context


def _insert_prepared_order(conn, prepared_order_id, account_id, league="MLB"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO prepared_live_orders (
               prepared_order_id, recommendation_id, provider, provider_market_id, league,
               event, event_id, side, fingerprint, status, created_at, expires_at, account_id
           ) VALUES (?, 'rec-1', 'polymarket_us', 'mkt-1', ?, 'E', 'evt-1', 'YES', 'fp-1',
                     'READY', ?, ?, ?)""",
        (prepared_order_id, league, now, now, account_id),
    )
    conn.commit()


def _insert_live_position(conn, position_id, account_id, *, total_entry_cost=10.0, status="OPEN",
                           realized_pnl=None, settled_at=None, opened_at=None, event_id="evt-1",
                           provider="polymarket_us", recommendation_id="rec-1", side="YES"):
    conn.execute(
        """INSERT INTO live_positions (
               position_id, approval_id, prepared_order_id, live_order_id, recommendation_id,
               provider, provider_market_id, event_id, side, quantity, total_entry_cost,
               opened_at, status, settled_at, realized_pnl, account_id
           ) VALUES (?, 'appr-1', 1, 1, ?, ?, 'mkt-1', ?, ?, 10, ?, ?, ?, ?, ?, ?)""",
        (position_id, recommendation_id, provider, event_id, side, total_entry_cost,
         opened_at or datetime.now(timezone.utc).isoformat(), status, settled_at, realized_pnl, account_id),
    )
    conn.commit()


class TestTagging:
    def test_tag_prepared_order_with_account(self, db_conn):
        _insert_prepared_order(db_conn, 1, None)
        customer_store.tag_prepared_order_with_account(db_conn, 1, "acct-1")
        assert customer_store.get_account_id_for_prepared_order(db_conn, 1) == "acct-1"

    def test_untagged_prepared_order_has_no_account(self, db_conn):
        _insert_prepared_order(db_conn, 1, None)
        assert customer_store.get_account_id_for_prepared_order(db_conn, 1) is None

    def test_tag_position_with_account(self, db_conn):
        _insert_live_position(db_conn, 1, None)
        customer_store.tag_position_with_account(db_conn, 1, "acct-1")
        assert customer_store.get_account_id_for_position(db_conn, 1) == "acct-1"


class TestExposureIsolation:
    def test_open_positions_scoped_per_account(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A")
        _insert_live_position(db_conn, 2, "acct-B")
        _insert_live_position(db_conn, 3, "acct-B")
        assert customer_store.count_open_live_positions_for_account(db_conn, "acct-A") == 1
        assert customer_store.count_open_live_positions_for_account(db_conn, "acct-B") == 2

    def test_operators_own_untagged_positions_never_count_for_a_customer(self, db_conn):
        _insert_live_position(db_conn, 1, None)  # operator's own manual trade
        assert customer_store.count_open_live_positions_for_account(db_conn, "acct-A") == 0

    def test_total_exposure_scoped_per_account(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", total_entry_cost=10.0)
        _insert_live_position(db_conn, 2, "acct-A", total_entry_cost=15.0)
        _insert_live_position(db_conn, 3, "acct-B", total_entry_cost=1000.0)
        assert customer_store.sum_open_live_exposure_for_account(db_conn, "acct-A") == Decimal("25.0")

    def test_closed_positions_excluded_from_open_exposure(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", total_entry_cost=10.0, status="OPEN")
        _insert_live_position(db_conn, 2, "acct-A", total_entry_cost=999.0, status="WON")
        assert customer_store.sum_open_live_exposure_for_account(db_conn, "acct-A") == Decimal("10.0")

    def test_event_scoped_exposure(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", total_entry_cost=10.0, event_id="evt-1")
        _insert_live_position(db_conn, 2, "acct-A", total_entry_cost=20.0, event_id="evt-2")
        assert customer_store.sum_open_live_exposure_for_account(db_conn, "acct-A", event_id="evt-1") == Decimal("10.0")

    def test_provider_scoped_exposure(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", total_entry_cost=10.0, provider="polymarket_us")
        _insert_live_position(db_conn, 2, "acct-A", total_entry_cost=20.0, provider="kalshi")
        assert customer_store.sum_open_live_exposure_for_account(db_conn, "acct-A", provider="polymarket_us") == Decimal("10.0")

    def test_league_scoped_exposure_joins_through_prepared_orders(self, db_conn):
        _insert_prepared_order(db_conn, 1, "acct-A", league="MLB")
        _insert_prepared_order(db_conn, 2, "acct-A", league="NFL")
        conn = db_conn
        conn.execute(
            """INSERT INTO live_positions (
                   position_id, approval_id, prepared_order_id, live_order_id, recommendation_id,
                   provider, provider_market_id, event_id, side, quantity, total_entry_cost,
                   opened_at, status, account_id
               ) VALUES (1, 'a', 1, 1, 'rec-1', 'polymarket_us', 'mkt-1', 'evt-1', 'YES', 10, 10.0, ?, 'OPEN', 'acct-A')""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """INSERT INTO live_positions (
                   position_id, approval_id, prepared_order_id, live_order_id, recommendation_id,
                   provider, provider_market_id, event_id, side, quantity, total_entry_cost,
                   opened_at, status, account_id
               ) VALUES (2, 'a', 2, 1, 'rec-2', 'polymarket_us', 'mkt-2', 'evt-2', 'YES', 10, 20.0, ?, 'OPEN', 'acct-A')""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        assert customer_store.sum_open_live_exposure_for_account(db_conn, "acct-A", league="MLB") == Decimal("10.0")

    def test_duplicate_check_scoped_per_account(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", recommendation_id="rec-1", provider="polymarket_us", side="YES")
        assert customer_store.get_open_live_position_by_fingerprint_for_account(
            db_conn, "acct-A", "rec-1", "polymarket_us", "YES",
        ) is not None
        assert customer_store.get_open_live_position_by_fingerprint_for_account(
            db_conn, "acct-B", "rec-1", "polymarket_us", "YES",
        ) is None


class TestBuildLiveRiskContextAccountScoping:
    def _fake_provider(self):
        provider = mock.MagicMock()
        provider.get_balance.return_value = mock.MagicMock(available=1000.0)
        return provider

    def test_none_account_id_uses_global_scope_unchanged(self, db_conn):
        """Regression: the existing operator call site (no account_id
        kwarg passed at all) must behave identically to before."""
        _insert_live_position(db_conn, 1, None, total_entry_cost=50.0)
        ctx = build_live_risk_context(
            db_conn, self._fake_provider(), "evt-1", "polymarket_us", "MLB", "rec-1", "YES",
        )
        assert ctx.total_open_exposure_usd == Decimal("50.0")

    def test_customer_scoped_context_excludes_the_operators_own_trades(self, db_conn):
        _insert_live_position(db_conn, 1, None, total_entry_cost=9999.0)  # operator's own
        _insert_live_position(db_conn, 2, "acct-A", total_entry_cost=25.0)
        ctx = build_live_risk_context(
            db_conn, self._fake_provider(), "evt-1", "polymarket_us", "MLB", "rec-1", "YES",
            account_id="acct-A",
        )
        assert ctx.total_open_exposure_usd == Decimal("25.0")

    def test_two_customers_contexts_are_independent(self, db_conn):
        _insert_live_position(db_conn, 1, "acct-A", total_entry_cost=25.0)
        _insert_live_position(db_conn, 2, "acct-B", total_entry_cost=500.0)
        ctx_a = build_live_risk_context(
            db_conn, self._fake_provider(), "evt-1", "polymarket_us", "MLB", "rec-1", "YES",
            account_id="acct-A",
        )
        ctx_b = build_live_risk_context(
            db_conn, self._fake_provider(), "evt-1", "polymarket_us", "MLB", "rec-1", "YES",
            account_id="acct-B",
        )
        assert ctx_a.total_open_exposure_usd == Decimal("25.0")
        assert ctx_b.total_open_exposure_usd == Decimal("500.0")

    def test_bankroll_comes_from_the_passed_in_providers_own_balance(self, db_conn):
        """Correctly-scoped bankroll relies entirely on the CALLER
        passing a per-customer provider instance (see
        src/execution/customer_autobet.py) -- this test proves the
        function itself never substitutes a different balance."""
        provider = mock.MagicMock()
        provider.get_balance.return_value = mock.MagicMock(available=42.0)
        ctx = build_live_risk_context(
            db_conn, provider, "evt-1", "polymarket_us", "MLB", "rec-1", "YES", account_id="acct-A",
        )
        assert ctx.available_bankroll_usd == Decimal("42.0")
