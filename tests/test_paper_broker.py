"""Integration tests for src/execution/paper/broker.py::PaperBroker
against the db_conn fixture and fake providers -- exercises the full
submit_opportunity/settle_positions flow end-to-end."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest

from src.execution.base import FeeEstimate, Market, NormalizedOrderBook, OrderLevel
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.paper.broker import PaperBroker
from src.execution.paper.models import PaperPositionStatus, PaperRejectionReason
from src.execution.paper import store


class _FakeConfig:
    paper_starting_bankroll_usd = 1000.0
    unit_size_usd = 10.0
    bet_sizing_mode = "FLAT"
    default_units = 1.0
    max_units_per_bet = 2.0
    kelly_multiplier = 0.25
    max_bet_usd = 25.0
    max_bet_pct_bankroll = 0.5
    max_event_exposure_usd = 500.0
    max_provider_exposure_usd = 500.0
    max_sport_exposure_usd = 500.0
    max_open_exposure_usd = 500.0
    max_daily_wagered_usd = 500.0
    max_daily_loss_usd = 1000.0
    max_open_positions = 20
    max_trades_per_hour = 20
    min_paper_trade_usd = 1.0
    allow_risk_size_reduction = True
    allow_partial_paper_fills = False
    allow_position_addons = False
    allow_retrade_settled_recommendation = False
    max_opportunity_age_seconds = 30
    stop_after_daily_profit_target = False
    daily_profit_target_usd = 0.0
    min_net_ev_pct = 1.0

    def ev_tiered_sizing_tiers(self):
        return ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))


def _opportunity(**overrides) -> ExecutionOpportunity:
    now = datetime.now(timezone.utc)
    defaults = dict(
        recommendation_id="rec-1", league="MLB", event="Athletics @ Toronto Blue Jays",
        market="moneyline", side="YES", model_probability=Decimal("0.72"),
        provider="kalshi", provider_market_id="KXMLBGAME-EX", match_confidence=1.0,
        best_bid=Decimal("0.66"), best_ask=Decimal("0.68"), spread=Decimal("0.02"),
        analysis_stake_usd=Decimal("10"), quantity_analyzed=Decimal("14"),
        expected_fill_price=Decimal("0.68"), estimated_fees=Decimal("0.23"),
        expected_slippage=Decimal("0"), available_liquidity=Decimal("100"),
        raw_ev_pct=Decimal("5.88"), net_ev_pct=Decimal("3.5"),
        max_acceptable_price=Decimal("0.69"), market_data_timestamp=now,
        signal_timestamp=now, generated_at=now, expiration_time=now,
    )
    defaults.update(overrides)
    return ExecutionOpportunity(**defaults)


def _signal(**overrides) -> ExecutionSignal:
    defaults = dict(
        recommendation_id="rec-1", league="MLB", market_type="moneyline",
        home_team="Toronto Blue Jays", away_team="Athletics", side="AWAY", line=None,
        event_start_time=None, model_probability=Decimal("0.72"), sportsbook_ev_pct=6.0,
        rec_status="STRONG_EDGE", signal_timestamp=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return ExecutionSignal(**defaults)


class _FakeProvider:
    def __init__(self, asks=None, bids=None, fee_coefficient=Decimal("0.07"), market_raw=None):
        self.asks = asks if asks is not None else [OrderLevel(Decimal("0.68"), Decimal("150"))]
        self.bids = bids if bids is not None else [OrderLevel(Decimal("0.66"), Decimal("200"))]
        self.fee_coefficient = fee_coefficient
        self.market_raw = market_raw or {}
        self.place_order_called = False
        self.cancel_order_called = False

    def get_orderbook(self, market_id):
        return object()

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id="KXMLBGAME-EX", yes_bids=self.bids, yes_asks=self.asks,
            no_bids=[OrderLevel(Decimal("0.30"), Decimal("150"))],
            no_asks=[OrderLevel(Decimal("0.34"), Decimal("200"))],
            timestamp=datetime.now(timezone.utc),
        )

    def estimate_fees(self, side, price, quantity):
        fee = self.fee_coefficient * quantity * price * (Decimal("1") - price)
        return FeeEstimate(fee=fee, fee_estimate=True, detail="test")

    def get_market(self, market_id):
        return Market(id=market_id, title="x", status=self.market_raw.get("status", "open"), raw=self.market_raw)

    def place_order(self, *a, **k):
        self.place_order_called = True
        raise NotImplementedError

    def cancel_order(self, *a, **k):
        self.cancel_order_called = True
        raise NotImplementedError


class TestSuccessfulSubmission:
    def test_fills_and_creates_open_position(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        assert result.status == "FILLED"
        assert result.position_id is not None
        position = broker.get_position(result.position_id)
        assert position["status"] == "OPEN"

    def test_debits_bankroll_by_total_cost(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        bankroll_before = broker.get_bankroll()
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        position = broker.get_position(result.position_id)
        bankroll_after = broker.get_bankroll()
        assert bankroll_after.cash == bankroll_before.cash - Decimal(str(position["entry_cost"]))

    def test_fees_and_vwap_recorded_on_the_fill(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        position = broker.get_position(result.position_id)
        assert position["fees_paid"] > 0
        assert position["average_entry_price"] == 0.68

    def test_risk_decision_persisted_regardless_of_outcome(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        row = db_conn.execute("SELECT * FROM risk_decisions").fetchone()
        assert row is not None
        assert row["approved"] == 1


class TestDuplicateControl:
    def test_second_submission_of_same_opportunity_is_duplicate(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        r1 = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        r2 = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        assert r1.status == "FILLED"
        assert r2.status == "REJECTED"
        assert r2.rejection_reason == PaperRejectionReason.DUPLICATE_POSITION.value

    def test_addon_allowed_when_configured(self, db_conn):
        config = _FakeConfig()
        config.allow_position_addons = True
        broker = PaperBroker(db_conn, config)
        r1 = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        r2 = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        assert r1.status == "FILLED"
        assert r2.status == "FILLED"


class TestStaleOpportunity:
    def test_stale_opportunity_rejected_before_sizing(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        old = datetime.now(timezone.utc) - timedelta(seconds=120)
        opp = _opportunity(generated_at=old)
        result = broker.submit_opportunity(opp, _signal(), _FakeProvider())
        assert result.status == "REJECTED"
        assert result.rejection_reason == PaperRejectionReason.OPPORTUNITY_STALE.value
        assert result.sizing is None  # never reached sizing

    def test_event_already_started_rejected(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        result = broker.submit_opportunity(_opportunity(), _signal(event_start_time=past), _FakeProvider())
        assert result.status == "REJECTED"
        assert result.rejection_reason == PaperRejectionReason.EVENT_STARTED.value


class TestPriceCeiling:
    def test_book_moved_beyond_ceiling_rejects(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        provider = _FakeProvider(asks=[OrderLevel(Decimal("0.90"), Decimal("100"))])
        result = broker.submit_opportunity(_opportunity(), _signal(), provider)
        assert result.status == "REJECTED"
        assert result.rejection_reason == PaperRejectionReason.PRICE_MOVED_BEYOND_LIMIT.value


class TestInsufficientLiquidity:
    def test_empty_book_rejects(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        provider = _FakeProvider(asks=[])
        result = broker.submit_opportunity(_opportunity(), _signal(), provider)
        assert result.status == "REJECTED"
        assert result.rejection_reason == PaperRejectionReason.INSUFFICIENT_LIQUIDITY.value


class TestSettlement:
    def test_won_position_credits_cash_and_realized_pnl(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        provider = _FakeProvider(market_raw={"status": "finalized", "result": "yes"})
        outcomes = broker.settle_positions({"kalshi": provider})
        assert len(outcomes) == 1
        assert outcomes[0].status == PaperPositionStatus.WON
        assert outcomes[0].realized_pnl > 0
        position = broker.get_position(result.position_id)
        assert position["status"] == "WON"

    def test_lost_position_has_negative_pnl(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        provider = _FakeProvider(market_raw={"status": "finalized", "result": "no"})
        outcomes = broker.settle_positions({"kalshi": provider})
        assert outcomes[0].status == PaperPositionStatus.LOST
        assert outcomes[0].realized_pnl < 0

    def test_void_refunds_minus_fees(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        position_before = broker.get_position(result.position_id)
        provider = _FakeProvider(market_raw={"status": "voided"})
        outcomes = broker.settle_positions({"kalshi": provider})
        assert outcomes[0].status == PaperPositionStatus.VOID
        assert outcomes[0].realized_pnl == Decimal(str(-position_before["fees_paid"]))

    def test_unresolved_market_leaves_position_open(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        result = broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        provider = _FakeProvider(market_raw={"status": "open"})
        outcomes = broker.settle_positions({"kalshi": provider})
        assert outcomes[0].status == PaperPositionStatus.OPEN
        position = broker.get_position(result.position_id)
        assert position["status"] == "OPEN"

    def test_units_won_lost_via_daily_stats(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        broker.submit_opportunity(_opportunity(), _signal(), _FakeProvider())
        provider = _FakeProvider(market_raw={"status": "finalized", "result": "yes"})
        broker.settle_positions({"kalshi": provider})
        stats = broker.get_daily_stats()
        assert stats["units_won_lost"] == stats["realized_pnl"] / Decimal("10")


class TestSafetyNeverPlacesOrRealOrder:
    def test_submit_and_settle_never_call_place_or_cancel_order(self, db_conn):
        broker = PaperBroker(db_conn, _FakeConfig())
        provider = _FakeProvider(market_raw={"status": "finalized", "result": "yes"})
        result = broker.submit_opportunity(_opportunity(), _signal(), provider)
        broker.settle_positions({"kalshi": provider})
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False

    def test_place_order_still_raises_notimplementederror_if_ever_called(self, db_conn):
        provider = _FakeProvider()
        with pytest.raises(NotImplementedError):
            provider.place_order()
        with pytest.raises(NotImplementedError):
            provider.cancel_order()
