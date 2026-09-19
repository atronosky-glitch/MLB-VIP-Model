"""College Football (NCAAF) league adapter.

Game-level markets only — no player props (2026-09-19 operator
decision: "u cant even bet props so it would j be moneylines spreads
over unders and like only a few more bets maybe over or under team
totals"). Verified live against the production SportsGameOdds v2 API
before building this, never guessed:

1. ``GET /leagues`` — confirmed ``leagueID: NCAAF`` is present and
   ``enabled: true`` on the account's CURRENT plan (unlike WNBA, which
   needed a paid-tier upgrade — NCAAF was already one of the free
   Amateur tier's 8 leagues, so this needed no plan change at all).
2. ``GET /events?leagueID=NCAAF&startsAfter=<now>&oddsAvailable=true``
   — real upcoming games with real odds the same day this was built
   (2026-09-19), oddIDs following the exact same
   ``{statID}-{entityID}-{periodID}-{betTypeID}-{sideID}`` grammar
   MLB/NFL already use: ``points-home-game-ml-home``,
   ``points-away-game-sp-away``, ``points-all-game-ou-over``.
3. Team totals are real, live data on this same feed —
   ``points-away-game-ou-over``/``points-home-game-ou-under`` etc. —
   confirmed on a real event (Temple @ Toledo), same "ou" betTypeID as
   the overall game total but scoped to a single team's own score via
   entityID "away"/"home" instead of "all". This is also the exact
   example already documented in ``src/odds_parser.py``'s own
   module docstring (marketName "Tampa Bay Rays Runs Over/Under"), so
   the raw parsing/entity-mapping layer already understands this shape
   — registering it here is the only new step, not new architecture.
4. Results: ESPN's public scoreboard API has a working college-football
   endpoint at the same URL shape as its NFL one (see
   ``src/cfb_results.py``), confirmed live the same day.

The Odds API also lists ``americanfootball_ncaaf`` as an active sport
(confirmed live), so it's wired in as the identical quota-exhaustion
fallback MLB/NFL already use — never the primary path.
"""

from __future__ import annotations

from src.sports.base import MarketConfig

LEAGUE_ID = "NCAAF"
SPORT = "football"
AVAILABLE = True
UNAVAILABLE_REASON = None

# Same quota-exhaustion fallback pattern as MLB/NFL (see src/sports/nfl.py's
# module docstring) — SportsGameOdds remains the primary source; this is
# only reached on a real 429.
ODDS_API_SPORT_KEY = "americanfootball_ncaaf"

# ── Game markets only — no player props (operator decision, see module
# docstring) ─────────────────────────────────────────────────────────

GAME_MONEYLINE = MarketConfig(
    cli_name="moneyline",
    odd_id_stat_prefix="points",
    market_type_ou="game_moneyline",
    market_type_yn=None,
    display_name="Moneyline",
    short_label="ML",
    period="game",
    scanner_title="NCAAF MONEYLINE EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    bet_type="ml",
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

GAME_SPREAD = MarketConfig(
    cli_name="spread",
    odd_id_stat_prefix="points",
    market_type_ou="game_spread_ou",
    market_type_yn=None,
    display_name="Spread",
    short_label="SP",
    period="game",
    scanner_title="NCAAF SPREAD EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    bet_type="sp",
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

GAME_TOTAL = MarketConfig(
    cli_name="game_total",
    odd_id_stat_prefix="points",
    market_type_ou="game_total_ou",
    market_type_yn=None,
    display_name="Game Total",
    short_label="Tot",
    period="game",
    scanner_title="NCAAF GAME TOTAL EDGE SCANNER",
    entity=("all",),
    supports_yn=False,
    game_level=True,
)

# Team totals: same "ou" betTypeID and "game" period as GAME_TOTAL, just
# scoped to a single team's own score via entity ("away"/"home") instead
# of "all" — two separate MarketConfigs (not one with entity=("away",
# "home")) so away/home team totals never collide into the same O/U
# group the way GAME_MONEYLINE/GAME_SPREAD's away+home DO deliberately
# merge (there, away and home are the two complementary sides of ONE
# market — who wins, who covers; here, "Team A's own total" and "Team
# B's own total" are two INDEPENDENT O/U markets that happen to share a
# betTypeID, not two sides of the same bet). See src/game_settlement.py
# for the matching settlement split.
GAME_TEAM_TOTAL_AWAY = MarketConfig(
    cli_name="team_total_away",
    odd_id_stat_prefix="points",
    market_type_ou="game_team_total_away_ou",
    market_type_yn=None,
    display_name="Away Team Total",
    short_label="ATT",
    period="game",
    scanner_title="NCAAF AWAY TEAM TOTAL EDGE SCANNER",
    entity=("away",),
    supports_yn=False,
    game_level=True,
)

GAME_TEAM_TOTAL_HOME = MarketConfig(
    cli_name="team_total_home",
    odd_id_stat_prefix="points",
    market_type_ou="game_team_total_home_ou",
    market_type_yn=None,
    display_name="Home Team Total",
    short_label="HTT",
    period="game",
    scanner_title="NCAAF HOME TEAM TOTAL EDGE SCANNER",
    entity=("home",),
    supports_yn=False,
    game_level=True,
)

MARKET_REGISTRY: list[MarketConfig] = [
    GAME_MONEYLINE,
    GAME_SPREAD,
    GAME_TOTAL,
    GAME_TEAM_TOTAL_AWAY,
    GAME_TEAM_TOTAL_HOME,
]


def get_market_registry() -> list[MarketConfig]:
    return MARKET_REGISTRY


def get_settlement_module():
    """Return the module with ingest_results_for_recommendations() for CFB."""
    from src import cfb_results
    return cfb_results


def fetch_game_odds_via_odds_api(
    event_id: str | None = None, conn=None,
) -> tuple[list[dict], list[dict], list[dict], bool]:
    """Fetch live CFB game odds (moneyline/spread/total) via The Odds API
    — the SportsGameOdds quota-exhaustion fallback, not the primary path
    (see module docstring). Same return shape as
    ``src.sports.nfl.fetch_game_odds_via_odds_api()``.

    Team totals are NOT available through this fallback path — The Odds
    API's h2h/spreads/totals markets don't include a per-team total
    market (confirmed against its own market list); a fallback run
    simply won't produce team-total recommendations that pass, same as
    any other data gap this codebase treats as "nothing found" rather
    than guessed.
    """
    from src.odds_api_client import OddsAPIClient, TRACKED_BOOKMAKERS
    from src.cfb_odds_parser import parse_cfb_game_odds
    from src.odds_api_credits import credit_budget_check, GAME_ODDS_COST
    from datetime import datetime, timedelta, timezone

    if conn is not None:
        try:
            ok, reason = credit_budget_check(conn, GAME_ODDS_COST)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not run Odds API budget check for CFB fallback — proceeding", exc_info=True,
            )
            ok, reason = True, "budget check failed, proceeding"
        if not ok:
            raise RuntimeError(
                f"CFB SportsGameOdds fallback skipped — Odds API budget "
                f"exhausted: {reason}"
            )

    client = OddsAPIClient()
    now = datetime.now(timezone.utc)
    games, from_cache = client.get_odds(
        sport_key=ODDS_API_SPORT_KEY, bookmakers=TRACKED_BOOKMAKERS, markets="h2h,spreads,totals",
        commence_time_from=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        commence_time_to=(now + timedelta(hours=42)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if conn is not None:
        from src.odds_api_credits import record_client_quota
        try:
            record_client_quota(conn, client, endpoint="odds", job_type="cfb_fallback_game_odds",
                                 cache_hit=from_cache)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not record CFB fallback odds-API credit usage", exc_info=True,
            )
    if event_id:
        games = [g for g in games if g.get("id") == event_id]

    parsed = parse_cfb_game_odds(games)

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
