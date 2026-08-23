"""Parse The Odds API's MLB player-prop response — a second, supplemental
data source alongside MLB's primary provider (SportsGameOdds), added
2026-08-22 after the operator asked to put the shared Odds-API budget
(upgraded that same day to 20,000 credits/month, mostly idle) to use.

Verified live 2026-08-22 against a real upcoming MLB game (Athletics @
Houston Astros): 13 of 14 candidate market keys returned real bookmaker
data. ``batter_rbis``/``batter_runs_scored``/``batter_walks``/
``batter_stolen_bases``/``pitcher_hits_allowed``/``pitcher_earned_runs``/
``pitcher_walks``/``pitcher_record_a_win`` all returned real data too but
with only 1-3 books on that single sample — thin, not registered without
a broader liquidity check first (same "never fake support" standard
NFL's own market catalog audit used).

Registered markets updated 2026-08-23: swapped ``batter_hits`` out for
``batter_home_runs``. Reason: Pinnacle's real feed (see
``src/pinnacle_feed.py``) only prices 6 fixed MLB stat types, and batter
hits isn't one of them — batter_hits could have real Odds-API book depth
(4 books observed) but could NEVER become an Official pick, since Gate 9
requires a Pinnacle match for O/U markets. batter_home_runs IS one of
Pinnacle's 6 covered stats (confirmed live), even though its own
Odds-API book depth was thinner (1 book) on the same sample — a real
path to Official beats better raw liquidity that can never qualify. The
lowered book-count gate (docs/DECISIONS.md "Book-count gate lowered to
the LOO floor") makes a thin market usable again anyway.

Each registered market reuses MLB's EXISTING market_type string from its
primary SportsGameOdds registry (src/prop_config.py) rather than
inventing a new one, so the existing settlement contract
(src/mlb_results.py) applies automatically — confirmed all 4 are already
in AUTO_SETTLEABLE_MARKET_TYPES.
"""

from __future__ import annotations

from .odds_api_props_parser import ParsedPropsResult, parse_player_props
from .player_identity import ESPNRosterClient

# Odds-API market key -> this platform's canonical market_type (matches
# src/prop_config.py's PITCHER_STRIKEOUTS/PITCHER_OUTS/BATTER_HOME_RUNS/
# BATTER_TOTAL_BASES exactly). All 4 are confirmed on Pinnacle's real
# covered-market list (src/prop_config.py::PINNACLE_PROP_UNITS_BY_LEAGUE)
# so every one of them has a real path to Official status, not just
# Discovery/Research.
_PROP_MARKET_TYPE = {
    "batter_home_runs": "batting_homeRuns_ou",
    "batter_total_bases": "batting_totalBases_ou",
    "pitcher_strikeouts": "pitching_strikeouts_ou",
    "pitcher_outs": "pitching_outs_ou",
}
PROP_MARKET_KEYS = ",".join(_PROP_MARKET_TYPE)


def parse_mlb_player_props(
    event_odds_responses: list[dict],
    *,
    conn,
    roster_client: ESPNRosterClient | None = None,
) -> ParsedPropsResult:
    """Thin MLB-specific wrapper around the sport-agnostic
    ``src.odds_api_props_parser.parse_player_props``."""
    return parse_player_props(
        event_odds_responses, conn=conn, league="MLB",
        prop_market_type_map=_PROP_MARKET_TYPE, roster_client=roster_client,
    )
