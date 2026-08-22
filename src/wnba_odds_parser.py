"""Parse The Odds API's WNBA response into the platform's generic odds-row schema.

The Odds API's wire format is fundamentally different from SportsGameOdds's
oddID grammar (no composed ID string to pattern-match — nested
``bookmakers[].markets[].outcomes[]`` objects instead), so this is a
separate parser rather than an extension of ``player_prop_parser.py``. It
produces rows in the exact same schema
(``event_id``/``sportsbook``/``player_id``/``player_name``/``team_id``/
``team_name``/``market_type``/``market_group_key``/``side``/``line``/
``price``/``decimal_odds``/``is_alt_line``/``available``/
``validation_status``/``mapping_confidence``/``mapping_method``/
``validation_reason``/``captured_at``/``observation_time``), so it flows
through the existing generic scanner/analysis pipeline (``player_prop_scanner.py``,
``player_prop_analysis.py``) completely unchanged.

Game markets (moneyline/spread/total via ``h2h``/``spreads``/``totals``)
and player props (points/rebounds/assists/threes and their PA/PR/RA/PRA
combos — the 8 markets verified live 2026-08-19 to be genuine two-sided
Over/Under props; see docs/MARKET_CAPABILITY.md for what else the
provider offers but isn't registered). Props route every name through
``src/player_identity.py`` before becoming a row — a player who cannot be
resolved with HIGH/MEDIUM confidence against the game's actual rosters is
excluded, never guessed.
"""

from __future__ import annotations

from .player_identity import ESPNRosterClient
from .odds_api_game_parser import ParsedGameOddsResult, parse_game_odds
from .odds_api_props_parser import ParsedPropsResult, parse_player_props

# Verified live 2026-08-19 against a real WNBA event: all 8 are genuine
# two-sided (Over/Under, outcome.description = player name) props.
_PROP_MARKET_TYPE = {
    "player_points": "player_points_ou",
    "player_rebounds": "player_rebounds_ou",
    "player_assists": "player_assists_ou",
    "player_threes": "player_threes_ou",
    "player_points_assists": "player_points_assists_ou",
    "player_points_rebounds": "player_points_rebounds_ou",
    "player_rebounds_assists": "player_rebounds_assists_ou",
    "player_points_rebounds_assists": "player_points_rebounds_assists_ou",
}
PROP_MARKET_KEYS = ",".join(_PROP_MARKET_TYPE)

# statID-equivalent market_type naming, consistent with src/sports/nfl.py's
# convention (same DB market_type shape across every league/provider).
_MARKET_TYPE = {
    "h2h": "game_moneyline",
    "spreads": "game_spread_ou",
    "totals": "game_total_ou",
}
_DISPLAY_NAME = {
    "h2h": "Moneyline",
    "spreads": "Spread",
    "totals": "Game Total",
}


def parse_wnba_game_odds(games: list[dict]) -> ParsedGameOddsResult:
    """Flatten The Odds API's WNBA game-odds response (h2h/spreads/totals).

    Thin WNBA-specific wrapper around the sport-agnostic
    ``src.odds_api_game_parser.parse_game_odds`` — the row-building logic
    was never actually WNBA-specific, only the market-type naming was, so
    it moved to a shared module rather than being duplicated for MLB
    (which uses the exact same provider/wire shape, just baseball's
    "run line" naming instead of a generic "spread").
    """
    return parse_game_odds(games, market_type_map=_MARKET_TYPE, display_name_map=_DISPLAY_NAME)


# ==================================================================
# Player props — routed through canonical player identity resolution
# ==================================================================

def parse_wnba_player_props(
    event_odds_responses: list[dict],
    *,
    conn,
    roster_client: ESPNRosterClient | None = None,
) -> ParsedPropsResult:
    """Flatten per-event player-prop responses from
    ``OddsAPIClient.get_event_odds()`` into generic odds rows.

    Thin WNBA-specific wrapper around the sport-agnostic
    ``src.odds_api_props_parser.parse_player_props`` — the row-building
    and identity-resolution logic was never actually WNBA-specific, only
    the market-key-to-market_type mapping was, so it moved to a shared
    module the same day MLB/NFL got their own Odds-API-sourced props
    (2026-08-22), the same pattern ``parse_wnba_game_odds`` already used
    for game markets.
    """
    return parse_player_props(
        event_odds_responses, conn=conn, league="WNBA",
        prop_market_type_map=_PROP_MARKET_TYPE, roster_client=roster_client,
    )
