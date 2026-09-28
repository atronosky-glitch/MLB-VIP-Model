"""Shared, pure spread-sign formatting.

game_spread_ou/game_runline_ou (and CFB's 1Q/1H spread variants) store TWO
numbers per recommendation: ``line`` (an unsigned magnitude, used for
provider/market matching) and ``raw_line`` (the SIGNED line for the
recommended side -- favorite negative, underdog positive -- the exact value
``src/game_settlement.py::grade_spread()`` already grades against).
Displaying ``line`` alone drops the sign ("Away 6.5" instead of
"Away +6.5"/"Away -6.5" depending on which team is favored) -- found live
2026-09-27 across three independent formatters (customer site, Discord,
admin dashboard). One place holds the market list and the formatting so all
three stay in sync.

Every other market (moneyline has no line; totals/player O-U have no
favorite/underdog concept) is untouched -- callers keep using the plain
``line`` value for anything not in ``SIGNED_SPREAD_MARKETS``.

Kept in its own module (not customer_view.py) so it can be imported directly
in tests -- importing src.customer_view outside a Streamlit runtime executes
the whole page and leaks st.form/st.expander contexts into later tests (see
tests/test_customer_view_auth.py::test_no_test_imports_the_customer_page_module_directly).
"""

from __future__ import annotations

# Mirrors src/arb_middle_scan.py's own _EXCLUDED_MARKET_TYPES for the same
# market set (arb/middle deliberately never includes spreads at all).
SIGNED_SPREAD_MARKETS = frozenset({
    "game_spread_ou", "game_runline_ou", "game_spread_1q_ou", "game_spread_1h_ou",
})


def signed_spread_line_text(pick: dict) -> str | None:
    """Explicitly signed spread line text (e.g. "+6.5", "-3.5"), or None if
    this isn't a signed-spread market or the signed value was never
    captured. Never guesses a sign from the unsigned magnitude."""
    if (pick.get("market_type") or "") not in SIGNED_SPREAD_MARKETS:
        return None
    raw_line = pick.get("raw_line")
    if raw_line is None:
        return None
    return f"{raw_line:+g}"
