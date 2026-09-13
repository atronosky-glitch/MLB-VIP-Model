"""Tests for src/execution/live/revalidation.py -- every InvalidationReason
in isolation, using hand-built authorization/prepared_order dicts (the
shape store.get_authorization()/get_prepared_order() actually return)
and fake providers. No DB needed for most cases -- build_live_risk_context
needs a real conn only for the RiskEngine re-check step, so those tests
use the db_conn fixture."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.execution.base import FeeEstimate, Market, NormalizedOrderBook, OrderLevel, Balance
from src.execution.live.models import InvalidationReason
from src.execution.live.revalidation import revalidate_approved_order


class _FakeConfig:
    unit_size_usd = 10.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    live_require_fresh_orderbook = True
    live_max_orderbook_age_seconds = 10
    live_allow_post_approval_size_reduction = False
    max_bet_usd = 1000.0
    max_bet_pct_bankroll = 1.0
    max_units_per_bet = 1000.0
    max_event_exposure_usd = 1000.0
    max_provider_exposure_usd = 1000.0
    max_sport_exposure_usd = 1000.0
    max_open_exposure_usd = 1000.0
    max_daily_wagered_usd = 1000.0
    max_daily_loss_usd = 1000.0
    max_open_positions = 20
    max_trades_per_hour = 20
    min_paper_trade_usd = 1.0
    allow_risk_size_reduction = True
    allow_position_addons = False
    stop_after_daily_profit_target = False
    daily_profit_target_usd = 0.0


def _authorization(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    defaults = dict(
        approval_id="approval-1", prepared_order_id=1, opportunity_id=None,
        recommendation_id="rec-1", provider="polymarket_us", provider_market_id="M1", side="YES",
        approved_units=1.0, approved_stake_usd=10.0, approved_quantity=14.0, approved_max_price=0.69,
        approved_min_net_ev_pct=3.5, approved_at=now.isoformat(), expires_at=(now + timedelta(seconds=30)).isoformat(),
        approved_by="local_operator", status="APPROVED", used_at=None, invalidated_at=None, invalidation_reason=None,
    )
    defaults.update(overrides)
    return defaults


def _prepared_order(**overrides) -> dict:
    defaults = dict(
        prepared_order_id=1, event_id="MLB|athletics|toronto blue jays|2026-09-13", league="MLB",
        model_probability=0.72, event_start_time=None,
    )
    defaults.update(overrides)
    return defaults


def _levels(*pairs):
    return [OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in pairs]


class _FakeProvider:
    def __init__(
        self, market_status="active", asks=None, bids=None, orderbook_error=None, market_error=None,
        balance=1000.0,
    ):
        self.market_status = market_status
        self.asks = asks if asks is not None else _levels((0.68, 150))
        self.bids = bids if bids is not None else _levels((0.66, 200))
        self.orderbook_error = orderbook_error
        self.market_error = market_error
        self.balance = balance

    def get_market(self, market_id):
        if self.market_error:
            raise self.market_error
        return Market(id=market_id, title="x", status=self.market_status, raw={})

    def get_orderbook(self, market_id):
        if self.orderbook_error:
            raise self.orderbook_error
        return object()

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id="M1", yes_bids=self.bids, yes_asks=self.asks,
            no_bids=_levels((0.30, 150)), no_asks=_levels((0.34, 200)),
            timestamp=datetime.now(timezone.utc),
        )

    def estimate_fees(self, side, price, quantity):
        fee = Decimal("0.06") * quantity * price * (Decimal("1") - price)
        return FeeEstimate(fee=fee, fee_estimate=False, detail="test")

    def get_balance(self):
        return Balance(currency="USD", available=self.balance)


class TestEventStarted:
    def test_event_already_started_fails(self, db_conn):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(event_start_time=past.isoformat()),
            _FakeProvider(), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.EVENT_STARTED

    def test_event_in_future_passes_this_check(self, db_conn):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(event_start_time=future.isoformat()),
            _FakeProvider(), _FakeConfig(),
        )
        assert result.invalidation_reason != InvalidationReason.EVENT_STARTED


class TestMarketClosed:
    def test_closed_status_fails(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(), _FakeProvider(market_status="closed"), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.MARKET_CLOSED

    def test_unrecognized_status_does_not_block(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(), _FakeProvider(market_status="some_unknown_value"), _FakeConfig(),
        )
        assert result.invalidation_reason != InvalidationReason.MARKET_CLOSED

    def test_market_fetch_error_is_provider_unavailable(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(),
            _FakeProvider(market_error=RuntimeError("timeout")), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.PROVIDER_UNAVAILABLE


class TestMarketDataStale:
    def test_orderbook_fetch_error_is_stale(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(),
            _FakeProvider(orderbook_error=RuntimeError("boom")), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.MARKET_DATA_STALE


class TestPriceMovedAboveLimit:
    def test_all_asks_above_approved_max_price_fails(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(approved_max_price=0.50), _prepared_order(),
            _FakeProvider(asks=_levels((0.90, 100))), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.PRICE_MOVED_ABOVE_APPROVED_LIMIT


class TestLiquidityDropped:
    def test_zero_quantity_within_bounds_fails(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(approved_stake_usd=0.01), _prepared_order(),
            _FakeProvider(), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.LIQUIDITY_DROPPED

    def test_liquidity_below_minimum_fails(self, db_conn):
        config = _FakeConfig()
        config.min_available_liquidity_usd = 100000.0
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(), _FakeProvider(), config,
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.LIQUIDITY_DROPPED


class TestSpreadAndSlippage:
    def test_spread_too_wide_fails(self, db_conn):
        config = _FakeConfig()
        config.max_spread_pct = 0.001
        result = revalidate_approved_order(
            db_conn, _authorization(), _prepared_order(),
            _FakeProvider(bids=_levels((0.40, 200)), asks=_levels((0.68, 150))), config,
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.SPREAD_TOO_WIDE

    def test_slippage_too_high_fails(self, db_conn):
        config = _FakeConfig()
        config.max_slippage_pct = 0.001
        # Both levels stay within the approved_max_price ceiling (0.69)
        # so the fill genuinely walks across two prices and produces
        # real slippage -- a level above the ceiling would just be
        # excluded before ever contributing to VWAP.
        result = revalidate_approved_order(
            db_conn, _authorization(approved_quantity=200, approved_stake_usd=1000, approved_max_price=0.69),
            _prepared_order(), _FakeProvider(asks=_levels((0.50, 5), (0.65, 200))), config,
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.SLIPPAGE_TOO_HIGH


class TestNetEvBelowApprovedMinimum:
    def test_fresh_ev_below_approved_minimum_fails(self, db_conn):
        result = revalidate_approved_order(
            db_conn, _authorization(approved_min_net_ev_pct=50.0), _prepared_order(),
            _FakeProvider(), _FakeConfig(),
        )
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.NET_EV_BELOW_APPROVED_MINIMUM


class TestRiskLimitChanged:
    def test_daily_stop_loss_reached_maps_to_its_own_reason(self, db_conn):
        config = _FakeConfig()
        config.max_daily_loss_usd = 0.01
        db_conn.execute(
            "INSERT INTO live_positions (approval_id, prepared_order_id, live_order_id, recommendation_id, "
            "provider, provider_market_id, event_id, side, quantity, average_entry_price, total_entry_cost, "
            "fees_paid, opened_at, status, settled_at, realized_pnl) VALUES "
            "('a', 1, 1, 'rec-x', 'polymarket_us', 'M1', 'event-x', 'YES', 10, 0.5, 5.0, 0.1, "
            "datetime('now'), 'LOST', datetime('now'), -5.0)"
        )
        db_conn.commit()
        result = revalidate_approved_order(db_conn, _authorization(), _prepared_order(), _FakeProvider(), config)
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.DAILY_STOP_LOSS_REACHED

    def test_size_reduction_not_allowed_by_default(self, db_conn):
        config = _FakeConfig()
        config.max_event_exposure_usd = 0.01  # forces a much smaller risk-approved stake
        result = revalidate_approved_order(db_conn, _authorization(), _prepared_order(), _FakeProvider(), config)
        assert result.passed is False
        assert result.invalidation_reason == InvalidationReason.RISK_LIMIT_CHANGED

    def test_size_reduction_allowed_when_configured(self, db_conn):
        config = _FakeConfig()
        # Reduces the risk-approved stake from $10 to $5 -- still above
        # min_paper_trade_usd, so RiskEngine approves a SMALLER amount
        # rather than rejecting outright (unlike the 0.01 case above).
        config.max_event_exposure_usd = 5.0
        config.live_allow_post_approval_size_reduction = True
        result = revalidate_approved_order(db_conn, _authorization(), _prepared_order(), _FakeProvider(), config)
        assert result.passed is True
        assert result.risk_decision.approved_stake_usd == Decimal("5.0")


class TestPassingRevalidation:
    def test_all_checks_pass_returns_fresh_numbers(self, db_conn):
        result = revalidate_approved_order(db_conn, _authorization(), _prepared_order(), _FakeProvider(), _FakeConfig())
        assert result.passed is True
        assert result.quantity == Decimal("14")
        assert result.fill_price == Decimal("0.68")
        assert result.risk_decision is not None
        assert result.risk_decision.approved is True
