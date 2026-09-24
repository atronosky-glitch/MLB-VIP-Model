"""Exact parsing of Polymarket US game-level moneyline markets. Pure: no
network, no I/O.

Verified against real, live, unauthenticated Polymarket US reads
(gateway.polymarket.us ``GET /v1/markets?sportsMarketType=moneyline``,
2026-09-23; 1,500 markets; fixture
tests/fixtures/polymarket_us_moneyline_markets.json holds real payloads).
Observed structure of ONE game = ONE market:

  slug          "aec-nfl-lac-ten-2025-11-02"   (away-home order in the slug)
  marketSides   exactly two entries sharing the market slug as identifier;
                the one with ``long: true`` is the YES side and carries
                ``team{name, league, ordering}`` -- so which TEAM is YES is
                stated by the payload, not inferred. Across all 1,487
                team-sport markets observed the YES side was the away team
                (cbb/cfb/nba/nfl/nhl); UFC fights are the only home-YES
                case. This module reads the payload instead of relying on
                that regularity, so a home-YES team market would map
                correctly (or fail closed), never silently flip.
  gameStartTime the scheduled start (UTC ISO)
  active/closed booleans

Scope: MLB / NFL / WNBA moneyline only (the leagues with verified team
identity tables in src/execution/team_codes.py). Every other league
(nba, nhl, cbb, cfb, ufc, ...), sports market types other than
``moneyline`` (no spread/total shape has been observed), futures and
props are UNSUPPORTED and return (None, reason) -- never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.execution.kalshi_mapping import EASTERN
from src.execution.team_codes import TEAM_TABLES, canonical_team_name


@dataclass(frozen=True)
class PolymarketContract:
    slug: str
    league: str            # "MLB" | "NFL" | "WNBA"
    team_a: str            # canonical names (payload side order; orientation not asserted)
    team_b: str
    yes_team: str          # team of the ``long`` (YES) side
    no_team: str           # team of the other (NO) side
    event_start_time: datetime
    event_date: date       # US Eastern calendar date of the start


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_polymarket_moneyline(raw: dict[str, Any] | None) -> tuple[PolymarketContract | None, str]:
    """Parse one raw Polymarket US market dict into a verified two-team
    moneyline contract, or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "not_a_market"
    slug = raw.get("slug") or ""
    if raw.get("sportsMarketType") != "moneyline" or raw.get("marketType") != "moneyline":
        return None, "not_moneyline"
    if raw.get("active") is not True or raw.get("closed") is not False:
        return None, "market_not_open"

    sides = raw.get("marketSides")
    if not isinstance(sides, list) or len(sides) != 2 or not all(isinstance(s, dict) for s in sides):
        return None, "unexpected_market_sides"
    if sorted(bool(s.get("long")) for s in sides) != [False, True]:
        return None, "unexpected_market_sides"
    if any(s.get("identifier") != slug for s in sides):
        return None, "side_identifier_mismatch"

    teams = [s.get("team") for s in sides]
    if not all(isinstance(t, dict) and t.get("name") and t.get("league") for t in teams):
        return None, "missing_team"
    leagues = {str(t["league"]).upper() for t in teams}
    if len(leagues) != 1:
        return None, "league_mismatch"
    league = next(iter(leagues))
    if league not in TEAM_TABLES:
        return None, "unsupported_league"
    # slug is "{prefix}-{league}-..." -- the league token must agree
    slug_parts = slug.split("-")
    if len(slug_parts) < 2 or slug_parts[1].upper() != league:
        return None, "slug_league_mismatch"

    names = [canonical_team_name(league, t["name"]) for t in teams]
    if None in names or names[0] == names[1]:
        return None, "unknown_team"

    start = _parse_iso(raw.get("gameStartTime"))
    if start is None or EASTERN is None:
        return None, "unknown_start_time"

    yes_index = 0 if sides[0].get("long") else 1
    return PolymarketContract(
        slug=slug, league=league,
        team_a=names[0], team_b=names[1],
        yes_team=names[yes_index], no_team=names[1 - yes_index],
        event_start_time=start.astimezone(timezone.utc),
        event_date=start.astimezone(EASTERN).date(),
    ), "ok"
