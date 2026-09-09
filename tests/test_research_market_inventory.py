"""Regression checks for registry-complete Research market filtering."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_research_filter_uses_market_registry_not_only_saved_rows():
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "MARKET_REGISTRY" in source
    assert "Registry markets remain selectable" in source
    assert "selected_types = market_options[sel_market]" in source
    assert "Raw approved coverage" in source
    assert "No approved raw rows were recorded" in source
    assert "paired O/U groups" in source


def test_market_intelligence_tab_distinguishes_no_data_from_no_bet_qualified():
    """Regression check (2026-09-06): the always-visible Market Intelligence
    table must label every market's Status using the shared, tested
    classification (src.qualification_funnel.classify_market_data_status),
    and must include registry markets with zero raw rows today — which
    previously just didn't appear in the table at all, indistinguishable
    from a market with data but nothing that qualified."""
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "from src.qualification_funnel import (" in source
    assert "classify_market_data_status" in source
    assert "MARKET_STATUS_LABELS" in source
    assert "NO_DATA_FROM_PROVIDER" in source
    assert '"Status": MARKET_STATUS_LABELS[status]' in source
    assert "Registry markets with ZERO raw rows today never appear" in source


def test_line_movement_tab_shows_a_clv_leaderboard():
    """The Line Movement tab must show a Best CLV all-time top-5 table and
    today's picks' own CLV, both reading from the canonical closing_prices
    table (the one src/automatic_grading.py's _capture_final_closing_price
    fix, 2026-09-06, actually populates)."""
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "Best Closing Line Value" in source
    assert "Today's Picks — CLV" in source
    assert "FROM closing_prices cp" in source
    assert "ORDER BY cp.clv_probability DESC" in source
    assert "LIMIT 5" in source


def test_admin_dashboard_has_arbitrage_and_middling_tabs():
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert ":material/balance: Arbitrage" in source
    assert ":material/compress: Middling" in source
    assert "get_active_arbitrage_opportunities" in source
    assert "get_graded_arbitrage_opportunities" in source
    assert "get_active_middle_opportunities" in source
    assert "get_graded_middle_opportunities" in source


def test_arbitrage_and_middling_tables_show_freshness():
    """2026-09-09 (operator request): live opportunities must read as
    just as reconfirmed/live as EV Picks' own freshness indicator."""
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert '"Freshness": _opportunity_freshness(r.get("last_seen_at"))' in source
    assert source.count('"Freshness": _opportunity_freshness(r.get("last_seen_at"))') == 2


def _load_opportunity_freshness():
    """AST-extracts just _opportunity_freshness, avoiding importing
    control_panel.py as a module (it runs Streamlit page code — a live
    DB connection and st.set_page_config -- at import time)."""
    import ast
    from datetime import datetime, timezone

    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_opportunity_freshness"
    )
    namespace = {"datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_opportunity_freshness"]


class TestOpportunityFreshness:
    def test_just_now(self):
        from datetime import datetime, timezone
        opportunity_freshness = _load_opportunity_freshness()
        assert opportunity_freshness(datetime.now(timezone.utc).isoformat()) == "Fresh (just now)"

    def test_a_few_minutes_ago_is_fresh(self):
        from datetime import datetime, timezone, timedelta
        opportunity_freshness = _load_opportunity_freshness()
        five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert opportunity_freshness(five_min_ago) == "Fresh (5m ago)"

    def test_past_the_15_minute_scan_interval_is_stale(self):
        from datetime import datetime, timezone, timedelta
        opportunity_freshness = _load_opportunity_freshness()
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        assert opportunity_freshness(old).startswith("Stale")

    def test_missing_timestamp_is_no_data_not_an_error(self):
        opportunity_freshness = _load_opportunity_freshness()
        assert opportunity_freshness(None) == "No data"
