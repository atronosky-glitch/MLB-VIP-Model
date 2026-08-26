"""Centralized configuration for player prop analysis thresholds.

All thresholds are exposed as module-level constants so they can be
overridden in tests or adjusted at a single point.

Also contains the market registry — a data-driven configuration layer
that defines every supported player-prop market.  The parser and scanner
look up markets from this registry rather than hard-coding market types.
"""

from __future__ import annotations

from src.sports.base import MarketConfig, match_ou_market as _base_match_ou_market, match_yn_market as _base_match_yn_market

# ── Market quality ────────────────────────────────────────────────

MARKET_QUALITY_VALID = "VALID_MARKET"
MARKET_QUALITY_NEEDS_REVIEW = "NEEDS_REVIEW"
MARKET_QUALITY_INSUFFICIENT = "INSUFFICIENT_MARKET"
MARKET_QUALITY_EXCLUDED = "EXCLUDED"

# Minimum number of OTHER books required for a market to be VALID.
# Lowered from 4 (5 total) to 1 (2 total, the hard mathematical floor for
# Leave-One-Out consensus at all) 2026-08-22 per operator decision — see
# docs/DECISIONS.md "Book-count gate lowered to the LOO floor". The
# consensus/fair-price is still computed from every book actually present,
# however many that is; this only controls the minimum before a group is
# considered at all, not how many books must individually agree.
MIN_COMPARISON_BOOKS = 1  # meaning at least 2 paired books total
# EV is not treated as reliable unless its inputs pass this independent gate.
# Same 2026-08-22 decision — see docs/DECISIONS.md.
RELIABLE_EV_MIN_BOOKS = 1
RELIABLE_EV_TOLERANCE_PP = 0.15
RELIABLE_EV_MAX_PCT = 20.0
RELIABLE_EV_MIN_DECIMAL_ODDS = 1.05
RELIABLE_EV_MAX_DECIMAL_ODDS = 10.0

# Only markets with a verified MLB StatsAPI settlement field may reach the
# Discovery/Official tiers. Other registry markets remain research-only.
AUTO_SETTLEABLE_MARKET_TYPES = frozenset({
    "pitching_strikeouts_ou", "pitching_strikeouts_yn",
    "pitching_hits_ou", "pitching_basesOnBalls_ou", "pitching_outs_ou",
    "pitching_pitchesThrown_ou",
    "pitching_earnedRuns_ou", "pitching_earnedRuns_yn", "pitching_win_yn",
    "batting_hits_ou", "batting_hits_yn", "batting_totalBases_ou",
    "batting_homeRuns_ou", "batting_homeRuns_yn",
    "batting_RBI_ou", "batting_RBI_yn", "batting_runs_ou", "batting_runs_yn",
    "batting_singles_ou", "batting_singles_yn", "batting_doubles_ou", "batting_doubles_yn",
    "batting_triples_ou", "batting_triples_yn", "batting_basesOnBalls_ou", "batting_basesOnBalls_yn",
    "batting_strikeouts_ou", "batting_strikeouts_yn",
    "batting_stolenBases_ou", "batting_stolenBases_yn",
    "batting_hits+runs+rbi_ou", "batting_hits+runs+rbi_yn",
    "batting_runs+rbi_ou", "batting_runs+rbi_yn",
    # Game-level markets — settled generically by src/game_settlement.py
    # for every league (moneyline/spread-or-runline/total), not this
    # player-prop-oriented registry's per-stat fields. Missing here meant
    # Gate 1 in src/official_picks.py unconditionally disqualified every
    # single game-market recommendation from Official status regardless
    # of EV, book count, or Pinnacle approval (caught live 2026-08-23).
    "game_moneyline", "game_spread_ou", "game_runline_ou", "game_total_ou",
    # WNBA player props — the exact same class of oversight as the
    # game-market gap above, found live 2026-08-26 investigating "zero
    # WNBA recommendations saved for 6 straight days": src/wnba_results.py
    # has real, verified (live 2026-08-19, against a real completed game)
    # ESPN-boxscore settlement for all 8 of src/sports/wnba.py's
    # registered player-prop markets, but none of them were ever added
    # here — so every WNBA player-prop recommendation was structurally
    # disqualified from Discovery/Official regardless of EV or model
    # score. Confirmed live: 9 real actionable opportunities (up to 6.35%
    # EV, model_score up to 7.6) all collapsed to RESEARCH_ONLY solely
    # for this reason. This registers real, already-working settlement
    # coverage — it does not loosen any EV/model-score/book-count gate.
    "player_points_ou", "player_rebounds_ou", "player_assists_ou",
    "player_threes_ou", "player_points_assists_ou", "player_points_rebounds_ou",
    "player_rebounds_assists_ou", "player_points_rebounds_assists_ou",
})

# Extreme outlier: EV magnitude beyond this threshold triggers NEEDS_REVIEW
OUTLIER_EV_THRESHOLD = 0.10  # 10%


# ── Bet status thresholds (EV percentage) ─────────────────────────

BET_STATUS_STRONG = "STRONG_EDGE"
BET_STATUS_POSITIVE = "POSITIVE_EDGE"
BET_STATUS_MARGINAL = "MARGINAL_EDGE"
BET_STATUS_NO_EDGE = "NO_EDGE"
BET_STATUS_EXCLUDED = "EXCLUDED"

STRONG_EDGE_THRESHOLD = 0.05   # EV >= 5%
POSITIVE_EDGE_THRESHOLD = 0.02 # EV >= 2%
# EV > 0 and < 2% → MARGINAL
# EV <= 0 → NO_EDGE

# ── Yes/No (single-sided) comparison statuses ─────────────────────
# These replace EV-based names since no two-sided vig removal is possible.

YN_STATUS_STRONG_OUTLIER = "STRONG_PRICE_OUTLIER"
YN_STATUS_OUTLIER = "PRICE_OUTLIER"
YN_STATUS_MARGINAL_OUTLIER = "MARGINAL_PRICE_OUTLIER"
YN_STATUS_IN_LINE = "IN_LINE_WITH_MARKET"
YN_STATUS_WORSE = "WORSE_THAN_MARKET"
YN_STATUS_EXCLUDED = "EXCLUDED"

# YN comparison thresholds (price_advantage_pct = reference_prob - offered_prob)
# Units: decimal probability points (0.08 = 8 percentage points)
YN_STRONG_OUTLIER_THRESHOLD = 0.08   # >= 8% advantage
YN_OUTLIER_THRESHOLD = 0.04          # >= 4% advantage
YN_MARGINAL_OUTLIER_THRESHOLD = 0.02 # >= 2% advantage
# < 2% but >= 0% → IN_LINE_WITH_MARKET
# < 0% → WORSE_THAN_MARKET

# Minimum books required for a YN market reference to be valid. Same
# 2026-08-22 decision as MIN_COMPARISON_BOOKS above — see docs/DECISIONS.md.
YN_MIN_COMPARISON_BOOKS = 1

# ── Scanner defaults ───────────────────────────────────────────────
# Minimum EV for actionable-mode scan (above MARGINAL threshold)
ACTIONABLE_EDGE_THRESHOLD = 0.02  # 2%
# Maximum age of odds data before stale-data warning (in seconds)
FRESHNESS_THRESHOLD_SECONDS = 3600  # 1 hour
# Max age of the SportsGameOdds disk cache for live scans.  Older data is
# re-fetched from the API so a live run never analyzes a previous day's slate.
LIVE_CACHE_TTL_SECONDS = 900  # 15 minutes

# ── Duplicate official-pick suppression ─────────────────────────────
# How much an already-frozen official pick's price must move (in implied-
# probability percentage points) before a rescan's new snapshot counts as
# a materially different opportunity rather than the same pick re-observed.
# +110 -> +108 is ~0.46pp (noise, same pick); +110 -> +125 is ~3.2pp
# (real market movement, treated as a new opportunity). Chosen below the
# smallest MARGINAL EV bucket and above typical same-book quote noise.
# A line change is ALWAYS material regardless of this threshold — a
# different number is a structurally different wager.
MATERIAL_PRICE_DELTA_PP = 0.015  # 1.5 percentage points of implied probability


# ── Pinnacle-first sharp value model ───────────────────────────────
# When enabled, Pinnacle's two-sided prices define the no-vig fair
# probability for a prop group and every other book is compared to it.
USE_PINNACLE_VALUE_MODEL = True
# If True, Pinnacle approval is REQUIRED for a book to be an official pick.
# Pinnacle approval means both-sides Pinnacle prices exist at the exact same
# line and EV >= MIN_PINNACLE_EV and probability edge >= MIN_PINNACLE_PROB_EDGE.
# Fallback analysis still runs and opportunities are still displayed, but
# books without Pinnacle approval are marked is_official=False.
REQUIRE_PINNACLE_FOR_OFFICIAL = True
# If True, groups without Pinnacle fall back to the existing LOO
# market-median consensus (unchanged behaviour).
PINNACLE_FALLBACK_TO_MARKET_MEDIAN = True
# Minimum EV for a Pinnacle-approved pick (decimal, 0.04 = 4%).
MIN_PINNACLE_EV = 0.04
# Minimum probability edge vs Pinnacle no-vig (decimal, 0.025 = 2.5%).
MIN_PINNACLE_PROB_EDGE = 0.025


# ── Pinnacle feed (pinnapi.com) ───────────────────────────────────
# Free-tier Pinnacle-reseller API that serves Pinnacle's sharp O/U
# player props AND game-level (moneyline/spread/total) prices. When
# enabled, the scanner fetches this feed and injects a "pinnacle" book
# entry into every O/U group (props and game markets alike) at the exact
# same line, which activates the frozen Pinnacle value model above.
#
# Multi-league as of 2026-08-23 — verified live which of pinnapi's own
# sport_id values correspond to our 3 leagues (their numbering doesn't
# match anything documented, had to probe it): 6=Baseball(MLB),
# 5=Football(NFL), 3=Basketball(WNBA, alongside NBA/other basketball
# leagues also under sport_id 3 — league_name still needs filtering to
# "WNBA" specifically, same as MLB already filtered to "MLB" from
# Baseball's NPB/KBO/Mexican League neighbors).
PINNACLE_FEED_ENABLED = True
PINNACLE_FEED_API_KEY_ENV = "PINNAPI_API_KEY"
PINNACLE_FEED_BASE_URL = "https://pinnapi.com"
PINNACLE_FEED_TIMEOUT_SECONDS = 30
PINNACLE_FEED_CACHE_TTL_SECONDS = 300          # reuse a feed for this long
PINNACLE_FEED_MIN_INTERVAL_SECONDS = 10.0      # min gap between live calls

# Max age (seconds) of pinnapi's own "last" timestamp before a Pinnacle
# quote is treated as stale and skipped (falls back to LOO consensus,
# same as if Pinnacle had no data at all — see
# inject_pinnacle_reference/inject_pinnacle_game_reference in
# src/pinnacle_feed.py). Evidence-based, not arbitrary: live-checked
# 2026-08-23 against the real feed, "last" was consistently 9-22 seconds
# old across multiple real calls (it's a payload-wide refresh timestamp,
# not a genuinely per-price one — see PinnacleProp.last_updated). 900s
# (15 min) gives ~40-100x headroom over observed normal operation —
# generous enough to never false-positive on the 5-min cache TTL plus
# request latency, tight enough to catch a genuinely stuck/dead feed
# rather than silently using hours-old sharp prices as the sole
# reference. Re-verify if pinnapi's real refresh cadence changes.
PINNACLE_MAX_STALENESS_SECONDS = 900

# Shorter effective cache TTL (seconds) applied specifically when the
# cached raw payload had ZERO Player Props specials for a league —
# added 2026-08-23 per operator directive: a genuinely-empty props
# response must not be cached as long as a normal one, or we risk
# missing props that get posted a few minutes later, closer to game
# time. 120s (2 min) vs. the normal 300s (5 min) — still well above the
# 10s min-fetch-interval floor, short enough that a scan running near
# game time (the existing pregame-check cadence already re-scans at
# start-60min and start-15min) picks up newly-posted props promptly
# rather than serving a stale "nothing here" cache entry.
PINNACLE_PROPS_EMPTY_RECHECK_SECONDS = 120

# league -> pinnapi sport_id, verified live 2026-08-23 (see module
# docstring above).
PINNACLE_SPORT_ID_BY_LEAGUE = {
    "MLB": 6,
    "NFL": 5,
    "WNBA": 3,
}
# league -> the real league_name string pinnapi uses inside that sport_id
# (a sport_id can carry multiple real leagues, e.g. Basketball also
# carries NBA/other regional leagues alongside WNBA).
PINNACLE_LEAGUE_NAME_BY_LEAGUE = {
    "MLB": "MLB",
    "NFL": "NFL",
    "WNBA": "WNBA",
}

# ── The Odds API's own Pinnacle bookmaker (added 2026-08-26) ───────
#
# The operator's paid Odds-API plan includes Pinnacle as a real
# bookmaker — confirmed live 2026-08-26 for MLB/WNBA moneyline, spread,
# total, and several player-prop markets. It was never reachable before
# this because every existing Odds-API call in this codebase hardcodes
# `regions="us"`, and Pinnacle is classified under `eu`, never `us`.
#
# This is now the PRIMARY Pinnacle source (src/odds_api_pinnacle_feed.py)
# — a targeted `bookmakers=pinnacle` request (not the whole `eu` region,
# to avoid pulling in dozens of irrelevant European books and to keep
# the credit cost identical to a single extra region: The Odds API's own
# docs state up to 10 explicitly-named books count as one region for
# quota purposes). Direct pinnapi.com (PINNACLE_FEED_ENABLED above)
# remains a fallback for whatever this source doesn't cover (confirmed
# live: alternate lines are not available from Pinnacle via The Odds
# API) — see src/pinnacle_feed.py's inject_pinnacle_reference /
# inject_pinnacle_game_reference, which already skip injection whenever
# a "pinnacle" entry is already present, so trying this source first and
# direct pinnapi second requires no change to that non-destructive
# merge behavior.
ODDS_API_PINNACLE_ENABLED = True
ODDS_API_PINNACLE_BOOKMAKER_KEY = "pinnacle"
# 600s (10 min), not the direct feed's 300s: this bounds the worst case
# under a burst of back-to-back scans (confirmed happening in real
# production logs) to roughly one fetch per event per 10 minutes,
# without ever risking serving data older than
# PINNACLE_MAX_STALENESS_SECONDS (900s) — the injection-side staleness
# check would reject it before use anyway. It does NOT eliminate cost
# scaling with real scan cadence when that cadence is itself longer than
# 10 minutes (confirmed live: MLB scans roughly every 20-40 min) — see
# the credit-cost estimate in docs/DECISIONS.md. credit_budget_check()
# (already wired into the props fetch loop) is the real backstop against
# this ever silently overspending the shared monthly budget.
ODDS_API_PINNACLE_CACHE_TTL_SECONDS = 600

# Dedicated refresh throttle, independent of scan cadence and separate
# from the per-request cache above — added 2026-08-26 after computing
# the real cost of fetching on every scan: MLB alone (~15 games/day x
# 4 prop markets = 60 credits/round, plus 3 for game odds) at the real
# observed production scan cadence (~20-40 min) works out to roughly
# 45,000-55,000 credits/month for MLB props ALONE — 2-3x the entire
# 20,000/month budget, before WNBA. The per-request cache above doesn't
# help here since it expires well before the next real scan.
#
# This throttle instead tracks the last REAL (non-cache-hit) fetch per
# (league, data-type) in the existing odds_api_credits usage log (see
# odds_api_credits.py — already written to by this module's own calls,
# no new table needed) and skips the fetch attempt entirely — not just
# reusing a cached response, but not even trying — when the last real
# fetch was more recent than this. 480 minutes (8 hours) was chosen by
# computing the resulting monthly cost directly: at 3 refreshes/day,
# MLB (63 credits/round) + WNBA (35 credits/round) ≈ 8,820 credits/month
# combined, alongside the existing ~8,200/month primary props/game-odds
# usage — comfortably under the 20,000/month budget with real headroom
# for NFL once its season starts. A single constant to adjust if a
# different freshness/cost trade-off is wanted; credit_budget_check()
# (already wired into the props loop) remains the hard backstop either way.
ODDS_API_PINNACLE_REFRESH_THROTTLE_MINUTES = 480

# league -> {pinnapi special_units -> our market_type_ou}, for Player
# Props specials. Verified live 2026-08-23 against real posted props
# (see docs/DECISIONS.md "Pinnacle wired in for all 3 leagues" for the
# full audit). NFL is intentionally empty — confirmed live that Pinnacle
# has zero specials posted for NFL this far before its 2026-09-10 season
# opener; re-verify closer to kickoff before assuming this stays empty.
PINNACLE_PROP_UNITS_BY_LEAGUE = {
    "MLB": {
        "Strikeouts": "pitching_strikeouts_ou",
        "HitsAllowed": "pitching_hits_ou",
        "EarnedRuns": "pitching_earnedRuns_ou",
        "PitchingOuts": "pitching_outs_ou",
        "TotalBases": "batting_totalBases_ou",
        "HomeRuns": "batting_homeRuns_ou",
    },
    "WNBA": {
        "Points": "player_points_ou",
        "Rebounds": "player_rebounds_ou",
        "Assists": "player_assists_ou",
        "Threes Made": "player_threes_ou",
    },
    "NFL": {},
}

# league -> {pinnapi special_units -> the real suffix pinnapi appends to
# the player's name in the "special" field, e.g. "Alanna Smith Total
# Points"}. Verified live 2026-08-23 — NOT a uniform "Total X" pattern
# (MLB's own units mix "Total X" and plain "X Y" forms), so this must be
# looked up per unit per league, not derived.
PINNACLE_PROP_SUFFIXES_BY_LEAGUE = {
    "MLB": {
        "Strikeouts": "Total Strikeouts",
        "HitsAllowed": "Hits Allowed",
        "EarnedRuns": "Earned Runs",
        "PitchingOuts": "Pitching Outs",
        "TotalBases": "Total Bases",
        "HomeRuns": "Home Runs",
    },
    "WNBA": {
        "Points": "Total Points",
        "Rebounds": "Total Rebounds",
        "Assists": "Total Assists",
        "Threes Made": "Total Threes Made",
    },
    "NFL": {},
}

# league -> our game-market market_type strings, for matching Pinnacle's
# "Game" period moneyline/spreads/totals. Baseball uses "run line"
# naming (game_runline_ou) where NFL/WNBA use the generic "spread"
# (game_spread_ou) — same real naming difference already handled for
# the Odds-API game-odds fallback (src/mlb_odds_parser.py vs
# src/nfl_odds_parser.py / src/wnba_odds_parser.py).
PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE = {
    "MLB": {"moneyline": "game_moneyline", "spread": "game_runline_ou", "total": "game_total_ou"},
    "NFL": {"moneyline": "game_moneyline", "spread": "game_spread_ou", "total": "game_total_ou"},
    "WNBA": {"moneyline": "game_moneyline", "spread": "game_spread_ou", "total": "game_total_ou"},
}

# ── Confidence score weights ───────────────────────────────────────
# These weights control the relative importance of each component
# in the confidence score calculation. Adjust to tune scoring.
CONFIDENCE_WEIGHTS = {
    "n_books": 2.0,
    "market_quality": 1.5,
    "ev_magnitude": 2.5,
    "freshness": 1.0,
    "mapping_confidence": 1.0,
}


# ── Market registry ───────────────────────────────────────────────
# Each supported player-prop market is described by a MarketConfig.
# The parser and scanner look up markets from this registry.

# MarketConfig itself is sport-agnostic and now lives in src/sports/base.py
# (imported above) since NFL/WNBA/future-league registries need the exact
# same dataclass. Re-exported here unchanged so every existing
# ``from src.prop_config import MarketConfig`` import keeps working.


def match_ou_market(odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig if odd_id matches a registered O/U market in MLB's registry."""
    return _base_match_ou_market(MARKET_REGISTRY, odd_id)


def match_yn_market(odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig if odd_id matches a registered YN market in MLB's registry."""
    return _base_match_yn_market(MARKET_REGISTRY, odd_id)


# ── Registry contents ──────────────────────────────────────────────

PITCHER_STRIKEOUTS = MarketConfig(
    cli_name="strikeouts",
    odd_id_stat_prefix="pitching_strikeouts",
    market_type_ou="pitching_strikeouts_ou",
    market_type_yn="pitching_strikeouts_yn",
    display_name="Pitcher Strikeouts",
    short_label="K",
    period="game",
    scanner_title="MLB PITCHER STRIKEOUTS EDGE SCANNER",
)

PITCHER_OUTS = MarketConfig(
    cli_name="outs",
    odd_id_stat_prefix="pitching_outs",
    market_type_ou="pitching_outs_ou",
    market_type_yn=None,           # no YN variant for outs
    display_name="Pitcher Outs Recorded",
    short_label="Outs",
    period="game",
    scanner_title="MLB PITCHER OUTS RECORDED EDGE SCANNER",
    supports_yn=False,
)

PITCHER_HITS_ALLOWED = MarketConfig(
    cli_name="hits_allowed",
    odd_id_stat_prefix="pitching_hits",
    market_type_ou="pitching_hits_ou",
    market_type_yn=None,           # no YN variant for hits allowed
    display_name="Pitcher Hits Allowed",
    short_label="Hits",
    period="game",
    scanner_title="MLB PITCHER HITS ALLOWED EDGE SCANNER",
    supports_yn=False,
)

PITCHER_WALKS_ALLOWED = MarketConfig(
    cli_name="walks_allowed",
    odd_id_stat_prefix="pitching_basesOnBalls",
    market_type_ou="pitching_basesOnBalls_ou",
    market_type_yn=None,
    display_name="Pitcher Walks Allowed",
    short_label="BB",
    period="game",
    scanner_title="MLB PITCHER WALKS ALLOWED EDGE SCANNER",
    supports_yn=False,
)

PITCHER_EARNED_RUNS = MarketConfig(
    cli_name="earned_runs",
    odd_id_stat_prefix="pitching_earnedRuns",
    market_type_ou="pitching_earnedRuns_ou",
    market_type_yn="pitching_earnedRuns_yn",
    display_name="Pitcher Earned Runs",
    short_label="ER",
    period="game",
    scanner_title="MLB PITCHER EARNED RUNS EDGE SCANNER",
    supports_yn=False,
)

PITCHER_PITCHES_THROWN = MarketConfig(
    cli_name="pitches_thrown",
    odd_id_stat_prefix="pitching_pitchesThrown",
    market_type_ou="pitching_pitchesThrown_ou",
    market_type_yn=None,
    display_name="Pitcher Pitches Thrown",
    short_label="Pit",
    period="game",
    scanner_title="MLB PITCHER PITCHES THROWN EDGE SCANNER",
    supports_yn=False,
)

PITCHER_WIN = MarketConfig(
    cli_name="pitching_win",
    odd_id_stat_prefix="pitching_win",
    market_type_ou=None,
    market_type_yn="pitching_win_yn",
    display_name="Pitching Win",
    short_label="Win",
    period="game",
    scanner_title="MLB PITCHING WIN EDGE SCANNER",
    supports_ou=False,
)

# ── Tier 1: Batter markets (highest value) ────────────────────────

BATTER_HITS = MarketConfig(
    cli_name="batter_hits",
    odd_id_stat_prefix="batting_hits",
    market_type_ou="batting_hits_ou",
    market_type_yn="batting_hits_yn",
    display_name="Batter Hits",
    short_label="H",
    period="game",
    scanner_title="MLB BATTER HITS EDGE SCANNER",
)

BATTER_TOTAL_BASES = MarketConfig(
    cli_name="total_bases",
    odd_id_stat_prefix="batting_totalBases",
    market_type_ou="batting_totalBases_ou",
    market_type_yn=None,
    display_name="Total Bases",
    short_label="TB",
    period="game",
    scanner_title="MLB TOTAL BASES EDGE SCANNER",
    supports_yn=False,
)

BATTER_HITS_RUNS_RBI = MarketConfig(
    cli_name="hits_runs_rbi",
    odd_id_stat_prefix="batting_hits+runs+rbi",
    market_type_ou="batting_hits+runs+rbi_ou",
    market_type_yn="batting_hits+runs+rbi_yn",
    display_name="Hits + Runs + RBI",
    short_label="H+R+RBI",
    period="game",
    scanner_title="MLB HITS + RUNS + RBI EDGE SCANNER",
)

BATTER_HOME_RUNS = MarketConfig(
    cli_name="home_runs",
    odd_id_stat_prefix="batting_homeRuns",
    market_type_ou="batting_homeRuns_ou",
    market_type_yn="batting_homeRuns_yn",
    display_name="Home Runs",
    short_label="HR",
    period="game",
    scanner_title="MLB HOME RUNS EDGE SCANNER",
)

BATTER_RBI = MarketConfig(
    cli_name="rbi",
    odd_id_stat_prefix="batting_RBI",
    market_type_ou="batting_RBI_ou",
    market_type_yn="batting_RBI_yn",
    display_name="Runs Batted In",
    short_label="RBI",
    period="game",
    scanner_title="MLB RUNS BATTED IN EDGE SCANNER",
)

BATTER_RUNS = MarketConfig(
    cli_name="batter_runs",
    odd_id_stat_prefix="batting_runs",
    market_type_ou="batting_runs_ou",
    market_type_yn="batting_runs_yn",
    display_name="Batter Runs",
    short_label="R",
    period="game",
    scanner_title="MLB BATTER RUNS EDGE SCANNER",
)

BATTER_RUNS_RBI = MarketConfig(
    cli_name="runs_rbi",
    odd_id_stat_prefix="batting_runs+rbi",
    market_type_ou="batting_runs+rbi_ou",
    market_type_yn="batting_runs+rbi_yn",
    display_name="Runs + RBI",
    short_label="R+RBI",
    period="game",
    scanner_title="MLB RUNS + RBI EDGE SCANNER",
)

# ── Tier 2: Additional batter markets ─────────────────────────────

BATTER_SINGLES = MarketConfig(
    cli_name="singles",
    odd_id_stat_prefix="batting_singles",
    market_type_ou="batting_singles_ou",
    market_type_yn="batting_singles_yn",
    display_name="Singles",
    short_label="1B",
    period="game",
    scanner_title="MLB SINGLES EDGE SCANNER",
)

BATTER_DOUBLES = MarketConfig(
    cli_name="doubles",
    odd_id_stat_prefix="batting_doubles",
    market_type_ou="batting_doubles_ou",
    market_type_yn="batting_doubles_yn",
    display_name="Doubles",
    short_label="2B",
    period="game",
    scanner_title="MLB DOUBLES EDGE SCANNER",
)

BATTER_WALKS = MarketConfig(
    cli_name="batter_walks",
    odd_id_stat_prefix="batting_basesOnBalls",
    market_type_ou="batting_basesOnBalls_ou",
    market_type_yn="batting_basesOnBalls_yn",
    display_name="Batter Walks",
    short_label="BB",
    period="game",
    scanner_title="MLB BATTER WALKS EDGE SCANNER",
)

BATTER_STOLEN_BASES = MarketConfig(
    cli_name="stolen_bases",
    odd_id_stat_prefix="batting_stolenBases",
    market_type_ou="batting_stolenBases_ou",
    market_type_yn="batting_stolenBases_yn",
    display_name="Stolen Bases",
    short_label="SB",
    period="game",
    scanner_title="MLB STOLEN BASES EDGE SCANNER",
)

BATTER_TRIPLES = MarketConfig(
    cli_name="triples",
    odd_id_stat_prefix="batting_triples",
    market_type_ou="batting_triples_ou",
    market_type_yn="batting_triples_yn",
    display_name="Triples",
    short_label="3B",
    period="game",
    scanner_title="MLB TRIPLES EDGE SCANNER",
)

# ── Tier 3: Remaining markets ─────────────────────────────────────

BATTER_STRIKEOUTS = MarketConfig(
    cli_name="batter_strikeouts",
    odd_id_stat_prefix="batting_strikeouts",
    market_type_ou="batting_strikeouts_ou",
    market_type_yn="batting_strikeouts_yn",
    display_name="Batter Strikeouts",
    short_label="K",
    period="game",
    scanner_title="MLB BATTER STRIKEOUTS EDGE SCANNER",
)

BATTER_FIRST_HR = MarketConfig(
    cli_name="first_home_run",
    odd_id_stat_prefix="batting_firstHomeRun",
    market_type_ou=None,
    market_type_yn="batting_firstHomeRun_yn",
    display_name="First Home Run",
    short_label="1stHR",
    period="game",
    scanner_title="MLB FIRST HOME RUN EDGE SCANNER",
    supports_ou=False,
)

# ── Game-level markets (not player props) ──────────────────────────
# These are two-sided whole-game markets (game total, moneyline, run
# line).  They share the SGO "points" stat prefix but are distinguished
# by their betTypeID ("ou"/"ml"/"sp") and statEntityID:
#   points-all-game-ou-*   → game total (entity "all")
#   points-*-game-ml-*     → moneyline (entity away/home)
#   points-*-game-sp-*     → run line / spread (entity away/home)
# Moneyline and run line are two-sided away/home markets; the scanner
# stores them in the generic over/under group slots (AWAY→over,
# HOME→under) and translates side labels back for display.

GAME_TOTAL = MarketConfig(
    cli_name="game_total",
    odd_id_stat_prefix="points",
    market_type_ou="game_total_ou",
    market_type_yn=None,
    display_name="Game Total",
    short_label="Tot",
    period="game",
    scanner_title="MLB GAME TOTAL EDGE SCANNER",
    entity=("all",),
    supports_yn=False,
    game_level=True,
)

GAME_MONEYLINE = MarketConfig(
    cli_name="moneyline",
    odd_id_stat_prefix="points",
    market_type_ou="game_moneyline",
    market_type_yn=None,
    display_name="Moneyline",
    short_label="ML",
    period="game",
    scanner_title="MLB MONEYLINE EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    bet_type="ml",
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

GAME_RUN_LINE = MarketConfig(
    cli_name="run_line",
    odd_id_stat_prefix="points",
    market_type_ou="game_runline_ou",
    market_type_yn=None,
    display_name="Run Line",
    short_label="RL",
    period="game",
    scanner_title="MLB RUN LINE EDGE SCANNER",
    allowed_sides_ou=("away", "home"),
    bet_type="sp",
    supports_yn=False,
    game_level=True,
    internal_side_map={"AWAY": "over", "HOME": "under"},
    group_sides=("AWAY", "HOME"),
)

MARKET_REGISTRY: list[MarketConfig] = [
    PITCHER_STRIKEOUTS,
    PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED,
    PITCHER_OUTS,
    PITCHER_EARNED_RUNS,
    PITCHER_PITCHES_THROWN,
    PITCHER_WIN,
    BATTER_HITS,
    BATTER_TOTAL_BASES,
    BATTER_HITS_RUNS_RBI,
    BATTER_HOME_RUNS,
    BATTER_RBI,
    BATTER_RUNS,
    BATTER_RUNS_RBI,
    BATTER_SINGLES,
    BATTER_DOUBLES,
    BATTER_WALKS,
    BATTER_STOLEN_BASES,
    BATTER_TRIPLES,
    BATTER_STRIKEOUTS,
    BATTER_FIRST_HR,
    GAME_TOTAL,
    GAME_MONEYLINE,
    GAME_RUN_LINE,
]

# Lookup helpers
_CLI_NAME_MAP: dict[str, MarketConfig] = {m.cli_name: m for m in MARKET_REGISTRY}
_OU_TYPE_MAP: dict[str, MarketConfig] = {m.market_type_ou: m for m in MARKET_REGISTRY}
_YN_TYPE_MAP: dict[str, MarketConfig] = {m.market_type_yn: m for m in MARKET_REGISTRY if m.market_type_yn}


def get_market_by_cli_name(name: str) -> MarketConfig | None:
    """Look up a market by its CLI name."""
    return _CLI_NAME_MAP.get(name)


def get_market_by_ou_type(market_type: str) -> MarketConfig | None:
    """Look up a market by its O/U market_type string."""
    return _OU_TYPE_MAP.get(market_type)


def get_market_by_yn_type(market_type: str) -> MarketConfig | None:
    """Look up a market by its YN market_type string."""
    return _YN_TYPE_MAP.get(market_type)


def is_auto_settleable_market(value: str | None) -> bool:
    """Accept canonical market types and registry CLI names."""
    if value in AUTO_SETTLEABLE_MARKET_TYPES:
        return True
    market = get_market_by_cli_name(value or "")
    if market is None:
        return False
    return any(
        market_type in AUTO_SETTLEABLE_MARKET_TYPES
        for market_type in (market.market_type_ou, market.market_type_yn)
        if market_type
    )


def validate_config() -> list[str]:
    """Validate configuration consistency.  Returns a list of error messages.

    An empty list means the configuration is valid.
    """
    errors: list[str] = []

    # Check threshold ordering
    if STRONG_EDGE_THRESHOLD <= POSITIVE_EDGE_THRESHOLD:
        errors.append(
            f"STRONG_EDGE_THRESHOLD ({STRONG_EDGE_THRESHOLD}) must be > "
            f"POSITIVE_EDGE_THRESHOLD ({POSITIVE_EDGE_THRESHOLD})"
        )
    if YN_STRONG_OUTLIER_THRESHOLD <= YN_OUTLIER_THRESHOLD:
        errors.append(
            f"YN_STRONG_OUTLIER_THRESHOLD ({YN_STRONG_OUTLIER_THRESHOLD}) must be > "
            f"YN_OUTLIER_THRESHOLD ({YN_OUTLIER_THRESHOLD})"
        )
    if YN_OUTLIER_THRESHOLD <= YN_MARGINAL_OUTLIER_THRESHOLD:
        errors.append(
            f"YN_OUTLIER_THRESHOLD ({YN_OUTLIER_THRESHOLD}) must be > "
            f"YN_MARGINAL_OUTLIER_THRESHOLD ({YN_MARGINAL_OUTLIER_THRESHOLD})"
        )

    # Check registry consistency
    for mc in MARKET_REGISTRY:
        if mc.supports_ou and not mc.market_type_ou:
            errors.append(f"{mc.cli_name}: supports_ou=True but market_type_ou is empty")
        if mc.supports_yn and not mc.market_type_yn:
            errors.append(f"{mc.cli_name}: supports_yn=True but market_type_yn is None")
        if not mc.cli_name:
            errors.append(f"MarketConfig has empty cli_name")

    # Check for duplicate CLI names
    cli_names = [mc.cli_name for mc in MARKET_REGISTRY]
    dupes = [n for n in cli_names if cli_names.count(n) > 1]
    if dupes:
        errors.append(f"Duplicate CLI names in registry: {set(dupes)}")

    # Check freshness threshold is positive
    if FRESHNESS_THRESHOLD_SECONDS <= 0:
        errors.append(f"FRESHNESS_THRESHOLD_SECONDS must be > 0, got {FRESHNESS_THRESHOLD_SECONDS}")

    # Check min comparison books
    if MIN_COMPARISON_BOOKS < 1:
        errors.append(f"MIN_COMPARISON_BOOKS must be >= 1, got {MIN_COMPARISON_BOOKS}")
    if YN_MIN_COMPARISON_BOOKS < 1:
        errors.append(f"YN_MIN_COMPARISON_BOOKS must be >= 1, got {YN_MIN_COMPARISON_BOOKS}")

    return errors
