"""Spread lines must always show an explicit sign in rendered output.

Found live 2026-09-27: a spread pick rendered as "Away 6.5" -- the actual
signed line (favorite negative, underdog positive, exactly what
src/game_settlement.py::grade_spread() already grades against) was stored
correctly in historical_recommendations.raw_line the whole time; it was only
ever LOST during formatting, which used the unsigned "line" magnitude
instead. Three independent formatters had the bug: src/customer_view.py's
_side_line_label, src/message_formatter.py's format_recommendation, and
src/control_panel.py's _format_pick_side_line (which was actually worse --
it applied "{:+g}" to the unsigned value, so it always showed a misleading
"+" instead of just missing the sign). All three now share one pure helper,
src/spread_formatting.py::signed_spread_line_text.

NOTE: src/customer_view.py itself is never imported here -- importing it
outside a Streamlit runtime executes the whole page and leaks st.form/
st.expander contexts into later AppTests (see
tests/test_customer_view_auth.py::test_no_test_imports_the_customer_page_module_directly).
Its _side_line_label is covered by a static source check in
tests/test_customer_view.py instead; this file exercises the shared pure
helper directly plus the two formatters that ARE safe to import
(control_panel, message_formatter -- an established pattern in this repo).
"""

import re

import pytest

from src.control_panel import _format_pick_side_line
from src.message_formatter import format_recommendation
from src.spread_formatting import SIGNED_SPREAD_MARKETS, signed_spread_line_text

SPREAD_MARKET = "game_spread_ou"


def _rec(side, line, raw_line, market_type=SPREAD_MARKET, **over):
    base = {"side": side, "line": line, "raw_line": raw_line, "market_type": market_type,
            "player_name": "Game Spread", "matchup": "New York Liberty @ Minnesota Lynx",
            "sportsbook": "draftkings", "offered_american_odds": -110}
    base.update(over)
    return base


class TestSharedHelper:
    @pytest.mark.parametrize("side,raw_line,expected", [
        ("AWAY", 6.5, "+6.5"), ("AWAY", -6.5, "-6.5"),
        ("HOME", 3.5, "+3.5"), ("HOME", -3.5, "-3.5"),
    ])
    def test_signed_text(self, side, raw_line, expected):
        assert signed_spread_line_text(_rec(side, line=abs(raw_line), raw_line=raw_line)) == expected

    def test_half_point_precision_preserved(self):
        assert signed_spread_line_text(_rec("AWAY", line=7.5, raw_line=-7.5)) == "-7.5"
        assert signed_spread_line_text(_rec("HOME", line=0.5, raw_line=0.5)) == "+0.5"

    def test_run_line_and_period_spreads_are_in_scope(self):
        for mt in ("game_runline_ou", "game_spread_1q_ou", "game_spread_1h_ou"):
            assert mt in SIGNED_SPREAD_MARKETS
            assert signed_spread_line_text(_rec("AWAY", line=1.5, raw_line=-1.5, market_type=mt)) == "-1.5"

    def test_missing_raw_line_never_guesses(self):
        assert signed_spread_line_text(_rec("AWAY", line=6.5, raw_line=None)) is None

    def test_totals_moneyline_and_player_props_are_out_of_scope(self):
        assert signed_spread_line_text({"market_type": "game_total_ou", "raw_line": 8.5}) is None
        assert signed_spread_line_text({"market_type": "game_moneyline", "raw_line": None}) is None
        assert signed_spread_line_text({"market_type": "batting_totalBases_ou", "raw_line": 4.5}) is None


class TestControlPanelSpreadSign:
    @pytest.mark.parametrize("side,raw_line,team,expected", [
        ("AWAY", 6.5, "New York Liberty", "New York Liberty +6.5"),
        ("AWAY", -6.5, "New York Liberty", "New York Liberty -6.5"),
        ("HOME", 3.5, "Minnesota Lynx", "Minnesota Lynx +3.5"),
        ("HOME", -3.5, "Minnesota Lynx", "Minnesota Lynx -3.5"),
    ])
    def test_signed_spread_resolved_to_team_name(self, side, raw_line, team, expected):
        rec = _rec(side, line=abs(raw_line), raw_line=raw_line)
        assert _format_pick_side_line(rec) == expected

    def test_never_shows_a_wrong_plus_for_a_favorite(self):
        # The old bug: {line:+g} on the UNSIGNED magnitude always printed "+",
        # even for a favorite that should show "-".
        rec = _rec("HOME", line=3.5, raw_line=-3.5)
        result = _format_pick_side_line(rec)
        assert result.endswith("-3.5"), result
        assert "+3.5" not in result

    def test_missing_raw_line_falls_back_never_guesses(self):
        rec = _rec("AWAY", line=6.5, raw_line=None)
        assert _format_pick_side_line(rec) == "New York Liberty +6.5"  # unsigned fallback, unchanged from before

    def test_totals_and_player_props_unaffected(self):
        assert _format_pick_side_line(
            {"side": "OVER", "line": 8.5, "raw_line": 8.5, "market_type": "game_total_ou", "matchup": "A @ B"}
        ) == "Over 8.5"
        assert _format_pick_side_line(
            {"side": "OVER", "line": 4.5, "raw_line": 4.5, "market_type": "batting_totalBases_ou"}
        ) == "Over 4.5"


class TestDiscordSpreadSign:
    @pytest.mark.parametrize("side,raw_line,expected_fragment", [
        ("AWAY", 6.5, "Line: +6.5 (AWAY)"),
        ("AWAY", -6.5, "Line: -6.5 (AWAY)"),
        ("HOME", 3.5, "Line: +3.5 (HOME)"),
        ("HOME", -3.5, "Line: -3.5 (HOME)"),
    ])
    def test_signed_spread_line_in_discord_message(self, side, raw_line, expected_fragment):
        text = format_recommendation(_rec(side, line=abs(raw_line), raw_line=raw_line))
        assert expected_fragment in text

    def test_missing_raw_line_falls_back_to_unsigned(self):
        text = format_recommendation(_rec("AWAY", line=6.5, raw_line=None))
        assert "Line: 6.5 (AWAY)" in text

    def test_totals_and_player_props_unaffected(self):
        total = format_recommendation(
            {"side": "OVER", "line": 8.5, "raw_line": 8.5, "market_type": "game_total_ou",
             "player_name": "Game Total", "matchup": "A @ B", "sportsbook": "dk", "offered_american_odds": -110})
        assert "Line: 8.5 (OVER)" in total
        prop = format_recommendation(
            {"side": "OVER", "line": 4.5, "raw_line": 4.5, "market_type": "batting_totalBases_ou",
             "player_name": "Test Player", "matchup": "A @ B", "sportsbook": "dk", "offered_american_odds": -110})
        assert "Line: 4.5 (OVER)" in prop


class TestNoUnsignedSpreadAnywhere:
    """A regression sweep: for every signed-spread fixture, none of the
    importable formatters' output ever contains a bare, unsigned spread
    number. (customer_view._side_line_label is covered separately by a
    static source check in tests/test_customer_view.py.)"""

    CASES = [("AWAY", 6.5), ("AWAY", -6.5), ("HOME", 3.5), ("HOME", -3.5), ("AWAY", 0.5), ("HOME", -12.5)]

    @pytest.mark.parametrize("side,raw_line", CASES)
    def test_no_bare_unsigned_line_in_any_formatter(self, side, raw_line):
        rec = _rec(side, line=abs(raw_line), raw_line=raw_line)
        outputs = [_format_pick_side_line(rec), format_recommendation(rec)]
        magnitude = f"{abs(raw_line):g}"
        for text in outputs:
            # every occurrence of the bare magnitude must be immediately
            # preceded by '+' or '-' -- never a naked number.
            for m in re.finditer(re.escape(magnitude), text):
                assert m.start() > 0 and text[m.start() - 1] in "+-", (text, magnitude)
