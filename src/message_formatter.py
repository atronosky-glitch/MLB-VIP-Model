"""Recommendation message formatting for delivery channels.

Formats recommendations into human-readable messages for Discord/Slack
and plain-text summaries. Handles both O/U and YN recommendation types,
chunking for platform message length limits, and confidence display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


# Discord character limit (with small safety margin)
DISCORD_CHAR_LIMIT = 1900


@dataclass(frozen=True)
class FormattedMessage:
    """A chunked message ready for delivery."""
    channel: str
    chunks: list[str]
    total_chars: int
    chunk_count: int


def format_recommendation(rec: dict[str, Any]) -> str:
    """Format a single recommendation into a human-readable block.

    Parameters
    ----------
    rec:
        Recommendation dict with keys from the DB schema (see
        database/db_manager.py's historical_recommendations table):
        player_name, matchup, sport/league, market_type, sportsbook,
        line, offered_american_odds, offered_implied_prob, fair_prob,
        status, ev_pct (optional), price_advantage_pct (optional),
        confidence_score (optional), rec_status,
        recommendation_fingerprint, etc.
    """
    lines = []

    # Header
    player = rec.get("player_name", "Unknown")
    # 2026-09-18: was rec.get("event_name") -- historical_recommendations
    # has no such column (it's "matchup"), so the event/game line never
    # rendered for a single real Discord message despite the data being
    # right there on every row. Confirmed via database/db_manager.py's
    # historical_recommendations schema and src/customer_view.py, which
    # reads the same column as "matchup" everywhere else in this codebase.
    event = rec.get("matchup", "")
    market = rec.get("market_type", "unknown")
    lines.append(f"**{player}** — {market.replace('_', ' ').title()}")
    if event:
        lines.append(f"_{event}_")

    sport_league = rec.get("league") or rec.get("sport")
    if sport_league:
        lines.append(f"Sport: {sport_league}")

    # Core details
    book = rec.get("sportsbook", "Unknown")
    odds = rec.get("offered_american_odds", 0)
    odds_str = f"+{odds}" if odds > 0 else str(odds)

    line_val = rec.get("line")
    side = rec.get("side", "")
    # 2026-09-21: "Period: game" showed on literally every message (every
    # current player-prop market is full-game; only CFB's 1Q/1H game
    # markets carry a genuinely different value) and read as meaningless
    # noise per direct operator feedback -- suppressed only for the
    # uninteresting default, still shown for a real sub-game period.
    period = rec.get("period", "")

    if line_val is not None:
        lines.append(f"Line: {line_val} ({side})" if side else f"Line: {line_val}")
    if period and period != "game":
        lines.append(f"Period: {period}")

    lines.append(f"Book: **{book}** @ {odds_str}")

    # EV or price advantage
    ev = rec.get("ev_pct")
    if ev is not None:
        lines.append(f"EV: {ev:+.2f}%")

    # 2026-09-21: was "Price Advantage: +5.38 pp" -- "pp" (percentage
    # points) confused the operator and, per their own feedback, everyone
    # else reading it too. Same number, plainer label.
    pa = rec.get("yn_implied_prob_adv") or rec.get("price_advantage_pct")
    if pa is not None:
        lines.append(f"Edge: {pa:+.2f}%")

    # Fair vs. implied probability -- both already computed at
    # recommendation time (see src/player_prop_analysis.py), just not
    # previously surfaced in the Discord message.
    fair_prob = rec.get("fair_prob")
    implied_prob = rec.get("offered_implied_prob")
    if fair_prob is not None or implied_prob is not None:
        parts = []
        if fair_prob is not None:
            parts.append(f"Fair {fair_prob * 100:.1f}%")
        if implied_prob is not None:
            parts.append(f"Implied {implied_prob * 100:.1f}%")
        lines.append("Probability: " + " vs. ".join(parts))

    # 2026-09-21: "Consensus books" removed per direct operator feedback
    # -- not meaningful to a customer reading the alert.

    # Confidence
    conf = rec.get("confidence_score")
    if conf is not None:
        conf_label = _confidence_label(conf)
        lines.append(f"Confidence: {conf:.0f}/100 ({conf_label})")

    # Status
    status = rec.get("rec_status", rec.get("status", ""))
    if status:
        lines.append(f"Status: {status}")

    # Fingerprint (short)
    fp = rec.get("recommendation_fingerprint", "")
    if fp:
        lines.append(f"ID: `{fp[:16]}`")

    return "\n".join(lines)


def format_daily_summary(
    recs: list[dict[str, Any]],
    stats: dict[str, Any] | None = None,
    *,
    date_label: str = "",
) -> str:
    """Format a daily summary message.

    Parameters
    ----------
    recs:
        List of recommendation dicts.
    stats:
        Optional pipeline stats (total_scanned, total_recommended, etc.).
    date_label:
        Optional date string for the header.
    """
    lines = []
    header = "MLB Model — Daily Summary"
    if date_label:
        header += f" ({date_label})"
    lines.append(f"**{header}**")
    lines.append("")

    if stats:
        lines.append(f"Markets scanned: {stats.get('total_scanned', 0)}")
        lines.append(f"Recommendations: {stats.get('total_recommended', 0)}")
        lines.append(f"Strong edges: {stats.get('strong_edges', 0)}")
        lines.append("")

    if not recs:
        lines.append("No actionable recommendations today.")
        return "\n".join(lines)

    # Group by status. Real values written by src/prop_config.py's
    # classification (BET_STATUS_*/YN_STATUS_*) -- 'BET'/'LEAN'/'MONITOR'
    # never existed in this schema, so these groupings previously matched
    # nothing.
    strong = [r for r in recs if r.get("rec_status") in ("STRONG_EDGE", "STRONG_PRICE_OUTLIER")]
    positive = [r for r in recs if r.get("rec_status") in ("POSITIVE_EDGE", "PRICE_OUTLIER")]
    monitor = [r for r in recs if r.get("rec_status") in ("MARGINAL_EDGE", "MARGINAL_PRICE_OUTLIER")]

    if strong:
        lines.append(f"**Strong Edge ({len(strong)})**")
        for r in strong:
            lines.append(_compact_line(r))
        lines.append("")

    if positive:
        lines.append(f"**Positive Edge ({len(positive)})**")
        for r in positive:
            lines.append(_compact_line(r))
        lines.append("")

    if monitor:
        lines.append(f"**MONITOR ({len(monitor)})**")
        for r in monitor[:5]:
            lines.append(_compact_line(r))
        if len(monitor) > 5:
            lines.append(f"  ...and {len(monitor) - 5} more")
        lines.append("")

    return "\n".join(lines).rstrip()


def chunk_message(text: str, max_length: int = DISCORD_CHAR_LIMIT) -> list[str]:
    """Split a message into chunks that fit within a character limit.

    Tries to split on newline boundaries. Each chunk starts with a
    continuation marker if it's not the first chunk.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Find last newline within limit
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            # No newline found; hard split
            split_at = max_length

        chunk = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n")
        chunks.append(chunk)

    # Add continuation markers
    if len(chunks) > 1:
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            chunks[i] = f"_(Part {i + 1}/{total})_\n{chunk}"

    return chunks


def format_for_discord(recs: list[dict[str, Any]], date_label: str = "") -> FormattedMessage:
    """Format recommendations as a Discord-ready message with chunking."""
    text = format_daily_summary(recs, date_label=date_label)
    chunks = chunk_message(text)
    return FormattedMessage(
        channel="discord",
        chunks=chunks,
        total_chars=len(text),
        chunk_count=len(chunks),
    )


def format_for_slack(recs: list[dict[str, Any]], date_label: str = "") -> FormattedMessage:
    """Format recommendations for Slack (no chunking needed, higher limit)."""
    text = format_daily_summary(recs, date_label=date_label)
    return FormattedMessage(
        channel="slack",
        chunks=[text],
        total_chars=len(text),
        chunk_count=1,
    )


def format_arbitrage_alert(opportunities: list[dict[str, Any]]) -> str:
    """Format newly-detected arbitrage opportunities as a Discord alert."""
    if not opportunities:
        return ""
    label = "Opportunity" if len(opportunities) == 1 else "Opportunities"
    lines = [f"**\U0001F512 New Arbitrage {label} ({len(opportunities)})**", ""]
    for o in opportunities:
        lines.append(_arbitrage_line(o))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_middle_alert(opportunities: list[dict[str, Any]]) -> str:
    """Format newly-detected middle opportunities as a Discord alert."""
    if not opportunities:
        return ""
    label = "Opportunity" if len(opportunities) == 1 else "Opportunities"
    lines = [f"**\U0001F3AF New Middle {label} ({len(opportunities)})**", ""]
    for o in opportunities:
        lines.append(_middle_line(o))
        lines.append("")
    return "\n".join(lines).rstrip()


def _results_summary_line(label: str, stats: dict[str, Any], *, include_pushes: bool) -> str:
    """One 'Today: 12-3-1 | +4.25u' style line. EV picks get a real
    win/loss/push/void record (from market_settlements); arbitrage and
    middles get a profitable/unprofitable record instead (see
    database/db_manager.py::_profit_based_results_summary's docstring
    for why neither has a natural single WIN/LOSS tag)."""
    profit = stats.get("profit_units", 0.0)
    profit_str = f"{profit:+.2f}u"
    if include_pushes:
        record = f"{stats['wins']}-{stats['losses']}-{stats['pushes']}"
        if stats.get("voids"):
            record += f" ({stats['voids']} void)"
    else:
        record = f"{stats['wins']}-{stats['losses']}"
    return f"{label}: {record} | {profit_str}"


def format_daily_results_summary(
    ev_today: dict[str, Any], ev_all_time: dict[str, Any],
    arb_today: dict[str, Any], arb_all_time: dict[str, Any],
    mid_today: dict[str, Any], mid_all_time: dict[str, Any],
    *, date_label: str,
) -> str:
    """One end-of-day message with today's and all-time record + profit
    (in units) for every category actually delivered to Discord -- EV
    picks, arbitrage, middles. See database/db_manager.py's
    get_*_results_summary functions for exactly what "a bet made in
    that section" means (literally what was posted there)."""
    lines = [f"**\U0001F4CA Daily Results Summary — {date_label}**", ""]

    lines.append("**\U0001F4B0 EV Picks**")
    lines.append(_results_summary_line("Today", ev_today, include_pushes=True))
    lines.append(_results_summary_line("All-Time", ev_all_time, include_pushes=True))
    lines.append("")

    lines.append("**\U0001F512 Arbitrage**")
    lines.append(_results_summary_line("Today", arb_today, include_pushes=False))
    lines.append(_results_summary_line("All-Time", arb_all_time, include_pushes=False))
    lines.append("")

    lines.append("**\U0001F3AF Middles**")
    lines.append(_results_summary_line("Today", mid_today, include_pushes=False))
    lines.append(_results_summary_line("All-Time", mid_all_time, include_pushes=False))

    return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────

def _american(price: Any) -> str:
    """Format a price as an American-odds string, or '?' if missing."""
    if price is None:
        return "?"
    return f"+{price}" if price > 0 else str(price)


def _league_prefix(o: dict[str, Any]) -> str:
    """"[MLB] " style prefix from whichever of league/sport is present,
    or "" when neither is -- both arbitrage_opportunities and
    middle_opportunities carry a league column, sport as a secondary
    fallback (see database/db_manager.py)."""
    league = o.get("league") or o.get("sport")
    return f"[{league}] " if league else ""


def _arbitrage_line(o: dict[str, Any]) -> str:
    player = o.get("player_name") or "?"
    market = (o.get("market_type") or "?").replace("_", " ").title()
    matchup = o.get("matchup")
    roi = o.get("guaranteed_roi_pct")
    roi_str = f"{roi:+.2f}%" if roi is not None else "?"
    header = f"**{_league_prefix(o)}{player}** — {market}" + (f" ({matchup})" if matchup else "")
    return (
        f"{header}\n"
        f"  {o.get('side_a', '?')} {_american(o.get('side_a_price'))} @ **{o.get('side_a_sportsbook', '?')}**"
        f"  vs  {o.get('side_b', '?')} {_american(o.get('side_b_price'))} @ **{o.get('side_b_sportsbook', '?')}**\n"
        f"  Guaranteed ROI: {roi_str}"
    )


_VERDICT_LABELS = {
    "WORTH_IT": "✅ WORTH IT",
    "NOT_WORTH_IT": "❌ NOT WORTH IT",
    "UNKNOWN": "❓ UNKNOWN (not enough data)",
}


def _middle_line(o: dict[str, Any]) -> str:
    player = o.get("player_name") or "?"
    market = (o.get("market_type") or "?").replace("_", " ").title()
    matchup = o.get("matchup")
    best = o.get("best_case_roi_pct")
    worst = o.get("worst_case_roi_pct")
    best_str = f"{best:+.2f}%" if best is not None else "?"
    worst_str = f"{worst:+.2f}%" if worst is not None else "?"
    verdict_label = _VERDICT_LABELS.get(o.get("verdict"), _VERDICT_LABELS["UNKNOWN"])
    header = f"**[{verdict_label}]** {_league_prefix(o)}{player} — {market}" + (f" ({matchup})" if matchup else "")
    lines = [
        header,
        f"  Over {o.get('over_line', '?')} @ **{o.get('over_sportsbook', '?')}** ({_american(o.get('over_price'))})"
        f"  /  Under {o.get('under_line', '?')} @ **{o.get('under_sportsbook', '?')}** ({_american(o.get('under_price'))})",
        f"  Best case: {best_str} | Worst case: {worst_str}",
        _middle_stake_line(o),
    ]
    hit_line = _middle_hit_probability_line(o)
    if hit_line:
        lines.append(hit_line)
    return "\n".join(lines)


def _middle_stake_line(o: dict[str, Any]) -> str:
    """One unmissable, bolded line stating exactly how much to bet
    before this settles -- same "Stake: X.XXu" phrasing the customer
    site's main EV-pick cards use (see src/customer_view.py's
    _render_pick_card), so it reads the same way everywhere. Only a
    WORTH_IT verdict ever gets a number -- recommended_stake_units is
    None (not 0) for anything else, so this can never be misread as "a
    real recommendation of size zero.\""""
    stake = o.get("recommended_stake_units")
    if o.get("verdict") == "WORTH_IT" and stake:
        return f"  **STAKE: {stake:.2f} units**"
    return "  **STAKE: — (not worth betting)**"


def _middle_hit_probability_line(o: dict[str, Any]) -> str:
    """Devigged hit probability + true EV, for context on WHY the stake
    line says what it says -- see src/middling.py::estimate_middle_hit_
    probability. Best/worst-case ROI alone don't say how likely the
    window is to hit, which is what this line adds."""
    hit_prob = o.get("hit_probability")
    true_ev = o.get("true_ev_pct")
    if hit_prob is None or true_ev is None:
        return "  Hit probability: not estimable (thin alt-line data) — treat this one with caution"
    return f"  Est. hit chance: {hit_prob * 100:.1f}% | True EV: {true_ev:+.2f}%"


def _compact_line(rec: dict[str, Any]) -> str:
    """One-line summary for a recommendation."""
    player = rec.get("player_name", "?")
    market = rec.get("market_type", "?").replace("_", " ").title()
    book = rec.get("sportsbook", "?")
    odds = rec.get("offered_american_odds", 0)
    odds_str = f"+{odds}" if odds > 0 else str(odds)
    ev = rec.get("ev_pct")
    ev_str = f" ({ev:+.2f}% EV)" if ev is not None else ""
    pa = rec.get("yn_implied_prob_adv") or rec.get("price_advantage_pct")
    pa_str = f" ({pa:+.2f} pp adv)" if pa is not None and ev is None else ""
    return f"  {player} — {market} — {book} {odds_str}{ev_str}{pa_str}"


def _confidence_label(score: float) -> str:
    """Map numeric confidence score to a human label."""
    if score >= 80:
        return "Very High"
    if score >= 60:
        return "High"
    if score >= 40:
        return "Medium"
    if score >= 20:
        return "Low"
    return "Very Low"
