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
    assert "1+ hit" in source
    assert "price advantage" in source
    assert 'AND hr.scan_timestamp >= ?' in source
    assert 'logger.exception("Customer data load failed")' in source


def test_render_defines_customer_service():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "name: mlb-vip-customer" in render
    assert "streamlit run src/customer_view.py" in render


def test_customer_view_is_multi_sport():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "NFL" in source
    assert "WNBA" in source
    assert "hr.league" in source
    assert "hr.sport" in source


def test_customer_view_exposes_pick_lifecycle_fields():
    """Upcoming/Past Picks must carry fair odds, confidence, market quality,
    and (for settled picks) closing price/line + CLV — required fields for
    the pick-lifecycle mandate, not just whatever happened to be queried."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "hr.fair_american_odds" in source
    assert "hr.confidence_score" in source
    assert "hr.confidence_grade" in source
    assert "hr.market_quality" in source
    assert "cp.closing_american" in source
    assert "cp.closing_line" in source
    assert "cp.line_movement_direction" in source


def test_customer_view_has_pick_filters():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "def _apply_filters" in source
    assert "def render_pick_filters" in source
    assert '"sports"' in source
    assert '"sportsbooks"' in source
    assert '"markets"' in source
    assert '"confidence_grades"' in source
    assert '"min_ev"' in source


def test_customer_view_has_performance_dashboard_breakdowns():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "from src.grading import performance_summary, breakdown_by_field" in source
    assert "Performance Dashboard" in source
    assert "pct_beating_close" in source
    assert "avg_clv_probability" in source
    assert "breakdown_by_field(filtered_settled" in source


def _load_apply_filters():
    """Extract and exec just the _apply_filters function body, without
    importing customer_view.py as a module — that module runs Streamlit
    page code (st.set_page_config, a live DB load) at import time, which
    this file deliberately avoids triggering (see module docstring)."""
    import ast

    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_apply_filters"
    )
    namespace = {}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_apply_filters"]


def test_apply_filters_pure_function_behavior():
    apply_filters = _load_apply_filters()
    picks = [
        {"league": "MLB", "sportsbook": "DraftKings", "market_type": "pitching_strikeouts_ou",
         "ev_pct": 5.0, "confidence_grade": "A", "scan_timestamp": "2026-08-01T00:00:00Z"},
        {"league": "NFL", "sportsbook": "FanDuel", "market_type": "player_receiving_yards_ou",
         "ev_pct": -2.0, "confidence_grade": "D", "scan_timestamp": "2026-08-02T00:00:00Z"},
    ]

    # Default/empty filters must be a no-op — losing picks are never hidden.
    assert apply_filters(picks, {}) == picks

    assert len(apply_filters(picks, {"sports": ["MLB"]})) == 1
    assert len(apply_filters(picks, {"sportsbooks": ["fanduel"]})) == 1
    assert len(apply_filters(picks, {"markets": ["pitching_strikeouts_ou"]})) == 1
    assert len(apply_filters(picks, {"confidence_grades": ["A"]})) == 1
    assert len(apply_filters(picks, {"min_ev": 0.0})) == 1
    # A losing/negative-EV pick narrowed OUT by an explicit filter is a
    # deliberate user choice, not the page hiding it by default.
    assert apply_filters(picks, {"min_ev": 0.0})[0]["ev_pct"] == 5.0
