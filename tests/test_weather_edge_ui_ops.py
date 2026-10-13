"""Weather Edge: UI data/filters, Streamlit pages, config, worker/CLI integration and safety invariants."""

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from src.weather_edge import cli, store, ui_data
from src.weather_edge.config import WeatherEdgeConfig
from src.weather_edge.scanner import run_scan
from tests.weather_edge_helpers import NOW, FakeForecasts, FakeKalshi, FakePoly, book, one_event_contract, quote, seed_calibration

PKG = Path("src/weather_edge")
CHEAP_YES = book(yes_bids=[("0.15", 500)], no_bids=[("0.80", 500)])


def seeded(conn, *, with_signal=True):
    seed_calibration(conn, n=80)
    if with_signal:
        c = one_event_contract(quote=quote(yes_asks=[("0.20", 500)], no_asks=[("0.85", 500)], yes_bid="0.15", no_bid="0.80", depth=False))
        bk = CHEAP_YES
    else:     # fairly priced on both sides: no edge anywhere
        c = one_event_contract(quote=quote(yes_asks=[("0.40", 500)], no_asks=[("0.70", 500)], yes_bid="0.30", no_bid="0.60", depth=False))
        bk = book(yes_bids=[("0.30", 500)], no_bids=[("0.60", 500)])
    run_scan(conn, WeatherEdgeConfig(max_quote_age_seconds=300), now=NOW, kalshi=FakeKalshi([c], bk),
             polymarket=FakePoly(), forecasts=FakeForecasts(73.0))
    return conn


class TestUiData:
    def test_empty_database_has_no_scan_and_no_rows(self, db_conn):
        cfg = WeatherEdgeConfig()
        assert ui_data.overview(db_conn, cfg) == {"has_scan": False, "freshness": "never", "age_minutes": None}
        assert ui_data.opportunities(db_conn) == []
        assert ui_data.market_detail(db_conn, "kalshi", "nope") is None

    def test_overview_reports_counts_freshness_and_never_a_fake_average(self, db_conn):
        seeded(db_conn)
        cfg = WeatherEdgeConfig()
        ov = ui_data.overview(db_conn, cfg, now=NOW + timedelta(minutes=10))
        assert ov["has_scan"] and ov["opportunities"] == 1 and ov["freshness"] == "fresh" and ov["avg_net_ev_pct"] > 0
        assert ov["contracts_modeled"] == 1 and ov["calibrated_buckets"] == 1
        stale = ui_data.overview(db_conn, cfg, now=NOW + timedelta(hours=6))
        assert stale["freshness"] == "stale"

    def test_overview_without_signals_has_no_average(self, db_conn):
        seeded(db_conn, with_signal=False)
        ov = ui_data.overview(db_conn, WeatherEdgeConfig(), now=NOW)
        assert ov["opportunities"] == 0 and ov["avg_net_ev_pct"] is None

    def test_opportunity_rows_carry_every_displayed_field(self, db_conn):
        seeded(db_conn)
        r = ui_data.opportunities(db_conn)[0]
        for k in ("platform", "city", "target_date", "metric", "side", "bin_label", "station_label", "p_win", "avg_price",
                  "net_ev_pct", "net_ev", "spread", "liquidity_usd", "confidence", "created_at", "lead_days", "market_implied"):
            assert k in r and r[k] is not None, k
        assert r["city"].startswith("New York") and r["bin_label"] == "72° to 73°"

    def test_filters(self, db_conn):
        seeded(db_conn)
        rows = ui_data.opportunities(db_conn)
        assert ui_data.apply_filters(rows, platforms=["kalshi"]) == rows
        assert ui_data.apply_filters(rows, platforms=["polymarket"]) == []
        assert ui_data.apply_filters(rows, stations=["KLGA"]) == []
        assert ui_data.apply_filters(rows, metrics=["low"]) == []
        assert ui_data.apply_filters(rows, max_lead_days=-1) == []
        assert ui_data.apply_filters(rows, min_net_ev_pct=10_000) == []
        assert ui_data.apply_filters(rows, min_liquidity_usd=10 ** 9) == []
        assert ui_data.apply_filters(rows, min_confidence="high") in (rows, [])

    def test_market_detail_has_rules_forecast_distribution_and_ev_breakdown(self, db_conn):
        seeded(db_conn)
        d = ui_data.market_detail(db_conn, "kalshi", "T-1")
        assert d["prediction"]["p_yes"] > 0 and d["signals"] and d["snapshot"]["n_members"] == 80
        assert set(d["family_means"]) == {"gefs", "ecmwf"} and d["event_distribution"] and d["price_history"]
        assert d["calibration"]["n"] == 80


@pytest.fixture
def ui_db(tmp_path, monkeypatch):
    path = tmp_path / "we.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store.create_schema(conn)
    conn.commit()
    monkeypatch.setenv("WE_TEST_DB", str(path))
    monkeypatch.delenv("WEATHER_EDGE_DISCORD_WEBHOOK_URL", raising=False)
    yield conn
    conn.close()


def _app(editable):
    from streamlit.testing.v1 import AppTest
    src = ("import os, sqlite3\nfrom src.weather_edge.ui import render_weather_edge\n\n"
           "def open_conn():\n    c = sqlite3.connect(os.environ['WE_TEST_DB'])\n    c.row_factory = sqlite3.Row\n    return c\n\n"
           f"render_weather_edge(open_conn, editable={editable!r}, key_prefix='t')\n")
    return AppTest.from_string(src, default_timeout=30)


class TestStreamlitPage:
    def test_empty_state_is_honest(self, ui_db):
        at = _app(False).run()
        assert not at.exception
        text = " ".join(str(x.value) for x in at.info) + " ".join(str(x.value) for x in at.caption)
        assert "No Weather Edge scan has completed" in text and "cannot place orders" in text
        assert not at.metric                         # no cards with made-up zeros

    def test_page_with_data_shows_overview_cards_table_and_tabs(self, ui_db):
        seeded(ui_db)
        ui_db.commit()
        at = _app(False).run()
        assert not at.exception
        labels = {m.label for m in at.metric}
        assert {"Markets scanned", "Valid opportunities", "Avg modeled net EV", "Data freshness"} <= labels
        assert [t.label for t in at.tabs][:5] == ["Opportunities", "Market detail", "Performance", "Watchlist & alerts", "How it works"]
        assert len(at.dataframe) >= 1

    def test_performance_tab_says_no_results_when_there_are_none(self, ui_db):
        seeded(ui_db)
        ui_db.commit()
        at = _app(False).run()
        info = " ".join(str(x.value) for x in at.info)
        assert "No settled paper trades yet" in info and "No predictions have resolved yet" in info and "No backtest has been run" in info

    def test_readonly_watchlist_has_no_edit_controls_but_editable_does(self, ui_db):
        seeded(ui_db)
        ui_db.commit()
        ro = _app(False).run()
        assert not any("Add / update" == b.label for b in ro.button)
        rw = _app(True).run()
        assert any("Add / update" == b.label for b in rw.button)
        add = [b for b in rw.button if b.label == "Add / update"][0]
        add.click()
        rw.run()
        assert not rw.exception and len(store.watchlist(ui_db)) == 1

    def test_stale_scan_shows_a_warning(self, ui_db):
        seeded(ui_db)
        ui_db.execute("UPDATE weather_scan_runs SET finished_at = ?", ((NOW - timedelta(days=2)).isoformat(),))
        ui_db.commit()
        at = _app(False).run()
        assert any("older than expected" in w.value for w in at.warning)


class TestConfig:
    def test_defaults_are_conservative_and_disabled(self, monkeypatch):
        for k in list(__import__("os").environ):
            if k.startswith("WEATHER_EDGE_"):
                monkeypatch.delenv(k)
        c = WeatherEdgeConfig.from_env()
        assert c.enabled is False and c.require_calibration and c.require_robust_ev and c.min_net_ev_pct == 5.0
        assert c.discord_webhook_url == "" and c.scan_interval_minutes >= 5

    def test_environment_overrides_and_bad_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("WEATHER_EDGE_ENABLED", "true")
        monkeypatch.setenv("WEATHER_EDGE_MIN_NET_EV_PCT", "9.5")
        monkeypatch.setenv("WEATHER_EDGE_MAX_SPREAD", "not-a-number")
        monkeypatch.setenv("WEATHER_EDGE_SCAN_INTERVAL_MINUTES", "1")
        c = WeatherEdgeConfig.from_env()
        assert c.enabled and c.min_net_ev_pct == 9.5 and c.max_spread == 0.08 and c.scan_interval_minutes == 5

    def test_webhook_is_read_from_env_only(self, monkeypatch):
        monkeypatch.setenv("WEATHER_EDGE_DISCORD_WEBHOOK_URL", "https://discord.test/x")
        assert WeatherEdgeConfig.from_env().discord_webhook_url == "https://discord.test/x"


class TestWorkerAndCli:
    def test_worker_scan_job_is_a_noop_unless_enabled(self, db_conn, monkeypatch):
        from src import worker
        monkeypatch.delenv("WEATHER_EDGE_ENABLED", raising=False)
        out = worker._execute_job("weather-edge-scan", db_conn, object())
        assert out["status"] == "skipped"
        assert worker._execute_job("weather-edge-settle", db_conn, object())["status"] == "skipped"

    def test_worker_scan_job_runs_when_enabled(self, db_conn, monkeypatch):
        from src import worker
        monkeypatch.setenv("WEATHER_EDGE_ENABLED", "true")

        class S:
            status, scan_id, signals, contracts_modeled = "ok", "abc", 2, 5
        monkeypatch.setattr("src.weather_edge.scanner.run_scan", lambda conn, cfg: S())
        out = worker._execute_job("weather-edge-scan", db_conn, object())
        assert out == {"status": "success", "scan_id": "abc", "signals": 2, "contracts_modeled": 5}

    def test_worker_scan_failure_is_contained(self, db_conn, monkeypatch):
        from src import worker
        monkeypatch.setenv("WEATHER_EDGE_ENABLED", "true")

        def boom(conn, cfg):
            raise RuntimeError("x")
        monkeypatch.setattr("src.weather_edge.scanner.run_scan", boom)
        assert worker._execute_job("weather-edge-scan", db_conn, object())["status"] == "failed"

    def test_scheduler_only_queues_weather_jobs_when_enabled(self):
        src = Path("src/worker.py").read_text(encoding="utf-8")
        i = src.index("_we_cfg = _WeatherEdgeConfig.from_env()")
        assert "if _we_cfg.enabled:" in src[i:i + 120]

    def test_cli_status_runs_without_network(self, db_conn, monkeypatch, capsys):
        # sqlite3.Connection.close cannot be monkeypatched; use a thin proxy that ignores close()
        class P:
            def __getattr__(self, n):
                return getattr(db_conn, n)

            def close(self):
                pass
        monkeypatch.setattr(cli, "_open", lambda: P())
        assert cli.main(["status"]) == 0
        assert "last scan: never" in capsys.readouterr().out

    def test_init_db_creates_every_weather_table_idempotently(self, db_conn):
        names = {r["name"] for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        expected = {"weather_scan_runs", "weather_contracts", "weather_forecast_snapshots", "weather_market_snapshots",
                    "weather_predictions", "weather_signals", "weather_paper_trades", "weather_calibration",
                    "weather_alert_log", "weather_watchlist", "weather_backtests"}
        assert expected <= names

    def test_postgres_ddl_parses_with_postgres_grammar(self):
        pglast = pytest.importorskip("pglast")
        from src.weather_edge.store import SCHEMA_STATEMENTS
        from database.connection import _convert_sql
        for stmt in SCHEMA_STATEMENTS:
            pglast.parse_sql(_convert_sql(stmt, "postgresql"))
            assert "IF NOT EXISTS" in stmt.upper()


class TestSafetyInvariants:
    SOURCES = {p.name: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}

    def test_package_never_issues_non_get_requests_or_places_orders(self):
        for name, text in self.SOURCES.items():
            assert not re.search(r"\.(post|put|delete|patch)\(", text.replace("requests.Session", "")) or name == "alerts.py", name
            assert "place_order" not in text and "submit_order" not in text, name
            assert "private_key" not in text and "KALSHI_PRIVATE" not in text and "api_key_id" not in text, name

    def test_only_documented_environment_variables_are_read(self):
        allowed = {"WEATHER_EDGE_", "WEATHER_EDGE_OPENMETEO_KEY", "WEATHER_EDGE_USER_AGENT"}
        for name, text in self.SOURCES.items():
            for var in re.findall(r"environ(?:\.get)?\(\s*\"([A-Z_]+)\"", text):
                assert var.startswith("WEATHER_EDGE_"), (name, var)

    def test_http_layer_is_get_only(self):
        assert "session.get(" in self.SOURCES["http.py"] and "session.post" not in self.SOURCES["http.py"]

    def test_no_secrets_or_keys_are_hardcoded(self):
        for name, text in self.SOURCES.items():
            assert not re.search(r"(sk-|apikey=|Bearer )[A-Za-z0-9]{10,}", text), name

    def test_feature_is_off_by_default_in_committed_code(self):
        assert WeatherEdgeConfig().enabled is False
