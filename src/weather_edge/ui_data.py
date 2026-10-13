"""Read-side queries and pure filters behind the Weather Edge pages (no Streamlit imports here).

Everything shown to a user comes from rows the scanner recorded; when nothing was recorded the
functions return empty results and the page says so instead of inventing numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from . import store
from .config import WeatherEdgeConfig
from .models import Bin
from .registry import STATIONS

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def city_name(icao: str | None) -> str:
    st = STATIONS.get(icao or "")
    return st.name if st else (icao or "unknown")


def overview(conn, cfg: WeatherEdgeConfig, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    scan = store.latest_scan(conn, only_finished=True)
    if scan is None:
        return {"has_scan": False, "freshness": "never", "age_minutes": None}
    finished = _parse_ts(scan["finished_at"])
    age_min = (now - finished).total_seconds() / 60 if finished else None
    census = json.loads(scan["census_json"] or "{}")
    sigs = _rows(conn.execute(
        "SELECT net_ev_pct FROM weather_signals WHERE scan_id = ? AND decision = 'SIGNAL'", (scan["scan_id"],)))
    calibrated = _rows(conn.execute("SELECT COUNT(*) AS n FROM weather_calibration WHERE n >= 30"))[0]["n"]
    stale_after = max(2 * cfg.scan_interval_minutes, 30)
    return {
        "has_scan": True, "scan_id": scan["scan_id"], "status": scan["status"],
        "contracts_seen": scan["contracts_seen"], "contracts_modeled": scan["contracts_modeled"],
        "opportunities": len(sigs),
        "avg_net_ev_pct": (sum(s["net_ev_pct"] for s in sigs) / len(sigs)) if sigs else None,
        "age_minutes": age_min,
        "freshness": "stale" if age_min is None or age_min > stale_after else "fresh",
        "sources_ok": census.get("sources_ok", {}), "geoblock": census.get("polymarket_geoblock"),
        "skipped_events": census.get("skipped_events", {}), "reject_reasons": census.get("reject_reasons", {}),
        "calibrated_buckets": int(calibrated), "errors": json.loads(scan["errors_json"] or "[]"),
    }


_OPP_SQL = """
SELECT s.signal_id, s.platform, s.market_id, s.side, s.decision, s.reasons, s.qty, s.best_price, s.avg_price, s.fee,
       s.total_cost, s.p_win, s.gross_ev, s.net_ev, s.net_ev_pct, s.conservative_net_ev, s.break_even_prob,
       s.spread, s.liquidity_usd, s.confidence, s.created_at,
       c.event_key, c.title, c.metric, c.target_date, c.station_icao, c.station_label, c.settlement_source,
       c.bin_lo, c.bin_hi, c.status AS contract_status,
       p.p_yes, p.p_yes_low, p.p_yes_high, p.baseline_p_yes, p.market_yes_mid, p.lead_days, p.calibrated
FROM weather_signals s
JOIN weather_predictions p ON p.prediction_id = s.prediction_id
JOIN weather_contracts c ON c.platform = s.platform AND c.market_id = s.market_id
WHERE s.scan_id = ? {decision_clause}
ORDER BY s.net_ev_pct DESC
"""


def opportunities(conn, *, include_rejected: bool = False) -> list[dict]:
    scan = store.latest_scan(conn, only_finished=True)
    if scan is None:
        return []
    clause = "" if include_rejected else "AND s.decision = 'SIGNAL'"
    out = []
    for r in _rows(conn.execute(_OPP_SQL.format(decision_clause=clause), (scan["scan_id"],))):
        b = Bin(r["bin_lo"], r["bin_hi"]) if (r["bin_lo"] is not None or r["bin_hi"] is not None) else None
        r["bin_label"] = b.label if b else ""
        r["city"] = city_name(r["station_icao"])
        mid = r["market_yes_mid"]
        r["market_implied"] = None if mid is None else (mid if r["side"] == "YES" else 1 - mid)
        out.append(r)
    return out


def apply_filters(
    rows: list[dict], *, platforms=None, stations=None, metrics=None, max_lead_days: int | None = None,
    min_net_ev_pct: float | None = None, min_confidence: str | None = None, min_liquidity_usd: float | None = None,
) -> list[dict]:
    out = []
    for r in rows:
        if platforms and r["platform"] not in platforms:
            continue
        if stations and r["station_icao"] not in stations:
            continue
        if metrics and r["metric"] not in metrics:
            continue
        if max_lead_days is not None and r["lead_days"] > max_lead_days:
            continue
        if min_net_ev_pct is not None and r["net_ev_pct"] < min_net_ev_pct:
            continue
        if min_confidence and CONFIDENCE_RANK.get(r["confidence"], 0) < CONFIDENCE_RANK[min_confidence]:
            continue
        if min_liquidity_usd is not None and (r["liquidity_usd"] or 0) < min_liquidity_usd:
            continue
        out.append(r)
    return out


def market_detail(conn, platform: str, market_id: str) -> dict | None:
    contract = store.get_contract(conn, platform, market_id)
    if contract is None:
        return None
    pred = _rows(conn.execute(
        "SELECT * FROM weather_predictions WHERE platform = ? AND market_id = ? ORDER BY created_at DESC LIMIT 1",
        (platform, market_id)))
    pred = pred[0] if pred else None
    signals = []
    snapshot = None
    if pred:
        signals = _rows(conn.execute(
            "SELECT * FROM weather_signals WHERE prediction_id = ? ORDER BY net_ev_pct DESC", (pred["prediction_id"],)))
        if pred.get("forecast_snapshot_id"):
            snap = _rows(conn.execute(
                "SELECT * FROM weather_forecast_snapshots WHERE snapshot_id = ?", (pred["forecast_snapshot_id"],)))
            snapshot = snap[0] if snap else None
    members = json.loads(snapshot["members_json"]) if snapshot else {}
    family_means = {k: sum(v) / len(v) for k, v in members.items() if v}
    return {
        "contract": contract, "prediction": pred, "signals": signals, "snapshot": snapshot,
        "family_means": family_means, "price_history": store.price_history(conn, platform, market_id),
        "event_distribution": event_distribution(conn, platform, contract["event_key"]),
        "calibration": (store.get_calibration(conn, platform, contract["station_icao"], contract["metric"],
                                              _bucket(pred["lead_days"])) if pred else None),
    }


def _bucket(lead: int) -> str:
    return "d0" if lead <= 0 else "d1" if lead == 1 else "d2" if lead == 2 else "d3+"


def event_distribution(conn, platform: str, event_key: str) -> list[dict]:
    """Latest model probability and market mid for every outcome of one event (a coherent set)."""
    rows = _rows(conn.execute(
        """SELECT c.market_id, c.bin_lo, c.bin_hi, p.p_yes, p.market_yes_mid, p.created_at
           FROM weather_contracts c
           JOIN weather_predictions p ON p.platform = c.platform AND p.market_id = c.market_id
           WHERE c.platform = ? AND c.event_key = ?
           ORDER BY p.created_at DESC""", (platform, event_key)))
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["market_id"], r)
    out = []
    for r in latest.values():
        b = Bin(r["bin_lo"], r["bin_hi"])
        out.append({"bin": b.label, "sort": -10 ** 6 if b.lo is None else b.lo,
                    "model": r["p_yes"], "market": r["market_yes_mid"]})
    out.sort(key=lambda x: x["sort"])
    return out


def recent_scans(conn, limit: int = 10) -> list[dict]:
    return _rows(conn.execute(
        "SELECT scan_id, started_at, finished_at, status, contracts_seen, contracts_modeled, signals_created "
        "FROM weather_scan_runs ORDER BY started_at DESC LIMIT ?", (limit,)))
