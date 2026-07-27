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
        Recommendation dict with keys from the DB schema:
        player_name, event_name, market_type, sportsbook, line,
        offered_american_odds, status, ev_pct (optional),
        price_advantage_pct (optional), confidence_score (optional),
        rec_status, recommendation_fingerprint, etc.
    """
    lines = []

    # Header
    player = rec.get("player_name", "Unknown")
    event = rec.get("event_name", "")
    market = rec.get("market_type", "unknown")
    lines.append(f"**{player}** — {market.replace('_', ' ').title()}")
    if event:
        lines.append(f"_{event}_")

    # Core details
    book = rec.get("sportsbook", "Unknown")
    odds = rec.get("offered_american_odds", 0)
    odds_str = f"+{odds}" if odds > 0 else str(odds)

    line_val = rec.get("line")
    side = rec.get("side", "")
    period = rec.get("period", "")

    if line_val is not None:
        lines.append(f"Line: {line_val} ({side})" if side else f"Line: {line_val}")
    if period:
        lines.append(f"Period: {period}")

    lines.append(f"Book: **{book}** @ {odds_str}")

    # EV or price advantage
    ev = rec.get("ev_pct")
    if ev is not None:
        lines.append(f"EV: {ev:+.2f}%")

    pa = rec.get("yn_implied_prob_adv") or rec.get("price_advantage_pct")
    if pa is not None:
        lines.append(f"Price Advantage: {pa:+.2f} pp")

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

    # Group by status
    strong = [r for r in recs if r.get("rec_status") == "BET"]
    positive = [r for r in recs if r.get("rec_status") == "LEAN"]
    monitor = [r for r in recs if r.get("rec_status") == "MONITOR"]

    if strong:
        lines.append(f"**BET ({len(strong)})**")
        for r in strong:
            lines.append(_compact_line(r))
        lines.append("")

    if positive:
        lines.append(f"**LEAN ({len(positive)})**")
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


# ── Helpers ────────────────────────────────────────────────────────

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
