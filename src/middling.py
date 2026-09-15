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

from src.line_plausibility import consensus_lines, is_plausible_line, consensus_prices, is_plausible_price


def _implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


# ── Hit-probability estimation and Kelly-based sizing ────────────────
#
# Neither worst_case_roi_pct nor best_case_roi_pct alone says whether a
# middle is actually worth betting -- that depends on how LIKELY the
# final number is to land in the window, which those two fields don't
# estimate at all. This section adds that estimate (devigged from real
# market consensus, never guessed) plus a Kelly-based stake so bad
# windows (wide loss, thin hit chance) can be told apart from good ones
# (tight window, real hit chance) instead of relying on best-case ROI
# alone, which rewards wide windows regardless of how unlikely they are
# to actually pay off.

KELLY_FRACTION = 0.25  # matches src/tracker.py::compute_variable_stake's own 25% fractional Kelly
MIN_STAKE_UNITS = 0.25
MAX_STAKE_UNITS = 2.0


def _no_vig_probability(key: tuple, line: float, side: str, price_consensus: dict) -> float | None:
    """No-vig fair P(actual is on *side* of *line*), devigged from BOTH
    sides' own consensus implied probability at this EXACT line -- not
    the vig-inflated price of a single leg, which would overstate the
    probability (vig inflates both sides' implied probability, so using
    one side's raw price alone systematically overstates how likely it
    is). Returns None if either side isn't quoted at this line by any
    book -- common for far alt lines, and not something to guess around."""
    over_p = price_consensus.get(key + (line, "OVER"))
    under_p = price_consensus.get(key + (line, "UNDER"))
    if over_p is None or under_p is None:
        return None
    total = over_p + under_p
    if total <= 0:
        return None
    return (over_p if side == "OVER" else under_p) / total


def estimate_middle_hit_probability(
    key: tuple, over_line: float, under_line: float, price_consensus: dict,
) -> tuple[float | None, str]:
    """P(the final number lands strictly between over_line and
    under_line), i.e. the probability BOTH legs of the middle win.

    P(hit) = P(actual > over_line) + P(actual < under_line) - 1, using
    each line's own no-vig fair probability (see ``_no_vig_probability``).
    This is the standard middle-probability identity: P(actual >
    over_line) already includes the hit region PLUS "actual >=
    under_line", and P(actual < under_line) already includes the hit
    region PLUS "actual <= over_line" -- adding them double-counts the
    hit region exactly once, so subtracting 1 (the total probability of
    the two non-overlapping "miss" regions plus the hit region) isolates
    it.

    Requires a two-sided (Over AND Under) consensus at BOTH lines.
    Returns ``(None, "UNAVAILABLE")`` rather than fabricate a number
    when that data isn't there -- matching this module's existing
    "never guess" convention for line/price plausibility (see the
    module and ``find_middle_opportunities`` docstrings). Returns
    ``(p_hit, "DEVIGGED")`` otherwise, clamped to [0, 1] since real
    market noise can occasionally push the raw identity a hair outside
    that range.
    """
    p_over = _no_vig_probability(key, over_line, "OVER", price_consensus)
    p_under = _no_vig_probability(key, under_line, "UNDER", price_consensus)
    if p_over is None or p_under is None:
        return None, "UNAVAILABLE"
    p_hit = p_over + p_under - 1.0
    return round(max(0.0, min(1.0, p_hit)), 6), "DEVIGGED"


def kelly_fraction_bounded(p_hit: float, r_hit: float, r_miss: float) -> float:
    """Optimal Kelly stake fraction for a two-outcome bet whose "miss"
    loses only ``r_miss`` (a bounded fraction of stake, e.g. -0.03) not
    the whole stake -- the actual shape of a middle's payoff (missing
    the window loses roughly the combined vig, never everything, thanks
    to how ``find_middle_between_lines`` sizes the two legs).

    Derived from setting d/df[p*ln(1+f*r_hit) + (1-p)*ln(1+f*r_miss)] to
    0. Because p + (1-p) = 1, the f^2 cross term cancels and the result
    is a closed form (no numeric solve needed):

        f* = -(p*r_hit + (1-p)*r_miss) / (r_hit * r_miss) = -EV / (r_hit * r_miss)

    Sanity-checked against the textbook binary-Kelly formula f*=p-q/b:
    setting r_miss=-1 (lose-everything) reduces this exactly to that
    formula. Returns 0.0 when EV<=0 (no edge -- never a negative stake)
    or r_hit<=0 (nothing to size). Returns 1.0 when r_miss>=0 (no real
    downside at all -- that's arbitrage, not a middle, and Kelly has no
    interior optimum against a riskless bet; sizing is capped externally
    by MAX_STAKE_UNITS instead).
    """
    ev = p_hit * r_hit + (1.0 - p_hit) * r_miss
    if ev <= 0 or r_hit <= 0:
        return 0.0
    if r_miss >= 0:
        return 1.0
    return max(0.0, -ev / (r_hit * r_miss))


def compute_middle_stake_units(
    hit_probability: float | None, best_case_roi_pct: float, worst_case_roi_pct: float,
) -> float | None:
    """25% fractional Kelly, clamped to [0.25, 2.0] units (1 unit = 1%
    of bankroll) -- the same fractional-Kelly-with-clamp convention
    src/tracker.py::compute_variable_stake uses for the model's own
    picks.

    Returns None (no recommendation -- not a silent default) when
    hit_probability is unavailable: the entire point of this function
    is separating the middles genuinely worth betting from the ones
    that aren't, so guessing a stake without a real probability estimate
    would defeat that purpose. Returns 0.0 (a real, computed "don't bet
    this one") when true EV works out <= 0.
    """
    if hit_probability is None:
        return None
    r_hit = best_case_roi_pct / 100.0
    r_miss = worst_case_roi_pct / 100.0
    full_kelly = kelly_fraction_bounded(hit_probability, r_hit, r_miss)
    if full_kelly <= 0:
        return 0.0
    units = full_kelly * KELLY_FRACTION * 100.0
    return round(max(MIN_STAKE_UNITS, min(MAX_STAKE_UNITS, units)), 2)


def find_middle_between_lines(
    over_line: float,
    over_price: dict,
    under_line: float,
    under_price: dict,
    stake_total: float = 1.0,
    hit_probability: float | None = None,
    hit_probability_confidence: str = "UNAVAILABLE",
) -> dict | None:
    """Check one specific (lower Over line, higher Under line) pair.

    ``over_price``/``under_price``: ``{"sportsbook": str, "price": int,
    "decimal_odds": float}``. Returns None if ``over_line >= under_line``
    (no window — not a middle at all) or if the prices are missing.

    ``hit_probability``/``hit_probability_confidence`` are optional --
    when the caller has already devigged a real P(hit) estimate (see
    ``estimate_middle_hit_probability``), pass it through here to get
    ``true_ev_pct``, a ``verdict`` ("WORTH_IT"/"NOT_WORTH_IT"/"UNKNOWN"),
    and -- WORTH_IT only -- a Kelly-sized ``recommended_stake_units``
    back in the result. A NOT_WORTH_IT middle still gets ``true_ev_pct``
    (so it's visible WHY) but ``recommended_stake_units`` stays None,
    never 0 -- 0 would read as "a real recommendation of size zero"
    instead of "no recommendation at all." Left at their defaults
    (None/"UNAVAILABLE"), everything new comes back None/"UNKNOWN" --
    this function never estimates a probability itself, only uses one
    if given.
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

    worst_case_roi_pct = round((worst_case_return / stake_total) * 100, 4)
    best_case_roi_pct = round((best_case_return / stake_total) * 100, 4)

    true_ev_pct = None
    recommended_stake_units = None
    verdict = "UNKNOWN"
    if hit_probability is not None:
        true_ev_pct = round(
            hit_probability * best_case_roi_pct + (1.0 - hit_probability) * worst_case_roi_pct, 4,
        )
        verdict = "WORTH_IT" if true_ev_pct > 0 else "NOT_WORTH_IT"
        if verdict == "WORTH_IT":
            # Sizing is only ever attached to a WORTH_IT verdict -- a
            # NOT_WORTH_IT middle keeps true_ev_pct visible (so it's
            # clear WHY) but gets no stake number at all, not even 0,
            # so nothing downstream can mistake "no edge" for "a real
            # recommendation of size zero."
            recommended_stake_units = compute_middle_stake_units(
                hit_probability, best_case_roi_pct, worst_case_roi_pct,
            )

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
        "worst_case_roi_pct": worst_case_roi_pct,
        "best_case_roi_pct": best_case_roi_pct,
        "hit_probability": hit_probability,
        "hit_probability_confidence": hit_probability_confidence,
        "true_ev_pct": true_ev_pct,
        "verdict": verdict,
        "recommended_stake_units": recommended_stake_units,
    }


def find_middle_opportunities(
    rows: list[dict],
    max_worst_case_loss_pct: float = 5.0,
    min_true_ev_pct: float | None = None,
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

    A single row whose PRICE is implausibly far from its own side's
    consensus implied probability is dropped before it can be paired at
    all — found live 2026-09-10 evaluating exchange venues: a perfectly
    normal, plausible line can still carry one thin-liquidity outlier
    price a line-only check can't catch.

    Each result also carries a devigged ``hit_probability`` (see
    ``estimate_middle_hit_probability``) plus the ``true_ev_pct`` and
    Kelly-sized ``recommended_stake_units`` it implies, whenever both
    lines have two-sided consensus pricing to devig from —
    ``worst_case_roi_pct``/``best_case_roi_pct`` alone say nothing about
    how LIKELY the window actually is to hit, which is what separates a
    middle genuinely worth betting from one that only looks good on a
    wide-but-unlikely window. When that probability can't be estimated
    (thin alt-line data, one side never two-sided), those three fields
    come back None rather than a guess — still returned, just flagged
    ``hit_probability_confidence="UNAVAILABLE"`` instead of silently
    dropped.

    ``min_true_ev_pct``, if given, additionally drops any opportunity
    whose ``true_ev_pct`` is known and below this threshold — but never
    an "UNAVAILABLE"-confidence one, since there's no computed EV to
    compare against a threshold in that case.
    """
    price_consensus = consensus_prices(rows)

    groups: dict[tuple, dict[float, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    meta: dict[tuple, dict] = {}
    for row in rows:
        side = (row.get("side") or "").upper()
        if side not in ("OVER", "UNDER"):
            continue
        line = row.get("line")
        if line is None:
            continue
        book = row.get("sportsbook")
        price = row.get("price")
        if not book or price is None:
            continue
        prob_key = (row.get("event_id"), row.get("player_id"), row.get("market_type"), line, side)
        side_consensus = price_consensus.get(prob_key)
        if side_consensus is not None and not is_plausible_price(price, side_consensus):
            continue
        key = (row.get("event_id"), row.get("player_id"), row.get("market_type"))
        groups[key].setdefault(line, {"OVER": {}, "UNDER": {}})
        entry = {"sportsbook": book, "price": price, "decimal_odds": row.get("decimal_odds")}
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
                hit_probability, hit_confidence = estimate_middle_hit_probability(
                    key, over_line, under_line, price_consensus,
                )
                result = find_middle_between_lines(
                    over_line, over_price, under_line, under_price,
                    hit_probability=hit_probability, hit_probability_confidence=hit_confidence,
                )
                if result is None:
                    continue
                if result["worst_case_roi_pct"] < -max_worst_case_loss_pct:
                    continue
                if (
                    min_true_ev_pct is not None
                    and result["true_ev_pct"] is not None
                    and result["true_ev_pct"] < min_true_ev_pct
                ):
                    continue
                result.update(meta[key])
                opportunities.append(result)

    # Sort by true EV when it's known (a real, probability-weighted
    # ranking) -- fall back to best-case ROI only for the UNAVAILABLE-
    # confidence opportunities where there's no computed EV to sort by.
    opportunities.sort(
        key=lambda o: -(o["true_ev_pct"] if o["true_ev_pct"] is not None else o["best_case_roi_pct"])
    )
    return opportunities
