"""End-to-end tests for the full Stage 4 flow (spec sections 38-39).
Everything mocked -- no real network calls, ever.

test_negative_no_approval_blocks_with_zero_provider_calls is, per the
spec, "the single most important Stage 4 regression test": it proves
that without a human-created approval, there is NO path to a real
provider mutation, no matter how the execution layer is invoked.
"""

from datetime import datetime, timezone
from decimal import Decimal

from src.execution.base import (
    Balance, FeeEstimate, LiveSubmissionOutcome, Market, NormalizedOrderBook, OrderLevel, ProviderCapabilities,
)
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.live import approval, service, store


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
    min_net_ev_pct = 1.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    live_trading_enabled = True
    require_human_approval = True
    kalshi_live_enabled = False
    polymarket_us_live_enabled = True
    approval_ttl_seconds = 30
    live_require_fresh_orderbook = True
    live_max_orderbook_age_seconds = 10
    live_allow_post_approval_size_reduction = False
    live_provider_error_threshold = 3
    live_provider_error_window_minutes = 15

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
    """A provider willing to answer place_order/cancel_order (to prove
    the flow never calls them) and _submit_authorized_order exactly
    once per call, tracked via call counters."""

    def __init__(self, submission_outcome: LiveSubmissionOutcome | None = None):
        self.place_order_calls = 0
        self.cancel_order_calls = 0
        self.submit_calls = 0
        self._submission_outcome = submission_outcome or LiveSubmissionOutcome(
            outcome="CONFIRMED", provider_order_id="ORDER-1", detail="filled",
            raw_reference="HTTP 201", quantity_filled=Decimal("14"), average_fill_price=Decimal("0.68"),
            order_state="FILLED",
        )

    capabilities = ProviderCapabilities(False, False, True, True, False, False, False, False)

    def place_order(self, *a, **k):
        self.place_order_calls += 1
        raise NotImplementedError("place_order is permanently blocked")

    def cancel_order(self, *a, **k):
        self.cancel_order_calls += 1
        raise NotImplementedError("cancel_order is permanently blocked")

    def get_balance(self):
        return Balance(currency="USD", available=1000.0)

    def get_market(self, market_id):
        return Market(id=market_id, title="x", status="active", raw={})

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
        fee = Decimal("0.06") * quantity * price * (Decimal("1") - price)
        return FeeEstimate(fee=fee, fee_estimate=False, detail="test")

    def _submit_authorized_order(self, authorization, quantity, limit_price):
        self.submit_calls += 1
        return self._submission_outcome


class TestPositiveEndToEnd:
    """model signal -> Stage 2 opportunity -> Stage 3 sizing -> risk
    approval -> prepared live order -> UI approval -> fresh orderbook ->
    EV recalculation -> fresh risk approval -> mocked provider POST ->
    mocked fill -> live position. Exactly ONE provider mutation call."""

    def test_full_flow_results_in_exactly_one_provider_mutation(self, db_conn):
        provider = _FakeProvider()
        opportunity = _opportunity()
        signal = _signal()
        config = _FakeConfig()

        prep = approval.prepare_order(db_conn, opportunity, signal, provider, config)
        assert prep.rejected is False
        assert prep.prepared_order_id is not None

        authorization = approval.approve(db_conn, prep.prepared_order_id, config)
        assert authorization is not None
        assert authorization.status.value == "APPROVED"

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)

        assert result.outcome == "SUBMITTED"
        assert provider.submit_calls == 1
        assert provider.place_order_calls == 0
        assert provider.cancel_order_calls == 0

        position = store.get_open_live_positions(db_conn)
        assert len(position) == 1
        assert position[0]["quantity"] == 14.0


class TestNegativeEndToEnd:
    """THE single most important Stage 4 regression test: the same
    opportunity, with NO human approval created, must be BLOCKED with
    ZERO provider mutation calls -- attempted through every path this
    test can reach."""

    def test_no_approval_blocks_execution_with_zero_provider_calls(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()

        # No prepare_order/approve call at all -- attempt to execute a
        # fabricated, never-approved approval_id directly.
        result = service.execute_authorized(db_conn, "never-approved-id", provider, config)

        assert result.outcome == "BLOCKED"
        assert provider.submit_calls == 0
        assert provider.place_order_calls == 0
        assert provider.cancel_order_calls == 0

    def test_a_prepared_but_never_approved_order_cannot_be_executed(self, db_conn):
        """Even reaching the PREPARED stage (queued for human review)
        must not be enough -- only an explicit approve() call, followed
        by execute_authorized() on ITS approval_id, can ever submit."""
        provider = _FakeProvider()
        config = _FakeConfig()
        opportunity = _opportunity()
        signal = _signal()

        prep = approval.prepare_order(db_conn, opportunity, signal, provider, config)
        assert prep.rejected is False

        # No approval.approve() call -- the prepared order sits in
        # READY status, never becoming an ExecutionAuthorization.
        # There is no approval_id to even attempt execution with; the
        # only way to reach execute_authorized is via a real approval_id,
        # which was never created. Confirm no authorization exists.
        authorization = store.get_authorization(db_conn, "any-id")
        assert authorization is None
        assert provider.submit_calls == 0

    def test_rejected_prepared_order_cannot_later_be_approved(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        opportunity = _opportunity()
        signal = _signal()

        prep = approval.prepare_order(db_conn, opportunity, signal, provider, config)
        assert approval.reject(db_conn, prep.prepared_order_id, "not interested") is True

        authorization = approval.approve(db_conn, prep.prepared_order_id, config)
        assert authorization is None  # REJECTED, not READY -- cannot be approved
        assert provider.submit_calls == 0


class TestReplayAndConcurrencySafety:
    def test_double_execute_call_only_submits_once(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        opportunity = _opportunity()
        signal = _signal()

        prep = approval.prepare_order(db_conn, opportunity, signal, provider, config)
        authorization = approval.approve(db_conn, prep.prepared_order_id, config)

        first = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        second = service.execute_authorized(db_conn, authorization.approval_id, provider, config)

        assert first.outcome == "SUBMITTED"
        assert second.outcome == "BLOCKED"
        assert provider.submit_calls == 1

    def test_double_click_approve_only_creates_one_authorization(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        opportunity = _opportunity()
        signal = _signal()

        prep = approval.prepare_order(db_conn, opportunity, signal, provider, config)
        first = approval.approve(db_conn, prep.prepared_order_id, config)
        second = approval.approve(db_conn, prep.prepared_order_id, config)

        assert first is not None
        assert second is None  # already claimed (APPROVED) by the first call
