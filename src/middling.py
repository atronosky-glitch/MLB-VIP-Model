"""Middle detection: Over at one line, Under at a higher line.

A middle is betting Over the lower of two available lines at one book
and Under the higher of two available lines at another book. If the
final number lands strictly between the two lines, BOTH bets win — a
bonus payout on top of the stakes. Outside that window, exactly one
side wins and the other loses, so (unlike true arbitrage) a middle is
not risk-free — but with two close-to-fair prices the guaranteed
worst-case outcome is a small loss (roughly the combined vig), which is
what "little to no risk" means here. If the numbers land exactly on one
of the two lines, that leg pushes instead of losing, which only helps
the worst case — deliberately not modeled here, so the worst-case
figure this reports is conservative (a true floor, never optimistic).

Deliberately Over/Under markets only (player props, game totals) —
not spreads or run lines. A spread's Home/Away sides map to "the
favorite's margin is over/under this number" only through a sign
(favorite vs. underdog) that varies per line and per book, and getting
that sign wrong would produce a "middle" that doesn't actually cover
both outcomes — a correctness risk not worth taking in the first
version of a feature whose entire point is safety.
"""

from __future__ import annotations

from collections import defaultdict

from src.line_plausibility import consensus_lines, is_plausible_line


def _implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


def find_middle_between_lines(
    over_line: float,
    over_price: dict,
    under_line: float,
    under_price: dict,
    stake_total: float = 1.0,
) -> dict | None:
    """Check one specific (lower Over line, higher Under line) pair.

    ``over_price``/``under_price``: ``{"sportsbook": str, "price": int,
    "decimal_odds": float}``. Returns None if ``over_line >= under_line``
    (no window — not a middle at all) or if the prices are missing.
    """
    if over_line is None or under_line is None or over_line >= under_line:
        return None
    if not over_price or not under_price:
        return None

    over_dec = over_price.get("decimal_odds")
    under_dec = under_price.get("decimal_odds")
    if not over_dec or not under_dec or over_dec <= 1.0 or under_dec <= 1.0:
        return None

    # Stake split so a win on either single leg alone returns the same
    # amount back (the standard way to size a middle) — proportional to
    # each leg's own break-even stake.
    prob_over = _implied_prob(over_dec)
    prob_under = _implied_prob(under_dec)
    combined = prob_over + prob_under
    stake_over = stake_total * (prob_over / combined)
    stake_under = stake_total * (prob_under / combined)

    payout_if_over_wins = stake_over * over_dec
    payout_if_under_wins = stake_under * under_dec
    # Worst case: only one leg wins (the other's full stake is lost).
    worst_case_return = min(payout_if_over_wins, payout_if_under_wins) - stake_total
    # Best case: the number lands strictly inside the window and both win.
    best_case_return = payout_if_over_wins + payout_if_under_wins - stake_total

    return {
        "over_line": over_line,
        "over_sportsbook": over_price.get("sportsbook"),
        "over_price": over_price.get("price"),
        "over_decimal_odds": over_dec,
        "over_stake_pct": round(prob_over / combined, 6),
        "under_line": under_line,
        "under_sportsbook": under_price.get("sportsbook"),
        "under_price": under_price.get("price"),
        "under_decimal_odds": under_dec,
        "under_stake_pct": round(prob_under / combined, 6),
        "window_width": round(under_line - over_line, 4),
        "worst_case_roi_pct": round((worst_case_return / stake_total) * 100, 4),
        "best_case_roi_pct": round((best_case_return / stake_total) * 100, 4),
    }


def find_middle_opportunities(
    rows: list[dict],
    max_worst_case_loss_pct: float = 5.0,
) -> list[dict]:
    """Scan a batch of raw odds rows for middle opportunities.

    Groups by (event_id, player_id, market_type) — deliberately ignoring
    line, since a middle only exists ACROSS different lines — restricted
    to rows whose side is literally "OVER"/"UNDER" (see module docstring
    for why spreads are excluded). For each group, for every pair of
    distinct lines where an Over exists at the lower one and an Under at
    the higher one, checks whether it's a middle.

    ``max_worst_case_loss_pct``: only opportunities whose guaranteed
    worst case loses at most this much are returned — this is the
    "little to no risk" filter the feature is named for. A guaranteed
    worst-case *gain* (worst_case_roi_pct >= 0) is arbitrage, not a
    middle, and is left for src/arbitrage.py to find on its own exact-line
    groups; still included here since it clears the same risk bar, but
    callers wanting a strict middle-only list should also check
    ``best_case_roi_pct > worst_case_roi_pct`` (true here by construction
    whenever a real window exists).

    Both lines of a candidate pair must be plausible relative to this
    market's own consensus number (see src/line_plausibility.py) — found
    live 2026-09-09: a far-out alternate line can carry unreliable
    near-even-money pricing instead of the extreme odds a line that far
    from the truth would actually have, which otherwise looks like a
    "small guaranteed risk" middle with a huge fake window instead of the
    unreliable data it actually is.
    """
    groups: dict[tuple, dict[float, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    meta: dict[tuple, dict] = {}
    for row in rows:
        side = (row.get("side") or "").upper()
        if side not in ("OVER", "UNDER"):
            continue
        line = row.get("line")
        if line is None:
            continue
        key = (row.get("event_id"), row.get("player_id"), row.get("market_type"))
        groups[key].setdefault(line, {"OVER": {}, "UNDER": {}})
        book = row.get("sportsbook")
        if not book:
            continue
        entry = {"sportsbook": book, "price": row.get("price"), "decimal_odds": row.get("decimal_odds")}
        existing = groups[key][line][side].get(book)
        if existing is None or entry["decimal_odds"] > existing["decimal_odds"]:
            groups[key][line][side][book] = entry
        meta.setdefault(key, {
            "event_id": row.get("event_id"),
            "player_id": row.get("player_id"),
            "player_name": row.get("player_name"),
            "team_name": row.get("team_name"),
            "market_type": row.get("market_type"),
        })

    consensus = consensus_lines(rows)

    opportunities = []
    for key, lines_map in groups.items():
        distinct_lines = sorted(lines_map.keys())
        if len(distinct_lines) < 2:
            continue
        market_type = meta[key]["market_type"]
        group_consensus = consensus.get(key)
        for i, over_line in enumerate(distinct_lines):
            if group_consensus is not None and not is_plausible_line(market_type, over_line, group_consensus):
                continue
            over_books = lines_map[over_line]["OVER"]
            if not over_books:
                continue
            best_over_book = max(over_books, key=lambda b: over_books[b]["decimal_odds"])
            over_price = over_books[best_over_book]
            for under_line in distinct_lines[i + 1:]:
                if group_consensus is not None and not is_plausible_line(market_type, under_line, group_consensus):
                    continue
                under_books = lines_map[under_line]["UNDER"]
                if not under_books:
                    continue
                best_under_book = max(under_books, key=lambda b: under_books[b]["decimal_odds"])
                under_price = under_books[best_under_book]
                if over_price["sportsbook"] == under_price["sportsbook"]:
                    # Same book quoting both lines still counts — a real
                    # middle doesn't require different books the way
                    # arbitrage does, since it's exploiting the book(s)'
                    # own line movement between two points in time or
                    # two separate markets, not a cross-book price error.
                    pass
                result = find_middle_between_lines(over_line, over_price, under_line, under_price)
                if result is None:
                    continue
                if result["worst_case_roi_pct"] < -max_worst_case_loss_pct:
                    continue
                result.update(meta[key])
                opportunities.append(result)

    opportunities.sort(key=lambda o: -o["best_case_roi_pct"])
    return opportunities
