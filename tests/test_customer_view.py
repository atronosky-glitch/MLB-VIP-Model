"""Static safety checks for the read-only customer view."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_customer_view_is_separate_from_admin_dashboard():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "official_picks" in source
    assert "Research Opportunities" in source
    assert "No Official Picks today" in source
    assert "subprocess" not in source
    assert "SPORTSODDS_API_KEY" not in source


def test_render_defines_customer_service():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "name: mlb-vip-customer" in render
    assert "streamlit run src/customer_view.py" in render
