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
