"""Stage 4.1 section 19: genuine multi-threaded concurrency test for the
atomic approval claim. Uses a real file-backed SQLite database with two
SEPARATE connections (not the in-memory db_conn fixture, which can't be
shared safely across threads) to simulate two processes racing to
execute the SAME approval at nearly the same time.

Expected: exactly one connection's claim succeeds (rowcount == 1);
the other observes rowcount == 0 and must report the equivalent of
APPROVAL_ALREADY_USED. Provider mutation call count across both
threads combined must never exceed 1.
"""

import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from database.db_manager import init_db
from src.execution.base import Balance, FeeEstimate, LiveSubmissionOutcome, Market, NormalizedOrderBook, OrderLevel, ProviderCapabilities
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
    live_provider_error_threshold = 100
    live_provider_error_window_minutes = 15

    def ev_tiered_sizing_tiers(self):
        return ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))


class _FakeProvider:
    """Each thread gets its OWN instance (a real provider client would
    be one-per-thread/process too), but the class-level counter is
    shared so we can prove at most one submission happened across all
    instances combined."""
    submit_calls = 0
    _lock = threading.Lock()
    capabilities = ProviderCapabilities(False, False, True, True, False, False, False, False)

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
        with type(self)._lock:
            type(self).submit_calls += 1
        return LiveSubmissionOutcome(
            outcome="CONFIRMED", provider_order_id="ORDER-1", detail="filled", raw_reference="HTTP 201",
            quantity_filled=quantity, average_fill_price=limit_price, order_state="FILLED",
        )


@pytest.fixture
def file_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.remove(path)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _opportunity() -> ExecutionOpportunity:
    now = datetime.now(timezone.utc)
    return ExecutionOpportunity(
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


def _signal() -> ExecutionSignal:
    return ExecutionSignal(
        recommendation_id="rec-1", league="MLB", market_type="moneyline",
        home_team="Toronto Blue Jays", away_team="Athletics", side="AWAY", line=None,
        event_start_time=None, model_probability=Decimal("0.72"), sportsbook_ev_pct=6.0,
        rec_status="STRONG_EDGE", signal_timestamp=datetime.now(timezone.utc),
    )


class TestConcurrentExecutionClaim:
    def test_two_threads_racing_the_same_approval_only_one_submits(self, file_db_path):
        _FakeProvider.submit_calls = 0

        setup_conn = _connect(file_db_path)
        provider_for_setup = _FakeProvider()
        prep = approval.prepare_order(setup_conn, _opportunity(), _signal(), provider_for_setup, _FakeConfig())
        authorization = approval.approve(setup_conn, prep.prepared_order_id, _FakeConfig())
        setup_conn.close()
        assert authorization is not None

        results = []
        barrier = threading.Barrier(2)

        def worker():
            conn = _connect(file_db_path)
            provider = _FakeProvider()
            try:
                barrier.wait(timeout=5)  # maximize the chance both threads hit the claim at nearly the same time
                result = service.execute_authorized(conn, authorization.approval_id, provider, _FakeConfig())
                results.append(result)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 2
        outcomes = [r.outcome for r in results]
        assert outcomes.count("SUBMITTED") == 1
        assert outcomes.count("BLOCKED") == 1
        assert _FakeProvider.submit_calls == 1  # the single most important assertion in this test

    def test_loser_gets_approval_already_used_equivalent(self, file_db_path):
        _FakeProvider.submit_calls = 0

        setup_conn = _connect(file_db_path)
        provider_for_setup = _FakeProvider()
        prep = approval.prepare_order(setup_conn, _opportunity(), _signal(), provider_for_setup, _FakeConfig())
        authorization = approval.approve(setup_conn, prep.prepared_order_id, _FakeConfig())
        setup_conn.close()

        # Sequential (not threaded) this time -- isolates that the SECOND
        # call specifically reports the correct reason once the first has
        # already claimed the approval.
        conn1 = _connect(file_db_path)
        result1 = service.execute_authorized(conn1, authorization.approval_id, _FakeProvider(), _FakeConfig())
        conn1.close()

        conn2 = _connect(file_db_path)
        result2 = service.execute_authorized(conn2, authorization.approval_id, _FakeProvider(), _FakeConfig())
        conn2.close()

        assert result1.outcome == "SUBMITTED"
        assert result2.outcome == "BLOCKED"
        assert result2.reason in ("APPROVAL_ALREADY_USED", "USED")
        assert _FakeProvider.submit_calls == 1


class TestRestartBehavior:
    """Section 14: DB-persisted approval state must survive a 'restart'
    (modeled here as closing every connection/object and reopening
    fresh ones -- there is no Python-process-level state anywhere in
    the approval/execution path, only the database and, for Streamlit
    specifically, an explicitly session-local, non-persisted toggle)."""

    def test_expired_approval_still_rejected_after_restart(self, file_db_path):
        conn = _connect(file_db_path)
        prep = approval.prepare_order(conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        authorization = approval.approve(conn, prep.prepared_order_id, _FakeConfig())
        conn.execute(
            "UPDATE execution_authorizations SET expires_at = ? WHERE approval_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(), authorization.approval_id),
        )
        conn.commit()
        conn.close()  # simulates process exit

        # "Restart": brand-new connection and provider instance, as a
        # fresh process would create.
        fresh_conn = _connect(file_db_path)
        fresh_provider = _FakeProvider()
        _FakeProvider.submit_calls = 0
        result = service.execute_authorized(fresh_conn, authorization.approval_id, fresh_provider, _FakeConfig())
        fresh_conn.close()

        assert result.outcome == "BLOCKED"
        assert result.reason == "APPROVAL_EXPIRED"
        assert _FakeProvider.submit_calls == 0

    def test_used_approval_still_rejected_after_restart(self, file_db_path):
        conn = _connect(file_db_path)
        prep = approval.prepare_order(conn, _opportunity(), _signal(), _FakeProvider(), _FakeConfig())
        authorization = approval.approve(conn, prep.prepared_order_id, _FakeConfig())
        _FakeProvider.submit_calls = 0
        service.execute_authorized(conn, authorization.approval_id, _FakeProvider(), _FakeConfig())
        assert _FakeProvider.submit_calls == 1
        conn.close()

        fresh_conn = _connect(file_db_path)
        fresh_provider = _FakeProvider()
        result = service.execute_authorized(fresh_conn, authorization.approval_id, fresh_provider, _FakeConfig())
        fresh_conn.close()

        assert result.outcome == "BLOCKED"
        assert _FakeProvider.submit_calls == 1  # still just the one from before "restart"

    def test_streamlit_live_mode_toggle_is_never_persisted_to_the_database(self, file_db_path):
        """Confirms there is no DB row anywhere that could let the
        Streamlit-only 'LIVE MODE' toggle survive a restart -- it must
        be pure st.session_state, reset to OFF on every fresh process."""
        conn = _connect(file_db_path)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        conn.close()
        assert not any("live_mode" in t.lower() for t in tables)
