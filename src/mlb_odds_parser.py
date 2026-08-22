"""Parse The Odds API's MLB game-odds response into the platform's
generic odds-row schema.

Thin MLB-specific wrapper around the sport-agnostic
``src.odds_api_game_parser.parse_game_odds`` — see that module's
docstring for why the row-building logic itself isn't duplicated here.

Only the market-type naming differs from WNBA: MLB's registered game
markets use baseball's "run line" convention (``game_runline_ou``, see
``src/prop_config.py::GAME_RUN_LINE``) rather than WNBA's generic
``game_spread_ou`` — both come from the same ``spreads`` market key on
The Odds API's side, this is purely this platform's own internal naming.

Live-verified 2026-08-22 against a real MLB slate: real events (e.g.
Toronto Blue Jays @ New York Yankees), real bookmakers (fanduel, lowvig,
betonlineag, mybookieag, draftkings, bovada, betrivers, betus, betmgm).
"""

from __future__ import annotations

from .odds_api_game_parser import ParsedGameOddsResult, parse_game_odds

# Same h2h/spreads/totals market keys The Odds API uses for every sport;
# only the internal market_type strings differ, matching MLB's existing
# MARKET_REGISTRY entries in src/prop_config.py (GAME_MONEYLINE,
# GAME_RUN_LINE, GAME_TOTAL) so the shared, sport-agnostic settlement
# path (src/game_settlement.py) grades these identically to SportsGameOdds-
# sourced MLB game recommendations, no new settlement code needed.
_MARKET_TYPE = {
    "h2h": "game_moneyline",
    "spreads": "game_runline_ou",
    "totals": "game_total_ou",
}
_DISPLAY_NAME = {
    "h2h": "Moneyline",
    "spreads": "Run Line",
    "totals": "Game Total",
}


def parse_mlb_game_odds(games: list[dict]) -> ParsedGameOddsResult:
    """Flatten The Odds API's MLB game-odds response (h2h/spreads/totals)."""
    return parse_game_odds(games, market_type_map=_MARKET_TYPE, display_name_map=_DISPLAY_NAME)
