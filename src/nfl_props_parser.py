"""Parse The Odds API's NFL player-prop response — a second, supplemental
data source alongside NFL's primary provider (SportsGameOdds), added
2026-08-22 after the operator asked to put the shared Odds-API budget
(upgraded that same day to 20,000 credits/month, mostly idle) to use.

Verified live 2026-08-22 against the earliest real NFL event on the
account (Patriots @ Seahawks, 2026-09-10 — 19 days out at verification
time, so book depth was thin/early; expected to deepen closer to
kickoff, same as any NFL line). Confirmed genuinely two-sided
Over/Under: ``player_pass_yds``, ``player_pass_tds``, ``player_rush_yds``,
``player_receptions``, ``player_reception_yds``. Confirmed present but
NOT usable by this generic O/U parser: ``player_anytime_td`` is a
single-sided "Yes" market (real outcome shape: ``{"name": "Yes",
"description": "<player>", "price": N}``, no Under side) — deliberately
not registered, same "different market shape" reason WNBA's
first-basket/double-double/triple-double and NFL's own SportsGameOdds
catalog's non-standard markets were left out.

Only 4 of the 5 confirmed O/U markets are registered here, matching
MLB's props registry size; re-verify liquidity closer to the regular
season (2026-09-10) before trusting these book counts for real
qualification thresholds — 19-days-out data is a availability check,
not a liquidity vote.

Each registered market reuses NFL's EXISTING market_type string from its
primary SportsGameOdds registry (src/sports/nfl.py) rather than
inventing a new one, so the existing settlement contract
(src/nfl_results.py) applies automatically — confirmed all 4 are already
in its market-type field mapping.
"""

from __future__ import annotations

from .odds_api_props_parser import ParsedPropsResult, parse_player_props
from .player_identity import ESPNRosterClient

# Odds-API market key -> this platform's canonical market_type (matches
# src/sports/nfl.py's PASSING_YARDS/RUSHING_YARDS/RECEIVING_RECEPTIONS/
# RECEIVING_YARDS exactly).
_PROP_MARKET_TYPE = {
    "player_pass_yds": "passing_yards_ou",
    "player_rush_yds": "rushing_yards_ou",
    "player_receptions": "receiving_receptions_ou",
    "player_reception_yds": "receiving_yards_ou",
}
PROP_MARKET_KEYS = ",".join(_PROP_MARKET_TYPE)


def parse_nfl_player_props(
    event_odds_responses: list[dict],
    *,
    conn,
    roster_client: ESPNRosterClient | None = None,
) -> ParsedPropsResult:
    """Thin NFL-specific wrapper around the sport-agnostic
    ``src.odds_api_props_parser.parse_player_props``."""
    return parse_player_props(
        event_odds_responses, conn=conn, league="NFL",
        prop_market_type_map=_PROP_MARKET_TYPE, roster_client=roster_client,
    )
