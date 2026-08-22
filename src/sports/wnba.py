"""WNBA league adapter.

Verified 2026-08-19: SportsGameOdds (the primary provider used by MLB/NFL)
does not offer WNBA on any plan tier — checked both the live account
(``GET /leagues`` omits it, ``GET /events?leagueID=WNBA`` returns HTTP 400)
and their own public pricing page (free through $299/mo Pro, 53+ leagues,
still no WNBA). That is a real provider gap, not something a config change
fixes.

The Odds API (the-odds-api.com) does cover WNBA, confirmed live the same
day with a free API key (``THE_ODDS_API_KEY``): real multi-book game odds
(``GET /v4/sports/basketball_wnba/odds``, 9 bookmakers observed —
fanduel, draftkings, betmgm, bovada, betrivers, and others) and player
props (8 markets confirmed live and registered — see below). See
``src/odds_api_client.py`` and ``src/wnba_odds_parser.py`` for the
verified schema notes.

**Player props (2026-08-19)**: routed through
``src/player_identity.py``, which resolves each prop's free-text player
name against the actual two teams' real ESPN rosters before it can become
a row — a name that can't be resolved with HIGH/MEDIUM confidence is
excluded, never guessed (see AGENTS.md's "never infer participants" rule,
applied here to a provider that gives no stable player ID at all). 8
markets registered from a verified live check: points, rebounds, assists,
threes, and the PA/PR/RA/PRA combos — all confirmed genuine two-sided
Over/Under props. `player_first_basket`/`player_double_double`/
`player_triple_double` were also confirmed live but have a different
market shape (no natural two-sided price) and are deliberately not
registered — see docs/MARKET_CAPABILITY.md.

Props are billed per-event (unlike the bulk game-odds call), so they are
NOT included in the default ``fetch_and_parse()`` used by a routine scan
— see ``fetch_and_parse_props()``, an explicit opt-in path, and
docs/MARKET_CAPABILITY.md for the credit-cost math on why.

WNBA is architecturally different from MLB/NFL in one respect: it uses a
different odds provider (``ODDS_PROVIDER = "the_odds_api"``, not
SportsGameOdds), so it has its own ``fetch_and_parse()`` entry point
instead of going through ``player_prop_parser.parse_player_props()``.
``player_prop_scanner.run_scan()`` branches on this per league.
"""

from __future__ import annotations

import logging

from src.sports.base import MarketConfig

logger = logging.getLogger(__name__)

LEAGUE_ID = "WNBA"
SPORT = "basketball"
AVAILABLE = True
UNAVAILABLE_REASON = None

# WNBA uses a different odds provider than MLB/NFL — see module docstring.
ODDS_PROVIDER = "the_odds_api"
ODDS_API_SPORT_KEY = "basketball_wnba"

# The odd_id_stat_prefix/bet_type/entity fields below are vestigial for
# WNBA — The Odds API has no oddID grammar to pattern-match, so
# fetch_and_parse() bypasses match_ou_market/match_yn_market entirely and
# produces rows with market_type already set to the values below. These
# MarketConfig entries exist only so resolve_markets()/_accepted_market_types()/
# the --market CLI flag work identically to every other league.
GAME_MONEYLINE = MarketConfig(
    cli_name="moneyline",
    odd_id_stat_prefix="h2h",
    market_type_ou="game_moneyline",
    market_type_yn=None,
    display_name="Moneyline",
    short_label="ML",
    period="game",
    scanner_title="WNBA MONEYLINE EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

GAME_SPREAD = MarketConfig(
    cli_name="spread",
    odd_id_stat_prefix="spreads",
    market_type_ou="game_spread_ou",
    market_type_yn=None,
    display_name="Spread",
    short_label="SP",
    period="game",
    scanner_title="WNBA SPREAD EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

GAME_TOTAL = MarketConfig(
    cli_name="game_total",
    odd_id_stat_prefix="totals",
    market_type_ou="game_total_ou",
    market_type_yn=None,
    display_name="Game Total",
    short_label="Tot",
    period="game",
    scanner_title="WNBA GAME TOTAL EDGE SCANNER",
    entity=("all",),
    supports_yn=False,
    game_level=True,
)

# Player props — odd_id_stat_prefix/bet_type/entity are vestigial here too
# (see comment above); market_type_ou values match
# src/wnba_odds_parser.py::_PROP_MARKET_TYPE exactly, which is the only
# thing that actually routes a parsed row into these configs.
PLAYER_POINTS = MarketConfig(
    cli_name="points", odd_id_stat_prefix="player_points",
    market_type_ou="player_points_ou", market_type_yn=None,
    display_name="Points", short_label="PTS", period="game",
    scanner_title="WNBA POINTS EDGE SCANNER", supports_yn=False,
)
PLAYER_REBOUNDS = MarketConfig(
    cli_name="rebounds", odd_id_stat_prefix="player_rebounds",
    market_type_ou="player_rebounds_ou", market_type_yn=None,
    display_name="Rebounds", short_label="REB", period="game",
    scanner_title="WNBA REBOUNDS EDGE SCANNER", supports_yn=False,
)
PLAYER_ASSISTS = MarketConfig(
    cli_name="assists", odd_id_stat_prefix="player_assists",
    market_type_ou="player_assists_ou", market_type_yn=None,
    display_name="Assists", short_label="AST", period="game",
    scanner_title="WNBA ASSISTS EDGE SCANNER", supports_yn=False,
)
PLAYER_THREES = MarketConfig(
    cli_name="threes", odd_id_stat_prefix="player_threes",
    market_type_ou="player_threes_ou", market_type_yn=None,
    display_name="Three-Pointers Made", short_label="3PM", period="game",
    scanner_title="WNBA THREES EDGE SCANNER", supports_yn=False,
)
PLAYER_POINTS_ASSISTS = MarketConfig(
    cli_name="points_assists", odd_id_stat_prefix="player_points_assists",
    market_type_ou="player_points_assists_ou", market_type_yn=None,
    display_name="Points + Assists", short_label="P+A", period="game",
    scanner_title="WNBA POINTS+ASSISTS EDGE SCANNER", supports_yn=False,
)
PLAYER_POINTS_REBOUNDS = MarketConfig(
    cli_name="points_rebounds", odd_id_stat_prefix="player_points_rebounds",
    market_type_ou="player_points_rebounds_ou", market_type_yn=None,
    display_name="Points + Rebounds", short_label="P+R", period="game",
    scanner_title="WNBA POINTS+REBOUNDS EDGE SCANNER", supports_yn=False,
)
PLAYER_REBOUNDS_ASSISTS = MarketConfig(
    cli_name="rebounds_assists", odd_id_stat_prefix="player_rebounds_assists",
    market_type_ou="player_rebounds_assists_ou", market_type_yn=None,
    display_name="Rebounds + Assists", short_label="R+A", period="game",
    scanner_title="WNBA REBOUNDS+ASSISTS EDGE SCANNER", supports_yn=False,
)
PLAYER_PRA = MarketConfig(
    cli_name="pra", odd_id_stat_prefix="player_points_rebounds_assists",
    market_type_ou="player_points_rebounds_assists_ou", market_type_yn=None,
    display_name="Points + Rebounds + Assists", short_label="PRA", period="game",
    scanner_title="WNBA PRA EDGE SCANNER", supports_yn=False,
)

MARKET_REGISTRY: list[MarketConfig] = [
    GAME_MONEYLINE,
    GAME_SPREAD,
    GAME_TOTAL,
    PLAYER_POINTS,
    PLAYER_REBOUNDS,
    PLAYER_ASSISTS,
    PLAYER_THREES,
    PLAYER_POINTS_ASSISTS,
    PLAYER_POINTS_REBOUNDS,
    PLAYER_REBOUNDS_ASSISTS,
    PLAYER_PRA,
]

# Confirmed live 2026-08-19 but not registered — different market shape
# (no natural two-sided price; "who scores first" / "does X get a
# double-double" fields need their own EV-modeling design, not a simple
# Over/Under comparison).
PARTIALLY_SUPPORTED_MARKETS = [
    "player_first_basket", "player_double_double", "player_triple_double",
]


def get_market_registry() -> list[MarketConfig]:
    return MARKET_REGISTRY


def get_settlement_module():
    """ESPN-based settlement — verified live 2026-08-19 against a real
    completed game. Covers both game-level markets (moneyline/spread/total,
    via src/game_settlement.py using event_results scores) and all 8
    registered player-prop markets. See src/wnba_results.py.
    """
    from src import wnba_results
    return wnba_results


def fetch_and_parse(
    event_id: str | None = None, conn=None,
) -> tuple[list[dict], list[dict], list[dict], bool]:
    """Fetch and parse live WNBA game odds via The Odds API.

    Returns (odds_rows, audit_rows, normalized_events, from_cache), where
    normalized_events are shaped compatible with
    player_prop_scanner._build_event_map (which already accepts either
    SportsGameOdds's or this normalized shape — see its fallback field
    lookups) without needing any changes there.

    *event_id* filters to a single game client-side (The Odds API's bulk
    odds endpoint has no per-event filter param); the per-event odds
    endpoint exists for player props, not used by this game-markets-only
    path yet.

    *conn*, when given, persists the real credit-usage headers this call
    returned (src/odds_api_credits.py) — optional so pure fetch+parse
    callers (e.g. tests) don't need a database at all.
    """
    from src.odds_api_client import OddsAPIClient
    from src.wnba_odds_parser import parse_wnba_game_odds
    from datetime import datetime, timedelta, timezone

    client = OddsAPIClient()
    # Same -6h/+42h near-term window MLB/NFL's fallback paths use. Not
    # currently a live problem for WNBA specifically — verified live
    # 2026-08-22 an unbounded call here returns only ~24h of real games,
    # unlike NFL's entire-season response — but relying on "WNBA books
    # happen not to post lines far ahead" as an implicit safety net is
    # fragile, so bounding it explicitly here too rather than only where
    # it's already been caught breaking something.
    now = datetime.now(timezone.utc)
    games, from_cache = client.get_odds(
        sport_key=ODDS_API_SPORT_KEY, regions="us", markets="h2h,spreads,totals",
        commence_time_from=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        commence_time_to=(now + timedelta(hours=42)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if conn is not None:
        from src.odds_api_credits import record_client_quota
        try:
            record_client_quota(conn, client, endpoint="odds", job_type="game_odds",
                                 cache_hit=from_cache)
        except Exception:
            logger.warning("Could not record WNBA odds-API credit usage", exc_info=True)
    if event_id:
        games = [g for g in games if g.get("id") == event_id]

    parsed = parse_wnba_game_odds(games)

    normalized_events = [
        {
            "id": g.get("id"),
            "eventID": g.get("id"),
            "teams": {
                "home": {"name": g.get("home_team", "")},
                "away": {"name": g.get("away_team", "")},
            },
            "status": {"startsAt": g.get("commence_time", "")},
        }
        for g in games
    ]

    return parsed.odds_rows, parsed.audit_rows, normalized_events, from_cache


def fetch_and_parse_props(
    conn, event_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch and parse live WNBA player props via The Odds API.

    Explicit opt-in, separate from ``fetch_and_parse()`` — props are
    billed per event (verified: 8 credits per event for the 8 registered
    markets), so a full slate costs materially more than the bulk
    game-odds call. A 5-game day is ~40 credits per props fetch; still
    comfortably affordable at the real current 20,000/month tier (see
    docs/MARKET_CAPABILITY.md for the exact math and how this changed
    2026-08-22) — callers should still decide cadence deliberately rather
    than having this bundled silently into every scan.

    Thin WNBA-specific wrapper around the sport-agnostic
    ``src.odds_api_props_fetch.fetch_player_props`` — extracted 2026-08-22
    the same day MLB/NFL got their own Odds-API-sourced props, since the
    discover/dedup/per-event credit-checked fetch loop was never actually
    WNBA-specific either. Requires a DB connection (*conn*) for
    identity-resolution caching and the props scheduler's own dedup
    check — unlike fetch_and_parse(), which is pure fetch+parse.
    """
    from src.odds_api_props_fetch import fetch_player_props
    from src.wnba_odds_parser import parse_wnba_player_props, PROP_MARKET_KEYS

    return fetch_player_props(
        conn, sport_key=ODDS_API_SPORT_KEY, prop_market_keys=PROP_MARKET_KEYS,
        parse_fn=parse_wnba_player_props, league="WNBA", event_id=event_id,
    )
