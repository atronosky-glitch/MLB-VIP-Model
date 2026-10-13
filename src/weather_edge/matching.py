"""Cross-platform contract equivalence (pure).

Two contracts are treated as the SAME bet only when every dimension that can change the outcome
matches: metric, local calendar date, settlement station, measurement-source family, unit, time
zone, and the exact set of whole-degree values that resolve YES. Anything else is reported as
non-equivalent WITH the reasons, so the UI can show why two similar-looking markets are not
interchangeable (e.g. Kalshi "NYC" settles on The Weather Company at Central Park while
Polymarket "NYC" settles on NOAA at LaGuardia).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import WeatherContract


def source_family(text: str | None) -> str:
    t = (text or "").lower()
    if "weather company" in t or "weather.com" in t or "wunderground" in t:
        return "twc"
    if "noaa" in t or "weather.gov" in t or "national weather service" in t or "nws" in t:
        return "noaa"
    return "unknown"


@dataclass(frozen=True)
class Equivalence:
    a: WeatherContract
    b: WeatherContract
    equivalent: bool
    reasons: tuple[str, ...]


def check_equivalence(a: WeatherContract, b: WeatherContract) -> Equivalence:
    reasons: list[str] = []
    if a.metric != b.metric:
        reasons.append(f"different measurement ({a.metric} vs {b.metric})")
    if a.target_date != b.target_date:
        reasons.append(f"different date ({a.target_date} vs {b.target_date})")
    if a.station_icao is None or b.station_icao is None:
        reasons.append("settlement station not verified on one side")
    elif a.station_icao != b.station_icao:
        reasons.append(f"different settlement stations ({a.station_icao} vs {b.station_icao})")
    fa, fb = source_family(a.settlement_source), source_family(b.settlement_source)
    if "unknown" in (fa, fb):
        reasons.append("settlement source not recognized on one side")
    elif fa != fb:
        reasons.append(f"different settlement sources ({a.settlement_source} vs {b.settlement_source})")
    if a.unit != b.unit:
        reasons.append(f"different units ({a.unit} vs {b.unit})")
    if a.tz != b.tz:
        reasons.append(f"different time zones ({a.tz} vs {b.tz})")
    if a.bin is None or b.bin is None:
        reasons.append("outcome range unknown on one side")
    elif a.bin != b.bin:
        reasons.append(f"different outcome range ({a.bin.label} vs {b.bin.label})")
    return Equivalence(a, b, not reasons, tuple(reasons))


def find_cross_platform_pairs(contracts: list[WeatherContract]) -> list[Equivalence]:
    """Every Kalshi/Polymarket pair about the same metric+date+station, each with its verdict.
    Pairs about different stations or dates are not even candidates."""
    kalshi = [c for c in contracts if c.platform == "kalshi" and c.modeled]
    poly = [c for c in contracts if c.platform == "polymarket" and c.modeled]
    out: list[Equivalence] = []
    for k in kalshi:
        for p in poly:
            if (k.metric, k.target_date, k.station_icao) == (p.metric, p.target_date, p.station_icao):
                out.append(check_equivalence(k, p))
    return out
