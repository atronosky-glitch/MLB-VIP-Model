"""Sport-agnostic building blocks shared by every league adapter.

``MarketConfig`` describes one market pattern in the SportsGameOdds oddID
grammar (``{statID}-{entityID}-{periodID}-{betTypeID}-{sideID}``) —
verified identical across every league on this provider (MLB, NFL, NBA,
NHL, NCAAF, NCAAB, MLS, ...). Nothing here is specific to any one sport;
what varies per league is *which* ``MarketConfig`` entries exist, which
lives in ``src/sports/<league>.py``.

EV math, market-quality scoring, and pick-qualification thresholds are
deliberately NOT here — that logic (``player_prop_analysis.py``,
``market_analysis.py``, ``official_picks.py``) is already sport-agnostic
and shared as a single global standard across every league.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketConfig:
    """Configuration for a single market type (player prop or game market)."""
    cli_name: str               # CLI identifier (e.g. "strikeouts")
    odd_id_stat_prefix: str     # API oddID stat prefix (e.g. "pitching_strikeouts")
    market_type_ou: str | None  # DB market_type for O/U, or None if no O/U variant
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
    game_level: bool = False                    # game-level market (not a player prop)
    entity: tuple[str, ...] | None = None       # allowed statEntityID values; None = any
    bet_type: str = "ou"                        # oddID betType segment ("ou", "ml", "sp")
    internal_side_map: dict[str, str] | None = None  # row side -> group dict slot (e.g. AWAY->over)
    group_sides: tuple[str, str] | None = None  # display side labels for the two group slots


def _match_odd_id(
    odd_id: str,
    stat_prefix: str,
    period: str,
    bet_type: str,
    allowed_sides: tuple[str, ...],
    entity: tuple[str, ...] | None = None,
) -> bool:
    """Check if an odd_id matches a market pattern."""
    parts = odd_id.rsplit("-", 4)
    if len(parts) < 5:
        return False
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
    if entity is not None and parts[-4] not in entity:
        return False
    return True


def match_ou_market(registry: list[MarketConfig], odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig in *registry* if odd_id matches a registered O/U market."""
    for mc in registry:
        if mc.supports_ou and _match_odd_id(
            odd_id, mc.odd_id_stat_prefix, mc.period, mc.bet_type,
            mc.allowed_sides_ou, mc.entity,
        ):
            return mc
    return None


def match_yn_market(registry: list[MarketConfig], odd_id: str) -> MarketConfig | None:
    """Return the MarketConfig in *registry* if odd_id matches a registered YN market."""
    for mc in registry:
        if mc.supports_yn and mc.market_type_yn and _match_odd_id(
            odd_id, mc.odd_id_stat_prefix, mc.period, "yn", mc.allowed_sides_yn,
        ):
            return mc
    return None


def build_lookup_maps(
    registry: list[MarketConfig],
) -> tuple[dict[str, MarketConfig], dict[str, MarketConfig], dict[str, MarketConfig]]:
    """Build (cli_name_map, ou_type_map, yn_type_map) lookup dicts for a registry."""
    cli_map = {m.cli_name: m for m in registry}
    ou_map = {m.market_type_ou: m for m in registry if m.market_type_ou}
    yn_map = {m.market_type_yn: m for m in registry if m.market_type_yn}
    return cli_map, ou_map, yn_map
