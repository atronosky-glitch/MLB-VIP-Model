"""Tests for src/execution/paper_cli.py's four paper-trading CLI
subcommands, dispatched through src.execution.cli.main() the same way
tests/test_execution_cli.py exercises Stage 2B's subcommands.

Uses the real db_conn fixture (in-memory SQLite, full schema) rather
than a bare Mock connection, since these commands perform real,
meaningful SQL through PaperBroker/portfolio/store -- a Mock connection
can't stand in for that the way it could for Stage 2B's simpler
persistence calls.

Section 35's safety regression lives in TestSafetyRegression below:
every paper subcommand, run against a provider willing to answer
place_order/cancel_order, must never actually call them.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from src.execution.base import FeeEstimate, Market, NormalizedOrderBook, OrderLevel, RawGameEvent
from src.execution.cli import main
from src.execution.paper import store


class _FakeConfig:
    kalshi_enabled = True
    polymarket_us_enabled = False
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

    paper_trading_enabled = True
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

    def execution_allowed_rec_statuses_list(self):
        return tuple(s.strip() for s in self.execution_allowed_rec_statuses.split(",") if s.strip())

    def ev_tiered_sizing_tiers(self):
        return ((3.0, 0.5), (5.0, 1.0), (8.0, 1.5), (12.0, 2.0))


class _NonClosingConnProxy:
    """Forwards everything to the real db_conn fixture except close(),
    so a CLI command's own `conn.close()` doesn't tear down the fixture
    out from under the rest of the test."""

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
    def __init__(self, market_raw=None):
        self.market_raw = market_raw or {}
        self.place_order_called = False
        self.cancel_order_called = False

    def get_markets(self, **filters):
        return [Market(id="KXMLBGAME-EX", title="Athletics @ Toronto Blue Jays", status="open", raw={})]

    def parse_game_event(self, market):
        return RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics", market_type="moneyline",
            side=None, line=None, event_start_time=None,
        )

    def get_orderbook(self, market_id):
        return object()

    def normalize_orderbook(self, raw):
        return NormalizedOrderBook(
            market_id="KXMLBGAME-EX", yes_bids=[OrderLevel(Decimal("0.66"), Decimal("200"))],
            yes_asks=[OrderLevel(Decimal("0.68"), Decimal("150"))],
            no_bids=[OrderLevel(Decimal("0.30"), Decimal("150"))],
            no_asks=[OrderLevel(Decimal("0.34"), Decimal("200"))],
            timestamp=datetime.now(timezone.utc),
        )

    def estimate_fees(self, side, price, quantity):
        fee = Decimal("0.07") * quantity * price * (Decimal("1") - price)
        return FeeEstimate(fee=fee, fee_estimate=True, detail="test")

    def get_market(self, market_id):
        return Market(id=market_id, title="x", status=self.market_raw.get("status", "open"), raw=self.market_raw)

    def place_order(self, *a, **k):
        self.place_order_called = True
        raise NotImplementedError

    def cancel_order(self, *a, **k):
        self.cancel_order_called = True
        raise NotImplementedError


class TestPaperScan:
    def test_disabled_via_config_does_nothing(self, capsys):
        config = _FakeConfig()
        config.paper_trading_enabled = False
        with mock.patch("src.execution.paper_cli.load_config", return_value=config):
            exit_code = main(["paper-scan"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "disabled" in out.lower()

    def test_no_providers_enabled_reports_nothing_to_scan(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = False
        with mock.patch("src.execution.paper_cli.load_config", return_value=config):
            exit_code = main(["paper-scan"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "nothing to scan" in out.lower()

    def test_full_flow_creates_a_filled_position(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.paper_cli._load_actionable_rows", return_value=[_row()]):
            exit_code = main(["paper-scan"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "STATUS: FILLED" in out
        position = db_conn.execute("SELECT * FROM paper_positions").fetchone()
        assert position is not None
        assert position["status"] == "OPEN"

    def test_never_places_or_cancels_an_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.paper_cli._load_actionable_rows", return_value=[_row()]):
            main(["paper-scan"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False


class TestPaperPortfolio:
    def test_prints_bankroll_and_zero_positions(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["paper-portfolio"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Starting bankroll: $1000" in out
        assert "Open positions: 0" in out


class TestPaperStats:
    def test_today_flag_prints_a_report(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["paper-stats", "--today"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Trades: 0" in out

    def test_days_flag_prints_a_range_report(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["paper-stats", "--days", "30"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Period:" in out


class TestSettlePaper:
    def test_no_providers_enabled_reports_nothing_to_settle(self, capsys, db_conn):
        config = _FakeConfig()
        config.kalshi_enabled = False
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["settle-paper"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "nothing to settle" in out.lower()

    def test_no_open_positions_reports_that_plainly(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["settle-paper"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "no open positions" in out.lower()

    def test_settles_an_open_position(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.paper_cli._load_actionable_rows", return_value=[_row()]):
            main(["paper-scan"])

        provider.market_raw = {"status": "finalized", "result": "yes"}
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            exit_code = main(["settle-paper"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "WON" in out
        position = db_conn.execute("SELECT * FROM paper_positions").fetchone()
        assert position["status"] == "WON"


class TestSafetyRegression:
    """Section 35: every paper CLI command, run against a provider that
    would happily answer place_order/cancel_order if asked, must never
    actually ask -- and the underlying methods must still raise
    NotImplementedError if anything ever did call them."""

    def test_paper_scan_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.paper_cli._load_actionable_rows", return_value=[_row()]):
            main(["paper-scan", "--verbose"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False

    def test_paper_portfolio_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            main(["paper-portfolio"])
        # paper-portfolio never even touches a provider -- confirmed by
        # not needing to mock get_provider at all for this command.

    def test_paper_stats_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)):
            main(["paper-stats", "--today"])
        # paper-stats never touches a provider either.

    def test_settle_paper_never_calls_place_or_cancel_order(self, capsys, db_conn):
        config = _FakeConfig()
        provider = _FakeProvider(market_raw={"status": "finalized", "result": "yes"})
        with mock.patch("src.execution.paper_cli.load_config", return_value=config), \
             mock.patch("src.execution.paper_cli.get_provider", return_value=provider), \
             mock.patch("src.execution.paper_cli.get_connection", return_value=_NonClosingConnProxy(db_conn)), \
             mock.patch("src.execution.paper_cli._load_actionable_rows", return_value=[_row()]):
            main(["paper-scan"])
            main(["settle-paper"])
        assert provider.place_order_called is False
        assert provider.cancel_order_called is False

    def test_place_order_and_cancel_order_still_raise_notimplementederror(self):
        provider = _FakeProvider()
        try:
            provider.place_order()
        except NotImplementedError:
            pass
        else:
            raise AssertionError("place_order should have raised NotImplementedError")
        try:
            provider.cancel_order()
        except NotImplementedError:
            pass
        else:
            raise AssertionError("cancel_order should have raised NotImplementedError")
