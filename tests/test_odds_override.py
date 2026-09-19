"""Tests for src/customer_view.py's "Got a different price?" odds-override
input -- lets a customer plug in a price they were actually quoted at
another book (e.g. a better number on Novig) and see EV%/suggested stake
recomputed for it live, without a new scan.

AST-extracted (pure logic functions only) to avoid importing
customer_view.py as a module, which runs Streamlit page code -- a live DB
connection and st.set_page_config -- at import time (same pattern
tests/test_bet_now_links.py and tests/test_customer_view.py already use).
The Streamlit-widget-rendering function (_render_odds_override) is covered
by static source-check assertions instead, matching this file's existing
convention for UI-touching code.
"""

import ast
from pathlib import Path

from src.market_analysis import american_to_decimal
from src.tracker import compute_variable_stake

ROOT = Path(__file__).resolve().parents[1]


def _load_pure_helpers():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"_odds_override_key", "_recompute_stake_for_odds", "_parse_american_odds"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == 3, "expected all three pure odds-override helpers"
    namespace = {"american_to_decimal": american_to_decimal, "compute_variable_stake": compute_variable_stake}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_odds_override_key"], namespace["_recompute_stake_for_odds"], namespace["_parse_american_odds"]


def _pick(**overrides):
    row = {
        "event_id": "E1", "player_id": "p1", "market_type": "batting_totalBases_ou",
        "line": 1.5, "side": "OVER", "sportsbook": "draftkings", "scan_timestamp": "2026-09-17T00:00:00Z",
        "ev_pct": 10.0, "offered_decimal_odds": 2.0, "offered_american_odds": 100,
        "model_score": None,
    }
    row.update(overrides)
    return row


class TestParseAmericanOdds:
    def test_positive_with_plus(self):
        _, _, parse = _load_pure_helpers()
        assert parse("+150") == 150

    def test_positive_without_plus(self):
        _, _, parse = _load_pure_helpers()
        assert parse("150") == 150

    def test_negative(self):
        _, _, parse = _load_pure_helpers()
        assert parse("-120") == -120

    def test_empty_string_is_none(self):
        _, _, parse = _load_pure_helpers()
        assert parse("") is None
        assert parse("   ") is None

    def test_non_numeric_is_none(self):
        _, _, parse = _load_pure_helpers()
        assert parse("abc") is None

    def test_below_real_odds_range_is_none(self):
        """Real American odds are never inside (-100, 100) -- e.g. "50"
        or "-50" is a typo, not a real price, and must not be silently
        treated as valid."""
        _, _, parse = _load_pure_helpers()
        assert parse("50") is None
        assert parse("-50") is None
        assert parse("0") is None

    def test_boundary_values_are_valid(self):
        _, _, parse = _load_pure_helpers()
        assert parse("100") == 100
        assert parse("-100") == -100

    def test_strips_commas_and_whitespace(self):
        _, _, parse = _load_pure_helpers()
        assert parse("  +1,500  ") == 1500


class TestOddsOverrideKey:
    def test_stable_for_same_pick(self):
        key_fn, _, _ = _load_pure_helpers()
        assert key_fn(_pick()) == key_fn(_pick())

    def test_differs_by_sportsbook(self):
        key_fn, _, _ = _load_pure_helpers()
        assert key_fn(_pick(sportsbook="draftkings")) != key_fn(_pick(sportsbook="fanduel"))

    def test_differs_by_side_and_line(self):
        key_fn, _, _ = _load_pure_helpers()
        assert key_fn(_pick(side="OVER", line=1.5)) != key_fn(_pick(side="UNDER", line=1.5))
        assert key_fn(_pick(line=1.5)) != key_fn(_pick(line=2.5))


class TestRecomputeStakeForOdds:
    def test_worked_example(self):
        """ev_pct=10.0 at decimal 2.0 implies true_probability = 0.55.
        At a better price (+150 -> decimal 2.5), new EV = 0.55*2.5-1 = 37.5%.
        compute_variable_stake(37.5, 2.5, None): b=1.5, kelly=0.25/1.5,
        fractional=0.25*that, raw_units=6.25 -> clamped to 2.0."""
        _, recompute, _ = _load_pure_helpers()
        result = recompute(_pick(), 150)
        assert result is not None
        assert abs(result["ev_pct"] - 37.5) < 1e-9
        assert abs(result["decimal_odds"] - 2.5) < 1e-9
        assert result["stake_units"] == 2.0

    def test_worse_price_lowers_ev_and_stake(self):
        _, recompute, _ = _load_pure_helpers()
        baseline = recompute(_pick(), 100)  # same as the original price
        worse = recompute(_pick(), -150)
        assert worse["ev_pct"] < baseline["ev_pct"]

    def test_missing_ev_pct_returns_none(self):
        _, recompute, _ = _load_pure_helpers()
        assert recompute(_pick(ev_pct=None), 150) is None

    def test_missing_offered_decimal_odds_returns_none(self):
        _, recompute, _ = _load_pure_helpers()
        assert recompute(_pick(offered_decimal_odds=None), 150) is None
        assert recompute(_pick(offered_decimal_odds=0), 150) is None
        assert recompute(_pick(offered_decimal_odds=1.0), 150) is None

    def test_degenerate_new_price_returns_none(self):
        """american_to_decimal(0) is exactly 1.0 -- a break-even, non-real
        price that must never be reapplied as if it were valid."""
        _, recompute, _ = _load_pure_helpers()
        assert recompute(_pick(), 0) is None

    def test_never_raises_on_bad_model_score(self):
        _, recompute, _ = _load_pure_helpers()
        result = recompute(_pick(model_score=None), 150)
        assert result is not None


def test_render_odds_override_exists_and_is_wired_into_full_pick():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "def _render_odds_override(pick: dict, unit_usd: float | None) -> None:" in source
    assert 'if pick.get("ev_pct") is None or not pick.get("offered_decimal_odds"):' in source
    assert "st.text_input(" in source
    assert "_parse_american_odds(entered)" in source
    assert "_recompute_stake_for_odds(pick, parsed)" in source
    # Only rendered for open picks -- settled picks already have a real result.
    assert "if not settled:\n        _render_odds_override(pick, unit_usd)" in source


def test_odds_override_never_shown_for_settled_picks():
    """_render_full_pick must gate the odds-override input behind the same
    settled flag used for the Bet Now button -- a resolved pick has a real
    outcome already, not a hypothetical stake to plan."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    idx = source.index("def _render_full_pick(")
    end = source.index("\ndef _render_locked_pick(")
    body = source[idx:end]
    assert body.count("_render_odds_override(pick, unit_usd)") == 1
