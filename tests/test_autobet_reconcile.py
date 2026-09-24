"""Tests for src/execution/autobet_reconcile.py -- replacing a LIVE Kalshi
execution's order-response numbers (limit price, formula fee) with what
the customer's own Kalshi fills report. Providers are fakes exposing only
the read-only GETs; nothing here can place an order."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest import mock

import pytest

import src.execution.customer_autobet as autobet
from database.db_manager import save_autobet_execution
from src.customer_performance import get_customer_performance
from src.execution.autobet_reconcile import (
    reconcile_customer_fills, reconcile_execution, summarize_kalshi_fills,
)


def _fill(count=10, price_cents=40, side="yes", **extra):
    fill = {"order_id": "O-1", "ticker": "KXNFLGAME-26OCT05ATLNO-ATL", "side": side, "action": "buy",
            "count": count, f"{side}_price": price_cents, "is_taker": True}
    fill.update(extra)
    return fill


class TestSummarizeKalshiFills:
    def test_single_fill_uses_the_side_specific_price_in_cents(self):
        s = summarize_kalshi_fills([_fill(count=10, price_cents=40)], "yes")
        assert (s.quantity, s.avg_price) == (Decimal("10"), Decimal("0.4"))

    def test_multiple_fills_are_volume_weighted(self):
        s = summarize_kalshi_fills([_fill(count=10, price_cents=40), _fill(count=30, price_cents=44)], "yes")
        assert s.quantity == Decimal("40")
        assert s.avg_price == Decimal("0.43")     # (4.00 + 13.20) / 40

    def test_no_side_reads_no_price(self):
        s = summarize_kalshi_fills([_fill(count=5, price_cents=62, side="no")], "no")
        assert s.avg_price == Decimal("0.62")

    def test_dollar_denominated_price_and_count_fp_are_understood(self):
        fill = {"side": "yes", "action": "buy", "count_fp": "12", "yes_price_dollars": "0.3750"}
        s = summarize_kalshi_fills([fill], "yes")
        assert (s.quantity, s.avg_price) == (Decimal("12"), Decimal("0.3750"))

    def test_fees_are_platform_reported_only_when_every_fill_has_one(self):
        both = summarize_kalshi_fills([_fill(fee_cost="0.05"), _fill(fee_cost="0.03")], "yes")
        assert both.fees_usd == Decimal("0.08")
        partial = summarize_kalshi_fills([_fill(fee_cost="0.05"), _fill()], "yes")
        assert partial.fees_usd is None

    @pytest.mark.parametrize("bad", [
        _fill(side="no", price_cents=40),                    # wrong side for the order
        {**_fill(), "action": "sell"},                       # not a buy
        {**_fill(), "yes_price": 0},                         # out of range
        {**_fill(), "yes_price": 100},
        {**_fill(), "yes_price": 40.5},                      # fractional cents
        {**_fill(), "count": 0},
        {**_fill(), "count": 2.5},
        {"side": "yes", "action": "buy", "count": 3},        # no price at all
    ])
    def test_unreadable_or_mismatched_fills_are_unreconcilable_never_guessed(self, bad):
        assert summarize_kalshi_fills([bad], "yes") is None

    def test_empty_fill_list_is_zero_contracts(self):
        s = summarize_kalshi_fills([], "yes")
        assert s.quantity == 0 and s.avg_price is None

    def test_garbage_input_never_raises(self):
        assert summarize_kalshi_fills(["x"], "yes") is None
        assert summarize_kalshi_fills([_fill()], "maybe") is None


def _order(count=40, remaining=0, status="executed"):
    return {"order_id": "O-1", "status": status, "count": count, "remaining_count": remaining}


class _FakeKalshi:
    """Read-only provider double. Any other attribute access raises, so a
    write call (place/cancel/post) would fail the test."""

    def __init__(self, fills=None, order=None, estimate="0.11"):
        self._fills, self._order, self._estimate = fills, order, Decimal(estimate)
        self.calls = []

    def get_fills(self, **filters):
        self.calls.append(("get_fills", filters))
        return self._fills

    def get_order_by_id(self, order_id):
        self.calls.append(("get_order_by_id", order_id))
        return self._order

    def estimate_fees(self, side, price, quantity):
        from src.execution.base import FeeEstimate
        return FeeEstimate(fee=self._estimate, fee_estimate=True, detail="test")


def _seed(conn, **overrides):
    row = {
        "execution_id": str(uuid.uuid4()), "account_id": "acct-1", "recommendation_id": "rec-1",
        "market_type": "moneyline", "matchup": "A @ B", "side": "YES", "stake_usd": 10.0,
        "price_at_detection": 0.40, "price_at_execution": 0.45,
        "requested_quantity": 40.0, "filled_quantity": 40.0, "avg_fill_price": 0.45,   # the LIMIT price
        "fees_usd": 0.50, "fees_source": "ESTIMATED", "fill_source": "ORDER_LIMIT_PRICE",
        "provider_order_id": "O-1", "status": "EXECUTED", "mode": "LIVE", "approval_mode": "AUTO",
        "platform": "kalshi",
    }
    row.update(overrides)
    save_autobet_execution(conn, row)
    return row


def _row(conn, execution_id):
    return dict(conn.execute("SELECT * FROM customer_autobet_executions WHERE execution_id = ?",
                             (execution_id,)).fetchone())


class TestReconcileExecution:
    def test_real_fills_replace_the_limit_price_and_estimated_fee(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=10, price_cents=40, fee_cost="0.09"),
                                      _fill(count=30, price_cents=42, fee_cost="0.27")], order=_order())
        assert reconcile_execution(db_conn, seeded, provider) == "RECONCILED"
        row = _row(db_conn, seeded["execution_id"])
        assert row["filled_quantity"] == 40.0
        assert row["avg_fill_price"] == pytest.approx(0.415)          # price improvement vs 0.45 limit
        assert row["fees_usd"] == pytest.approx(0.36) and row["fees_source"] == "PLATFORM"
        assert row["fill_source"] == "PLATFORM_FILLS" and row["reconciled_at"]
        assert row["status"] == "EXECUTED"

    def test_partial_fill_is_detected_against_the_requested_quantity(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=15, price_cents=41)], order=_order(40, 25, "canceled"))
        reconcile_execution(db_conn, seeded, provider)
        row = _row(db_conn, seeded["execution_id"])
        assert row["status"] == "PARTIALLY_FILLED" and row["filled_quantity"] == 15.0

    def test_missing_platform_fees_fall_back_to_an_estimate_for_the_real_fill(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=40, price_cents=41)], order=_order(), estimate="0.62")
        reconcile_execution(db_conn, seeded, provider)
        row = _row(db_conn, seeded["execution_id"])
        assert row["fees_usd"] == pytest.approx(0.62) and row["fees_source"] == "ESTIMATED"
        assert row["fill_source"] == "PLATFORM_FILLS"

    def test_confirmed_zero_fill_updates_to_zero_only_when_the_order_is_terminal(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[], order={"status": "canceled"})
        assert reconcile_execution(db_conn, seeded, provider) == "RECONCILED"
        row = _row(db_conn, seeded["execution_id"])
        assert row["filled_quantity"] == 0.0 and row["avg_fill_price"] is None

    def test_zero_fills_on_a_resting_order_is_not_conclusive(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[], order={"status": "resting"})
        assert reconcile_execution(db_conn, seeded, provider) == "PENDING"
        assert _row(db_conn, seeded["execution_id"])["fill_source"] == "ORDER_LIMIT_PRICE"

    def test_venue_error_or_unparseable_fills_leave_the_row_untouched(self, db_conn):
        seeded = _seed(db_conn)
        assert reconcile_execution(db_conn, seeded, _FakeKalshi(fills=None)) == "ERROR"
        bad = _FakeKalshi(fills=[{"side": "yes", "action": "buy", "count": 3}])
        assert reconcile_execution(db_conn, seeded, bad) == "ERROR"
        row = _row(db_conn, seeded["execution_id"])
        assert row["avg_fill_price"] == 0.45 and row["fill_source"] == "ORDER_LIMIT_PRICE"

    def test_order_that_is_still_resting_is_pending_even_with_fills(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=10, price_cents=41)], order=_order(40, 30, "resting"))
        assert reconcile_execution(db_conn, seeded, provider) == "PENDING"
        assert _row(db_conn, seeded["execution_id"])["fill_source"] == "ORDER_LIMIT_PRICE"

    def test_fills_that_disagree_with_the_orders_own_filled_quantity_are_rejected(self, db_conn):
        seeded = _seed(db_conn)          # e.g. a truncated fills page: 10 seen, order says 40 filled
        provider = _FakeKalshi(fills=[_fill(count=10, price_cents=41)], order=_order(40, 0))
        assert reconcile_execution(db_conn, seeded, provider) == "ERROR"
        assert _row(db_conn, seeded["execution_id"])["filled_quantity"] == 40.0

    def test_a_fill_belonging_to_another_order_is_rejected(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=40, price_cents=41, order_id="SOMEONE-ELSES")], order=_order())
        assert reconcile_execution(db_conn, seeded, provider) == "ERROR"

    def test_fills_exceeding_the_requested_quantity_are_rejected(self, db_conn):
        seeded = _seed(db_conn, requested_quantity=20.0)
        provider = _FakeKalshi(fills=[_fill(count=40, price_cents=41)], order=_order(40, 0))
        assert reconcile_execution(db_conn, seeded, provider) == "ERROR"

    def test_sdk_shape_fill_with_only_a_bare_price_is_never_guessed(self, db_conn):
        seeded = _seed(db_conn)
        bare = {"fill_id": "f", "order_id": "O-1", "ticker": "T", "side": "yes", "action": "buy",
                "count": 40, "price": 41, "is_taker": True}
        assert reconcile_execution(db_conn, seeded, _FakeKalshi(fills=[bare], order=_order())) == "ERROR"
        assert _row(db_conn, seeded["execution_id"])["avg_fill_price"] == 0.45

    def test_provider_exception_never_propagates(self, db_conn):
        seeded = _seed(db_conn)
        provider = mock.Mock()
        provider.get_fills.side_effect = RuntimeError("boom")
        assert reconcile_execution(db_conn, seeded, provider) == "ERROR"

    def test_only_read_endpoints_are_used(self, db_conn):
        seeded = _seed(db_conn)
        provider = _FakeKalshi(fills=[_fill(count=40, price_cents=41, fee_cost="0.3")], order=_order())
        reconcile_execution(db_conn, seeded, provider)
        assert {c[0] for c in provider.calls} <= {"get_fills", "get_order_by_id"}

    def test_reconciled_numbers_drive_settled_performance(self, db_conn):
        seeded = _seed(db_conn)
        reconcile_execution(db_conn, seeded, _FakeKalshi(
            fills=[_fill(count=40, price_cents=40, fee_cost="0.40")], order=_order()))
        db_conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
            "VALUES ('s', 'rec-1', 'WIN', '2026-09-20T00:00:00+00:00')")
        db_conn.commit()
        perf = get_customer_performance(db_conn, "acct-1", mode="LIVE", platform="kalshi")
        assert perf["realized_pnl_usd"] == pytest.approx(40 - 16.0 - 0.40)   # not the 0.45 limit / 0.50 estimate
        assert perf["pnl_basis"] == "NET" and perf["fills_confirmed"] is True


class TestReconcileCustomerFills:
    def test_each_customers_own_provider_is_used_and_others_are_untouched(self, db_conn):
        a = _seed(db_conn, account_id="acct-A", provider_order_id="O-A", recommendation_id="rec-a")
        b = _seed(db_conn, account_id="acct-B", provider_order_id="O-B", recommendation_id="rec-b")
        providers = {
            "acct-A": _FakeKalshi(fills=[_fill(count=40, price_cents=40, fee_cost="0.1", order_id="O-A")], order=_order()),
            "acct-B": _FakeKalshi(fills=[_fill(count=40, price_cents=30, fee_cost="0.1", order_id="O-B")], order=_order()),
        }
        for acct in ("acct-A", "acct-B"):
            db_conn.execute(
                "INSERT INTO customer_kalshi_accounts (account_id, kalshi_connected, autobet_enabled) "
                "VALUES (?, 1, 1)", (acct,))
        db_conn.commit()

        def build(account, platform):
            return providers[account["account_id"]]

        with mock.patch.object(autobet, "_build_customer_provider", side_effect=build):
            counts = reconcile_customer_fills(db_conn)
        assert counts["reconciled"] == 2
        assert providers["acct-A"].calls[0][1] == {"order_id": "O-A"}
        assert providers["acct-B"].calls[0][1] == {"order_id": "O-B"}
        assert _row(db_conn, a["execution_id"])["avg_fill_price"] == pytest.approx(0.40)
        assert _row(db_conn, b["execution_id"])["avg_fill_price"] == pytest.approx(0.30)

    def test_only_unreconciled_live_kalshi_rows_are_selected(self, db_conn):
        _seed(db_conn, fill_source="PLATFORM_FILLS")                                   # already done
        _seed(db_conn, platform="polymarket_us", provider_order_id="P-1")              # other platform
        _seed(db_conn, mode="PAPER", provider_order_id="P-2", fill_source="SIMULATED")  # simulated
        _seed(db_conn, provider_order_id=None)                                          # no order id
        with mock.patch.object(autobet, "_build_customer_provider") as build:
            counts = reconcile_customer_fills(db_conn)
        build.assert_not_called()
        assert counts == {"reconciled": 0, "pending": 0, "errors": 0}

    def test_a_customer_with_broken_credentials_does_not_block_others(self, db_conn):
        _seed(db_conn, account_id="acct-bad", provider_order_id="O-1", recommendation_id="r1")
        good = _seed(db_conn, account_id="acct-good", provider_order_id="O-2", recommendation_id="r2")
        for acct in ("acct-bad", "acct-good"):
            db_conn.execute("INSERT INTO customer_kalshi_accounts (account_id, kalshi_connected, autobet_enabled) "
                            "VALUES (?, 1, 1)", (acct,))
        db_conn.commit()
        ok = _FakeKalshi(fills=[_fill(count=40, price_cents=40, fee_cost="0.1", order_id="O-2")], order=_order())
        with mock.patch.object(autobet, "_build_customer_provider",
                               side_effect=lambda acct, plat: None if acct["account_id"] == "acct-bad" else ok):
            counts = reconcile_customer_fills(db_conn)
        assert counts["errors"] == 1 and counts["reconciled"] == 1
        assert _row(db_conn, good["execution_id"])["fill_source"] == "PLATFORM_FILLS"


class TestWorkerEntryPoint:
    def test_pass_reports_reconciliation_and_survives_its_failure(self):
        class _Cfg:
            database_path = ":memory:"

        with mock.patch.object(autobet, "_scan_and_dispatch", return_value={"executed": 1}), \
             mock.patch("src.execution.autobet_reconcile.reconcile_customer_fills",
                        return_value={"reconciled": 2, "pending": 0, "errors": 0}):
            result = autobet.run_customer_autobet_pass(_Cfg())
        assert result["executed"] == 1 and result["reconciliation"]["reconciled"] == 2

        with mock.patch.object(autobet, "_scan_and_dispatch", return_value={"executed": 1}), \
             mock.patch("src.execution.autobet_reconcile.reconcile_customer_fills", side_effect=RuntimeError("x")):
            result = autobet.run_customer_autobet_pass(_Cfg())
        assert result["executed"] == 1 and result["reconciliation"] == {"error": True}
