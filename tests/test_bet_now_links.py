"""Functional tests for src/customer_view.py's "Bet Now" deep-link
enrichment (_pick_bet_link_key / _attach_bet_links). AST-extracted
(with database.db_manager.get_bet_links pre-seeded) to avoid importing
customer_view.py as a module, which runs Streamlit page code -- a live
DB connection and st.set_page_config -- at import time (same pattern
tests/test_customer_view.py and tests/test_research_market_inventory.py
already use)."""

import ast
from pathlib import Path

from database.db_manager import get_bet_links, save_player_prop_batch
from src.tracker import compute_variable_stake

ROOT = Path(__file__).resolve().parents[1]


def _load_attach_bet_links():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("_pick_bet_link_key", "_attach_bet_links")
    ]
    assert len(nodes) == 2, "expected both _pick_bet_link_key and _attach_bet_links"
    namespace = {"get_bet_links": get_bet_links}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_attach_bet_links"]


def _load_suggested_stake_units():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_suggested_stake_units"
    )
    namespace = {"compute_variable_stake": compute_variable_stake}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_suggested_stake_units"]


def _load_bet_link_resolvers():
    """AST-extracts _UNRESOLVED_PLACEHOLDER_RE, _resolve_bet_link, and
    _bet_now_html together (_bet_now_html calls _resolve_bet_link, which
    uses the module-level regex)."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in ("_resolve_bet_link", "_bet_now_html"):
            nodes.append(n)
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_UNRESOLVED_PLACEHOLDER_RE" for t in n.targets
        ):
            nodes.append(n)
    assert len(nodes) == 3, "expected the regex constant plus both functions"
    import re
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_resolve_bet_link"], namespace["_bet_now_html"]


def _prop_row(**overrides):
    row = {
        "event_id": "E1", "odd_id": "o1", "sportsbook": "draftkings",
        "player_id": "p1", "player_name": "Aaron Judge", "team_id": "", "team_name": "",
        "market_type": "batting_totalBases_ou", "market_group_key": "E1|p1|1.5|0|OVER",
        "side": "OVER", "line": 1.5, "price": -110, "decimal_odds": 1.909,
        "is_alt_line": 0, "available": 1, "validation_status": "VALID",
        "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "OK",
        "captured_at": "2026-09-15T12:00:00+00:00",
    }
    row.update(overrides)
    return row


def _pick(**overrides):
    base = {
        "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou",
        "line": 1.5, "side": "OVER", "sportsbook": "draftkings",
    }
    base.update(overrides)
    return base


class TestPickBetLinkKey:
    def test_single_leg_pick_gets_its_link(self, db_conn):
        save_player_prop_batch(db_conn, [_prop_row(bet_link="https://dk-link")])
        attach = _load_attach_bet_links()
        upcoming = [_pick()]
        attach(db_conn, upcoming, [], [], [])
        assert upcoming[0]["bet_link"] == "https://dk-link"

    def test_missing_event_id_or_player_id_leaves_link_none(self, db_conn):
        save_player_prop_batch(db_conn, [_prop_row(bet_link="https://dk-link")])
        attach = _load_attach_bet_links()
        upcoming = [_pick(event_id=None)]
        attach(db_conn, upcoming, [], [], [])
        assert upcoming[0]["bet_link"] is None

    def test_no_matching_odds_row_leaves_link_none_not_a_crash(self, db_conn):
        attach = _load_attach_bet_links()
        upcoming = [_pick()]
        attach(db_conn, upcoming, [], [], [])
        assert upcoming[0]["bet_link"] is None


class TestArbitrageTwoLegs:
    def test_both_legs_get_their_own_link(self, db_conn):
        save_player_prop_batch(db_conn, [
            _prop_row(odd_id="o1", sportsbook="draftkings", side="OVER", bet_link="https://dk-link"),
            _prop_row(odd_id="o2", sportsbook="fanduel", side="UNDER", bet_link="https://fd-link"),
        ])
        attach = _load_attach_bet_links()
        arb = [{
            "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou", "line": 1.5,
            "side_a": "OVER", "side_a_sportsbook": "draftkings",
            "side_b": "UNDER", "side_b_sportsbook": "fanduel",
        }]
        attach(db_conn, [], [], arb, [])
        assert arb[0]["bet_link_a"] == "https://dk-link"
        assert arb[0]["bet_link_b"] == "https://fd-link"

    def test_one_leg_missing_link_only_that_leg_is_none(self, db_conn):
        save_player_prop_batch(db_conn, [
            _prop_row(odd_id="o1", sportsbook="draftkings", side="OVER", bet_link="https://dk-link"),
        ])
        attach = _load_attach_bet_links()
        arb = [{
            "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou", "line": 1.5,
            "side_a": "OVER", "side_a_sportsbook": "draftkings",
            "side_b": "UNDER", "side_b_sportsbook": "fanduel",
        }]
        attach(db_conn, [], [], arb, [])
        assert arb[0]["bet_link_a"] == "https://dk-link"
        assert arb[0]["bet_link_b"] is None


class TestMiddleTwoLegs:
    def test_over_and_under_legs_get_their_own_link(self, db_conn):
        save_player_prop_batch(db_conn, [
            _prop_row(odd_id="o1", sportsbook="draftkings", side="OVER", line=1.5, bet_link="https://over-link"),
            _prop_row(odd_id="o2", sportsbook="fanduel", side="UNDER", line=2.5, bet_link="https://under-link"),
        ])
        attach = _load_attach_bet_links()
        mid = [{
            "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou",
            "over_line": 1.5, "over_sportsbook": "draftkings",
            "under_line": 2.5, "under_sportsbook": "fanduel",
        }]
        attach(db_conn, [], [], [], mid)
        assert mid[0]["bet_link_over"] == "https://over-link"
        assert mid[0]["bet_link_under"] == "https://under-link"


def test_no_opportunities_at_all_does_not_call_get_bet_links_with_bad_input(db_conn):
    attach = _load_attach_bet_links()
    # Must not raise even with every list empty.
    attach(db_conn, [], [], [], [])


class TestSuggestedStakeUnits:
    """Real finding, live-verified 2026-09-15: risk_units is only ever
    populated by src/automatic_grading.py AFTER a pick settles -- always
    None for an upcoming/research pick. Confirmed live in a running
    instance of the app: before this fallback existed, every upcoming
    pick's "Suggested stake" was blank, making the whole $/unit feature
    a no-op. Falls back to src/tracker.py's own compute_variable_stake
    (the same formula already used for grading) computed prospectively
    from fields already on the pick at recommendation time."""

    def test_settled_risk_units_used_directly_when_present(self):
        suggested = _load_suggested_stake_units()
        assert suggested({"risk_units": 1.25, "ev_pct": 999, "offered_decimal_odds": 1, "model_score": 1}) == 1.25

    def test_falls_back_to_compute_variable_stake_when_risk_units_is_none(self):
        suggested = _load_suggested_stake_units()
        pick = {"risk_units": None, "ev_pct": 5.8, "offered_decimal_odds": 1.909, "model_score": 8.4}
        expected = compute_variable_stake(5.8, 1.909, 8.4)
        assert suggested(pick) == expected
        assert expected > 0  # a real, non-trivial recommendation, not just the 0.5 floor

    def test_missing_fields_still_returns_the_0point5_floor_not_none(self):
        """compute_variable_stake's own documented behavior (never None)
        -- confirmed this doesn't regress through the wrapper."""
        suggested = _load_suggested_stake_units()
        assert suggested({"risk_units": None, "ev_pct": None, "offered_decimal_odds": None, "model_score": None}) == 0.5


class TestResolveBetLink:
    """Real, live-confirmed 2026-09-15: The Odds API returns per-outcome
    bet-slip deep links; some carry real template placeholders. A link
    with any UNRESOLVED placeholder after best-effort substitution must
    never be returned as usable -- confirmed the browser tool refuses to
    navigate to a real regulated sportsbook URL (compliance restriction),
    so {pickType} (BetRivers) could not be verified and must never be
    guessed."""

    def test_no_link_returns_none(self):
        resolve, _ = _load_bet_link_resolvers()
        assert resolve(None, "PA", 10.0) == (None, None)

    def test_plain_link_passes_through_unchanged(self):
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve("https://sportsbook.draftkings.com/?outcomes=abc123", None, None)
        assert url == "https://sportsbook.draftkings.com/?outcomes=abc123"
        assert note is None

    def test_state_placeholder_needs_state_set(self):
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve("https://sports.{state}.betmgm.com/en/sports?options=1", None, None)
        assert url is None
        assert "state" in note.lower()

    def test_state_placeholder_substituted_when_state_set(self):
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve("https://sports.{state}.betmgm.com/en/sports?options=1", "PA", None)
        assert url == "https://sports.pa.betmgm.com/en/sports?options=1"
        assert note is None

    def test_wager_amount_substituted_when_stake_known(self):
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve("https://x.com/coupon=Single|123|{wagerAmount}", None, 12.5)
        assert url == "https://x.com/coupon=Single|123|12.50"

    def test_unresolved_pick_type_blocks_the_link_entirely(self):
        """The real BetRivers shape: {state} and {wagerAmount} can both
        be safely resolved, but {pickType} cannot -- the whole link must
        come back unusable rather than half-substituted and broken."""
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve(
            "https://{state}.betrivers.com/?page=sportsbook#event/1/coupon={pickType}|123|{wagerAmount}",
            "PA", 10.0,
        )
        assert url is None
        assert "unavailable" in note.lower()

    def test_wager_amount_placeholder_with_no_stake_stays_unresolved_and_blocks(self):
        resolve, _ = _load_bet_link_resolvers()
        url, note = resolve("https://x.com/coupon=Single|123|{wagerAmount}", None, None)
        assert url is None


class TestBetNowHtml:
    def test_no_link_no_stake_renders_nothing(self):
        _, bet_now_html = _load_bet_link_resolvers()
        assert bet_now_html("DraftKings", None, None, None, None) == ""

    def test_link_present_renders_a_button_with_the_label(self):
        _, bet_now_html = _load_bet_link_resolvers()
        html = bet_now_html("DraftKings", "https://sportsbook.draftkings.com/?outcomes=abc", None, None, None)
        assert "BET NOW" in html
        assert "DraftKings" in html
        assert 'href="https://sportsbook.draftkings.com/?outcomes=abc"' in html
        assert 'target="_blank"' in html

    def test_suggested_stake_shown_in_dollars_when_unit_usd_set(self):
        _, bet_now_html = _load_bet_link_resolvers()
        html = bet_now_html("DraftKings", "https://dk", None, 1.25, 10.0)
        assert "$12.50" in html

    def test_suggested_stake_shown_in_units_when_unit_usd_not_set(self):
        _, bet_now_html = _load_bet_link_resolvers()
        html = bet_now_html("DraftKings", "https://dk", None, 1.25, None)
        assert "1.25u" in html
        assert "set $/unit above" in html

    def test_no_link_but_stake_known_still_shows_suggested_stake_text(self):
        _, bet_now_html = _load_bet_link_resolvers()
        html = bet_now_html("DraftKings", None, None, 1.25, 10.0)
        assert "$12.50" in html
        assert "BET NOW" not in html
