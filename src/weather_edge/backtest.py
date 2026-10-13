"""Reconstructed, walk-forward backtest of the Weather Edge decision rule. SIMULATED, not a track record.

What it does (and the limits, which are printed with every result):

* Decision time = local midnight at the start of the target day.
* Forecast = Open-Meteo Previous Runs ``temperature_2m_previous_day1`` (the run actually available a
  day earlier). It is ONE deterministic forecast: historical ENSEMBLE members are not archived, so this
  tests the calibrated-deterministic version of the model, not the live ensemble model.
* Calibration for day D is fit ONLY on days <= D-2 (strictly before the decision), never on D or later.
* Prices = last hourly Kalshi candlestick bid/ask at or before the decision time (real executable
  top-of-book quotes; order-book DEPTH is not archived, so fills assume the paper stake is available).
* Fees = the real Kalshi quadratic fee. Outcomes = Kalshi's own settled results.
* The same gates as the live scanner (min net EV, robust EV, entry-price range, spread) apply.

It compares the calibrated model with the market-implied probability (mid of bid/ask) and with a raw
uncalibrated forecast baseline, and reports sample sizes so thin results are visibly thin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import store
from .calibration import PRIOR_STD_F, brier, fit_calibration, fetch_previous_runs, _daily_from_series, PREVIOUS_RUNS_BASE
from .config import WeatherEdgeConfig
from .ev import evaluate_side, paper_pnl
from .fees import fee_function
from .http import PublicHttpClient
from .models import Bin
from .probability import bin_probability, build_distribution, clip_probability, point_distribution, probability_interval
from src.execution.base import OrderLevel

logger = logging.getLogger(__name__)

LABEL = ("RECONSTRUCTED BACKTEST — simulated with archived forecasts and historical Kalshi prices. "
         "Not real trading results.")
CAVEATS = (
    "Single deterministic forecast (historical ensemble members are not archived).",
    "Top-of-book prices from hourly candles; order-book depth is not archived, so fills assume the stake is available.",
    "Calibration for each day uses only earlier days; early days are skipped for insufficient history.",
    "Settled values come from Kalshi (The Weather Company); sample sizes below 30 are not statistically meaningful.",
)


@dataclass
class DayContract:
    market_id: str
    bin: Bin
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    result: str                      # "yes" | "no"
    fee_mult: Decimal = Decimal("1")


@dataclass
class DayRecord:
    station: str
    metric: str
    day: date
    forecast_f: float
    settled_value: float
    contracts: list[DayContract] = field(default_factory=list)


def simulate(days: list[DayRecord], cfg: WeatherEdgeConfig, *, min_history: int = 30, bucket: str = "d1") -> dict:
    days = sorted(days, key=lambda d: (d.day, d.station, d.metric))
    bets: list[dict] = []
    scored: list[tuple[float, float | None, float, bool]] = []     # model p, market mid, baseline p, outcome
    skipped = 0
    simulated = 0
    prior_std = PRIOR_STD_F[bucket]
    for d in days:
        history = [x.settled_value - x.forecast_f for x in days
                   if x.station == d.station and x.metric == d.metric and x.day <= d.day - timedelta(days=2)]
        if len(history) < min_history:
            skipped += 1
            continue
        simulated += 1
        cal = fit_calibration(history, bucket, "walk-forward", min_n=min_history)
        dist = build_distribution([d.forecast_f], cal.bias, cal.residual_std)
        base = point_distribution(d.forecast_f, prior_std)
        event_cost = 0.0
        cands = []
        for c in d.contracts:
            p = clip_probability(bin_probability(dist, c.bin))
            lo, hi = probability_interval([d.forecast_f], cal.bias, cal.residual_std, cal.bias_se, c.bin)
            lo, hi = clip_probability(min(lo, p)), clip_probability(max(hi, p))
            outcome = c.result == "yes"
            mid = float((c.yes_bid + c.yes_ask) / 2) if c.yes_bid is not None and c.yes_ask is not None else None
            scored.append((p, mid, clip_probability(bin_probability(base, c.bin)), outcome))
            fee = fee_function("kalshi_quadratic", c.fee_mult)
            for side in ("YES", "NO"):
                if side == "YES":
                    ask, bid = c.yes_ask, c.yes_bid
                    pw, robust = p, lo
                else:
                    ask = (1 - c.yes_bid) if c.yes_bid is not None else None
                    bid = (1 - c.yes_ask) if c.yes_ask is not None else None
                    pw, robust = 1 - p, 1 - hi
                if ask is None or bid is None or not (0 < ask < 1):
                    continue
                ev = evaluate_side(side=side, asks=(OrderLevel(ask, Decimal(100000)),), p_win=pw,
                                   stake_usd=cfg.paper_stake, fee_fn=fee, max_contracts=Decimal(cfg.max_contracts))
                if ev is None or ev.net_ev_pct < Decimal(str(cfg.min_net_ev_pct)):
                    continue
                if not (Decimal(str(cfg.min_entry_price)) <= ev.avg_price <= Decimal(str(cfg.max_entry_price))):
                    continue
                if ask - bid > Decimal(str(cfg.max_spread)):
                    continue
                if cfg.require_robust_ev and Decimal(str(robust)) - ev.total_cost / ev.filled_qty <= 0:
                    continue
                cands.append((ev, c, side))
        cands.sort(key=lambda t: t[0].net_ev_pct, reverse=True)
        taken: set[str] = set()
        for ev, c, side in cands:
            if c.market_id in taken or event_cost + float(ev.total_cost) > cfg.max_event_exposure_usd:
                continue
            taken.add(c.market_id)
            event_cost += float(ev.total_cost)
            won = (side == "YES") == (c.result == "yes")
            bets.append({
                "day": d.day, "station": d.station, "metric": d.metric, "market_id": c.market_id, "side": side,
                "qty": float(ev.filled_qty), "cost": float(ev.total_cost), "won": won,
                "pnl": float(paper_pnl(filled_qty=ev.filled_qty, total_cost=ev.total_cost, won=won)),
                "predicted_net_ev": float(ev.net_ev_per_contract),
            })
    return _summarize(bets, scored, simulated, skipped, cfg)


def _summarize(bets, scored, simulated, skipped, cfg) -> dict:
    n = len(bets)
    cost = sum(b["cost"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    cum = peak = dd = 0.0
    for b in sorted(bets, key=lambda b: b["day"]):
        cum += b["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    with_mid = [(p, m, b, o) for p, m, b, o in scored if m is not None]
    res = {
        "label": LABEL, "caveats": list(CAVEATS),
        "days_simulated": simulated, "days_skipped_insufficient_history": skipped,
        "n_bets": n, "wins": sum(1 for b in bets if b["won"]),
        "win_rate": (sum(1 for b in bets if b["won"]) / n) if n else None,
        "total_cost": cost, "pnl": pnl, "roi_pct": (pnl / cost * 100) if cost else None, "max_drawdown": dd,
        "avg_predicted_net_ev_per_contract": (sum(b["predicted_net_ev"] for b in bets) / n) if n else None,
        "n_scored_contracts": len(scored),
        "brier_model": (sum(brier(p, o) for p, _, _, o in scored) / len(scored)) if scored else None,
        "brier_baseline_raw_forecast": (sum(brier(b, o) for _, _, b, o in scored) / len(scored)) if scored else None,
        "n_scored_vs_market": len(with_mid),
        "brier_model_vs_market_rows": (sum(brier(p, o) for p, _, _, o in with_mid) / len(with_mid)) if with_mid else None,
        "brier_market": (sum(brier(m, o) for _, m, _, o in with_mid) / len(with_mid)) if with_mid else None,
        "reliable": n >= 30,
        "params": {"min_net_ev_pct": cfg.min_net_ev_pct, "paper_stake_usd": cfg.paper_stake_usd,
                   "max_spread": cfg.max_spread, "require_robust_ev": cfg.require_robust_ev},
    }
    return res


# ------------------------------------------------------------------------------------ assembly
def _candle_quote(candles: list[dict], decision_ts: int) -> tuple[Decimal | None, Decimal | None]:
    usable = [c for c in candles if c.get("end_period_ts", 0) <= decision_ts]
    if not usable:
        return None, None
    last = max(usable, key=lambda c: c["end_period_ts"])

    def px(side):
        v = (last.get(side) or {}).get("close_dollars")
        return Decimal(str(v)) if v not in (None, "") else None
    bid, ask = px("yes_bid"), px("yes_ask")
    if bid is not None and bid <= 0:
        bid = None
    return bid, ask


def assemble_history(kalshi, stations: dict, now: datetime, lookback_days: int = 90,
                     previous_http: PublicHttpClient | None = None, max_contract_calls: int = 3000) -> tuple[list[DayRecord], dict]:
    """Network step: settled Kalshi markets + candlesticks + archived forecasts -> DayRecords."""
    from .kalshi_source import classify_series, parse_market, settlement_result

    previous_http = previous_http or PublicHttpClient("open-meteo-previous", PREVIOUS_RUNS_BASE, min_interval=0.6)
    report = {"series": 0, "contracts": 0, "candle_calls": 0, "no_price": 0, "no_forecast": 0, "errors": []}
    cutoff = (now - timedelta(days=lookback_days)).date()
    groups: dict[tuple, dict] = {}
    for s in kalshi.list_weather_series():
        if s.get("frequency") != "daily" or classify_series(s) not in ("high", "low"):
            continue
        report["series"] += 1
        try:
            markets = kalshi.list_markets(s["ticker"], status="settled")
        except Exception as exc:
            report["errors"].append(f"{s.get('ticker')}: {type(exc).__name__}")
            continue
        for m in markets:
            c = parse_market(m, s, now)
            res, val = settlement_result(m)
            if not c.modeled or res is None or val is None or c.target_date is None or c.target_date < cutoff:
                continue
            g = groups.setdefault((c.station_icao, c.metric, c.target_date, s["ticker"]), {"value": val, "items": []})
            g["items"].append((c, res, m, s))

    forecasts: dict[str, dict] = {}
    for icao in sorted({k[0] for k in groups}):
        days = [k[2] for k in groups if k[0] == icao]
        try:
            forecasts[icao] = fetch_previous_runs(previous_http, stations[icao], min(days), max(days))
        except Exception as exc:
            report["errors"].append(f"previous-runs {icao}: {type(exc).__name__}")

    records: list[DayRecord] = []
    for (icao, metric, day, series_ticker), g in sorted(groups.items(), key=lambda kv: kv[0][2]):
        hourly = (forecasts.get(icao) or {}).get("hourly", {})
        fc = _daily_from_series(hourly.get("time", []), hourly.get("temperature_2m_previous_day1", []), day, metric)
        if fc is None:
            report["no_forecast"] += 1
            continue
        zone = ZoneInfo(stations[icao].tz)
        decision = datetime.combine(day, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        dts = int(decision.timestamp())
        rec = DayRecord(station=icao, metric=metric, day=day, forecast_f=fc, settled_value=g["value"])
        for c, res, m, s in g["items"]:
            if report["candle_calls"] >= max_contract_calls:
                break
            try:
                candles = kalshi.get_candlesticks(series_ticker, c.market_id, dts - 6 * 3600, dts + 3600, 60)
                report["candle_calls"] += 1
            except Exception as exc:
                report["errors"].append(f"candles {c.market_id}: {type(exc).__name__}")
                continue
            bid, ask = _candle_quote(candles, dts)
            if ask is None:
                report["no_price"] += 1
                continue
            rec.contracts.append(DayContract(c.market_id, c.bin, bid, ask, res, c.fee_param))
            report["contracts"] += 1
        if rec.contracts:
            records.append(rec)
    return records, report


def run_backtest(conn, kalshi, stations: dict, cfg: WeatherEdgeConfig, now: datetime | None = None,
                 lookback_days: int = 90, previous_http=None) -> dict:
    now = now or datetime.now(timezone.utc)
    records, report = assemble_history(kalshi, stations, now, lookback_days, previous_http)
    result = simulate(records, cfg)
    result["data_report"] = report
    store.save_backtest(conn, "reconstructed-walk-forward", {"lookback_days": lookback_days}, result, now)
    return result
