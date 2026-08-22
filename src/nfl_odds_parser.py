"""Parse The Odds API's NFL game-odds response into the platform's
generic odds-row schema.

Thin NFL-specific wrapper around the sport-agnostic
``src.odds_api_game_parser.parse_game_odds`` — see that module's
docstring for why the row-building logic itself isn't duplicated here.

Unlike MLB (which uses baseball's "run line" naming), NFL's registered
spread market already uses the same ``game_spread_ou`` name The Odds API
convention defaults to (see ``src/sports/nfl.py::GAME_SPREAD``) — this
module still exists as its own file, rather than reusing WNBA's map
directly, so NFL's naming can diverge independently if it ever needs to
without touching an unrelated sport's module.
"""

from __future__ import annotations

from .odds_api_game_parser import ParsedGameOddsResult, parse_game_odds

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


def parse_nfl_game_odds(games: list[dict]) -> ParsedGameOddsResult:
    """Flatten The Odds API's NFL game-odds response (h2h/spreads/totals)."""
    return parse_game_odds(games, market_type_map=_MARKET_TYPE, display_name_map=_DISPLAY_NAME)
