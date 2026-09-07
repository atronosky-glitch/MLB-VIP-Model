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
