"""Regression check for current-day dashboard metrics."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_today_board_uses_today_performance_scope():
    source = (ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
    assert "compute_performance(conn_tab1, today_only=True)" in source


def test_tracker_supports_today_only_filter():
    source = (ROOT / "src" / "tracker.py").read_text(encoding="utf-8")
    assert "today_only: bool = False" in source
    assert "date(op.selected_at) = date('now')" in source
