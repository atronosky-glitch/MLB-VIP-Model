"""NFL league adapter.

Registry built 2026-08-19 from two live, verified sources against the
production SportsGameOdds v2 API (never guessed):

1. ``GET /markets?leagueID=NFL`` — the provider's full market catalog for
   NFL (339 market rows / 171 market groups), including each group's
   ``activeEvents`` count (recent real usage) and per-bookmaker support.
2. ``GET /events?leagueID=NFL`` — a live 10-event sample, to confirm the
   real oddID/event/odds shape matches MLB's exactly (it does — same
   ``eventID``/``teams``/``odds``/``byBookmaker`` schema, same
   ``{statID}-{entityID}-{periodID}-{betTypeID}-{sideID}`` oddID grammar).

Only markets with real observed liquidity (nonzero ``activeEvents`` and
multiple supporting bookmakers) are registered here — see
``PARTIALLY_SUPPORTED_MARKETS`` below for catalog entries the provider
lists but that don't yet have enough live coverage to trust, and
``docs/MARKET_CAPABILITY.md`` for the full audit. This mirrors MLB's own
Phase 17C market rationalization (24 markets kept from a larger catalog
based on real liquidity, not the theoretical maximum).
"""

from __future__ import annotations

from src.sports.base import MarketConfig

LEAGUE_ID = "NFL"
SPORT = "football"
AVAILABLE = True
UNAVAILABLE_REASON = None

# ── Game markets ────────────────────────────────────────────────────

GAME_MONEYLINE = MarketConfig(
    cli_name="moneyline",
    odd_id_stat_prefix="points",
    market_type_ou="game_moneyline",
    market_type_yn=None,
    display_name="Moneyline",
    short_label="ML",
    period="game",
    scanner_title="NFL MONEYLINE EDGE SCANNER",
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
    scanner_title="NFL SPREAD EDGE SCANNER",
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
    scanner_title="NFL GAME TOTAL EDGE SCANNER",
    entity=("all",),
    supports_yn=False,
    game_level=True,
)

# ── Player props ────────────────────────────────────────────────────
# statID / activeEvents (2026-08-19 preseason snapshot) / bookmaker depth:

PASSING_YARDS = MarketConfig(
    cli_name="passing_yards",
    odd_id_stat_prefix="passing_yards",
    market_type_ou="passing_yards_ou",
    market_type_yn=None,
    display_name="Passing Yards",
    short_label="PassYd",
    period="game",
    scanner_title="NFL PASSING YARDS EDGE SCANNER",
    supports_yn=False,
)

PASSING_TOUCHDOWNS = MarketConfig(
    cli_name="passing_touchdowns",
    odd_id_stat_prefix="passing_touchdowns",
    market_type_ou="passing_touchdowns_ou",
    market_type_yn=None,
    display_name="Passing Touchdowns",
    short_label="PassTD",
    period="game",
    scanner_title="NFL PASSING TOUCHDOWNS EDGE SCANNER",
    supports_yn=False,
)

PASSING_INTERCEPTIONS = MarketConfig(
    cli_name="passing_interceptions",
    odd_id_stat_prefix="passing_interceptions",
    market_type_ou="passing_interceptions_ou",
    market_type_yn="passing_interceptions_yn",
    display_name="Passing Interceptions",
    short_label="INT",
    period="game",
    scanner_title="NFL PASSING INTERCEPTIONS EDGE SCANNER",
)

RUSHING_YARDS = MarketConfig(
    cli_name="rushing_yards",
    odd_id_stat_prefix="rushing_yards",
    market_type_ou="rushing_yards_ou",
    market_type_yn=None,
    display_name="Rushing Yards",
    short_label="RushYd",
    period="game",
    scanner_title="NFL RUSHING YARDS EDGE SCANNER",
    supports_yn=False,
)

RECEIVING_YARDS = MarketConfig(
    cli_name="receiving_yards",
    odd_id_stat_prefix="receiving_yards",
    market_type_ou="receiving_yards_ou",
    market_type_yn=None,
    display_name="Receiving Yards",
    short_label="RecYd",
    period="game",
    scanner_title="NFL RECEIVING YARDS EDGE SCANNER",
    supports_yn=False,
)

RECEIVING_RECEPTIONS = MarketConfig(
    cli_name="receptions",
    odd_id_stat_prefix="receiving_receptions",
    market_type_ou="receiving_receptions_ou",
    market_type_yn=None,
    display_name="Receptions",
    short_label="Rec",
    period="game",
    scanner_title="NFL RECEPTIONS EDGE SCANNER",
    supports_yn=False,
)

ANYTIME_TOUCHDOWN = MarketConfig(
    cli_name="anytime_touchdown",
    odd_id_stat_prefix="touchdowns",
    market_type_ou="anytime_touchdown_ou",
    market_type_yn="anytime_touchdown_yn",
    display_name="Anytime Touchdown",
    short_label="TD",
    period="game",
    scanner_title="NFL ANYTIME TOUCHDOWN EDGE SCANNER",
)

FIELD_GOALS_MADE = MarketConfig(
    cli_name="field_goals_made",
    odd_id_stat_prefix="fieldGoals_made",
    market_type_ou="field_goals_made_ou",
    market_type_yn=None,
    display_name="Field Goals Made",
    short_label="FG",
    period="game",
    scanner_title="NFL FIELD GOALS MADE EDGE SCANNER",
    supports_yn=False,
)

MARKET_REGISTRY: list[MarketConfig] = [
    GAME_MONEYLINE,
    GAME_SPREAD,
    GAME_TOTAL,
    PASSING_YARDS,
    PASSING_TOUCHDOWNS,
    PASSING_INTERCEPTIONS,
    RUSHING_YARDS,
    RECEIVING_YARDS,
    RECEIVING_RECEPTIONS,
    ANYTIME_TOUCHDOWN,
    FIELD_GOALS_MADE,
]

# Provider catalog includes these (confirmed via /markets), but as of the
# 2026-08-19 audit they had zero or near-zero activeEvents / too few
# supporting bookmakers to trust for LOO consensus (MIN_COMPARISON_BOOKS=4).
# Not registered — do not add without re-verifying live liquidity first,
# per "never fake support for a market."
PARTIALLY_SUPPORTED_MARKETS = [
    "passing_attempts", "passing_completions", "passing_longestCompletion",
    "rushing_attempts", "rushing_longestRush", "rushing_touchdowns",
    "receiving_touchdowns", "receiving_longestReception",
    "kicking_totalPoints", "extraPoints_kicksMade",
    "defense_sacks", "defense_interceptions", "defense_soloTackles",
    "defense_assistedTackles", "defense_combinedTackles",
    "passing+rushing_yards", "rushing+receiving_yards",
    "fantasyScore", "turnovers", "firstTouchdown", "lastTouchdown",
]
# 1st-half/1st-quarter/2nd-half game markets (spread/total/ML) are also in
# the provider catalog with real liquidity but are not yet wired into this
# registry — a reasonable near-term expansion, not a data-availability gap.


def get_market_registry() -> list[MarketConfig]:
    return MARKET_REGISTRY


def get_settlement_module():
    """Return the module with ingest_results_for_recommendations() for NFL."""
    from src import nfl_results
    return nfl_results
