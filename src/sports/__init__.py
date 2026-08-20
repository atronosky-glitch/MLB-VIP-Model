"""League/sport adapter registry.

Each league module (``src.sports.mlb``, ``src.sports.nfl``,
``src.sports.wnba``, ...) exposes a common, small interface so the
generic engine (parser, scanner, analysis, qualification, grading) never
needs to know which sport it is looking at:

- ``LEAGUE_ID: str`` — the SportsGameOdds ``leagueID`` (e.g. ``"MLB"``).
- ``SPORT: str`` — human sport name (e.g. ``"baseball"``).
- ``AVAILABLE: bool`` — whether this league currently has a working odds
  data-provider integration. ``False`` means the adapter exists but is
  intentionally inert (see ``UNAVAILABLE_REASON``) rather than faking data.
- ``UNAVAILABLE_REASON: str | None`` — why, when ``AVAILABLE`` is False.
- ``get_market_registry() -> list[MarketConfig]`` — this league's markets.
- ``SETTLEMENT_MODULE`` — the module with a
  ``ingest_results_for_recommendations(conn, recommendations)`` function
  for this league's verified results source, or ``None`` if unavailable.

Thresholds, EV math, market-quality scoring, and pick qualification are
NOT part of this interface — they are shared, sport-agnostic logic
(``player_prop_analysis.py``, ``market_analysis.py``, ``official_picks.py``,
``grading.py``) that already operates generically on parsed odds rows and
does not need per-league tuning to be correct.
"""

from __future__ import annotations

# Adapter modules are imported lazily (inside get_league / supported_leagues)
# rather than at package-import time. src.sports.base defines MarketConfig,
# which src.prop_config (MLB's concrete market registry) imports; if this
# __init__ eagerly imported src.sports.mlb here, importing src.sports.base
# from prop_config.py would first run this file, which would import
# src.sports.mlb, which imports prop_config.py — a circular import. Lazy
# imports break the cycle since by the time a caller actually asks for a
# league adapter, every module involved has already finished loading.

_LEAGUE_MODULE_NAMES = {
    "MLB": "mlb",
    "NFL": "nfl",
    "WNBA": "wnba",
}
_LEAGUE_CACHE: dict[str, object] = {}


def get_league(league: str):
    """Return the adapter module for *league* (case-insensitive)."""
    key = (league or "").upper()
    if key not in _LEAGUE_MODULE_NAMES:
        raise ValueError(f"Unknown league: {key!r}. Supported: {sorted(_LEAGUE_MODULE_NAMES)}")
    if key not in _LEAGUE_CACHE:
        import importlib
        _LEAGUE_CACHE[key] = importlib.import_module(f".{_LEAGUE_MODULE_NAMES[key]}", __name__)
    return _LEAGUE_CACHE[key]


def supported_leagues() -> list[str]:
    """All leagues with an adapter module, regardless of data availability."""
    return sorted(_LEAGUE_MODULE_NAMES)


def available_leagues() -> list[str]:
    """Leagues with a working odds data-provider integration right now."""
    return sorted(k for k in _LEAGUE_MODULE_NAMES if getattr(get_league(k), "AVAILABLE", False))


def market_capability_report() -> dict:
    """Per-league summary of market/data availability for dashboards and docs.

    Returns a dict keyed by league id with: available (bool), reason (str|None),
    sport (str), n_markets (int), markets (list of cli_name/display_name/form).
    """
    report = {}
    for league_id in _LEAGUE_MODULE_NAMES:
        mod = get_league(league_id)
        registry = mod.get_market_registry()
        report[league_id] = {
            "available": getattr(mod, "AVAILABLE", False),
            "unavailable_reason": getattr(mod, "UNAVAILABLE_REASON", None),
            "sport": getattr(mod, "SPORT", ""),
            "n_markets": len(registry),
            "markets": [
                {
                    "cli_name": m.cli_name,
                    "display_name": m.display_name,
                    "supports_ou": m.supports_ou,
                    "supports_yn": m.supports_yn,
                    "game_level": m.game_level,
                }
                for m in registry
            ],
        }
    return report
