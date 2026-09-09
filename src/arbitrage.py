"""Cross-book arbitrage detection.

True arbitrage: the best available price on each side of a two-outcome
market (Over/Under, Home/Away, or Yes/No) — from any two books, not
necessarily the same book — combine to LESS than 100% implied
probability. Staking proportionally on both sides then guarantees a
profit regardless of which side wins.

This never trusts a single side's price in isolation the way EV scoring
does (that's src/player_prop_analysis.py's job, and needs a fair-value
reference like Pinnacle). Arbitrage needs no fair-value model at all —
it's pure cross-book mathematics: if 1/decimal_a + 1/decimal_b < 1, the
two books disagree enough that both sides can be bought at once for a
guaranteed profit no matter the outcome. Requires two DIFFERENT books
(a single book's own market always sums to >= 100%, or it wouldn't be
in business), and both sides must be at the exact same line — the
Over/Under (or Home/Away) of two different lines is a middle, not an
arbitrage (see src/middling.py).
"""

from __future__ import annotations

from collections import defaultdict

from src.line_plausibility import consensus_lines, is_plausible_line


def _implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


def find_arbitrage_in_group(
    group_key: str,
    side_a_prices: dict[str, dict],
    side_b_prices: dict[str, dict],
) -> dict | None:
    """Check one exact market group (same event/player/market/line) for a
    genuine cross-book arbitrage between its two sides.

    Parameters
    ----------
    side_a_prices, side_b_prices : dict[str, dict]
        ``{sportsbook: {"price": int, "decimal_odds": float}}`` — the two
        sides of the market (Over/Under, Home/Away, Yes/No — whatever
        this market's two outcomes are labelled).

    Returns
    -------
    dict describing the opportunity, or None if no arbitrage exists.
    """
    if not side_a_prices or not side_b_prices:
        return None

    best_a_book = max(side_a_prices, key=lambda b: side_a_prices[b]["decimal_odds"])
    best_b_book = max(side_b_prices, key=lambda b: side_b_prices[b]["decimal_odds"])

    if best_a_book == best_b_book:
        # The same book offering the best price on both sides of its own
        # market would mean that book's own market sums to under 100% —
        # not a real-world case worth trusting; arbitrage requires two
        # books actually disagreeing with each other.
        return None

    dec_a = side_a_prices[best_a_book]["decimal_odds"]
    dec_b = side_b_prices[best_b_book]["decimal_odds"]
    if dec_a <= 1.0 or dec_b <= 1.0:
        return None

    prob_a = _implied_prob(dec_a)
    prob_b = _implied_prob(dec_b)
    combined = prob_a + prob_b
    if combined >= 1.0:
        return None

    guaranteed_roi = (1.0 / combined) - 1.0
    stake_a_pct = prob_a / combined
    stake_b_pct = prob_b / combined

    return {
        "group_key": group_key,
        "side_a_book": best_a_book,
        "side_a_price": side_a_prices[best_a_book]["price"],
        "side_a_decimal_odds": dec_a,
        "side_a_stake_pct": round(stake_a_pct, 6),
        "side_b_book": best_b_book,
        "side_b_price": side_b_prices[best_b_book]["price"],
        "side_b_decimal_odds": dec_b,
        "side_b_stake_pct": round(stake_b_pct, 6),
        "guaranteed_roi_pct": round(guaranteed_roi * 100, 4),
        "combined_implied_prob": round(combined, 6),
    }


def find_arbitrage_opportunities(rows: list[dict]) -> list[dict]:
    """Scan a batch of raw odds rows (same shape as ``player_prop_odds``
    columns: event_id, player_id, market_type, market_group_key, side,
    line, sportsbook, price, decimal_odds) for cross-book arbitrage.

    Groups by ``market_group_key`` (already exact event/player/market/
    line), splits into the group's two sides, and checks each group
    independently. A market with more than two distinct side labels
    (shouldn't happen for a well-formed two-outcome market) is skipped
    rather than guessed at.

    A group whose line is implausibly far from this market's own
    consensus (see src/line_plausibility.py) is skipped too — found
    live 2026-09-09: far-out alternate lines can carry unreliable
    near-even-money pricing instead of the extreme odds a line that far
    from the true number would actually have, which can otherwise look
    like a "guaranteed profit" arbitrage that isn't real.
    """
    groups: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    meta: dict[str, dict] = {}
    for row in rows:
        key = row.get("market_group_key")
        side = row.get("side")
        book = row.get("sportsbook")
        if not key or not side or not book:
            continue
        groups[key][side][book] = {
            "price": row.get("price"),
            "decimal_odds": row.get("decimal_odds"),
        }
        meta.setdefault(key, {
            "event_id": row.get("event_id"),
            "player_id": row.get("player_id"),
            "player_name": row.get("player_name"),
            "team_name": row.get("team_name"),
            "market_type": row.get("market_type"),
            "line": row.get("line"),
        })

    consensus = consensus_lines(rows)

    opportunities = []
    for key, sides in groups.items():
        side_labels = list(sides.keys())
        if len(side_labels) != 2:
            continue

        line = meta[key].get("line")
        if line is not None:
            market_type = meta[key].get("market_type")
            consensus_key = (meta[key].get("event_id"), meta[key].get("player_id"), market_type)
            group_consensus = consensus.get(consensus_key)
            if group_consensus is not None and not is_plausible_line(market_type, line, group_consensus):
                continue

        side_a, side_b = side_labels
        result = find_arbitrage_in_group(key, sides[side_a], sides[side_b])
        if result is None:
            continue
        result["side_a"] = side_a
        result["side_b"] = side_b
        result.update(meta[key])
        opportunities.append(result)

    opportunities.sort(key=lambda o: -o["guaranteed_roi_pct"])
    return opportunities
