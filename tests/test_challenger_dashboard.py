"""Dashboard integration check for challenger shadow metrics."""

from pathlib import Path


def test_adaptive_dashboard_exposes_shadow_challenger():
    source = (Path(__file__).resolve().parents[1] / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "Independent Strikeout Challenger (Shadow Only)" in source
    assert "evaluate_shadow_from_connection" in source
