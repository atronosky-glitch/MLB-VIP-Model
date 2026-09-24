"""Exact parsing of Kalshi game-level market payloads into a structured
contract identity. Pure: no network, no I/O.

Verified against real, live, unauthenticated Kalshi reads (2026-09-23) of
KX{MLB,NFL,WNBA}{GAME,SPREAD,TOTAL}. Observed structure (every field
below was checked across ALL open markets of those nine series, and the
saved fixture tests/fixtures/kalshi_game_markets.json pins real payloads):

  moneyline  series KX<LG>GAME     one market PER TEAM. ticker
             KXNFLGAME-26OCT05ATLNO-NO, strike_type "structured",
             yes_sub_title = team label, rules: "If <team> wins ...".
             YES = that team wins.
  spread     series KX<LG>SPREAD   ticker ...-PHI8, strike_type "greater",
             floor_strike 7.5 (always x.5), yes_sub_title
             "<team> wins by over 7.5 points|runs". YES = that team wins
             by MORE than floor_strike. Suffix = team code + ceil(strike).
  total      series KX<LG>TOTAL    ticker ...-66, strike_type "greater",
             floor_strike 65.5, yes_sub_title "Over 65.5 points|runs
             scored". YES = combined score MORE than floor_strike.
  event      KX<LG><KIND>-<yy><MON><dd>[HHMM]<codeA><codeB>. HHMM (MLB
             only) is the scheduled start in US Eastern; the date is the
             Eastern calendar date (a Monday-night NFL game is dated the
             Monday, though it starts on the next UTC day).

Everything else Kalshi lists for a game (period totals like 1H/F5/1Q,
team totals, player props, combo/"Associated Events" markets) is
UNSUPPORTED here: this parser only accepts the nine series above and
returns (None, reason) for anything that deviates from the observed
structure, so an unrecognized shape can never become a tradable mapping.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.execution.team_codes import (
    TEAM_TABLES, code_to_canonical_name, label_matches_code,
)

try:  # Eastern-time conversion; without tzdata we fail closed on MLB times
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - environment without tz database
    EASTERN = None

# series ticker -> (league, kind). NCAAF/CFB is intentionally absent: no
# verified team-code table, so it is unsupported for Kalshi.
SUPPORTED_SERIES: dict[str, tuple[str, str]] = {
    f"KX{league}{suffix}": (league, kind)
    for league in ("MLB", "NFL", "WNBA")
    for suffix, kind in (("GAME", "moneyline"), ("SPREAD", "spread"), ("TOTAL", "total"))
}

_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_EVENT_RE = re.compile(r"^(KX[A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z]+)$")
_SPREAD_LABEL_RE = re.compile(r"^(.+) wins by over (\d+(?:\.\d+)?) (points|runs)$")
_TOTAL_LABEL_RE = re.compile(r"^Over (\d+(?:\.\d+)?) (points|runs) scored$")


@dataclass(frozen=True)
class KalshiContract:
    """A verified Kalshi game-level binary contract."""
    market_ticker: str
    event_ticker: str
    league: str                 # "MLB" | "NFL" | "WNBA"
    kind: str                   # "moneyline" | "spread" | "total"
    team_a: str                 # canonical names of the two teams in the
    team_b: str                 # event (Kalshi ticker order; orientation not asserted)
    yes_team: str | None        # canonical team YES refers to; None for totals
    line: float | None          # floor_strike; None for moneyline
    event_date: date            # US Eastern calendar date from the ticker
    event_start_time: datetime | None  # exact UTC start (MLB only)


def _is_half_integer(value: float) -> bool:
    doubled = value * 2
    return abs(doubled - round(doubled)) < 1e-9 and round(doubled) % 2 == 1


def _split_codes(league: str, blob: str) -> tuple[str, str] | None:
    """Split "ATLNO" into two known team codes; None unless exactly one
    valid split exists."""
    table = TEAM_TABLES[league]
    splits = [
        (blob[:i], blob[i:]) for i in range(1, len(blob))
        if blob[:i] in table and blob[i:] in table and blob[:i] != blob[i:]
    ]
    return splits[0] if len(splits) == 1 else None


def parse_kalshi_contract(raw: dict[str, Any] | None) -> tuple[KalshiContract | None, str]:
    """Parse one raw Kalshi market dict. Returns (contract, "ok") or
    (None, reason) -- the reason is a stable short code used in
    diagnostics/tests, never shown as a trade signal."""
    if not isinstance(raw, dict):
        return None, "not_a_market"
    ticker = raw.get("ticker") or ""
    event_ticker = raw.get("event_ticker") or ""
    m = _EVENT_RE.match(event_ticker)
    if not m:
        return None, "unrecognized_event_ticker"
    series, yy, mon, dd, hhmm, blob = m.groups()
    if series not in SUPPORTED_SERIES:
        return None, "unsupported_series"
    league, kind = SUPPORTED_SERIES[series]
    if not ticker.startswith(event_ticker + "-"):
        return None, "ticker_event_mismatch"
    suffix = ticker[len(event_ticker) + 1:]

    if raw.get("market_type") != "binary":
        return None, "not_binary"
    if raw.get("status") not in ("active", "open"):
        return None, "market_not_active"
    if raw.get("cap_strike") is not None:
        return None, "unexpected_cap_strike"
    custom = raw.get("custom_strike")
    if kind == "total":
        if custom:
            return None, "unexpected_custom_strike"
    else:
        # Exactly one "<sport>_team" reference; anything else (e.g. the
        # "Associated Events"/"Associated Market Sides" combo shape) is
        # not a plain team contract.
        if not isinstance(custom, dict) or len(custom) != 1 or not next(iter(custom)).endswith("_team"):
            return None, "unexpected_custom_strike"

    month = _MONTHS.get(mon)
    if month is None:
        return None, "bad_event_date"
    try:
        event_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None, "bad_event_date"

    event_start = None
    if hhmm:
        if EASTERN is None:
            return None, "no_timezone_database"
        try:
            local = datetime(event_date.year, event_date.month, event_date.day,
                             int(hhmm[:2]), int(hhmm[2:]), tzinfo=EASTERN)
        except ValueError:
            return None, "bad_event_time"
        event_start = local.astimezone(timezone.utc)

    codes = _split_codes(league, blob)
    if codes is None:
        return None, "unknown_or_ambiguous_team_codes"

    label = raw.get("yes_sub_title") or ""
    floor = raw.get("floor_strike")
    strike_type = raw.get("strike_type")

    if kind == "moneyline":
        if strike_type != "structured" or floor is not None:
            return None, "unexpected_strike_shape"
        if suffix not in codes:
            return None, "team_code_not_in_event"
        if not label_matches_code(league, suffix, label):
            return None, "label_code_mismatch"
        yes_code, line = suffix, None
    elif kind == "spread":
        if strike_type != "greater" or floor is None or not _is_half_integer(float(floor)):
            return None, "unexpected_strike_shape"
        code = re.sub(r"\d+$", "", suffix)
        if code not in codes:
            return None, "team_code_not_in_event"
        if suffix != f"{code}{math.ceil(float(floor))}":
            return None, "ticker_strike_mismatch"
        lm = _SPREAD_LABEL_RE.match(label)
        if not lm or not label_matches_code(league, code, lm.group(1)) or abs(float(lm.group(2)) - float(floor)) > 1e-9:
            return None, "label_strike_mismatch"
        yes_code, line = code, float(floor)
    else:  # total
        if strike_type != "greater" or floor is None or not _is_half_integer(float(floor)):
            return None, "unexpected_strike_shape"
        if suffix != str(math.ceil(float(floor))):
            return None, "ticker_strike_mismatch"
        lm = _TOTAL_LABEL_RE.match(label)
        if not lm or abs(float(lm.group(1)) - float(floor)) > 1e-9:
            return None, "label_strike_mismatch"
        yes_code, line = None, float(floor)

    return KalshiContract(
        market_ticker=ticker,
        event_ticker=event_ticker,
        league=league,
        kind=kind,
        team_a=code_to_canonical_name(league, codes[0]),
        team_b=code_to_canonical_name(league, codes[1]),
        yes_team=code_to_canonical_name(league, yes_code) if yes_code else None,
        line=line,
        event_date=event_date,
        event_start_time=event_start,
    ), "ok"


def series_for_leagues(leagues: list[str] | None = None) -> list[str]:
    """Supported series tickers to fetch, optionally limited to leagues."""
    wanted = {l.upper() for l in leagues} if leagues else None
    return [s for s, (lg, _k) in SUPPORTED_SERIES.items() if wanted is None or lg in wanted]
