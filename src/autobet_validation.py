"""Server-side validation of customer-supplied Auto-Bet input.

Customer input is hostile: bounds enforced only by UI widgets (min_value=...)
are not a security control. Everything here runs in the customer_* modules,
before anything reaches the database or a provider, and rejects NaN/Infinity,
booleans-as-numbers, negatives, absurd magnitudes, wrong types and oversized
strings. Pure functions; safe messages only (never echo credentials).
"""

from __future__ import annotations

import math
import re

# field -> (min, max, integer?, min_inclusive)
RISK_BOUNDS: dict[str, tuple[float, float, bool, bool]] = {
    "unit_size_usd": (0.5, 1_000.0, False, True),
    "max_bet_usd": (1.0, 5_000.0, False, True),
    "max_daily_loss_usd": (1.0, 50_000.0, False, True),
    "max_total_exposure_usd": (1.0, 100_000.0, False, True),
    "max_open_positions": (1, 200, True, True),
    "min_net_ev_pct": (0.0, 50.0, False, True),
    "max_price_move_pct": (0.0, 20.0, False, True),
    "max_slippage_pct": (0.0, 20.0, False, True),
    "auto_approve_max_usd": (0.0, 5_000.0, False, True),
}

ALLOWED_LEAGUES = frozenset({"MLB", "NFL", "WNBA", "NCAAF"})
MAX_API_KEY_ID_CHARS = 200
MAX_PRIVATE_KEY_CHARS = 10_000
_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9._\-:/+=]+$")


def _num(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def validate_risk_settings(settings: dict) -> dict:
    """Return a normalized copy (ints as int, floats as float) or raise
    ValueError. Unknown keys are the caller's concern (they raise earlier)."""
    out: dict = {}
    for name, value in settings.items():
        if name == "sport_filter":
            out[name] = normalize_sport_filter(value)
            continue
        bounds = RISK_BOUNDS.get(name)
        if bounds is None:
            out[name] = value
            continue
        lo, hi, integer, _ = bounds
        number = _num(name, value)
        if integer and number != int(number):
            raise ValueError(f"{name} must be a whole number")
        if number < lo or number > hi:
            raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
        out[name] = int(number) if integer else number
    return out


def normalize_sport_filter(value) -> str | None:
    """None/empty -> None (all sports); otherwise a comma list restricted to
    known leagues, upper-cased and de-duplicated. Anything else is rejected."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 60:
        raise ValueError("sport filter is invalid")
    tokens = [t.strip().upper() for t in value.split(",") if t.strip()]
    if not tokens:
        return None
    bad = [t for t in tokens if t not in ALLOWED_LEAGUES]
    if bad:
        raise ValueError(f"unknown sport in filter (allowed: {', '.join(sorted(ALLOWED_LEAGUES))})")
    return ",".join(dict.fromkeys(tokens))


def validate_credential_inputs(api_key_id, private_key) -> str | None:
    """A customer-safe error message, or None if the shape is acceptable.
    Never echoes the supplied values."""
    if not isinstance(api_key_id, str) or not isinstance(private_key, str):
        return "Enter both your API Key and Private Key."
    api_key_id, private_key = api_key_id.strip(), private_key.strip()
    if not api_key_id or not private_key:
        return "Enter both your API Key and Private Key."
    if len(api_key_id) > MAX_API_KEY_ID_CHARS or not _SAFE_KEY_ID.match(api_key_id):
        return "That API Key does not look valid."
    if len(private_key) > MAX_PRIVATE_KEY_CHARS or "\x00" in private_key:
        return "That Private Key does not look valid."
    return None
