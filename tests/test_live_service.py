"""Tests for src/execution/live/service.py -- kill switch, circuit
breaker, config gates, and REJECTED/AMBIGUOUS outcome handling not
already covered by the E2E happy/negative-path tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.execution.base import (
    Balance, FeeEstimate, LiveSubmissionOutcome, Market, NormalizedOrderBook, OrderLevel, ProviderCapabilities,
)
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.live import approval, kill_switch, service, store


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
        provider="polymarket_us", provider_market_id="M1", match_confidence=1.0,
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
    capabilities = ProviderCapabilities(False, False, True, True, False, False, False, False)

    def __init__(self, submission_outcome=None):
        self.submit_calls = 0
        self._submission_outcome = submission_outcome or LiveSubmissionOutcome(
            outcome="CONFIRMED", provider_order_id="ORDER-1", detail="filled", raw_reference="HTTP 201",
            quantity_filled=Decimal("14"), average_fill_price=Decimal("0.68"), order_state="FILLED",
        )

    def get_balance(self):
        return Balance(currency="USD", available=1000.0)

    def get_market(self, market_id):
        return Market(id=market_id, title="x", status="active", raw={})

    def get_orderbook(self, market_id):
        return object()

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id="M1", yes_bids=[OrderLevel(Decimal("0.66"), Decimal("200"))],
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


def _prepare_and_approve(conn, provider, config):
    prep = approval.prepare_order(conn, _opportunity(), _signal(), provider, config)
    return approval.approve(conn, prep.prepared_order_id, config)


class TestKillSwitch:
    def test_engaged_kill_switch_blocks_execution(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        authorization = _prepare_and_approve(db_conn, provider, config)
        kill_switch.engage_kill_switch(db_conn, "manual stop")

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert result.reason == "KILL_SWITCH_ENGAGED"
        assert provider.submit_calls == 0

    def test_disengaging_allows_execution_again(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        kill_switch.engage_kill_switch(db_conn, "test")
        kill_switch.disengage_kill_switch(db_conn)
        authorization = _prepare_and_approve(db_conn, provider, config)

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "SUBMITTED"

    def test_kill_switch_does_not_touch_existing_positions(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        authorization = _prepare_and_approve(db_conn, provider, config)
        service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert len(store.get_open_live_positions(db_conn)) == 1

        kill_switch.engage_kill_switch(db_conn, "stop")
        # Existing position should be untouched.
        assert len(store.get_open_live_positions(db_conn)) == 1


class TestCircuitBreaker:
    def test_repeated_ambiguous_errors_trip_the_circuit(self, db_conn):
        config = _FakeConfig()
        config.live_provider_error_threshold = 2
        ambiguous_outcome = LiveSubmissionOutcome(
            outcome="AMBIGUOUS", provider_order_id=None, detail="timeout", raw_reference=None,
        )

        for i in range(2):
            provider = _FakeProvider(submission_outcome=ambiguous_outcome)
            prep = approval.prepare_order(
                db_conn, _opportunity(recommendation_id=f"rec-{i}"), _signal(recommendation_id=f"rec-{i}"),
                provider, config,
            )
            authorization = approval.approve(db_conn, prep.prepared_order_id, config)
            service.execute_authorized(db_conn, authorization.approval_id, provider, config)

        assert kill_switch.is_circuit_tripped(db_conn, "polymarket_us") is True

    def test_tripped_circuit_blocks_further_submissions(self, db_conn):
        config = _FakeConfig()
        kill_switch.record_provider_error(db_conn, "polymarket_us", error_threshold=1, window_minutes=15)
        provider = _FakeProvider()
        authorization = _prepare_and_approve(db_conn, provider, config)

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert result.reason == "CIRCUIT_BREAKER_TRIPPED"
        assert provider.submit_calls == 0

    def test_confirmed_submission_resets_the_circuit(self, db_conn):
        config = _FakeConfig()
        kill_switch.record_provider_error(db_conn, "polymarket_us", error_threshold=100, window_minutes=15)
        provider = _FakeProvider()
        authorization = _prepare_and_approve(db_conn, provider, config)
        service.execute_authorized(db_conn, authorization.approval_id, provider, config)

        row = db_conn.execute(
            "SELECT consecutive_errors FROM live_provider_circuit_state WHERE provider = 'polymarket_us'"
        ).fetchone()
        assert row["consecutive_errors"] == 0


class TestConfigGates:
    def test_live_trading_disabled_blocks(self, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        authorization = _prepare_and_approve(db_conn, provider, config)
        config.live_trading_enabled = False

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert result.reason == "PROVIDER_DISABLED"
        assert provider.submit_calls == 0

    def test_provider_specific_flag_disabled_blocks(self, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        authorization = _prepare_and_approve(db_conn, provider, config)
        config.polymarket_us_live_enabled = False

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert provider.submit_calls == 0

    def test_require_human_approval_false_blocks_even_with_a_real_approval(self, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        authorization = _prepare_and_approve(db_conn, provider, config)
        config.require_human_approval = False

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert provider.submit_calls == 0


class TestExpiredApproval:
    def test_expired_approval_blocks_and_invalidates(self, db_conn):
        provider = _FakeProvider()
        config = _FakeConfig()
        authorization = _prepare_and_approve(db_conn, provider, config)
        db_conn.execute(
            "UPDATE execution_authorizations SET expires_at = ? WHERE approval_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), authorization.approval_id),
        )
        db_conn.commit()

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert result.reason == "APPROVAL_EXPIRED"
        assert provider.submit_calls == 0

        row = store.get_authorization(db_conn, authorization.approval_id)
        assert row["status"] == "INVALIDATED"


class TestProviderRejection:
    def test_definite_rejection_never_creates_a_live_order(self, db_conn):
        config = _FakeConfig()
        rejected_outcome = LiveSubmissionOutcome(
            outcome="REJECTED", provider_order_id=None, detail="validation failed", raw_reference="HTTP 400",
        )
        provider = _FakeProvider(submission_outcome=rejected_outcome)
        authorization = _prepare_and_approve(db_conn, provider, config)

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        assert result.reason == "PROVIDER_REJECTED"
        assert provider.submit_calls == 1  # one attempt was made, but no order resulted
        rows = db_conn.execute("SELECT COUNT(*) AS n FROM live_orders").fetchone()
        assert rows["n"] == 0


class TestAmbiguousSubmission:
    def test_ambiguous_outcome_creates_no_position_and_requires_manual_review(self, db_conn):
        config = _FakeConfig()
        ambiguous_outcome = LiveSubmissionOutcome(
            outcome="AMBIGUOUS", provider_order_id=None, detail="connection reset", raw_reference=None,
        )
        provider = _FakeProvider(submission_outcome=ambiguous_outcome)
        authorization = _prepare_and_approve(db_conn, provider, config)

        result = service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        assert result.outcome == "BLOCKED"
        # The fake provider's default capabilities have no reconciliation
        # read support, so reconciliation itself is INCONCLUSIVE -- still
        # blocked, still manual review, just a more specific reason string
        # now that Stage 4.1 wires reconciliation into this path.
        assert result.reason == "INCONCLUSIVE"
        assert provider.submit_calls == 1
        assert len(store.get_open_live_positions(db_conn)) == 0

        attempt = db_conn.execute(
            "SELECT * FROM live_submission_attempts WHERE approval_id = ?", (authorization.approval_id,)
        ).fetchone()
        assert attempt["state"] == "MANUAL_REVIEW_REQUIRED"

    def test_ambiguous_outcome_never_auto_retries(self, db_conn):
        """A second execute_authorized call for the SAME approval_id
        must not attempt a second POST -- the approval was already
        consumed (USED) by the first (ambiguous) attempt."""
        config = _FakeConfig()
        ambiguous_outcome = LiveSubmissionOutcome(
            outcome="AMBIGUOUS", provider_order_id=None, detail="timeout", raw_reference=None,
        )
        provider = _FakeProvider(submission_outcome=ambiguous_outcome)
        authorization = _prepare_and_approve(db_conn, provider, config)

        service.execute_authorized(db_conn, authorization.approval_id, provider, config)
        second = service.execute_authorized(db_conn, authorization.approval_id, provider, config)

        assert second.outcome == "BLOCKED"
        assert provider.submit_calls == 1  # never a second POST
