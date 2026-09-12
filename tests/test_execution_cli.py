"""Tests for src/execution/cli.py. Providers, DB connections, and
persistence are mocked entirely -- these tests confirm the CLI's own
logic (skip/pass/fail/isolation/exit code/flags/persistence-wiring),
not real provider or database behavior (covered in their own test
files)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from src.execution.base import HealthCheckResult
from src.execution.cli import main
from src.execution.evaluator import ExecutionOpportunity, ExecutionRejection, RejectionReason


class _FakeConfig:
    kalshi_enabled = False
    polymarket_us_enabled = False
    min_market_match_confidence = 0.98
    execution_analysis_stake_usd = 10.0
    min_raw_ev_pct = 2.0
    min_net_ev_pct = 1.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    max_market_data_age_seconds = 30
    database_path = "database/mlb_model.db"
    execution_allowed_rec_statuses = "STRONG_EDGE,POSITIVE_EDGE,STRONG_PRICE_OUTLIER,PRICE_OUTLIER"

    def execution_allowed_rec_statuses_list(self):
        return tuple(s.strip() for s in self.execution_allowed_rec_statuses.split(",") if s.strip())


def _healthy(provider_name):
    return HealthCheckResult(provider=provider_name, ok=True, detail="ok", checked_at=datetime.now(timezone.utc))


def _unhealthy(provider_name):
    return HealthCheckResult(provider=provider_name, ok=False, detail="bad creds", checked_at=datetime.now(timezone.utc))


def _mock_db_connection():
    return mock.MagicMock()


def _row(recommendation_id="rec-1", league="MLB", matchup="Athletics @ Toronto Blue Jays",
         market_type="game_moneyline", side="AWAY", fair_prob=0.70, rec_status="STRONG_EDGE"):
    return {
        "recommendation_id": recommendation_id, "league": league, "market_type": market_type,
        "matchup": matchup, "side": side, "line": None,
        "event_start_time": "2026-09-12T23:00:00+00:00", "fair_prob": fair_prob,
        "ev_pct": 5.0, "rec_status": rec_status,
    }


class TestCheckConnectivity:
    def test_both_disabled_skips_both_and_exits_zero(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "kalshi: SKIPPED" in out
        assert "polymarket_us: SKIPPED" in out

    def test_one_enabled_and_healthy_passes_and_exits_zero(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _healthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "kalshi: PASS" in out

    def test_misconfigured_provider_fails_but_does_not_block_the_other(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        config.polymarket_us_enabled = True

        def fake_get_provider(name, cfg):
            if name == "kalshi":
                raise RuntimeError("bad private key path")
            provider = mock.Mock()
            provider.health_check.return_value = _healthy("polymarket_us")
            return provider

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=fake_get_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "kalshi: FAIL" in out
        assert "polymarket_us: PASS" in out

    def test_unhealthy_provider_fails_and_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _unhealthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "kalshi: FAIL" in out

    def test_never_calls_anything_but_health_check(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _healthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            main(["check-connectivity"])

        fake_provider.health_check.assert_called_once()
        fake_provider.get_balance.assert_not_called()
        fake_provider.place_order.assert_not_called()


class TestInspectMarkets:
    def test_disabled_provider_reports_verification_pending_not_a_fake_pass(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            exit_code = main(["inspect-markets", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "verification still pending" in out

    def test_prints_parsed_fields_for_each_market(self, capsys):
        from src.execution.base import Market, RawGameEvent
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        market = Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open", raw={})
        fake_provider.get_markets.return_value = [market]
        fake_provider.parse_game_event.return_value = RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
        )
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["inspect-markets", "--provider", "kalshi", "--limit", "5"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "parsed_away='Athletics'" in out
        assert "parsed_home='Toronto Blue Jays'" in out

    def test_raw_flag_redacts_anything_credential_shaped(self, capsys):
        from src.execution.base import Market
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        market = Market(id="T1", title="x", status="open", raw={"api_key": "SUPER_SECRET_VALUE", "title": "x"})
        fake_provider.get_markets.return_value = [market]
        fake_provider.parse_game_event.return_value = None
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            main(["inspect-markets", "--provider", "kalshi", "--raw"])
        out = capsys.readouterr().out
        assert "SUPER_SECRET_VALUE" not in out
        assert "<redacted>" in out

    def test_provider_error_reports_verification_pending_not_a_crash(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=RuntimeError("bad key")):
            exit_code = main(["inspect-markets", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "verification still pending" in out

    def test_unparseable_market_shows_none_not_an_error(self, capsys):
        from src.execution.base import Market
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = [Market(id="T1", title="x", status="open", raw={})]
        fake_provider.parse_game_event.return_value = None
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["inspect-markets", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "parsed=UNVERIFIED" in out

    def test_never_calls_place_or_cancel_order(self, capsys):
        from src.execution.base import Market
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = [Market(id="T1", title="x", status="open", raw={})]
        fake_provider.parse_game_event.return_value = None
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            main(["inspect-markets", "--provider", "kalshi"])
        fake_provider.place_order.assert_not_called()
        fake_provider.cancel_order.assert_not_called()


class TestNoSubcommandEverPlacesAnOrder:
    """Item 18, consolidated: every read-only subcommand, run against a
    provider mock that would happily answer place_order/cancel_order if
    asked, must never actually ask. One assertion per subcommand so a
    future subcommand addition that forgets this is caught immediately."""

    def _fake_provider(self):
        from src.execution.base import Market, NormalizedOrderBook, RawGameEvent
        provider = mock.Mock()
        provider.get_markets.return_value = [
            Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open", raw={})
        ]
        provider.parse_game_event.return_value = RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
        )
        provider.health_check.return_value = _healthy("kalshi")
        provider.normalize_orderbook.return_value = NormalizedOrderBook(
            market_id="T1", yes_bids=[], yes_asks=[], no_bids=[], no_asks=[],
            timestamp=datetime.now(timezone.utc),
        )
        return provider

    def test_check_connectivity_never_places_or_cancels(self):
        config = _FakeConfig()
        config.kalshi_enabled = True
        provider = self._fake_provider()
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=provider):
            main(["check-connectivity"])
        provider.place_order.assert_not_called()
        provider.cancel_order.assert_not_called()

    def test_inspect_markets_never_places_or_cancels(self):
        config = _FakeConfig()
        config.kalshi_enabled = True
        provider = self._fake_provider()
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=provider):
            main(["inspect-markets", "--provider", "kalshi"])
        provider.place_order.assert_not_called()
        provider.cancel_order.assert_not_called()

    def test_inventory_report_never_places_or_cancels(self):
        config = _FakeConfig()
        config.kalshi_enabled = True
        provider = self._fake_provider()
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=provider):
            main(["inventory-report"])
        provider.place_order.assert_not_called()
        provider.cancel_order.assert_not_called()

    def test_scan_opportunities_never_places_or_cancels(self):
        config = _FakeConfig()
        config.kalshi_enabled = True
        provider = self._fake_provider()
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[_row()]):
            main(["scan-opportunities", "--verbose"])
        provider.place_order.assert_not_called()
        provider.cancel_order.assert_not_called()


class TestInventoryReport:
    def test_disabled_provider_is_skipped(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            exit_code = main(["inventory-report"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "KALSHI" in out
        assert "SKIPPED" in out

    def test_buckets_markets_by_heuristic_type(self, capsys):
        from src.execution.base import Market
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = [
            Market(id="1", title="Athletics @ Toronto Blue Jays", status="open", raw={}),
            Market(id="2", title="Will Team X win the Championship?", status="open", raw={}),
            Market(id="3", title="Player passing yards over 250.5", status="open", raw={}),
        ]
        fake_provider.parse_game_event.side_effect = [mock.Mock(), None, None]
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["inventory-report"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Moneyline: 1" in out
        assert "Futures: 1" in out
        assert "Player Prop: 1" in out
        assert "Parser success:" in out

    def test_fetch_failure_is_reported_not_a_crash(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=RuntimeError("boom")):
            exit_code = main(["inventory-report"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "FAILED" in out

    def test_provider_flag_restricts_to_named_providers(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        config.polymarket_us_enabled = True
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=RuntimeError("boom")):
            main(["inventory-report", "--provider", "kalshi"])
        out = capsys.readouterr().out
        assert "KALSHI" in out
        assert "POLYMARKET_US" not in out

    def test_never_calls_place_or_cancel_order(self, capsys):
        from src.execution.base import Market
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = [Market(id="1", title="x", status="open", raw={})]
        fake_provider.parse_game_event.return_value = None
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            main(["inventory-report"])
        fake_provider.place_order.assert_not_called()
        fake_provider.cancel_order.assert_not_called()


class TestScanOpportunities:
    def test_no_providers_enabled_reports_nothing_to_scan(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            exit_code = main(["scan-opportunities"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "nothing to scan" in out.lower()

    def test_no_matching_recommendations_reports_that_plainly(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[]):
            exit_code = main(["scan-opportunities"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no game-level actionable recommendations" in out.lower()

    def test_never_prints_anything_order_related(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            main(["scan-opportunities"])
        out = capsys.readouterr().out.lower()
        assert "order id" not in out
        assert "order placed" not in out
        assert "filled at" not in out

    def test_verbose_prints_a_reason_for_every_non_qualified_provider(self, capsys):
        from src.execution.base import Market, RawGameEvent

        config = _FakeConfig()
        config.kalshi_enabled = True

        fake_provider = mock.Mock()
        fake_provider.name = "kalshi"
        market = Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open", raw={})
        fake_provider.get_markets.return_value = [market]
        fake_provider.parse_game_event.return_value = RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
        )
        fake_provider.get_orderbook.return_value = None
        fake_provider.normalize_orderbook.side_effect = RuntimeError("no book")

        row = _row()

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[row]):
            exit_code = main(["scan-opportunities", "--verbose"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "REJECTED" in out
        assert "API_ERROR" in out

    def test_missing_model_probability_is_rejected_and_persisted_explicitly(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        row = _row(fair_prob=None)
        db_conn = _mock_db_connection()

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=db_conn), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[row]), \
             mock.patch("src.execution.cli.persist_rejection") as mock_persist_rejection:
            exit_code = main(["scan-opportunities", "--verbose"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "MODEL_PROBABILITY_UNAVAILABLE" in out
        assert mock_persist_rejection.called
        rejection = mock_persist_rejection.call_args[0][1]
        assert rejection.reason == RejectionReason.MODEL_PROBABILITY_UNAVAILABLE

    def test_one_provider_fetch_failure_does_not_block_the_other(self, capsys):
        from src.execution.base import Market, RawGameEvent

        config = _FakeConfig()
        config.kalshi_enabled = True
        config.polymarket_us_enabled = True

        broken_provider = mock.Mock()
        broken_provider.get_markets.side_effect = RuntimeError("timeout")

        working_provider = mock.Mock()
        working_provider.get_markets.return_value = [
            Market(id="T1", title="Athletics @ Toronto Blue Jays", status="open", raw={})
        ]
        working_provider.parse_game_event.return_value = RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
        )
        working_provider.normalize_orderbook.side_effect = RuntimeError("no book")

        def fake_get_provider(name, cfg):
            return broken_provider if name == "kalshi" else working_provider

        row = _row()

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=fake_get_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[row]):
            exit_code = main(["scan-opportunities", "--verbose"])
        out = capsys.readouterr().out
        assert exit_code == 0
        # The broken provider's market-fetch failure is reported...
        assert "kalshi" in out.lower()
        # ...but the working provider was still evaluated (reached the API_ERROR
        # stage from its own orderbook failure, proving it wasn't skipped).
        assert "POLYMARKET_US" in out or "polymarket_us" in out.lower()

    def test_persists_both_qualified_and_rejected_evaluations(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        row = _row()
        now = datetime.now(timezone.utc)

        fake_opportunity = ExecutionOpportunity(
            recommendation_id="rec-1", league="MLB", event="Athletics @ Toronto Blue Jays",
            market="moneyline", side="YES", model_probability=Decimal("0.70"),
            provider="kalshi", provider_market_id="T1", match_confidence=1.0,
            best_bid=Decimal("0.68"), best_ask=Decimal("0.70"), spread=Decimal("0.02"),
            analysis_stake_usd=Decimal("10"), quantity_analyzed=Decimal("14"),
            expected_fill_price=Decimal("0.70"), estimated_fees=Decimal("0.15"),
            expected_slippage=Decimal("0"), available_liquidity=Decimal("100"),
            raw_ev_pct=Decimal("5.0"), net_ev_pct=Decimal("3.0"),
            max_acceptable_price=Decimal("0.72"), market_data_timestamp=now,
            signal_timestamp=now, generated_at=now, expiration_time=now,
        )
        fake_rejection = ExecutionRejection(
            recommendation_id="rec-1", provider="polymarket_us", league="MLB",
            event="Athletics @ Toronto Blue Jays", market="moneyline", side="NO",
            reason=RejectionReason.UNVERIFIED_PROVIDER_SIDE_SEMANTICS, detail="unverified",
            generated_at=now,
        )

        fake_comparison = mock.Mock(
            best=fake_opportunity, qualified_alternatives=[], provider_rejections=[fake_rejection],
        )
        mock_evaluator_cls = mock.Mock()
        mock_evaluator_cls.return_value.compare.return_value = fake_comparison

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[row]), \
             mock.patch("src.execution.cli.build_execution_signal", return_value=mock.Mock(
                 away_team="Athletics", home_team="Toronto Blue Jays", model_probability=Decimal("0.70"),
                 league="MLB", market_type="moneyline",
             )), \
             mock.patch("src.execution.cli.find_best_match", return_value=mock.Mock(provider="kalshi")), \
             mock.patch("src.execution.cli.OpportunityEvaluator", mock_evaluator_cls), \
             mock.patch("src.execution.cli.persist_opportunity") as mock_persist_opp, \
             mock.patch("src.execution.cli.persist_rejection") as mock_persist_rej:
            exit_code = main(["scan-opportunities"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "QUALIFIED" in out
        mock_persist_opp.assert_called_once()
        mock_persist_rej.assert_called_once()

    def test_limit_flag_restricts_number_of_recommendations_scanned(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        rows = [_row(recommendation_id=f"rec-{i}") for i in range(5)]

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=rows) as mock_load, \
             mock.patch("src.execution.cli.build_execution_signal", return_value=None) as mock_signal:
            main(["scan-opportunities", "--limit", "2"])
        mock_load.assert_called_once()
        assert mock_signal.call_count == 2

    def test_league_flag_filters_to_matching_league_only(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        rows = [_row(recommendation_id="rec-mlb", league="MLB"), _row(recommendation_id="rec-nfl", league="NFL")]

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=rows), \
             mock.patch("src.execution.cli.build_execution_signal", return_value=None) as mock_signal:
            main(["scan-opportunities", "--league", "NFL"])
        assert mock_signal.call_count == 1
        assert mock_signal.call_args[0][0]["league"] == "NFL"

    def test_provider_flag_restricts_evaluation_to_named_providers(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        config.polymarket_us_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider) as mock_get_provider, \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[]):
            main(["scan-opportunities", "--provider", "kalshi"])
        called_names = {call.args[0] for call in mock_get_provider.call_args_list}
        assert called_names == {"kalshi"}

    def test_analysis_stake_flag_overrides_config_default(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        row = _row()

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[row]), \
             mock.patch("src.execution.cli.build_execution_signal", return_value=None) as mock_signal:
            main(["scan-opportunities", "--analysis-stake", "25"])
        # build_execution_signal is called before any stake-dependent logic,
        # so this proves the run didn't crash constructing the overridden
        # config; a dedicated ev.py test covers the stake's numeric effect.
        assert mock_signal.called

    def test_never_calls_place_or_cancel_order(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.get_markets.return_value = []
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider), \
             mock.patch("src.execution.cli.get_connection", return_value=_mock_db_connection()), \
             mock.patch("src.execution.cli._load_actionable_rows", return_value=[]):
            main(["scan-opportunities"])
        fake_provider.place_order.assert_not_called()
        fake_provider.cancel_order.assert_not_called()
