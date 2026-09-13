"""Tests for src/execution/live/store.py functions not already
exercised indirectly through approval/service/E2E tests -- notably
persist_live_fill, which no current code path calls yet (Polymarket's
synchronous-execution response gives an aggregate cumQuantity/avgPx,
not itemized fills, so LiveOrder tracks fills in aggregate for now --
see the Stage 4 completion report's known limitations). Tested directly
here so the persistence layer itself is proven correct even though it's
not wired into service.py's current flow."""

from datetime import datetime, timezone
from decimal import Decimal

from src.execution.live import store
from src.execution.live.models import LiveFill, LiveOrder, LiveOrderStatus


def _live_order(**overrides) -> LiveOrder:
    now = datetime.now(timezone.utc)
    defaults = dict(
        live_order_id=None, approval_id="approval-1", prepared_order_id=1, provider="polymarket_us",
        provider_order_id="ORDER-1", client_order_id=None, market_id="M1", side="YES",
        quantity_requested=Decimal("14"), quantity_filled=Decimal("14"), limit_price=Decimal("0.69"),
        average_fill_price=Decimal("0.68"), fees=Decimal("0.23"), status=LiveOrderStatus.FILLED,
        submitted_at=now, last_updated_at=now, provider_response_reference="HTTP 201",
    )
    defaults.update(overrides)
    return LiveOrder(**defaults)


class TestLiveFillPersistence:
    def test_persist_and_link_to_order(self, db_conn):
        order_id = store.persist_live_order(db_conn, _live_order())
        fill = LiveFill(
            fill_id=None, provider_fill_id="FILL-1", live_order_id=order_id,
            quantity=Decimal("14"), price=Decimal("0.68"), fees=Decimal("0.23"),
            timestamp=datetime.now(timezone.utc),
        )
        fill_id = store.persist_live_fill(db_conn, fill)
        row = db_conn.execute("SELECT * FROM live_fills WHERE fill_id = ?", (fill_id,)).fetchone()
        assert row["live_order_id"] == order_id
        assert row["quantity"] == 14.0
        assert row["price"] == 0.68

    def test_multiple_fills_can_link_to_the_same_order(self, db_conn):
        order_id = store.persist_live_order(db_conn, _live_order(quantity_filled=Decimal("14")))
        store.persist_live_fill(db_conn, LiveFill(
            fill_id=None, provider_fill_id="F1", live_order_id=order_id, quantity=Decimal("10"),
            price=Decimal("0.68"), fees=Decimal("0.15"), timestamp=datetime.now(timezone.utc),
        ))
        store.persist_live_fill(db_conn, LiveFill(
            fill_id=None, provider_fill_id="F2", live_order_id=order_id, quantity=Decimal("4"),
            price=Decimal("0.69"), fees=Decimal("0.08"), timestamp=datetime.now(timezone.utc),
        ))
        rows = db_conn.execute("SELECT * FROM live_fills WHERE live_order_id = ?", (order_id,)).fetchall()
        assert len(rows) == 2


class TestLiveOrderUpdate:
    def test_update_live_order_status_updates_only_provided_fields(self, db_conn):
        order_id = store.persist_live_order(db_conn, _live_order(status=LiveOrderStatus.SUBMITTED, quantity_filled=Decimal("0")))
        store.update_live_order_status(db_conn, order_id, "PARTIALLY_FILLED", quantity_filled=Decimal("5"))
        row = store.get_live_order(db_conn, order_id)
        assert row["status"] == "PARTIALLY_FILLED"
        assert row["quantity_filled"] == 5.0
        assert row["limit_price"] == 0.69  # untouched

    def test_get_live_order_returns_none_when_missing(self, db_conn):
        assert store.get_live_order(db_conn, 999999) is None


class TestAuditLog:
    def test_log_event_and_get_events_round_trip(self, db_conn):
        store.log_event(db_conn, "PREPARED", "net_ev=5.0", approval_id="approval-1", prepared_order_id=1)
        store.log_event(db_conn, "APPROVED", "approved_by=local_operator", approval_id="approval-1")
        events = store.get_events(db_conn, "approval-1")
        assert len(events) == 2
        assert events[0]["event_type"] == "PREPARED"
        assert events[1]["event_type"] == "APPROVED"

    def test_get_events_scoped_to_one_approval(self, db_conn):
        store.log_event(db_conn, "PREPARED", "", approval_id="approval-1")
        store.log_event(db_conn, "PREPARED", "", approval_id="approval-2")
        assert len(store.get_events(db_conn, "approval-1")) == 1
