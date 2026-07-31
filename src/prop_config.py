"""Centralized configuration for player prop analysis thresholds.

All thresholds are exposed as module-level constants so they can be
overridden in tests or adjusted at a single point.

Also contains the market registry — a data-driven configuration layer
that defines every supported player-prop market.  The parser and scanner
look up markets from this registry rather than hard-coding market types.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Market quality ────────────────────────────────────────────────

MARKET_QUALITY_VALID = "VALID_MARKET"
MARKET_QUALITY_NEEDS_REVIEW = "NEEDS_REVIEW"
MARKET_QUALITY_INSUFFICIENT = "INSUFFICIENT_MARKET"
MARKET_QUALITY_EXCLUDED = "EXCLUDED"

# Minimum number of OTHER books required for a market to be VALID
MIN_COMPARISON_BOOKS = 4  # meaning at least 5 paired books total

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

# Minimum books required for a YN market reference to be valid
YN_MIN_COMPARISON_BOOKS = 3

# ── Scanner defaults ───────────────────────────────────────────────
# Minimum EV for actionable-mode scan (above MARGINAL threshold)
ACTIONABLE_EDGE_THRESHOLD = 0.02  # 2%
# Maximum age of odds data before stale-data warning (in seconds)
FRESHNESS_THRESHOLD_SECONDS = 3600  # 1 hour


# ── Pinnacle-first sharp value model ───────────────────────────────
# When enabled, Pinnacle's two-sided prices define the no-vig fair
# probability for a prop group and every other book is compared to it.
USE_PINNACLE_VALUE_MODEL = True
# If True and Pinnacle is missing one/both sides, no book in that group
# can become an official pick (fallback analysis still runs if enabled).
REQUIRE_PINNACLE_FOR_OFFICIAL = False
# If True, groups without Pinnacle fall back to the existing LOO
# market-median consensus (unchanged behaviour).
PINNACLE_FALLBACK_TO_MARKET_MEDIAN = True
# Minimum EV for a Pinnacle-approved pick (decimal, 0.04 = 4%).
MIN_PINNACLE_EV = 0.04
# Minimum probability edge vs Pinnacle no-vig (decimal, 0.025 = 2.5%).
MIN_PINNACLE_PROB_EDGE = 0.025

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

@dataclass(frozen=True)
class MarketConfig:
    """Configuration for a single player-prop market type."""
    cli_name: str               # CLI identifier (e.g. "strikeouts")
    odd_id_stat_prefix: str     # API oddID stat prefix (e.g. "pitching_strikeouts")
    market_type_ou: str         # DB market_type for O/U (e.g. "pitching_strikeouts_ou")
    market_type_yn: str | None  # DB market_type for YN, or None if no YN variant
    display_name: str           # Human-readable name (e.g. "Pitcher Strikeouts")
    short_label: str            # Scanner column label (e.g. "K")
    period: str                 # Supported period (e.g. "game")
    scanner_title: str = ""     # Scanner header (e.g. "MLB PITCHER STRIKEOUTS EDGE SCANNER")
    allowed_sides_ou: tuple[str, ...] = ("over", "under")
    allowed_sides_yn: tuple[str, ...] = ("yes", "no")
    min_comparison_books_ou: int = 4   # O/U: need this many OTHER books
    min_comparison_books_yn: int = 3   # YN: need this many OTHER books
    supports_ou: bool = True
    supports_yn: bool = True


def _match_odd_id(odd_id: str, stat_prefix: str, period: str, bet_type: str, allowed_sides: tuple[str, ...]) -> bool:
    """Check if an odd_id matches a market pattern."""
    parts = odd_id.rsplit("-", 4)
    if len(parts) < 5:
        return False
    # Reconstruct the stat prefix from the odd_id
    if len(parts) > 4:
        stat_full = "-".join(parts[:-4])
    else:
        stat_full = parts[0]
    if stat_full != stat_prefix:
        return False
    if parts[-3] != period:
        return False
    if parts[-2] != bet_type:
        return False
    if parts[-1] not in allowed_sides:
        return False
    return True


def match_ou_market(odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig if odd_id matches a registered O/U market."""
    for cfg in MARKET_REGISTRY:
        if cfg.supports_ou and _match_odd_id(odd_id, cfg.odd_id_stat_prefix, cfg.period, "ou", cfg.allowed_sides_ou):
            return cfg
    return None


def match_yn_market(odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig if odd_id matches a registered YN market."""
    for cfg in MARKET_REGISTRY:
        if cfg.supports_yn and cfg.market_type_yn and _match_odd_id(odd_id, cfg.odd_id_stat_prefix, cfg.period, "yn", cfg.allowed_sides_yn):
            return cfg
    return None


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

MARKET_REGISTRY: list[MarketConfig] = [
    PITCHER_STRIKEOUTS,
    PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED,
    PITCHER_WIN,
    BATTER_HITS,
    BATTER_TOTAL_BASES,
    BATTER_HOME_RUNS,
    BATTER_STOLEN_BASES,
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
