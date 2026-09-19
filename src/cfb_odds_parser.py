"""Parse The Odds API's CFB (NCAAF) game-odds response into the
platform's generic odds-row schema.

Thin CFB-specific wrapper around the sport-agnostic
``src.odds_api_game_parser.parse_game_odds`` — see that module's
docstring for why the row-building logic itself isn't duplicated here.
Same market-type naming as NFL (``game_spread_ou``, not a run-line-style
name) since college football has no equivalent "run line" convention to
diverge toward.
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


def parse_cfb_game_odds(games: list[dict]) -> ParsedGameOddsResult:
    """Flatten The Odds API's CFB game-odds response (h2h/spreads/totals).

    No team-total mapping here — The Odds API's h2h/spreads/totals
    markets don't include a per-team total (see
    src.sports.cfb.fetch_game_odds_via_odds_api's docstring); this
    fallback path only ever produces the three markets it actually has.
    """
    return parse_game_odds(games, market_type_map=_MARKET_TYPE, display_name_map=_DISPLAY_NAME)
