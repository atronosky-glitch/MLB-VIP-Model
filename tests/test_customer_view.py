"""Static safety checks for the read-only customer view."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_customer_view_is_separate_from_admin_dashboard():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "official_picks" in source
    assert "Full Board" in source
    assert "No Top Picks Yet" in source
    assert "subprocess" not in source
    assert "SPORTSODDS_API_KEY" not in source


def test_full_access_is_open_by_default_and_reversible():
    """2026-09-09 (operator decision): no paywall for now -- everyone gets
    full access by default. Must stay reversible via an env var (no code
    change needed to turn the paywall back on later), and the original
    token-check path must still exist rather than being deleted."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'os.getenv("MLB_CUSTOMER_FREE_ACCESS", "true")' in source
    assert '!= "false":\n        return True' in source
    assert "MLB_CUSTOMER_ACCESS_TOKEN" in source  # original gate still present, just bypassed
    assert "hmac.compare_digest" in source


def test_arbitrage_and_middling_sections_are_subscriber_gated():
    """2026-09-09: live arbitrage/middling opportunities must only be
    shown in full to authorized subscribers -- publicly telegraphing a
    live cross-book price gap invites it being raced/removed immediately
    and undercuts the point of a subscription product."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "get_active_arbitrage_opportunities" in source
    assert "get_active_middle_opportunities" in source
    assert "Subscriber access unlocks live arbitrage opportunities." in source
    assert "Subscriber access unlocks live middling opportunities." in source
    assert "if not authorized:" in source


def _load_freshness_label():
    """Same AST-extraction pattern as _load_apply_filters -- customer_view.py
    runs Streamlit page code at import time, so this avoids importing it."""
    import ast

    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_freshness_label"
    )
    from datetime import datetime, timezone
    namespace = {"datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_freshness_label"]


class TestFreshnessLabel:
    """2026-09-09 (operator request): Arbitrage/Middling opportunities
    must show they're just as live-reconfirmed as EV Picks' own
    freshness — not a static one-time snapshot."""

    def test_just_now(self):
        from datetime import datetime, timezone
        freshness_label = _load_freshness_label()
        assert freshness_label(datetime.now(timezone.utc).isoformat()) == "Fresh (just now)"

    def test_a_few_minutes_ago_is_fresh(self):
        from datetime import datetime, timezone, timedelta
        freshness_label = _load_freshness_label()
        five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert freshness_label(five_min_ago) == "Fresh (5m ago)"

    def test_past_the_15_minute_scan_interval_is_stale(self):
        from datetime import datetime, timezone, timedelta
        freshness_label = _load_freshness_label()
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        assert freshness_label(old).startswith("Stale")

    def test_missing_timestamp_defaults_to_fresh_not_an_error(self):
        freshness_label = _load_freshness_label()
        assert freshness_label(None) == "Fresh"


def test_arbitrage_and_middling_pages_have_a_sportsbook_selector():
    """2026-09-09 (operator feedback): a bright-lime multiselect didn't
    fit the site's dark/gold theme and read as plain tag chips -- swapped
    for the shared popover picker (src/sportsbook_picker.py), which shows
    a tick-box row with a colored badge per book."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "from src.sportsbook_picker import render_sportsbook_picker" in source
    assert 'render_sportsbook_picker(arb_all_books, key_prefix="cust_arb")' in source
    assert 'render_sportsbook_picker(mid_all_books, key_prefix="cust_mid")' in source
    assert "_usable_with_books(data[\"active_arbitrage\"]" in source
    assert "_usable_with_books(data[\"active_middles\"]" in source


def test_arbitrage_and_middle_cards_show_freshness():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'fresh = _freshness_label(opp.get("last_seen_at"))' in source
    assert source.count('fresh = _freshness_label(opp.get("last_seen_at"))') == 2


def test_landing_page_is_three_independent_mode_boxes():
    """2026-09-09 (operator request): the customer site opens on a picker
    -- EV Picks / Arbitrage / Middling -- each its own independent page,
    not everything stacked on one long scroll."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'st.session_state.view_mode = "ev"' in source
    assert 'st.session_state.view_mode = "arbitrage"' in source
    assert 'st.session_state.view_mode = "middling"' in source
    assert 'elif st.session_state.view_mode == "ev":' in source
    assert 'elif st.session_state.view_mode == "arbitrage":' in source
    assert 'elif st.session_state.view_mode == "middling":' in source
    assert "← All Options" in source
    # Each mode keeps its own separate cumulative track record.
    assert '_cumulative_chart(data["graded_arbitrage"], "Arbitrage")' in source
    assert '_cumulative_chart(data["graded_middles"], "Middling")' in source


def test_research_picks_use_configured_timezone_not_utc_day():
    """Regression test (2026-09-06 fix): the customer-facing "Today's
    Research" list must use the Eastern (or whatever MLB_TIMEZONE says)
    calendar day, not a raw UTC date('now') — the earlier version made the
    list silently truncate for several hours every evening once UTC
    rolled to tomorrow's date while it was still evening Eastern time."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "get_today_in_configured_timezone" in source
    # The old buggy comparison must be gone from the research query.
    assert "date(scan_timestamp) = date('now')" not in source


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


def _load_function(name: str):
    """Extract and exec just one top-level function body, without
    importing customer_view.py as a module — that module runs Streamlit
    page code (st.set_page_config, a live DB load) at import time, which
    this file deliberately avoids triggering (see module docstring)."""
    import ast

    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name
    )
    namespace = {}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace[name]


def _load_apply_filters():
    return _load_function("_apply_filters")


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


class TestBookAvailabilityFilter:
    """2026-09-09 (operator request): an arbitrage/middle only counts as
    usable if the viewer actually has accounts at BOTH books it needs."""

    def test_books_in_opportunities_unions_both_legs(self):
        books_in_opportunities = _load_function("_books_in_opportunities")
        opps = [
            {"side_a_sportsbook": "DraftKings", "side_b_sportsbook": "FanDuel"},
            {"side_a_sportsbook": "BetMGM", "side_b_sportsbook": "FanDuel"},
        ]
        result = books_in_opportunities(opps, ("side_a_sportsbook", "side_b_sportsbook"))
        assert result == ["BetMGM", "DraftKings", "FanDuel"]

    def test_usable_with_books_requires_both_legs_available(self):
        usable_with_books = _load_function("_usable_with_books")
        opps = [
            {"id": 1, "side_a_sportsbook": "DraftKings", "side_b_sportsbook": "FanDuel"},
            {"id": 2, "side_a_sportsbook": "DraftKings", "side_b_sportsbook": "BetMGM"},
        ]
        fields = ("side_a_sportsbook", "side_b_sportsbook")
        # Has DraftKings and FanDuel, but not BetMGM -- only opp 1 is usable.
        result = usable_with_books(opps, {"DraftKings", "FanDuel"}, fields)
        assert [o["id"] for o in result] == [1]

    def test_usable_with_books_empty_selection_hides_everything(self):
        usable_with_books = _load_function("_usable_with_books")
        opps = [{"id": 1, "side_a_sportsbook": "DraftKings", "side_b_sportsbook": "FanDuel"}]
        result = usable_with_books(opps, set(), ("side_a_sportsbook", "side_b_sportsbook"))
        assert result == []

    def test_usable_with_books_all_books_selected_keeps_everything(self):
        usable_with_books = _load_function("_usable_with_books")
        opps = [
            {"id": 1, "side_a_sportsbook": "DraftKings", "side_b_sportsbook": "FanDuel"},
            {"id": 2, "over_sportsbook": "BetMGM", "under_sportsbook": "Bovada"},
        ]
        fields = ("side_a_sportsbook", "side_b_sportsbook")
        result = usable_with_books(opps, {"DraftKings", "FanDuel"}, fields)
        assert [o["id"] for o in result] == [1]
