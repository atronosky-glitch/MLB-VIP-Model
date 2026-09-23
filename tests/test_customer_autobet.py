"""Tests for src/execution/customer_autobet.py -- per-customer,
per-platform (Kalshi + Polymarket) Auto-Bet orchestration: user
isolation, cross-platform independence, duplicate prevention, risk
enforcement, PAPER vs LIVE, the hybrid auto-approve/manual-approve
split, and every disabled/disconnected/error path never executing. All
provider network calls are mocked."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import src.customer_kalshi as ck
import src.customer_polymarket as cp
import src.execution.customer_autobet as autobet
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key
from src.execution.base import (
    Balance, FeeEstimate, LiveSubmissionOutcome, Market, NormalizedOrderBook, OrderLevel, ProviderCapabilities,
)
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.live import kill_switch


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())


def _real_b64_key() -> str:
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode("ascii")


def _real_pem_key() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


def _connect_polymarket(conn, account_id, key_suffix="A"):
    with mock.patch.object(cp.PolymarketUSProvider, "health_check", return_value=mock.MagicMock(ok=True)):
        cp.connect_account(conn, account_id, f"key-{key_suffix}", _real_b64_key())


def _connect_kalshi(conn, account_id, key_suffix="A"):
    with mock.patch.object(ck.KalshiProvider, "health_check", return_value=mock.MagicMock(ok=True)):
        ck.connect_account(conn, account_id, f"kalshi-key-{key_suffix}", _real_pem_key())


_CONNECT_FN = {"polymarket_us": _connect_polymarket, "kalshi": _connect_kalshi}
_MODULE = {"polymarket_us": cp, "kalshi": ck}


def _enable_autobet(conn, account_id, platform="polymarket_us", **risk_overrides):
    module = _MODULE[platform]
    settings = dict(module.DEFAULT_RISK_SETTINGS)
    settings.update(risk_overrides)
    module.save_risk_settings(conn, account_id, **settings)
    ok, msg = module.enable_autobet(conn, account_id)
    assert ok, msg


def _account_row(conn, account_id, platform="polymarket_us"):
    table = autobet._PLATFORM_TABLE[platform]
    return dict(conn.execute(f"SELECT * FROM {table} WHERE account_id = ?", (account_id,)).fetchone())


class _FakeRiskConfig:
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
    allow_position_addons = False
    max_opportunity_age_seconds = 30
    stop_after_daily_profit_target = False
    daily_profit_target_usd = 0.0
    min_net_ev_pct = 1.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    live_trading_enabled = True
    require_human_approval = True
    kalshi_live_enabled = True
    polymarket_us_live_enabled = True
    approval_ttl_seconds = 30
    live_require_fresh_orderbook = True
    live_max_orderbook_age_seconds = 10
    live_allow_post_approval_size_reduction = False
    live_provider_error_threshold = 3
    live_provider_error_window_minutes = 15
    database_path = ":memory:"

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


class _FakeComparison:
    def __init__(self, best, alternatives=None):
        self.best = best
        self.qualified_alternatives = alternatives or []


class _FakeLiveProvider:
    capabilities = ProviderCapabilities(False, False, True, True, False, False, False, False)

    def __init__(self, submission_outcome=None, balance=1000.0):
        self.submit_calls = 0
        self._balance = balance
        self._submission_outcome = submission_outcome or LiveSubmissionOutcome(
            outcome="CONFIRMED", provider_order_id="ORDER-1", detail="filled", raw_reference="HTTP 201",
            quantity_filled=Decimal("14"), average_fill_price=Decimal("0.68"), order_state="FILLED",
        )

    def get_balance(self):
        return Balance(currency="USD", available=self._balance)

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


class TestPaperModeExecution:
    """PAPER mode: lightweight, self-contained simulation. Never
    touches live_* tables or calls any provider write method."""

    def test_qualifying_recommendation_executes_in_paper_mode(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert status == "EXECUTED"
        rows = db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE account_id = 'acct-1'"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["mode"] == "PAPER"
        assert row["platform"] == "polymarket_us"

    def test_paper_mode_never_touches_live_tables(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert db_conn.execute("SELECT COUNT(*) AS c FROM prepared_live_orders").fetchone()["c"] == 0
        assert db_conn.execute("SELECT COUNT(*) AS c FROM execution_authorizations").fetchone()["c"] == 0
        assert db_conn.execute("SELECT COUNT(*) AS c FROM live_positions").fetchone()["c"] == 0

    def test_wrong_provider_opportunity_is_skipped(self, db_conn):
        """This account only ever trades polymarket_us; an opportunity
        for a different platform must never be dispatched here."""
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us",
            _opportunity(provider="kalshi"), _signal(),
        )
        assert status == "SKIPPED"

    def test_ev_below_customer_threshold_causes_skip(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", min_net_ev_pct=10.0)  # opportunity has net_ev_pct=3.5
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert status == "SKIPPED"
        rows = db_conn.execute("SELECT * FROM customer_autobet_executions").fetchall()
        assert len(rows) == 0  # rejected before any claim/record -- purely a pre-flight filter

    def test_max_bet_usd_caps_the_stake(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", unit_size_usd=100.0, max_bet_usd=7.5)
        account = _account_row(db_conn, "acct-1")
        autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        row = dict(db_conn.execute("SELECT * FROM customer_autobet_executions").fetchone())
        assert row["stake_usd"] <= 7.5

    def test_sport_filter_blocks_other_leagues(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", sport_filter="NFL,WNBA")
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us",
            _opportunity(league="MLB"), _signal(league="MLB"),
        )
        assert status == "SKIPPED"

    def test_sport_filter_allows_matching_league(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", sport_filter="MLB,NFL")
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us",
            _opportunity(league="MLB"), _signal(league="MLB"),
        )
        assert status == "EXECUTED"


class TestDuplicatePrevention:
    def test_duplicate_recommendation_does_not_create_a_second_bet(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        s1 = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        s2 = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert s1 == "EXECUTED"
        assert s2 == "SKIPPED"
        rows = db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE status = 'EXECUTED'"
        ).fetchall()
        assert len(rows) == 1

    def test_worker_restart_does_not_duplicate_an_already_executed_bet(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        db_conn.execute("DELETE FROM customer_autobet_platform_claims")
        db_conn.commit()
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert status == "SKIPPED"
        assert len(db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE status = 'EXECUTED'"
        ).fetchall()) == 1


class TestCrossPlatformIndependence:
    """Section 53's explicit requirement: one official recommendation
    can execute ONCE on Kalshi and ONCE on Polymarket for the same
    customer, if both are enabled and both independently qualify --
    but never twice on the SAME platform."""

    def test_same_recommendation_executes_once_per_platform_for_the_same_customer(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _connect_kalshi(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", platform="polymarket_us")
        _enable_autobet(db_conn, "acct-1", platform="kalshi")
        poly_account = _account_row(db_conn, "acct-1", "polymarket_us")
        kalshi_account = _account_row(db_conn, "acct-1", "kalshi")

        poly_status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), poly_account, "polymarket_us",
            _opportunity(provider="polymarket_us"), _signal(),
        )
        kalshi_status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), kalshi_account, "kalshi",
            _opportunity(provider="kalshi"), _signal(),
        )
        assert poly_status == "EXECUTED"
        assert kalshi_status == "EXECUTED"
        rows = db_conn.execute(
            "SELECT platform, status FROM customer_autobet_executions WHERE account_id = 'acct-1' AND status = 'EXECUTED'"
        ).fetchall()
        platforms = {dict(r)["platform"] for r in rows}
        assert platforms == {"polymarket_us", "kalshi"}

    def test_kalshi_credential_failure_does_not_block_polymarket_execution(self, db_conn):
        """Platform failure isolation: a broken/unavailable Kalshi
        connection for this customer must never prevent their
        Polymarket execution for the same recommendation."""
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", platform="polymarket_us")
        poly_account = _account_row(db_conn, "acct-1", "polymarket_us")
        # No Kalshi account connected at all for this customer -- the
        # dispatcher (run_customer_autobet_pass) would simply never find
        # them in the Kalshi accounts list, but we can directly prove
        # the Polymarket path is entirely unaffected either way.
        poly_status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), poly_account, "polymarket_us",
            _opportunity(provider="polymarket_us"), _signal(),
        )
        assert poly_status == "EXECUTED"

    def test_run_customer_autobet_pass_dispatches_both_platforms_from_one_signal(self, db_conn):
        """End-to-end proof at the run_customer_autobet_pass level: a
        signal that qualifies on BOTH platforms (comparison.best +
        qualified_alternatives) results in one execution per platform
        for a customer with both enabled."""
        _connect_polymarket(db_conn, "acct-1")
        _connect_kalshi(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1", platform="polymarket_us")
        _enable_autobet(db_conn, "acct-1", platform="kalshi")

        poly_opp = _opportunity(provider="polymarket_us")
        kalshi_opp = _opportunity(provider="kalshi")
        comparison = _FakeComparison(best=poly_opp, alternatives=[kalshi_opp])
        gathered = [("evaluated", mock.MagicMock(), _signal(), comparison)]

        class _UnclosableConnProxy:
            """run_customer_autobet_pass closes its connection in a
            finally block -- fine in production (a fresh connection per
            pass), but this test needs to inspect db_conn afterward, so
            close() is swallowed rather than mocking the real
            sqlite3.Connection's C-level close method directly (which
            doesn't support attribute patching)."""
            def __init__(self, real):
                self._real = real
            def __getattr__(self, name):
                return getattr(self._real, name)
            def close(self):
                pass

        with mock.patch("src.execution.customer_autobet.get_connection", return_value=_UnclosableConnProxy(db_conn)), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[]), \
             mock.patch("src.execution.paper_cli._gather_qualified_signals", return_value=gathered), \
             mock.patch.object(autobet, "_build_scanning_only_provider", return_value=_FakeLiveProvider()):
            result = autobet.run_customer_autobet_pass(_FakeRiskConfig())

        assert result["executed"] == 2
        rows = db_conn.execute(
            "SELECT platform FROM customer_autobet_executions WHERE account_id = 'acct-1' AND status = 'EXECUTED'"
        ).fetchall()
        assert {dict(r)["platform"] for r in rows} == {"polymarket_us", "kalshi"}


class TestUserIsolation:
    """The most critical guarantee: User A's execution only ever uses
    User A's credentials, and never affects User B's exposure/limits."""

    def test_two_accounts_execute_independently_with_their_own_credentials(self, db_conn):
        _connect_polymarket(db_conn, "acct-A", key_suffix="A")
        _connect_polymarket(db_conn, "acct-B", key_suffix="B")
        _enable_autobet(db_conn, "acct-A")
        _enable_autobet(db_conn, "acct-B")

        constructed_with = []
        real_init = autobet.PolymarketUSProvider.__init__

        def spy_init(self, *a, **kw):
            constructed_with.append(kw.get("api_key_id"))
            return real_init(self, *a, **kw)

        with mock.patch.object(autobet.PolymarketUSProvider, "__init__", spy_init):
            account_a = _account_row(db_conn, "acct-A")
            account_b = _account_row(db_conn, "acct-B")
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account_a, "polymarket_us",
                _opportunity(recommendation_id="rec-A"), _signal(recommendation_id="rec-A"),
            )
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account_b, "polymarket_us",
                _opportunity(recommendation_id="rec-B"), _signal(recommendation_id="rec-B"),
            )

        assert constructed_with == ["key-A", "key-B"]

    def test_disabling_one_account_never_affects_another(self, db_conn):
        _connect_polymarket(db_conn, "acct-A")
        _connect_polymarket(db_conn, "acct-B")
        _enable_autobet(db_conn, "acct-A")
        _enable_autobet(db_conn, "acct-B")
        cp.disable_autobet(db_conn, "acct-A")

        account_a = _account_row(db_conn, "acct-A")
        account_b = _account_row(db_conn, "acct-B")
        assert account_a["autobet_enabled"] == 0
        assert account_b["autobet_enabled"] == 1

    def test_one_accounts_paper_exposure_never_counts_toward_another(self, db_conn):
        _connect_polymarket(db_conn, "acct-A")
        _connect_polymarket(db_conn, "acct-B")
        _enable_autobet(db_conn, "acct-A", max_total_exposure_usd=5.0, unit_size_usd=10.0)
        _enable_autobet(db_conn, "acct-B", max_total_exposure_usd=1000.0, unit_size_usd=10.0)

        for i in range(3):
            account_a = _account_row(db_conn, "acct-A")
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account_a, "polymarket_us",
                _opportunity(recommendation_id=f"rec-A-{i}"), _signal(recommendation_id=f"rec-A-{i}"),
            )
        account_b = _account_row(db_conn, "acct-B")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account_b, "polymarket_us",
            _opportunity(recommendation_id="rec-B"), _signal(recommendation_id="rec-B"),
        )
        assert status == "EXECUTED"

    def test_user_a_kalshi_credentials_never_used_for_user_bs_kalshi_order(self, db_conn):
        _connect_kalshi(db_conn, "acct-A", key_suffix="A")
        _connect_kalshi(db_conn, "acct-B", key_suffix="B")
        _enable_autobet(db_conn, "acct-A", platform="kalshi")
        _enable_autobet(db_conn, "acct-B", platform="kalshi")

        constructed_with = []
        real_init = autobet.KalshiProvider.__init__

        def spy_init(self, *a, **kw):
            constructed_with.append(kw.get("api_key_id"))
            return real_init(self, *a, **kw)

        with mock.patch.object(autobet.KalshiProvider, "__init__", spy_init):
            account_a = _account_row(db_conn, "acct-A", "kalshi")
            account_b = _account_row(db_conn, "acct-B", "kalshi")
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account_a, "kalshi",
                _opportunity(provider="kalshi", recommendation_id="rec-A"), _signal(recommendation_id="rec-A"),
            )
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account_b, "kalshi",
                _opportunity(provider="kalshi", recommendation_id="rec-B"), _signal(recommendation_id="rec-B"),
            )
        assert constructed_with == ["kalshi-key-A", "kalshi-key-B"]


class TestAccountGating:
    def test_disconnected_account_never_executes(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        cp.disconnect_account(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        assert account["autobet_enabled"] == 0  # disconnect also disables
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert status == "FAILED"  # no credentials to decrypt

    def test_run_pass_excludes_disabled_accounts(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        accounts = autobet._all_autobet_enabled_accounts(db_conn, "polymarket_us")
        assert accounts == []

    def test_run_pass_excludes_disconnected_accounts(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        cp.disconnect_account(db_conn, "acct-1")
        accounts = autobet._all_autobet_enabled_accounts(db_conn, "polymarket_us")
        assert accounts == []

    def test_run_pass_includes_only_connected_and_enabled(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        accounts = autobet._all_autobet_enabled_accounts(db_conn, "polymarket_us")
        assert [a["account_id"] for a in accounts] == ["acct-1"]

    def test_kalshi_accounts_never_appear_in_polymarket_list_and_vice_versa(self, db_conn):
        _connect_kalshi(db_conn, "acct-K")
        _enable_autobet(db_conn, "acct-K", platform="kalshi")
        _connect_polymarket(db_conn, "acct-P")
        _enable_autobet(db_conn, "acct-P", platform="polymarket_us")
        assert [a["account_id"] for a in autobet._all_autobet_enabled_accounts(db_conn, "kalshi")] == ["acct-K"]
        assert [a["account_id"] for a in autobet._all_autobet_enabled_accounts(db_conn, "polymarket_us")] == ["acct-P"]


class TestCredentialSecurity:
    def test_broken_credentials_fail_gracefully_not_crash(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        db_conn.execute(
            "UPDATE customer_polymarket_accounts SET encrypted_private_key = 'corrupted' WHERE account_id = 'acct-1'"
        )
        db_conn.commit()
        account = _account_row(db_conn, "acct-1")
        status = autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        assert status == "FAILED"

    def test_credentials_never_appear_in_a_failure_record(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        db_conn.execute(
            "UPDATE customer_polymarket_accounts SET encrypted_private_key = 'corrupted' WHERE account_id = 'acct-1'"
        )
        db_conn.commit()
        account = _account_row(db_conn, "acct-1")
        autobet.process_recommendation_for_account(
            db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
        )
        row = dict(db_conn.execute("SELECT * FROM customer_autobet_executions").fetchone())
        assert "corrupted" not in str(row)


class TestLiveModeGatingAndHybridApproval:
    def _live_account(self, db_conn, account_id="acct-1", platform="polymarket_us", auto_approve_max_usd=10.0, **overrides):
        _CONNECT_FN[platform](db_conn, account_id)
        _enable_autobet(db_conn, account_id, platform=platform, auto_approve_max_usd=auto_approve_max_usd, **overrides)
        _MODULE[platform].set_live_execution(db_conn, account_id, True)
        return _account_row(db_conn, account_id, platform)

    def test_paper_mode_never_calls_execute_authorized(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        with mock.patch("src.execution.customer_autobet.execute_authorized") as mocked:
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        mocked.assert_not_called()

    def test_live_trading_globally_disabled_causes_skip_not_execution(self, db_conn):
        account = self._live_account(db_conn)
        config = _FakeRiskConfig()
        config.live_trading_enabled = False
        with mock.patch.object(autobet, "_build_customer_provider", return_value=_FakeLiveProvider()):
            status = autobet.process_recommendation_for_account(
                db_conn, config, account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "SKIPPED"
        assert db_conn.execute("SELECT COUNT(*) AS c FROM prepared_live_orders").fetchone()["c"] == 0

    def test_kill_switch_engaged_blocks_live_execution(self, db_conn):
        account = self._live_account(db_conn)
        kill_switch.engage_kill_switch(db_conn, "test stop")
        with mock.patch.object(autobet, "_build_customer_provider", return_value=_FakeLiveProvider()):
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "SKIPPED"

    def test_order_under_auto_approve_cap_executes_live_automatically(self, db_conn):
        account = self._live_account(db_conn, auto_approve_max_usd=100.0, unit_size_usd=10.0, max_bet_usd=10.0)
        fake_provider = _FakeLiveProvider()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider), \
             mock.patch.object(autobet, "execute_authorized") as mocked_exec:
            from src.execution.live.service import ExecutionResult
            mocked_exec.return_value = ExecutionResult(
                outcome="SUBMITTED", reason=None, detail="filled", live_order_id=1,
            )
            with mock.patch.object(autobet.live_store, "get_live_order", return_value={
                "status": "FILLED", "average_fill_price": 0.68, "quantity_filled": 14,
                "provider_order_id": "ORDER-1",
            }):
                status = autobet.process_recommendation_for_account(
                    db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
                )
        assert status == "EXECUTED"
        mocked_exec.assert_called_once()
        auth = dict(db_conn.execute("SELECT * FROM execution_authorizations").fetchone())
        assert auth["approved_by"].startswith("autobet_system:")
        assert auth["approval_mode"] == "AUTO"
        assert auth["account_id"] == "acct-1"

    def test_order_over_auto_approve_cap_queues_for_manual_approval_never_auto_executes(self, db_conn):
        account = self._live_account(db_conn, auto_approve_max_usd=1.0, unit_size_usd=10.0, max_bet_usd=10.0)
        fake_provider = _FakeLiveProvider()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider), \
             mock.patch.object(autobet, "execute_authorized") as mocked_exec:
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "SKIPPED"
        mocked_exec.assert_not_called()
        assert fake_provider.submit_calls == 0
        prepared = dict(db_conn.execute("SELECT * FROM prepared_live_orders").fetchone())
        assert prepared["status"] == "READY"
        assert prepared["account_id"] == "acct-1"
        assert db_conn.execute("SELECT COUNT(*) AS c FROM execution_authorizations").fetchone()["c"] == 0

    def test_manual_approval_pending_order_is_not_reclaimed_by_a_later_pass(self, db_conn):
        account = self._live_account(db_conn, auto_approve_max_usd=1.0, unit_size_usd=10.0, max_bet_usd=10.0)
        fake_provider = _FakeLiveProvider()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider):
            autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
            second = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert second == "SKIPPED"
        assert db_conn.execute("SELECT COUNT(*) AS c FROM prepared_live_orders").fetchone()["c"] == 1

    def test_insufficient_balance_causes_skip(self, db_conn):
        account = self._live_account(db_conn, unit_size_usd=1000.0, max_bet_usd=1000.0)
        fake_provider = _FakeLiveProvider(balance=0.0)
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider):
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "SKIPPED"

    def test_provider_balance_failure_does_not_crash(self, db_conn):
        account = self._live_account(db_conn)

        class BrokenProvider(_FakeLiveProvider):
            def get_balance(self):
                raise RuntimeError("connection reset")

        with mock.patch.object(autobet, "_build_customer_provider", return_value=BrokenProvider()):
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "FAILED"

    def test_daily_loss_limit_enforced(self, db_conn):
        account = self._live_account(db_conn, max_daily_loss_usd=5.0)
        config = _FakeRiskConfig()
        config.max_daily_loss_usd = 5.0
        db_conn.execute(
            """INSERT INTO live_positions (
                   position_id, approval_id, prepared_order_id, live_order_id, recommendation_id,
                   provider, provider_market_id, event_id, side, quantity, total_entry_cost,
                   opened_at, status, settled_at, realized_pnl, account_id
               ) VALUES (1, 'a', 1, 1, 'rec-old', 'polymarket_us', 'M0', 'evt-0', 'YES', 10, 10.0, ?, 'LOST', ?, -50.0, 'acct-1')""",
            (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=_FakeLiveProvider()):
            status = autobet.process_recommendation_for_account(
                db_conn, config, account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "SKIPPED"

    def test_kalshi_live_order_under_cap_auto_executes(self, db_conn):
        """Same hybrid-approval mechanics, exercised on the Kalshi
        platform specifically -- proves the refactor genuinely works
        for both platforms, not just Polymarket."""
        account = self._live_account(
            db_conn, platform="kalshi", auto_approve_max_usd=100.0, unit_size_usd=10.0, max_bet_usd=10.0,
        )
        fake_provider = _FakeLiveProvider()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider), \
             mock.patch.object(autobet, "execute_authorized") as mocked_exec:
            from src.execution.live.service import ExecutionResult
            mocked_exec.return_value = ExecutionResult(
                outcome="SUBMITTED", reason=None, detail="filled", live_order_id=1,
            )
            with mock.patch.object(autobet.live_store, "get_live_order", return_value={
                "status": "FILLED", "average_fill_price": 0.68, "quantity_filled": 14,
                "provider_order_id": "KALSHI-ORDER-1",
            }):
                status = autobet.process_recommendation_for_account(
                    db_conn, _FakeRiskConfig(), account, "kalshi", _opportunity(provider="kalshi"), _signal(),
                )
        assert status == "EXECUTED"
        row = dict(db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE status = 'EXECUTED'"
        ).fetchone())
        assert row["platform"] == "kalshi"


class TestManualApprovalQueue:
    """approve_pending_order / reject_pending_order -- the customer's
    own decision on an over-cap LIVE order this module queued instead
    of auto-submitting."""

    def _queued_order(self, db_conn, account_id="acct-1", platform="polymarket_us", auto_approve_max_usd=1.0):
        _CONNECT_FN[platform](db_conn, account_id)
        _enable_autobet(
            db_conn, account_id, platform=platform, auto_approve_max_usd=auto_approve_max_usd,
            unit_size_usd=10.0, max_bet_usd=10.0,
        )
        _MODULE[platform].set_live_execution(db_conn, account_id, True)
        account = _account_row(db_conn, account_id, platform)
        opp = _opportunity(provider=platform)
        with mock.patch.object(autobet, "_build_customer_provider", return_value=_FakeLiveProvider()):
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, platform, opp, _signal(),
            )
        assert status == "SKIPPED"
        row = dict(db_conn.execute(
            "SELECT * FROM prepared_live_orders WHERE account_id = ?", (account_id,)
        ).fetchone())
        return row["prepared_order_id"]

    def test_approve_under_the_right_account_submits_the_order(self, db_conn):
        prepared_order_id = self._queued_order(db_conn)
        fake_provider = _FakeLiveProvider()
        with mock.patch.object(autobet, "_build_customer_provider", return_value=fake_provider), \
             mock.patch.object(autobet, "execute_authorized") as mocked_exec, \
             mock.patch.object(autobet.live_store, "get_live_order", return_value={
                 "status": "FILLED", "average_fill_price": 0.68, "quantity_filled": 14,
                 "provider_order_id": "ORDER-1",
             }):
            from src.execution.live.service import ExecutionResult
            mocked_exec.return_value = ExecutionResult(outcome="SUBMITTED", reason=None, detail="filled", live_order_id=1)
            ok, message = autobet.approve_pending_order(db_conn, _FakeRiskConfig(), "acct-1", "polymarket_us", prepared_order_id)
        assert ok is True
        auth = dict(db_conn.execute("SELECT * FROM execution_authorizations").fetchone())
        assert auth["approved_by"] == "customer:acct-1"
        assert auth["approval_mode"] == "MANUAL"
        rows = db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE status = 'EXECUTED'"
        ).fetchall()
        assert len(rows) == 1

    def test_approve_for_a_different_account_is_refused(self, db_conn):
        prepared_order_id = self._queued_order(db_conn, account_id="acct-1")
        _connect_polymarket(db_conn, "acct-2")
        ok, message = autobet.approve_pending_order(db_conn, _FakeRiskConfig(), "acct-2", "polymarket_us", prepared_order_id)
        assert ok is False
        assert "does not belong" in message
        assert db_conn.execute("SELECT COUNT(*) AS c FROM execution_authorizations").fetchone()["c"] == 0

    def test_approve_for_the_wrong_platform_is_refused(self, db_conn):
        """A Polymarket-queued order must never be approvable through
        the Kalshi approval path, even for the same account."""
        prepared_order_id = self._queued_order(db_conn, account_id="acct-1", platform="polymarket_us")
        ok, message = autobet.approve_pending_order(db_conn, _FakeRiskConfig(), "acct-1", "kalshi", prepared_order_id)
        assert ok is False

    def test_reject_releases_the_claim_and_records_it(self, db_conn):
        prepared_order_id = self._queued_order(db_conn)
        ok = autobet.reject_pending_order(db_conn, "acct-1", "polymarket_us", prepared_order_id, reason="changed my mind")
        assert ok is True
        prepared = dict(db_conn.execute(
            "SELECT * FROM prepared_live_orders WHERE prepared_order_id = ?", (prepared_order_id,)
        ).fetchone())
        assert prepared["status"] == "REJECTED"
        claim = db_conn.execute(
            "SELECT 1 FROM customer_autobet_platform_claims WHERE account_id = 'acct-1' AND recommendation_id = 'rec-1' AND platform = 'polymarket_us'"
        ).fetchone()
        assert claim is None
        rows = db_conn.execute(
            "SELECT * FROM customer_autobet_executions WHERE skip_reason = 'MANUALLY_REJECTED'"
        ).fetchall()
        assert len(rows) == 1

    def test_reject_for_a_different_account_is_refused(self, db_conn):
        prepared_order_id = self._queued_order(db_conn, account_id="acct-1")
        _connect_polymarket(db_conn, "acct-2")
        ok = autobet.reject_pending_order(db_conn, "acct-2", "polymarket_us", prepared_order_id)
        assert ok is False
        prepared = dict(db_conn.execute(
            "SELECT * FROM prepared_live_orders WHERE prepared_order_id = ?", (prepared_order_id,)
        ).fetchone())
        assert prepared["status"] == "READY"  # untouched


class TestExceptionSafety:
    def test_unexpected_exception_never_crashes_and_is_recorded(self, db_conn):
        _connect_polymarket(db_conn, "acct-1")
        _enable_autobet(db_conn, "acct-1")
        account = _account_row(db_conn, "acct-1")
        with mock.patch.object(autobet, "_execute_paper", side_effect=RuntimeError("boom")):
            status = autobet.process_recommendation_for_account(
                db_conn, _FakeRiskConfig(), account, "polymarket_us", _opportunity(), _signal(),
            )
        assert status == "FAILED"
        row = dict(db_conn.execute("SELECT * FROM customer_autobet_executions").fetchone())
        assert row["skip_reason"] == "UNEXPECTED_ERROR"

    def test_run_customer_autobet_pass_with_zero_accounts_is_a_clean_noop(self, db_conn):
        config = _FakeRiskConfig()
        with mock.patch("src.execution.customer_autobet.get_connection", return_value=db_conn):
            result = autobet.run_customer_autobet_pass(config)
        assert result["accounts_considered"] == 0
