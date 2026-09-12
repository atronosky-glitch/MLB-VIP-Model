"""Prediction-market execution layer (Stage 1: read-only provider access).

Provider modules are imported lazily -- mirrors src.sports.get_league()
-- so importing this package never requires the `cryptography` package's
transitive dependencies to be present unless a provider is actually
requested.
"""

from __future__ import annotations

from typing import Any

_PROVIDER_MODULE_NAMES = {
    "kalshi": "kalshi",
    "polymarket_us": "polymarket_us",
}


def get_provider(name: str, config: Any):
    """Construct the named provider (case-insensitive) with *config*.

    Raises ValueError for an unknown name; whatever the provider's own
    __init__ raises (e.g. CredentialLoadError) otherwise.
    """
    key = (name or "").lower()
    if key not in _PROVIDER_MODULE_NAMES:
        raise ValueError(f"Unknown provider: {name!r}. Supported: {sorted(_PROVIDER_MODULE_NAMES)}")

    import importlib
    module = importlib.import_module(f".{_PROVIDER_MODULE_NAMES[key]}", __name__)
    if key == "kalshi":
        return module.KalshiProvider(config)
    return module.PolymarketUSProvider(config)


def supported_providers() -> list[str]:
    return sorted(_PROVIDER_MODULE_NAMES)
