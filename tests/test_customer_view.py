"""Static safety checks for the read-only customer view."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_customer_view_is_separate_from_admin_dashboard():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "official_picks" in source
    assert "Research Opportunities" in source
    assert "No Official Plays Yet" in source
    assert "subprocess" not in source
    assert "SPORTSODDS_API_KEY" not in source


def test_expected_actual_series_uses_recorded_values():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'frame["expected_units"] = frame["risk_units"] * frame["ev_pct"] / 100.0' in source
    assert 'frame["actual_cumulative"] = frame["profit_units"].cumsum()' in source
    assert "hr.event_start_time >= ?" in source
    assert "hr.event_start_time <= ?" in source
    assert "settlement_status IN ('UNRESOLVED','ungraded')" in source
    assert "Expected Units" in source
    assert "Actual Units" in source
    assert "Stake:" in source
    assert "Result:" in source


def test_render_defines_customer_service():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "name: mlb-vip-customer" in render
    assert "streamlit run src/customer_view.py" in render
