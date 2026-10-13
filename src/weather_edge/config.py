"""Weather Edge settings, read from environment variables (no secrets live in code or the database).

Every threshold has a conservative default; see docs/WEATHER_EDGE.md for the full table.
``enabled`` defaults to False: the scheduled job does nothing until an operator opts in, mainly
because Open-Meteo's free tier is licensed for non-commercial use only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from decimal import Decimal


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


@dataclass(frozen=True)
class WeatherEdgeConfig:
    enabled: bool = False
    scan_interval_minutes: int = 30
    min_net_ev_pct: float = 5.0            # net EV as % of cost required for a signal
    require_robust_ev: bool = True         # conservative-parameter EV must also be > 0
    min_entry_price: float = 0.05          # tails are where model error is largest
    max_entry_price: float = 0.95
    max_spread: float = 0.08               # dollars on the side being bought
    min_depth_usd: float = 50.0            # resting ask depth on the side being bought
    paper_stake_usd: float = 25.0
    max_contracts: int = 500
    max_event_exposure_usd: float = 100.0  # open paper cost allowed per event (outcomes are correlated)
    max_lead_days: int = 3
    max_forecast_age_minutes: int = 180
    max_quote_age_seconds: int = 180
    require_calibration: bool = True
    decision_bucket_minutes: int = 30
    alert_min_net_ev_pct: float = 8.0
    alerts_enabled: bool = True
    discord_webhook_url: str = ""

    @property
    def paper_stake(self) -> Decimal:
        return Decimal(str(self.paper_stake_usd))

    def with_overrides(self, **kw) -> "WeatherEdgeConfig":
        return replace(self, **kw)

    @classmethod
    def from_env(cls) -> "WeatherEdgeConfig":
        d = cls()
        return cls(
            enabled=_env_bool("WEATHER_EDGE_ENABLED", d.enabled),
            scan_interval_minutes=max(5, _env_int("WEATHER_EDGE_SCAN_INTERVAL_MINUTES", d.scan_interval_minutes)),
            min_net_ev_pct=_env_float("WEATHER_EDGE_MIN_NET_EV_PCT", d.min_net_ev_pct),
            require_robust_ev=_env_bool("WEATHER_EDGE_REQUIRE_ROBUST_EV", d.require_robust_ev),
            min_entry_price=_env_float("WEATHER_EDGE_MIN_ENTRY_PRICE", d.min_entry_price),
            max_entry_price=_env_float("WEATHER_EDGE_MAX_ENTRY_PRICE", d.max_entry_price),
            max_spread=_env_float("WEATHER_EDGE_MAX_SPREAD", d.max_spread),
            min_depth_usd=_env_float("WEATHER_EDGE_MIN_DEPTH_USD", d.min_depth_usd),
            paper_stake_usd=_env_float("WEATHER_EDGE_PAPER_STAKE_USD", d.paper_stake_usd),
            max_contracts=_env_int("WEATHER_EDGE_MAX_CONTRACTS", d.max_contracts),
            max_event_exposure_usd=_env_float("WEATHER_EDGE_MAX_EVENT_EXPOSURE_USD", d.max_event_exposure_usd),
            max_lead_days=_env_int("WEATHER_EDGE_MAX_LEAD_DAYS", d.max_lead_days),
            max_forecast_age_minutes=_env_int("WEATHER_EDGE_MAX_FORECAST_AGE_MINUTES", d.max_forecast_age_minutes),
            max_quote_age_seconds=_env_int("WEATHER_EDGE_MAX_QUOTE_AGE_SECONDS", d.max_quote_age_seconds),
            require_calibration=_env_bool("WEATHER_EDGE_REQUIRE_CALIBRATION", d.require_calibration),
            decision_bucket_minutes=max(1, _env_int("WEATHER_EDGE_DECISION_BUCKET_MINUTES", d.decision_bucket_minutes)),
            alert_min_net_ev_pct=_env_float("WEATHER_EDGE_ALERT_MIN_NET_EV_PCT", d.alert_min_net_ev_pct),
            alerts_enabled=_env_bool("WEATHER_EDGE_ALERTS_ENABLED", d.alerts_enabled),
            discord_webhook_url=os.environ.get("WEATHER_EDGE_DISCORD_WEBHOOK_URL", "").strip(),
        )
