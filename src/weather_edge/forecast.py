"""Forecast acquisition: Open-Meteo ensembles (GEFS + ECMWF IFS), the NWS gridpoint forecast, and NWS
station observations. Everything is matched to the MARKET's own settlement station, time zone,
unit (°F) and local calendar-day window.

Sources (verified live 2026-10-12):

* Open-Meteo ensemble API: 31 GEFS members + 51 ECMWF IFS members (control + 50), hourly
  ``temperature_2m`` in the requested time zone, so the hourly ``time`` strings are already the
  station's LOCAL wall clock. The free tier is licensed for NON-COMMERCIAL use only; set
  ``WEATHER_EDGE_OPENMETEO_KEY`` to use the paid commercial host (see docs/WEATHER_EDGE.md).
* NWS api.weather.gov: ``/points`` -> hourly gridpoint forecast (independent deterministic source)
  and ``/stations/{id}/observations`` (what has already happened today).

A daily extreme from hourly samples is slightly less extreme than the continuously measured one;
that systematic offset is estimated in calibration.py against the platform's own settled values.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .http import PublicHttpClient
from .models import ForecastBundle, MemberSet
from .registry import Station

logger = logging.getLogger(__name__)

OPENMETEO_ENSEMBLE_BASE = "https://ensemble-api.open-meteo.com"
OPENMETEO_COMMERCIAL_ENSEMBLE_BASE = "https://customer-ensemble-api.open-meteo.com"
NWS_BASE = os.environ.get("WEATHER_EDGE_NWS_BASE", "https://api.weather.gov")
ENSEMBLE_MODELS = ("ncep_gefs_seamless", "ecmwf_ifs025")
_FAMILY = {"ncep_gefs_seamless": "gefs", "ecmwf_ifs025_ensemble": "ecmwf"}
MIN_MEMBERS = 20
MIN_HOURS_FULL_DAY = 22
_MEMBER_COL = re.compile(r"^temperature_2m(?:_member\d+)?_(ncep_gefs_seamless|ecmwf_ifs025_ensemble)$")


class ForecastUnavailable(RuntimeError):
    """No usable forecast for this station/date (the scanner skips it with a reason, never guesses)."""


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def _extreme(values: list[float], metric: str) -> float:
    return max(values) if metric == "high" else min(values)


def daily_extremes_by_member(hourly: dict, target: date, metric: str) -> list[MemberSet]:
    """Per-member daily high/low over the station-local calendar day ``target``.

    Members missing hours are dropped (an extreme from a partial day would be biased), and a column
    that is entirely null is ignored. Returns one MemberSet per model family present.
    """
    if metric not in ("high", "low"):
        raise ValueError(f"unsupported metric {metric!r}")
    times = hourly.get("time") or []
    idx = [i for i, t in enumerate(times) if t.startswith(target.isoformat())]
    by_family: dict[str, list[float]] = {}
    for col, series in hourly.items():
        m = _MEMBER_COL.match(col)
        if not m or not isinstance(series, list):
            continue
        vals = [series[i] for i in idx if i < len(series) and series[i] is not None]
        if len(vals) < MIN_HOURS_FULL_DAY:
            continue
        by_family.setdefault(_FAMILY[m.group(1)], []).append(float(_extreme(vals, metric)))
    return [MemberSet(family=f, values=tuple(v)) for f, v in sorted(by_family.items())]


def nws_daily_extreme(periods: list[dict], target: date, tz: str, metric: str) -> float | None:
    """Deterministic NWS hourly forecast extreme for the local day, or None if the day is incomplete."""
    zone = ZoneInfo(tz)
    vals: list[float] = []
    for p in periods or []:
        try:
            start = datetime.fromisoformat(p["startTime"]).astimezone(zone)
            temp = float(p["temperature"])
        except (KeyError, ValueError, TypeError):
            continue
        if p.get("temperatureUnit", "F") != "F":
            continue
        if start.date() == target:
            vals.append(temp)
    return float(_extreme(vals, metric)) if len(vals) >= MIN_HOURS_FULL_DAY else None


def observed_extreme(features: list[dict], target: date, tz: str, metric: str) -> float | None:
    """Extreme of the station's reported temperatures so far on local day ``target`` (°F)."""
    zone = ZoneInfo(tz)
    vals: list[float] = []
    for f in features or []:
        props = f.get("properties", f)
        t = props.get("temperature") or {}
        if t.get("value") is None:
            continue
        try:
            ts = datetime.fromisoformat(props["timestamp"]).astimezone(zone)
        except (KeyError, ValueError):
            continue
        if ts.date() != target:
            continue
        unit = t.get("unitCode", "")
        v = float(t["value"])
        vals.append(c_to_f(v) if unit.endswith("degC") else v)
    return _extreme(vals, metric) if vals else None


@dataclass
class _StationCache:
    ensemble: dict | None = None
    ensemble_at: datetime | None = None
    nws_periods: list[dict] | None = None
    observations: list[dict] | None = None


@dataclass
class ForecastService:
    """Per-scan forecast access: one provider call per station, reused for every contract/date."""

    ensemble_http: PublicHttpClient | None = None
    nws_http: PublicHttpClient | None = None
    clock: callable = lambda: datetime.now(timezone.utc)
    _cache: dict = field(default_factory=dict)
    _hourly_url: dict = field(default_factory=dict)

    def __post_init__(self):
        key = os.environ.get("WEATHER_EDGE_OPENMETEO_KEY", "").strip()
        self._api_key = key or None
        if self.ensemble_http is None:
            base = OPENMETEO_COMMERCIAL_ENSEMBLE_BASE if key else OPENMETEO_ENSEMBLE_BASE
            self.ensemble_http = PublicHttpClient("open-meteo", base, min_interval=0.5)
        if self.nws_http is None:
            self.nws_http = PublicHttpClient("nws", NWS_BASE, min_interval=0.4)

    # ---- provider calls (each isolated so one failing source never hides the others) -------------
    def _fetch_ensemble(self, st: Station) -> dict:
        params = {
            "latitude": st.lat, "longitude": st.lon, "hourly": "temperature_2m",
            "models": ",".join(ENSEMBLE_MODELS), "temperature_unit": "fahrenheit",
            "timezone": st.tz, "forecast_days": 5,
        }
        if self._api_key:
            params["apikey"] = self._api_key
        return self.ensemble_http.get_json("/v1/ensemble", params)

    def _fetch_nws_periods(self, st: Station) -> list[dict]:
        url = self._hourly_url.get(st.icao)
        if url is None:
            meta = self.nws_http.get_json(f"/points/{st.lat:.4f},{st.lon:.4f}")
            url = meta["properties"]["forecastHourly"]
            self._hourly_url[st.icao] = url
        return self.nws_http.get_json(url)["properties"]["periods"]

    def _fetch_observations(self, st: Station, target: date) -> list[dict]:
        zone = ZoneInfo(st.tz)
        start = datetime.combine(target, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        data = self.nws_http.get_json(
            f"/stations/{st.icao}/observations",
            {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 200},
        )
        return data.get("features", [])

    def _station(self, st: Station) -> _StationCache:
        return self._cache.setdefault(st.icao, _StationCache())

    def _ensemble(self, st: Station) -> tuple[dict, datetime]:
        c = self._station(st)
        if c.ensemble is None:
            c.ensemble = self._fetch_ensemble(st)
            c.ensemble_at = self.clock()
        return c.ensemble, c.ensemble_at

    # ---- public ------------------------------------------------------------------------------------
    def bundle(self, st: Station, metric: str, target: date) -> ForecastBundle:
        """Forecast bundle for one station/metric/local date, or raise ForecastUnavailable."""
        now = self.clock()
        local_today = now.astimezone(ZoneInfo(st.tz)).date()
        if target < local_today:
            raise ForecastUnavailable("target date is in the past")
        try:
            hourly, fetched_at = self._ensemble(st)
        except Exception as exc:
            raise ForecastUnavailable(f"ensemble unavailable ({type(exc).__name__})") from exc
        members = daily_extremes_by_member(hourly.get("hourly", {}), target, metric)
        total = sum(len(m.values) for m in members)
        if total < MIN_MEMBERS:
            raise ForecastUnavailable(f"only {total} complete ensemble members for {target}")

        notes = [f"ensemble families: {', '.join(f'{m.family}={len(m.values)}' for m in members)}"]
        nws_point = None
        c = self._station(st)
        try:
            if c.nws_periods is None:
                c.nws_periods = self._fetch_nws_periods(st)
            nws_point = nws_daily_extreme(c.nws_periods, target, st.tz, metric)
        except Exception as exc:  # NWS is a cross-check; its absence lowers confidence, not validity
            notes.append(f"NWS forecast unavailable ({type(exc).__name__})")

        same_day = target == local_today
        observed = None
        if same_day:
            try:
                if c.observations is None:
                    c.observations = self._fetch_observations(st, target)
                observed = observed_extreme(c.observations, target, st.tz, metric)
            except Exception as exc:
                notes.append(f"observations unavailable ({type(exc).__name__})")
        return ForecastBundle(
            station_icao=st.icao, metric=metric, target_date=target, fetched_at=fetched_at,
            members=tuple(members), nws_point=nws_point, observed_so_far=observed, same_day=same_day,
            source_note="; ".join(notes),
        )


def lead_days(target: date, now: datetime, tz: str) -> int:
    """Whole local calendar days between ``now`` and the target day (0 = same day)."""
    return (target - now.astimezone(ZoneInfo(tz)).date()).days


def lead_bucket(days: int) -> str:
    return "d0" if days <= 0 else "d1" if days == 1 else "d2" if days == 2 else "d3+"
