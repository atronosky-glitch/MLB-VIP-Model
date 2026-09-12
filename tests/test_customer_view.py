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
    a tick-box row with a colored badge per book.

    2026-09-10 (follow-up operator request): the picker always shows the
    full TRACKED_BOOKMAKERS roster now, not just books in currently live
    opportunities -- a book with nothing live right now is harmless to
    show as an option."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "from src.sportsbook_picker import render_sportsbook_picker" in source
    assert "from src.odds_api_client import TRACKED_BOOKMAKERS" in source
    assert 'render_sportsbook_picker(sorted(TRACKED_BOOKMAKERS.split(",")), key_prefix="cust_arb")' in source
    assert 'render_sportsbook_picker(sorted(TRACKED_BOOKMAKERS.split(",")), key_prefix="cust_mid")' in source
    assert "_usable_with_books(arb_status_filtered" in source
    assert "_usable_with_books(mid_status_filtered" in source


def test_arbitrage_and_middle_cards_show_freshness():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'fresh = _freshness_label(opp.get("last_seen_at"))' in source
    assert source.count('fresh = _freshness_label(opp.get("last_seen_at"))') == 2


def test_arbitrage_and_middle_cards_show_game_start_time():
    """2026-09-09 (operator request): both legs have to be placed before
    kickoff, so each card shows when the game starts."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "from database.db_manager import (" in source
    assert "format_event_start_local" in source
    assert 'game_time = format_event_start_local(opp.get("event_start_time"))' in source
    assert source.count('game_time = format_event_start_local(opp.get("event_start_time"))') == 2
    assert "Game starts: {game_time}" in source


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


def test_cumulative_charts_disable_vega_default_stacking():
    """Regression test for a real production bug (2026-09-12): Vega-Lite
    silently applies default stacking to mark_area's quantitative Y
    channel whenever multiple rows share an x position -- which happens
    constantly here, since many opportunities settle within the same
    day (or, for EV picks, the same scan_timestamp). That summed EVERY
    Cumulative value sharing an x bucket instead of drawing a single
    running total, inflating the Arbitrage Track Record chart's axis
    into the tens/hundreds while the correctly-computed headline number
    stayed accurate -- confirmed live: real cumulative topped out at
    3.27u, but the chart rendered a plateau near 80-90 until stack=None
    was added. Both cumulative charts must opt out of stacking."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert source.count('stack=None') >= 2
    assert 'alt.Y("Cumulative:Q", title="Cumulative units", stack=None,' in source
    assert 'alt.Y("Actual Units:Q", title="Cumulative units", stack=None,' in source
    # The date-truncation that collapsed dozens of distinct settlements
    # onto just 1-2 x positions per day (and made the stacking bug much
    # worse) must not come back either.
    assert 'r["graded_at"][:10]' not in source


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
    usable if the viewer actually has accounts at BOTH books it needs.

    2026-09-10 (follow-up operator request): the picker's own option list
    used to be derived from whichever books happened to be in currently
    LIVE opportunities (_books_in_opportunities, removed) -- switched to
    always showing the full TRACKED_BOOKMAKERS roster instead, since a
    book with zero current opportunities is harmless to show (nothing to
    filter, no effect) and a fixed list is simpler than one that shifts
    membership as opportunities come and go."""

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


def _load_filter_by_game_status():
    """Same AST-extraction as _load_function, but pre-seeds the real
    database.db_manager.is_event_live into the exec namespace since
    _filter_by_game_status calls it -- an empty namespace would raise
    NameError the moment the extracted function actually runs."""
    import ast
    from database.db_manager import is_event_live

    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_filter_by_game_status"
    )
    namespace = {"is_event_live": is_event_live}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_filter_by_game_status"]


class TestGameStatusFilter:
    """2026-09-10 (operator request): a dropdown on the Arbitrage/Middling
    pages to split pregame vs. live opportunities -- the operator's own
    stated preference is pregame only, since live prices move faster and
    can be less reliable."""

    def _opp(self, event_start_time):
        return {"id": event_start_time, "event_start_time": event_start_time}

    def test_pregame_keeps_only_future_start_times(self):
        from datetime import datetime, timedelta, timezone
        filter_by_game_status = _load_filter_by_game_status()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        opps = [self._opp(future), self._opp(past)]
        result = filter_by_game_status(opps, "Pregame")
        assert [o["id"] for o in result] == [future]

    def test_live_keeps_only_past_start_times(self):
        from datetime import datetime, timedelta, timezone
        filter_by_game_status = _load_filter_by_game_status()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        opps = [self._opp(future), self._opp(past)]
        result = filter_by_game_status(opps, "Live")
        assert [o["id"] for o in result] == [past]

    def test_all_is_a_pass_through(self):
        from datetime import datetime, timedelta, timezone
        filter_by_game_status = _load_filter_by_game_status()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        opps = [self._opp(future), self._opp(past)]
        result = filter_by_game_status(opps, "All")
        assert len(result) == 2

    def test_missing_start_time_counts_as_pregame_not_hidden(self):
        """A missing/unknown game time must stay visible in the default
        Pregame view rather than silently vanish because it couldn't be
        classified as live."""
        filter_by_game_status = _load_filter_by_game_status()
        opps = [{"id": "unknown", "event_start_time": None}]
        assert filter_by_game_status(opps, "Pregame") == opps
        assert filter_by_game_status(opps, "Live") == []


def test_arbitrage_and_middling_pages_have_a_game_status_dropdown():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'st.selectbox(\n                "Game status", ["Pregame", "Live", "All"]' in source
    assert source.count('st.selectbox(\n                "Game status", ["Pregame", "Live", "All"]') == 2
    assert '_filter_by_game_status(data["active_arbitrage"], arb_status)' in source
    assert '_filter_by_game_status(data["active_middles"], mid_status)' in source


def test_arbitrage_and_middle_cards_show_a_live_or_pregame_badge():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'status_label = "🔴 LIVE" if is_event_live(opp.get("event_start_time")) else "PREGAME"' in source
    assert source.count('status_label = "🔴 LIVE" if is_event_live(opp.get("event_start_time")) else "PREGAME"') == 2


def test_secondary_buttons_have_a_light_theme_override():
    """Real bug, found live 2026-09-10 from an operator screenshot: the
    "All Options" secondary button was unreadable (dark text, inherited
    from .stApp's own color rule, on the shared dark theme's near-black
    secondary-button background -- dark on dark). Primary buttons already
    had a light-theme override; secondary ones didn't. Confirmed live
    after the fix: the button renders as white with dark, readable text."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert '[data-testid="stBaseButton-secondary"]' in source
    assert "background-color:#ffffff !important" in source


def test_sportsbook_popover_has_a_light_theme_override():
    """Real bug, found live 2026-09-10 checking the "Filter by sportsbook"
    popover after the button fix above: the popover TRIGGER (its own
    testid, stPopoverButton -- not stBaseButton-secondary) and the
    popover's floating BODY (a separate DOM subtree) both still used the
    shared dark theme. Confirmed live after the fix: both render white
    with dark, readable text."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert '[data-testid="stPopoverButton"]' in source
    assert '[data-testid="stPopoverBody"] { background-color:#ffffff !important;' in source


def test_multiselect_filters_have_a_light_theme_override():
    """Real bug, found live 2026-09-10: the Sport/Sportsbook/Market/
    Confidence multiselect filters (inside "Filter upcoming/settled
    picks") had a dark control box and bright-lime selected tags, same
    root cause as everywhere else on this page. Confirmed live after the
    fix: white control, dark tags, readable dropdown list."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert '[data-testid="stMultiSelect"] [data-baseweb="select"] > div' in source
    assert '[data-testid="stMultiSelect"] [data-baseweb="tag"]' in source


def test_radio_and_slider_use_verified_not_guessed_selectors():
    """Real bug, found live 2026-09-10: the ORIGINAL radio/slider CSS
    (written without live verification) used data-baseweb="radio" and
    role="slider" selectors that never matched this Streamlit version at
    all -- the "Performance period" radio's selected dot and the "Minimum
    EV%" slider's thumb were still bright lime, unfixed, despite the CSS
    rules existing. Replaced with selectors confirmed live against the
    actual DOM (stRadioOption/data-selected, and the slider's [role=
    "group"] wrapper). This test guards against silently reintroducing
    the old, non-matching selectors."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'label[data-baseweb="radio"]' not in source
    assert 'div[data-baseweb="slider"] div[role="slider"]' not in source
    assert '[data-testid="stRadioOption"][data-selected="true"]' in source
    assert '[data-testid="stSlider"] [role="group"] > div > div' in source
