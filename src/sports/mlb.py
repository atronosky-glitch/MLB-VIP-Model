"""MLB league adapter.

Thin wrapper around the existing, mature MLB-specific modules — no new
logic. MLB's market registry and results/settlement source predate the
sports/ package and are intentionally left in place rather than moved,
to avoid any behavior change to the production MLB pipeline.
"""

from __future__ import annotations

LEAGUE_ID = "MLB"
SPORT = "baseball"
AVAILABLE = True
UNAVAILABLE_REASON = None


def get_market_registry():
    from src.prop_config import MARKET_REGISTRY
    return MARKET_REGISTRY


def get_settlement_module():
    """Return the module with ingest_results_for_recommendations() for MLB."""
    from src import mlb_results
    return mlb_results
