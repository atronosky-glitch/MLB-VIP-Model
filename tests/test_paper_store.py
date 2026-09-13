"""Tests for src/execution/paper/store.py against the db_conn fixture
(full schema via tests/conftest.py, including the Stage 3 paper_*
tables). Mirrors tests/test_execution_opportunity_store.py."""

from datetime import datetime, timezone
from decimal import Decimal

from src.execution.paper import store
from src.execution.paper.models import PaperFill, PaperOrder, PaperOrderStatus, PaperPosition, PaperPositionStatus


def _order(**overrides) -> PaperOrder:
    now = datetime.now(timezone.utc)
    defaults = dict(
        paper_order_id=None, opportunity_id=1, recommendation_id="rec-1", provider="kalshi",
        provider_market_id="M1", league="MLB", event="Athletics @ Toronto Blue Jays", side="YES",
        sizing_mode="FLAT", model_probability=Decimal("0.72"), net_ev_pct=Decimal("3.5"),
        requested_units=Decimal("1"), requested_stake=Decimal("10"), approved_units=Decimal("1"),
        approved_stake=Decimal("10"), requested_quantity=Decimal("14"), limit_price=Decimal("0.69"),
        fingerprint="rec-1|kalshi|M1|YES||FLAT", status=PaperOrderStatus.FILLED, rejection_reason=None,
        limiting_constraint=None, submitted_at=now, created_at=now,
    )
    defaults.update(overrides)
    return PaperOrder(**defaults)


def _position(**overrides) -> PaperPosition:
    now = datetime.now(timezone.utc)
    defaults = dict(
        position_id=None, paper_order_id=1, recommendation_id="rec-1", provider="kalshi",
        provider_market_id="M1", event_id="MLB|athletics|toronto blue jays|2026-09-12", league="MLB",
        side="YES", quantity=Decimal("14"), average_entry_price=Decimal("0.68"), entry_cost=Decimal("9.73"),
        fees_paid=Decimal("0.23"), model_probability_at_entry=Decimal("0.72"), net_ev_at_entry=Decimal("3.5"),
        fingerprint="rec-1|kalshi|M1|YES||FLAT", opened_at=now, status=PaperPositionStatus.OPEN,
        settled_at=None, settlement_value=None, realized_pnl=None,
    )
    defaults.update(overrides)
    return PaperPosition(**defaults)


class TestAccount:
    def test_get_account_returns_none_when_missing(self, db_conn):
        assert store.get_account(db_conn) is None

    def test_get_or_create_creates_with_starting_bankroll(self, db_conn):
        account = store.get_or_create_account(db_conn, Decimal("1000"))
        assert account["cash_usd"] == 1000.0
        assert account["starting_bankroll_usd"] == 1000.0

    def test_get_or_create_is_idempotent(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.get_or_create_account(db_conn, Decimal("9999"))  # should not overwrite
        account = store.get_account(db_conn)
        assert account["starting_bankroll_usd"] == 1000.0

    def test_adjust_cash_updates_running_total(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("-50"), Decimal("0"))
        account = store.get_account(db_conn)
        assert account["cash_usd"] == 950.0

    def test_adjust_cash_updates_realized_pnl(self, db_conn):
        store.get_or_create_account(db_conn, Decimal("1000"))
        store.adjust_account_cash(db_conn, store.DEFAULT_ACCOUNT_ID, Decimal("14"), Decimal("4.27"))
        account = store.get_account(db_conn)
        assert account["realized_pnl_usd"] == 4.27


class TestPersistOrder:
    def test_persisted_order_gets_an_id(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        assert order_id is not None
        assert order_id > 0

    def test_persisted_order_is_retrievable_by_query(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        row = db_conn.execute("SELECT * FROM paper_orders WHERE paper_order_id = ?", (order_id,)).fetchone()
        assert row["recommendation_id"] == "rec-1"
        assert row["status"] == "FILLED"


class TestPersistFillAndPosition:
    def test_fill_links_to_order(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        fill = PaperFill(
            paper_fill_id=None, paper_order_id=order_id, quantity_requested=Decimal("14"),
            quantity_filled=Decimal("14"), average_fill_price=Decimal("0.68"), gross_cost=Decimal("9.52"),
            fees=Decimal("0.21"), total_cost=Decimal("9.73"), slippage=Decimal("0"),
            timestamp=datetime.now(timezone.utc),
        )
        fill_id = store.persist_paper_fill(db_conn, fill)
        row = db_conn.execute("SELECT * FROM paper_fills WHERE paper_fill_id = ?", (fill_id,)).fetchone()
        assert row["paper_order_id"] == order_id
        assert row["quantity_filled"] == 14.0

    def test_position_persists_and_is_open(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id))
        position = store.get_position_by_id(db_conn, position_id)
        assert position["status"] == "OPEN"
        assert position["quantity"] == 14.0


class TestOpenPositionsAndDuplicates:
    def test_get_open_positions_returns_only_open(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id))
        open_positions = store.get_open_positions(db_conn)
        assert len(open_positions) == 1

    def test_open_position_by_fingerprint_found(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="fp-1"))
        assert store.get_open_position_by_fingerprint(db_conn, "fp-1") is not None
        assert store.get_open_position_by_fingerprint(db_conn, "fp-2") is None

    def test_settled_position_by_fingerprint_excludes_open(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="fp-1"))
        assert store.get_settled_position_by_fingerprint(db_conn, "fp-1") is None

    def test_settled_position_by_fingerprint_found_after_settlement(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, fingerprint="fp-1"))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))
        found = store.get_settled_position_by_fingerprint(db_conn, "fp-1")
        assert found is not None
        assert found["status"] == "WON"
        assert store.get_open_position_by_fingerprint(db_conn, "fp-1") is None


class TestExposureQueries:
    def test_count_open_positions(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id))
        assert store.count_open_positions(db_conn) == 1

    def test_sum_open_exposure_total(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        assert store.sum_open_exposure(db_conn) == Decimal("9.73")

    def test_sum_open_exposure_by_event(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, event_id="event-A", entry_cost=Decimal("5")))
        assert store.sum_open_exposure(db_conn, event_id="event-A") == Decimal("5")
        assert store.sum_open_exposure(db_conn, event_id="event-B") == Decimal("0")

    def test_sum_open_exposure_by_provider(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, provider="kalshi", entry_cost=Decimal("5")))
        assert store.sum_open_exposure(db_conn, provider="kalshi") == Decimal("5")
        assert store.sum_open_exposure(db_conn, provider="polymarket_us") == Decimal("0")

    def test_sum_open_exposure_by_league(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        store.persist_paper_position(db_conn, _position(paper_order_id=order_id, league="MLB", entry_cost=Decimal("5")))
        assert store.sum_open_exposure(db_conn, league="MLB") == Decimal("5")
        assert store.sum_open_exposure(db_conn, league="NFL") == Decimal("0")

    def test_settled_positions_excluded_from_open_exposure(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id, entry_cost=Decimal("9.73")))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))
        assert store.sum_open_exposure(db_conn) == Decimal("0")


class TestDailyAndRateQueries:
    def test_daily_wagered_sums_filled_orders_today(self, db_conn):
        store.persist_paper_order(db_conn, _order(approved_stake=Decimal("10")))
        store.persist_paper_order(db_conn, _order(approved_stake=Decimal("15")))
        assert store.sum_daily_wagered(db_conn) == Decimal("25")

    def test_daily_wagered_excludes_rejected_orders(self, db_conn):
        store.persist_paper_order(db_conn, _order(status=PaperOrderStatus.REJECTED, approved_stake=Decimal("0")))
        assert store.sum_daily_wagered(db_conn) == Decimal("0")

    def test_daily_realized_pnl_sums_settled_positions_today(self, db_conn):
        order_id = store.persist_paper_order(db_conn, _order())
        position_id = store.persist_paper_position(db_conn, _position(paper_order_id=order_id))
        store.settle_paper_position(db_conn, position_id, "WON", Decimal("14"), Decimal("4.27"))
        assert store.sum_daily_realized_pnl(db_conn) == Decimal("4.27")

    def test_trades_in_last_hour_counts_filled_orders(self, db_conn):
        store.persist_paper_order(db_conn, _order())
        store.persist_paper_order(db_conn, _order())
        assert store.count_trades_in_last_hour(db_conn) == 2

    def test_trades_in_last_hour_excludes_rejected(self, db_conn):
        store.persist_paper_order(db_conn, _order(status=PaperOrderStatus.REJECTED))
        assert store.count_trades_in_last_hour(db_conn) == 0


class TestRiskDecisions:
    def test_persist_risk_decision_round_trips(self, db_conn):
        decision_id = store.persist_risk_decision(db_conn, {
            "recommendation_id": "rec-1", "provider": "kalshi", "opportunity_id": 1,
            "paper_order_id": None, "recommended_stake_usd": Decimal("10"),
            "approved_stake_usd": Decimal("8"), "approved": True,
            "rejection_reason": None, "limiting_constraint": "MAX_EVENT_EXPOSURE",
            "bankroll_before": Decimal("1000"), "event_exposure_before": Decimal("50"),
            "provider_exposure_before": Decimal("0"), "sport_exposure_before": Decimal("0"),
            "daily_exposure_before": Decimal("0"), "daily_pnl_before": Decimal("0"),
            "open_positions_before": 0,
        })
        row = db_conn.execute("SELECT * FROM risk_decisions WHERE risk_decision_id = ?", (decision_id,)).fetchone()
        assert row["approved"] == 1
        assert row["limiting_constraint"] == "MAX_EVENT_EXPOSURE"
        assert row["approved_stake_usd"] == 8.0

    def test_rejected_decision_persists_rejection_reason(self, db_conn):
        decision_id = store.persist_risk_decision(db_conn, {
            "recommendation_id": "rec-1", "provider": "kalshi", "opportunity_id": None,
            "paper_order_id": None, "recommended_stake_usd": Decimal("10"),
            "approved_stake_usd": Decimal("0"), "approved": False,
            "rejection_reason": "DAILY_STOP_LOSS_REACHED", "limiting_constraint": "DAILY_STOP_LOSS_REACHED",
            "bankroll_before": Decimal("1000"), "event_exposure_before": Decimal("0"),
            "provider_exposure_before": Decimal("0"), "sport_exposure_before": Decimal("0"),
            "daily_exposure_before": Decimal("0"), "daily_pnl_before": Decimal("-100"),
            "open_positions_before": 0,
        })
        row = db_conn.execute("SELECT * FROM risk_decisions WHERE risk_decision_id = ?", (decision_id,)).fetchone()
        assert row["approved"] == 0
        assert row["rejection_reason"] == "DAILY_STOP_LOSS_REACHED"
