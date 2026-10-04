"""De-duplicate customer-facing pick rows by the bet they describe.

The worker re-scans every opportunity many times, and each scan is stored as
its own historical_recommendations row. Rows are therefore NOT unique per bet:
on 2026-10-04 the customer "Full Board" query returned 25 rows that were only
16 distinct bets (one game total appeared 7 times). Two identical rows made
Streamlit raise StreamlitDuplicateElementKey (every pick card builds its
"different price" input key from the bet's identity), which crashed the EV Picks
page and hid everything below it, including the track record.

Callers pass rows already ordered best-first, so the first row seen for a bet is
the one that is kept. Kept in its own module (not customer_view.py) so tests can
import it; importing src.customer_view outside Streamlit leaks st.form contexts
into later tests (see tests/test_customer_view_auth.py).
"""

from __future__ import annotations

from typing import Iterable

# What makes two rows "the same bet" for display purposes. scan time and price are
# deliberately excluded: a re-scan of an unchanged bet must collapse into one card.
PICK_IDENTITY_FIELDS = ("event_id", "player_id", "market_type", "line", "side", "sportsbook")


def pick_identity(pick: dict) -> tuple:
    return tuple(pick.get(field) for field in PICK_IDENTITY_FIELDS)


def dedupe_picks(picks: Iterable[dict], limit: int | None = None) -> list[dict]:
    """First occurrence of each bet wins; input order is preserved.

    ``limit`` caps the number of DISTINCT bets returned.
    """
    seen: set[tuple] = set()
    unique: list[dict] = []
    for pick in picks:
        identity = pick_identity(pick)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(pick)
        if limit is not None and len(unique) >= limit:
            break
    return unique
