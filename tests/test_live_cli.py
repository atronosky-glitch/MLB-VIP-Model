"""Tests for src/execution/live_cli.py's five live-execution CLI
subcommands, dispatched through src.execution.cli.main(). Uses the real
db_conn fixture (in-memory SQLite) since these commands perform
meaningful SQL through approval.py/service.py.

The safety regression here is the most important part: every command,
run against a provider willing to answer place_order/cancel_order,
must never call them -- and live-execute (the one command that CAN
submit) must call _submit_authorized_order at most once, only for a
genuinely approved authorization.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from src.execution.base import (
    Balance, FeeEstimate, LiveSubmissionOutcome, Market, NormalizedOrderBook, OrderLevel, ProviderCapabilities,
    RawGameEvent,
)
from src.execution.cli import main
from src.execution.live import approval, store


class _FakeConfig:
    kalshi_enabled = False
    polymarket_us_enabled = True
    min_market_match_confidence = 0.98
    execution_analysis_stake_usd = 10.0
    min_raw_ev_pct = 2.0
    min_net_ev_pct = 1.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    max_market_data_age_seconds = 30
    database_path = "unused"
    execution_allowed_rec_statuses = "STRONG_EDGE,POSITIVE_EDGE,STRONG_PRICE_OUTLIER,PRICE_OUTLIER"

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

    def execution_allowed_rec_statuses_list(self):
        return tuple(s.strip() for s in self.execution_allowed_rec_statuses.split(",") if s.strip())

    def ev_tiered_sizing_tiers(self):
        return ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))


class _NonClosingConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _row(recommendation_id="rec-1", league="MLB", matchup="Athletics @ Toronto Blue Jays",
         market_type="game_moneyline", side="AWAY", fair_prob=0.72, rec_status="STRONG_EDGE"):
    return {
        "recommendation_id": recommendation_id, "league": league, "market_type": market_type,
        "matchup": matchup, "side": side, "line": None,
        "event_start_time": None, "fair_prob": fair_prob,
        "ev_pct": 6.0, "rec_status": rec_status,
    }


class _FakeProvider:
    capabilities = ProviderCapabilities(False, False, True, True, False, False, False, False)

    def __init__(self, submission_outcome=None):
        self.place_order_called = False
        self.cancel_order_called = False
        self.submit_calls = 0
        self._submission_outcome = submission_outcome or LiveSubmissionOutcome(
            outcome="CONFIRMED", provider_order_id="ORDER-1", detail="filled", raw_reference="HTTP 201",
            quantity_filled=Decimal("14"), average_fill_price=Decimal("0.68"), order_state="FILLED",
        )

    def health_check(self):
        from src.execution.base import HealthCheckResult
        return HealthCheckResult(provider="kalshi", ok=True, detail="ok", checked_at=datetime.now(timezone.utc))

    def get_markets(self, **filters):
        return [Market(id="M1", title="Athletics @ Toronto Blue Jays", status="open", raw={})]

    def parse_game_event(self, market):
        return RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
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

    def place_order(self, *a, **k):
        self.place_order_called = True
        raise NotImplementedError

    def cancel_order(self, *a, **k):
        self.cancel_order_called = True
        raise NotImplementedError

    def _submit_authorized_order(self, authorization, quantity, limit_price):
        self.submit_calls += 1
        return self._submission_outcome


class TestLiveScan:
    def test_no_providers_enabled_reports_nothing_to_scan(self, capsys, db_conn):
        config = _FakeConfig()
        config.polymarket_us_enabled = False
        with mock.patch("src.execution.live_cli.load_config", return_value=config):
            exit_code = main(["live-scan"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "nothing to scan" in out.lower()

    def test_prepares_a_qualified_opportunity(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.live_cli._load_actionable_rows", return_value=[_row()]):
            exit_code = main(["live-scan"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Prepared order id:" in out
        prepared = db_conn.execute("SELECT * FROM prepared_live_orders").fetchone()
        assert prepared is not None
        assert prepared["status"] == "READY"

    def test_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.live_cli._load_actionable_rows", return_value=[_row()]):
            main(["live-scan", "--verbose"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False


class TestLiveApprove:
    def test_approve_with_yes_flag_creates_an_authorization(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            prep = approval.prepare_order(
                db_conn,
                __import__("src.execution.evaluator", fromlist=["ExecutionOpportunity"]).ExecutionOpportunity(
                    recommendation_id="rec-1", league="MLB", event="Athletics @ Toronto Blue Jays",
                    market="moneyline", side="YES", model_probability=Decimal("0.72"),
                    provider="polymarket_us", provider_market_id="M1", match_confidence=1.0,
                    best_bid=Decimal("0.66"), best_ask=Decimal("0.68"), spread=Decimal("0.02"),
                    analysis_stake_usd=Decimal("10"), quantity_analyzed=Decimal("14"),
                    expected_fill_price=Decimal("0.68"), estimated_fees=Decimal("0.23"),
                    expected_slippage=Decimal("0"), available_liquidity=Decimal("100"),
                    raw_ev_pct=Decimal("5.88"), net_ev_pct=Decimal("3.5"),
                    max_acceptable_price=Decimal("0.69"), market_data_timestamp=datetime.now(timezone.utc),
                    signal_timestamp=datetime.now(timezone.utc), generated_at=datetime.now(timezone.utc),
                    expiration_time=datetime.now(timezone.utc),
                ),
                __import__("src.execution.evaluator", fromlist=["ExecutionSignal"]).ExecutionSignal(
                    recommendation_id="rec-1", league="MLB", market_type="moneyline",
                    home_team="Toronto Blue Jays", away_team="Athletics", side="AWAY", line=None,
                    event_start_time=None, model_probability=Decimal("0.72"), sportsbook_ev_pct=6.0,
                    rec_status="STRONG_EDGE", signal_timestamp=datetime.now(timezone.utc),
                ),
                provider, config,
            )
            exit_code = main(["live-approve", str(prep.prepared_order_id), "--yes"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "APPROVED" in out
        assert provider.place_order_called is False


class TestLiveReject:
    def test_reject_marks_the_prepared_order_rejected(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
            provider = _FakeProvider()
            now = datetime.now(timezone.utc)
            prep = approval.prepare_order(
                db_conn,
                ExecutionOpportunity(
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
                ),
                ExecutionSignal(
                    recommendation_id="rec-1", league="MLB", market_type="moneyline",
                    home_team="Toronto Blue Jays", away_team="Athletics", side="AWAY", line=None,
                    event_start_time=None, model_probability=Decimal("0.72"), sportsbook_ev_pct=6.0,
                    rec_status="STRONG_EDGE", signal_timestamp=now,
                ),
                provider, config,
            )
            exit_code = main(["live-reject", str(prep.prepared_order_id), "--reason", "no thanks"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Rejected" in out


class TestLiveExecute:
    def test_no_such_approval_id_reports_error(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["live-execute", "does-not-exist"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "no such approval_id" in out.lower()

    def test_executes_an_approved_authorization_exactly_once(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
        now = datetime.now(timezone.utc)
        prep = approval.prepare_order(
            db_conn,
            ExecutionOpportunity(
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
            ),
            ExecutionSignal(
                recommendation_id="rec-1", league="MLB", market_type="moneyline",
                home_team="Toronto Blue Jays", away_team="Athletics", side="AWAY", line=None,
                event_start_time=None, model_probability=Decimal("0.72"), sportsbook_ev_pct=6.0,
                rec_status="STRONG_EDGE", signal_timestamp=now,
            ),
            provider, config,
        )
        authorization = approval.approve(db_conn, prep.prepared_order_id, config)

        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["live-execute", authorization.approval_id])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "SUBMITTED" in out
        assert provider.submit_calls == 1
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False


class TestLiveStatus:
    def test_prints_status_sections(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["live-status"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "KILL SWITCH" in out
        assert "READY FOR APPROVAL" in out
        assert "OPEN LIVE POSITIONS" in out


class TestSafetyRegression:
    def test_none_of_the_read_or_state_commands_ever_call_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.live_cli._load_actionable_rows", return_value=[_row()]):
            main(["live-scan", "--verbose"])
            main(["live-status"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False


class TestProviderDiagnostics:
    def test_no_credentials_reports_pending_not_a_fake_pass(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = ""
        config.kalshi_private_key_path = ""
        with mock.patch("src.execution.live_cli.load_config", return_value=config):
            exit_code = main(["provider-diagnostics", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Credentials configured...... NO" in out
        assert "PENDING" in out

    def test_never_prints_a_secret_value(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = "SUPER_SECRET_KEY_ID_VALUE"
        config.kalshi_private_key_path = "/path/to/key.pem"
        config.kalshi_enabled = False
        with mock.patch("src.execution.live_cli.load_config", return_value=config):
            main(["provider-diagnostics", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert "SUPER_SECRET_KEY_ID_VALUE" not in out

    def test_with_credentials_but_disabled_reports_pending(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = "some-id"
        config.kalshi_private_key_path = "/path/to/key.pem"
        config.kalshi_enabled = False
        with mock.patch("src.execution.live_cli.load_config", return_value=config):
            exit_code = main(["provider-diagnostics", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "PENDING" in out

    def test_healthy_provider_reports_pass_and_capability_flags(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = "some-id"
        config.kalshi_private_key_path = "/path/to/key.pem"
        config.kalshi_enabled = True
        provider = _FakeProvider()
        provider.capabilities = ProviderCapabilities(False, True, False, False, True, True, True, True)
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider):
            exit_code = main(["provider-diagnostics", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Authentication.............. PASS" in out
        assert "Balance...................... PASS" in out
        assert "Order history................ SUPPORTED" in out
        assert "OVERALL: READ-ONLY CONNECTIVITY OK" in out

    def test_failed_health_check_reports_not_ready(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = "some-id"
        config.kalshi_private_key_path = "/path/to/key.pem"
        config.kalshi_enabled = True

        class UnhealthyProvider(_FakeProvider):
            def health_check(self):
                from src.execution.base import HealthCheckResult
                return HealthCheckResult(provider="kalshi", ok=False, detail="401", checked_at=datetime.now(timezone.utc))

        provider = UnhealthyProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider):
            exit_code = main(["provider-diagnostics", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "OVERALL: NOT READY" in out

    def test_never_calls_place_or_cancel_order(self, capsys):
        config = _FakeConfig()
        config.kalshi_api_key_id = "some-id"
        config.kalshi_private_key_path = "/path/to/key.pem"
        config.kalshi_enabled = True
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider):
            main(["provider-diagnostics", "--provider", "kalshi"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False


class TestLiveReadiness:
    def test_no_credentials_anywhere_reports_pending(self, capsys, db_conn):
        config = _FakeConfig()
        config.kalshi_api_key_id = ""
        config.kalshi_private_key_path = ""
        config.polymarket_us_api_key_id = ""
        config.polymarket_us_private_key_path = ""
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["live-readiness"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "LIVE PROVIDER VERIFICATION PENDING" in out
        assert "LIVE_TRADING_ENABLED=" in out  # honestly reflects config, whatever its value

    def test_reports_polymarket_side_semantics_still_blocked(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            main(["live-readiness"])
        out = capsys.readouterr().out
        assert "BLOCKED (NO-side unverified)" in out

    def test_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.live_cli.load_config", return_value=config), \
             mock.patch("src.execution.live_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.live_cli.get_provider", return_value=provider):
            main(["live-readiness"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False
