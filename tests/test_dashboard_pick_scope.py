"""Regression checks for Official-vs-Discovery dashboard scope."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_today_board_requests_official_tier_and_today_scope():
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert 'tier="OFFICIAL_TRACKED", today_only=True' in source


def test_official_tab_filters_discovery_rows():
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "WHERE op.tier = 'OFFICIAL_TRACKED'" in source
