"""Cross-line plausibility check shared by arbitrage and middling detection.

Real-world bug this exists to catch (found live 2026-09-09, from an
operator spotting a bad card on the customer site): a game's OTHER
quoted lines are the best available evidence of where the true number
actually is. A "Game Total 4.5" line priced near even money (+100 to
+150) right next to that same game's "Game Total 8.5" line ALSO priced
near even money is physically impossible — combined MLB runs can't
plausibly be a coin flip at both 4.5 and 8.5 simultaneously. Confirmed
live: every line for that market was flagged is_alt_line=0 (so that
flag can't be trusted to separate a real main line from an unreliable
one), yet 3.5/4.5/7.5 were priced as if they were near the true number
while 8.0/8.5/9.0/9.5 (clustered tightly, consistent with each other)
clearly were. This looks like unreliable/placeholder pricing on far-out
alternate lines reaching the feed as if it were real tradeable data —
exactly the kind of "guaranteed profit" opportunity that isn't real and
must never reach a customer.

Rather than trust a market-type's is_alt_line flag (demonstrated
unreliable), this derives the market's own consensus number directly
from every line actually being quoted for it, and refuses to build an
arbitrage or middle around any line too far from that consensus to
trust its pricing.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import median

# Max plausible distance from the consensus line, by market_type. Game
# totals/spreads have a wider natural spread of legitimate alt lines
# than a player prop typically does.
MAX_LINE_DEVIATION: dict[str, float] = {
    "game_total_ou": 2.0,
    "game_spread_ou": 3.0,
    "game_runline_ou": 1.5,
}
DEFAULT_MAX_DEVIATION = 1.5


def consensus_lines(rows: list[dict]) -> dict[tuple, float]:
    """Median line per (event_id, player_id, market_type), computed from
    every row regardless of side/book/price — purely which number the
    market is actually quoting most, independent of any one price."""
    lines_by_key: dict[tuple, list[float]] = defaultdict(list)
    for row in rows:
        line = row.get("line")
        if line is None:
            continue
        key = (row.get("event_id"), row.get("player_id"), row.get("market_type"))
        lines_by_key[key].append(line)
    return {key: median(values) for key, values in lines_by_key.items()}


def is_plausible_line(market_type: str, line: float, consensus: float) -> bool:
    """Whether *line* is close enough to this market's own consensus
    number to trust its pricing."""
    max_dev = MAX_LINE_DEVIATION.get(market_type, DEFAULT_MAX_DEVIATION)
    return abs(line - consensus) <= max_dev
