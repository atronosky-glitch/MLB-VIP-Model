"""Forecast-error calibration per station / metric / lead bucket.

The ensemble's raw output is not trusted as a probability. For every (station, metric, lead bucket)
we estimate

* ``bias``         mean(observed - forecast): systematic offset incl. the hourly-sampling gap and the
                   difference between the forecast grid cell and the settlement source's reading;
* ``residual_std`` standard deviation of that error: how wrong forecasts of this kind really are;
* ``bias_se``      the standard error of ``bias`` (the model's own parameter uncertainty).

"Observed" is the value the PLATFORM settled on (Kalshi ``expiration_value``) -- the quantity the
contracts actually pay on -- so settlement-source quirks are absorbed rather than assumed away.
Forecasts come from Open-Meteo's Previous Runs archive (the forecast that was actually available N
days before), never from a reanalysis, so there is no look-ahead.

Until a bucket has ``MIN_CALIBRATION_N`` real samples it is reported ``calibrated=False`` with a
deliberately wide prior; the scanner refuses to raise signals from uncalibrated buckets by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .http import PublicHttpClient
from .registry import Station

MIN_CALIBRATION_N = 30
SHRINK_K = 5.0   # pseudo-observations of the prior (pulls small samples toward "no bias, prior spread")
PRIOR_STD_F = {"d0": 2.0, "d1": 2.8, "d2": 3.4, "d3+": 4.2}
PREVIOUS_RUNS_BASE = "https://previous-runs-api.open-meteo.com"
_PREVIOUS_COLUMN = {"d0": "temperature_2m_previous_day1", "d1": "temperature_2m_previous_day1",
                    "d2": "temperature_2m_previous_day2"}


@dataclass(frozen=True)
class Calibration:
    bias: float
    residual_std: float
    bias_se: float
    n: int
    calibrated: bool
    source: str


def prior_calibration(bucket: str) -> Calibration:
    std = PRIOR_STD_F.get(bucket, PRIOR_STD_F["d3+"])
    return Calibration(0.0, std, std / 3.0, 0, False, "prior (no history)")


def fit_calibration(errors: list[float], bucket: str, source: str, min_n: int = MIN_CALIBRATION_N) -> Calibration:
    """Shrunken bias / spread from forecast errors (observed - forecast)."""
    n = len(errors)
    prior = PRIOR_STD_F.get(bucket, PRIOR_STD_F["d3+"])
    if n == 0:
        return prior_calibration(bucket)
    mean = sum(errors) / n
    var = sum((e - mean) ** 2 for e in errors) / max(n - 1, 1)
    bias = mean * n / (n + SHRINK_K)
    pooled_var = (var * max(n - 1, 1) + (prior ** 2) * SHRINK_K) / (max(n - 1, 1) + SHRINK_K)
    std = max(math.sqrt(pooled_var), 0.75)
    se = math.sqrt(var / n) if n > 1 else prior
    return Calibration(bias, std, se, n, n >= min_n, source)


def lookup(conn, platform: str, station_icao: str, metric: str, bucket: str) -> Calibration:
    """Stored calibration for the platform's OWN settlement history; the wide prior when there is none.
    Calibration never transfers between platforms: they settle on different sources and stations."""
    from . import store
    row = store.get_calibration(conn, platform, station_icao, metric, bucket)
    if row is None:
        return prior_calibration(bucket)
    return Calibration(row["bias_f"], row["residual_std_f"], row["bias_se_f"], row["n"],
                       row["n"] >= MIN_CALIBRATION_N, row["source"])


# --------------------------------------------------------------------------------------- scoring
def brier(p: float, outcome: bool) -> float:
    return (p - (1.0 if outcome else 0.0)) ** 2


def log_loss(p: float, outcome: bool, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1 - eps)
    return -math.log(p if outcome else 1 - p)


# ---------------------------------------------------------------------------------------- backfill
def _daily_from_series(times: list[str], vals: list, target: date, metric: str) -> float | None:
    day = [v for t, v in zip(times, vals) if t.startswith(target.isoformat()) and v is not None]
    if len(day) < 22:
        return None
    return max(day) if metric == "high" else min(day)


def fetch_previous_runs(http: PublicHttpClient, st: Station, start: date, end: date) -> dict:
    return http.get_json("/v1/forecast", {
        "latitude": st.lat, "longitude": st.lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": "temperature_2m_previous_day1,temperature_2m_previous_day2",
        "temperature_unit": "fahrenheit", "timezone": st.tz,
    })


def collect_errors(
    settled: dict[tuple[str, str, date], float], previous: dict[str, dict], stations: dict[str, Station],
) -> dict[tuple[str, str, str], list[float]]:
    """errors[(station, metric, bucket)] = [observed - forecast] for every settled day with an archived forecast.

    ``settled`` maps (station_icao, metric, local_date) to the platform's settled value.
    ``previous`` maps station_icao to the Previous-Runs payload for that station.
    """
    out: dict[tuple[str, str, str], list[float]] = {}
    for (icao, metric, day), observed in settled.items():
        payload = previous.get(icao)
        if not payload or icao not in stations:
            continue
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        for bucket, col in _PREVIOUS_COLUMN.items():
            fc = _daily_from_series(times, hourly.get(col, []), day, metric)
            if fc is not None:
                out.setdefault((icao, metric, bucket), []).append(observed - fc)
    return out


def backfill(conn, kalshi_client, stations: dict[str, Station], previous_http: PublicHttpClient | None = None,
             now: datetime | None = None, lookback_days: int = 120) -> dict:
    """Fit and store calibration from Kalshi's settled temperature markets + archived forecasts.

    Returns a report with counts and an explicit list of what could NOT be fitted (reported honestly,
    never filled in).
    """
    from . import store
    from .kalshi_source import classify_series, parse_market, settlement_result

    now = now or datetime.now(timezone.utc)
    previous_http = previous_http or PublicHttpClient("open-meteo-previous", PREVIOUS_RUNS_BASE, min_interval=0.6)
    settled: dict[tuple[str, str, date], float] = {}
    report: dict = {"series": 0, "settled_days": 0, "fitted": 0, "skipped": [], "errors": []}
    cutoff = (now - timedelta(days=lookback_days)).date()
    for s in kalshi_client.list_weather_series():
        if s.get("frequency") != "daily" or classify_series(s) not in ("high", "low"):
            continue
        report["series"] += 1
        try:
            markets = kalshi_client.list_markets(s["ticker"], status="settled")
        except Exception as exc:
            report["errors"].append(f"{s.get('ticker')}: {type(exc).__name__}")
            continue
        for m in markets:
            c = parse_market(m, s, now)
            result, value = settlement_result(m)
            if not c.modeled or c.target_date is None or value is None or result is None or c.target_date < cutoff:
                continue
            settled[(c.station_icao, c.metric, c.target_date)] = value
    report["settled_days"] = len(settled)
    if not settled:
        report["skipped"].append("no settled Kalshi temperature markets available")
        return report

    previous: dict[str, dict] = {}
    for icao in sorted({k[0] for k in settled}):
        days = [k[2] for k in settled if k[0] == icao]
        try:
            previous[icao] = fetch_previous_runs(previous_http, stations[icao], min(days), max(days))
        except Exception as exc:
            report["errors"].append(f"previous-runs {icao}: {type(exc).__name__}")
    errors = collect_errors(settled, previous, stations)
    for (icao, metric, bucket), errs in errors.items():
        cal = fit_calibration(errs, bucket, f"kalshi settled vs open-meteo previous runs, {len(errs)} days")
        store.upsert_calibration(conn, {
            "platform": "kalshi", "station_icao": icao, "metric": metric, "lead_bucket": bucket, "n": cal.n, "bias_f": cal.bias,
            "residual_std_f": cal.residual_std, "bias_se_f": cal.bias_se,
            "fitted_at": now.isoformat(), "source": cal.source,
        })
        report["fitted"] += 1
        if not cal.calibrated:
            report["skipped"].append(f"{icao}/{metric}/{bucket}: only {cal.n} samples (< {MIN_CALIBRATION_N})")
    conn.commit()
    return report
