"""Tests for src/execution/live/approval.py -- prepare/approve/reject
lifecycle, duplicate suppression, and expiry (spec section 37's list)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.execution.base import Balance, FeeEstimate, NormalizedOrderBook, OrderLevel
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.live import approval, store
from src.execution.live.models import ApprovalStatus, PreparedOrderStatus


class _FakeConfig:
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
    max_opportunity_age_seconds = 30
    stop_after_daily_profit_target = False
    daily_profit_target_usd = 0.0
    approval_ttl_seconds = 30

    def ev_tiered_sizing_tiers(self):
        return ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))


def _opportunity(**overrides) -> ExecutionOpportunity:
    now = datetime.now(timezone.utc)
    defaults = dict(
        recommendation_id="rec-1", league="MLB", event="Athletics @ Toronto Blue Jays",
        market="moneyline", side="YES", model_probability=Decimal("0.72"),
        provider="polymarket_us", provider_market_id="aec-mlb-oak-tor-2026-09-13", match_confidence=1.0,
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
    def __init__(self, available_balance=1000.0):
        self._available = available_balance

    def get_balance(self):
        return Balance(currency="USD", available=self._available)

    def get_orderbook(self, market_id):
        return object()

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id="aec-mlb-oak-tor-2026-09-13",
            yes_bids=[OrderLevel(Decimal("0.66"), Decimal("200"))],
            yes_asks=[OrderLevel(Decimal("0.68"), Decimal("150"))],
            no_bids=[OrderLevel(Decimal("0.30"), Decimal("150"))],
            no_asks=[OrderLevel(Decimal("0.34"), Decimal("200"))],
            timestamp=datetime.now(timezone.utc),
        )

    def estimate_fees(self, side, price, quantity):
        return FeeEstimate(fee=Decimal("0.23"), fee_estimate=False, detail="test")


class TestPrepareOrder:
    def test_prepares_successfully_for_a_healthy_opportunity(self, db_conn):
        result = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        assert result.rejected is False
        assert result.prepared_order_id is not None
        prepared = store.get_prepared_order(db_conn, result.prepared_order_id)
        assert prepared["status"] == "READY"

    def test_duplicate_fingerprint_does_not_create_a_second_prepared_order(self, db_conn):
        first = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        second = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        assert second.prepared_order_id == first.prepared_order_id
        rows = db_conn.execute("SELECT COUNT(*) AS n FROM prepared_live_orders").fetchone()
        assert rows["n"] == 1

    def test_rejected_when_risk_engine_rejects(self, db_conn):
        config = _FakeConfig()
        config.max_bet_usd = 0.0  # forces BET_TOO_SMALL
        config.min_paper_trade_usd = 1.0
        result = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), config)
        assert result.rejected is True
        assert result.prepared_order_id is None

    def test_provider_balance_failure_is_rejected_not_crashed(self, db_conn):
        class BrokenProvider(_FakeProvider):
            def get_balance(self):
                raise RuntimeError("connection reset")
        result = approval.prepare_order(db_conn, _opportunity(), _signal(), BrokenProvider(), _FakeConfig())
        assert result.rejected is True
        assert result.rejection_detail == "PROVIDER_UNAVAILABLE"


class TestApprove:
    def test_approve_creates_a_bounded_authorization(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        authorization = approval.approve(db_conn, prep.prepared_order_id, _FakeConfig())
        assert authorization is not None
        assert authorization.status == ApprovalStatus.APPROVED
        assert authorization.approved_max_price == Decimal("0.69")
        assert authorization.approved_quantity == Decimal("14")

    def test_approve_consumes_the_prepared_order(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        approval.approve(db_conn, prep.prepared_order_id, _FakeConfig())
        prepared = store.get_prepared_order(db_conn, prep.prepared_order_id)
        assert prepared["status"] == "APPROVED"

    def test_cannot_approve_an_already_approved_order(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        first = approval.approve(db_conn, prep.prepared_order_id, _FakeConfig())
        second = approval.approve(db_conn, prep.prepared_order_id, _FakeConfig())
        assert first is not None
        assert second is None

    def test_cannot_approve_a_nonexistent_prepared_order(self, db_conn):
        assert approval.approve(db_conn, 999999, _FakeConfig()) is None

    def test_cannot_approve_an_expired_prepared_order(self, db_conn):
        config = _FakeConfig()
        config.max_opportunity_age_seconds = 1
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), config)
        # Force the expiry into the past directly (simulating time passing).
        db_conn.execute(
            "UPDATE prepared_live_orders SET expires_at = ? WHERE prepared_order_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), prep.prepared_order_id),
        )
        db_conn.commit()
        authorization = approval.approve(db_conn, prep.prepared_order_id, config)
        assert authorization is None
        prepared = store.get_prepared_order(db_conn, prep.prepared_order_id)
        assert prepared["status"] == "EXPIRED"

    def test_approval_ttl_uses_config_value(self, db_conn):
        config = _FakeConfig()
        config.approval_ttl_seconds = 5
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), config)
        authorization = approval.approve(db_conn, prep.prepared_order_id, config)
        delta = (authorization.expires_at - authorization.approved_at).total_seconds()
        assert 4 <= delta <= 6

    def test_approval_id_is_long_and_random(self, db_conn):
        prep1 = approval.prepare_order(db_conn, _opportunity(recommendation_id="a"), _signal(recommendation_id="a"), _FakeProvider(), _FakeConfig())
        prep2 = approval.prepare_order(db_conn, _opportunity(recommendation_id="b"), _signal(recommendation_id="b"), _FakeProvider(), _FakeConfig())
        auth1 = approval.approve(db_conn, prep1.prepared_order_id, _FakeConfig())
        auth2 = approval.approve(db_conn, prep2.prepared_order_id, _FakeConfig())
        assert len(auth1.approval_id) >= 32
        assert auth1.approval_id != auth2.approval_id


class TestReject:
    def test_reject_marks_the_prepared_order_rejected(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        assert approval.reject(db_conn, prep.prepared_order_id, "changed my mind") is True
        prepared = store.get_prepared_order(db_conn, prep.prepared_order_id)
        assert prepared["status"] == "REJECTED"

    def test_cannot_reject_twice(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        assert approval.reject(db_conn, prep.prepared_order_id) is True
        assert approval.reject(db_conn, prep.prepared_order_id) is False

    def test_cannot_reject_an_already_approved_order(self, db_conn):
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        approval.approve(db_conn, prep.prepared_order_id, _FakeConfig())
        assert approval.reject(db_conn, prep.prepared_order_id) is False


class TestGetPending:
    def test_returns_ready_orders_sorted_by_net_ev_desc(self, db_conn):
        approval.prepare_order(
            db_conn, _opportunity(recommendation_id="low", net_ev_pct=Decimal("2.0")),
            _signal(recommendation_id="low"), _FakeProvider(), _FakeConfig(),
        )
        approval.prepare_order(
            db_conn, _opportunity(recommendation_id="high", net_ev_pct=Decimal("9.0")),
            _signal(recommendation_id="high"), _FakeProvider(), _FakeConfig(),
        )
        pending = approval.get_pending(db_conn)
        assert len(pending) == 2
        assert pending[0]["net_ev_pct"] == 9.0
        assert pending[1]["net_ev_pct"] == 2.0

    def test_expired_orders_are_excluded_and_marked_expired(self, db_conn):
        config = _FakeConfig()
        prep = approval.prepare_order(db_conn, _opportunity(), _signal(), _FakeProvider(), config)
        db_conn.execute(
            "UPDATE prepared_live_orders SET expires_at = ? WHERE prepared_order_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), prep.prepared_order_id),
        )
        db_conn.commit()
        pending = approval.get_pending(db_conn)
        assert pending == []
        prepared = store.get_prepared_order(db_conn, prep.prepared_order_id)
        assert prepared["status"] == "EXPIRED"
