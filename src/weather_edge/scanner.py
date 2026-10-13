"""The Weather Edge scan: discover markets -> forecast -> probabilities -> executable EV -> signals.

Per scan, for every open temperature contract that can be verified (station in the registry, whole
degree bins, understood fee schedule):

1. build ONE predictive distribution per (station, metric, local date) from the ensemble members,
   shifted/dressed with that platform's calibrated bias and error spread;
2. turn it into a probability for each contract's outcome range (coherent by construction);
3. screen on top-of-book prices, then fetch the real order book only for candidates;
4. walk the book for a paper stake and compute fee-inclusive net EV for BOTH sides (YES and NO);
5. keep a side only if every gate passes (edge, robustness to calibration error, spread, depth, fill,
   price range, freshness, calibration, correlated-exposure cap); otherwise record WHY not.

Predictions are written before outcomes exist and never edited. Nothing here places an order.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from . import MODEL_VERSION, store
from .alerts import deliver_signal_alerts
from .calibration import Calibration, lookup
from .config import WeatherEdgeConfig
from .ev import EVResult, evaluate_side
from .fees import fee_function
from .forecast import ForecastService, ForecastUnavailable, lead_bucket, lead_days
from .kalshi_source import KalshiWeatherClient, quote_from_orderbook
from .models import ForecastBundle, Quote, WeatherContract
from .polymarket_source import PolymarketWeatherClient, quote_from_books
from .probability import (
    apply_observed_extreme, bin_probability, build_distribution, check_partition,
    clip_probability, point_distribution, probability_interval,
)
from .registry import STATIONS

logger = logging.getLogger(__name__)

MAX_DEPTH_FETCHES = 80
_ZERO = Decimal("0")


@dataclass
class SideAssessment:
    side: str
    ev: EVResult | None
    p_win: float
    p_win_robust: float
    conservative_net_ev: Decimal | None
    spread: Decimal | None
    depth_usd: Decimal | None
    reasons: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return self.ev is not None and not self.reasons


@dataclass
class ScanSummary:
    scan_id: str
    status: str
    contracts_seen: int = 0
    contracts_modeled: int = 0
    predictions: int = 0
    candidates: int = 0
    signals: int = 0
    paper_trades: int = 0
    alerts: dict = field(default_factory=dict)
    census: dict = field(default_factory=dict)
    skipped_events: dict = field(default_factory=dict)
    reject_reasons: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


# --------------------------------------------------------------------------------------- pure gates
def side_spread(quote: Quote, side: str) -> Decimal | None:
    if side == "YES":
        return quote.yes_spread
    ask = quote.no_ask
    bid = quote.no_bid if quote.no_bid is not None else (1 - quote.yes_ask if quote.yes_ask is not None else None)
    return None if ask is None or bid is None else ask - bid


def _depth_usd(asks) -> Decimal:
    return sum((lv.price * lv.quantity for lv in asks), _ZERO)


def assess_side(
    c: WeatherContract, quote: Quote, side: str, p_yes: float, p_low: float, p_high: float,
    cfg: WeatherEdgeConfig, now: datetime, *, calibrated: bool, forecast_fresh: bool,
) -> SideAssessment:
    """Every gate for one side of one contract. Reasons are machine codes shown in the UI."""
    p_win = p_yes if side == "YES" else 1.0 - p_yes
    robust = p_low if side == "YES" else 1.0 - p_high
    asks = quote.yes_asks if side == "YES" else quote.no_asks
    reasons: list[str] = []
    if c.status != "open" or (c.close_time is not None and c.close_time <= now):
        reasons.append("CLOSED")
    if not c.modeled:
        reasons.append("NOT_MODELED")
    if (now - quote.fetched_at).total_seconds() > cfg.max_quote_age_seconds:
        reasons.append("STALE_QUOTE")
    if not forecast_fresh:
        reasons.append("STALE_FORECAST")
    if cfg.require_calibration and not calibrated:
        reasons.append("UNCALIBRATED")
    if not quote.depth_known:
        reasons.append("DEPTH_UNKNOWN")

    ev = evaluate_side(
        side=side, asks=asks, p_win=p_win, stake_usd=cfg.paper_stake, fee_fn=fee_function(c.fee_model, c.fee_param),
        min_order=c.min_order, max_contracts=Decimal(cfg.max_contracts),
    )
    spread = side_spread(quote, side)
    depth = _depth_usd(asks) if asks else None
    conservative = None
    if ev is None:
        reasons.append("NO_ASK")
    else:
        conservative = Decimal(str(robust)) - ev.total_cost / ev.filled_qty
        if ev.fill_ratio < 1:
            reasons.append("PARTIAL_FILL")
        lo, hi = Decimal(str(cfg.min_entry_price)), Decimal(str(cfg.max_entry_price))
        if not (lo <= ev.avg_price <= hi):
            reasons.append("PRICE_OUT_OF_RANGE")
        if ev.net_ev_pct < Decimal(str(cfg.min_net_ev_pct)):
            reasons.append("BELOW_MIN_EDGE")
        if cfg.require_robust_ev and conservative <= 0:
            reasons.append("NONROBUST_EV")
    if spread is None:
        reasons.append("NO_BID")
    elif spread > Decimal(str(cfg.max_spread)):
        reasons.append("WIDE_SPREAD")
    if depth is not None and depth < Decimal(str(cfg.min_depth_usd)):
        reasons.append("THIN_DEPTH")
    return SideAssessment(side, ev, p_win, robust, conservative, spread, depth, reasons)


def confidence_label(cal: Calibration, bundle: ForecastBundle, lead: int, assessment: SideAssessment) -> str:
    """Coarse, rule-based label. It summarizes data quality, it is NOT a probability."""
    if not cal.calibrated:
        return "low"
    vals = bundle.all_values
    mean = sum(vals) / len(vals)
    agree = bundle.nws_point is None or abs(bundle.nws_point - mean) <= 2.0
    families = len(bundle.members) >= 2
    robust = assessment.conservative_net_ev is not None and assessment.conservative_net_ev > 0
    if cal.n >= 100 and agree and families and lead <= 1 and robust:
        return "high"
    if agree and robust:
        return "medium"
    return "low"


def _decision_bucket(now: datetime, minutes: int) -> str:
    epoch_min = int(now.timestamp() // 60)
    return str(epoch_min // minutes)


# ---------------------------------------------------------------------------------------- the scan
def run_scan(
    conn, cfg: WeatherEdgeConfig, *, now: datetime | None = None, kalshi: KalshiWeatherClient | None = None,
    polymarket: PolymarketWeatherClient | None = None, forecasts: ForecastService | None = None,
    sender=None, scan_id: str | None = None,
) -> ScanSummary:
    now = now or datetime.now(timezone.utc)
    scan_id = scan_id or uuid.uuid4().hex[:16]
    store.start_scan(conn, scan_id, now)
    summary = ScanSummary(scan_id=scan_id, status="ok")
    kalshi = kalshi or KalshiWeatherClient()
    polymarket = polymarket or PolymarketWeatherClient()
    forecasts = forecasts or ForecastService()

    contracts: list[WeatherContract] = []
    sources_ok = {}
    for name, client in (("kalshi", kalshi), ("polymarket", polymarket)):
        try:
            found, census = client.discover(now)
            contracts.extend(found)
            summary.census[name] = census
            sources_ok[name] = True
        except Exception as exc:
            sources_ok[name] = False
            summary.errors.append(f"{name} discovery failed: {type(exc).__name__}")
    geo = None
    try:
        geo = polymarket.geoblock_status()
    except Exception:
        geo = None
    summary.census["polymarket_geoblock"] = geo
    summary.census["sources_ok"] = sources_ok
    summary.contracts_seen = len(contracts)

    # Celsius contracts are out of scope by design; counting them (instead of storing thousands of rows
    # every scan) keeps the contract table meaningful. Every other unmodeled contract keeps its reason.
    celsius = [c for c in contracts if not c.modeled and (c.unmodeled_reason or "").startswith("Celsius")]
    summary.census["celsius_skipped"] = len(celsius)
    skip_ids = {id(c) for c in celsius}
    for c in contracts:
        if id(c) not in skip_ids:
            store.upsert_contract(conn, c, now)
    conn.commit()

    modeled = [c for c in contracts if c.modeled and c.status == "open" and c.target_date is not None]
    summary.contracts_modeled = len(modeled)
    events: dict[tuple, list[WeatherContract]] = {}
    for c in modeled:
        events.setdefault(c.group_key, []).append(c)

    depth_budget = MAX_DEPTH_FETCHES
    new_signals: list[dict] = []
    bundles: dict[tuple, ForecastBundle | str] = {}

    for (platform, event_key), group in events.items():
        head = group[0]
        station = STATIONS.get(head.station_icao)
        if station is None:
            summary.skipped_events[event_key] = "station not in registry"
            continue
        lead = lead_days(head.target_date, now, station.tz)
        if lead < 0:
            summary.skipped_events[event_key] = "target date already passed"
            continue
        if lead > cfg.max_lead_days:
            summary.skipped_events[event_key] = f"lead {lead}d beyond max {cfg.max_lead_days}d"
            continue
        bkey = (station.icao, head.metric, head.target_date)
        if bkey not in bundles:
            try:
                bundles[bkey] = forecasts.bundle(station, head.metric, head.target_date)
            except ForecastUnavailable as exc:
                bundles[bkey] = str(exc)
            except Exception as exc:
                bundles[bkey] = f"forecast error ({type(exc).__name__})"
        bundle = bundles[bkey]
        if isinstance(bundle, str):
            summary.skipped_events[event_key] = bundle
            continue
        if bundle.same_day and bundle.observed_so_far is None:
            summary.skipped_events[event_key] = "same-day market without station observations"
            continue

        bucket = lead_bucket(lead)
        cal = lookup(conn, platform, station.icao, head.metric, bucket)
        values = apply_observed_extreme(bundle.all_values, bundle.observed_so_far, head.metric)
        dist = build_distribution(values, cal.bias, cal.residual_std)
        baseline_point = bundle.nws_point if bundle.nws_point is not None else dist.mean
        baseline = point_distribution(baseline_point, cal.residual_std)
        fresh = (now - bundle.fetched_at).total_seconds() <= cfg.max_forecast_age_minutes * 60
        ok_partition, _problems = check_partition([c.bin for c in group])
        snapshot_id = store.insert_forecast_snapshot(conn, bundle, lead)

        cands: list[tuple[WeatherContract, float, float, float, float]] = []
        for c in group:
            p = clip_probability(bin_probability(dist, c.bin))
            lo, hi = probability_interval(values, cal.bias, cal.residual_std, cal.bias_se, c.bin)
            lo, hi = clip_probability(min(lo, p)), clip_probability(max(hi, p))
            base_p = clip_probability(bin_probability(baseline, c.bin))
            q = c.quote
            if q is None:
                continue
            msnap = store.insert_market_snapshot(conn, c, q, now)
            mid = float(q.yes_mid) if q.yes_mid is not None else None
            pkey = f"{c.platform}|{c.market_id}|{_decision_bucket(now, cfg.decision_bucket_minutes)}"
            pid, created = store.insert_prediction(conn, {
                "prediction_key": pkey, "scan_id": scan_id, "platform": c.platform, "market_id": c.market_id,
                "created_at": now.isoformat(), "model_version": MODEL_VERSION, "lead_days": lead, "p_yes": p,
                "p_yes_low": lo, "p_yes_high": hi, "baseline_p_yes": base_p, "market_yes_mid": mid,
                "bias_f": cal.bias, "residual_std_f": cal.residual_std, "calibrated": cal.calibrated and c.platform == "kalshi",
                "forecast_snapshot_id": snapshot_id, "market_snapshot_id": msnap,
            })
            if created:
                summary.predictions += 1
            # cheap screen on the top of book before spending an order-book request
            ya, na = q.yes_ask, q.no_ask
            if (ya is not None and p - float(ya) > 0.01) or (na is not None and (1 - p) - float(na) > 0.01):
                cands.append((c, p, lo, hi, pid))
        conn.commit()

        evaluated: list[tuple[SideAssessment, WeatherContract, int, float, float, float]] = []
        for c, p, lo, hi, pid in cands:
            q = c.quote
            if depth_budget <= 0:
                summary.reject_reasons["DEPTH_BUDGET_EXHAUSTED"] = summary.reject_reasons.get("DEPTH_BUDGET_EXHAUSTED", 0) + 1
                continue
            try:
                depth_budget -= 1 if c.platform == "kalshi" else 2
                if c.platform == "kalshi":
                    q = quote_from_orderbook(kalshi.get_orderbook(c.market_id), now, base=c.quote)
                else:
                    q = quote_from_books(polymarket.get_book(c.yes_token), polymarket.get_book(c.no_token), now, base=c.quote)
                store.insert_market_snapshot(conn, c, q, now)
            except Exception as exc:
                summary.errors.append(f"order book {c.platform}:{c.market_id}: {type(exc).__name__}")
                continue
            summary.candidates += 1
            platform_calibrated = cal.calibrated and c.platform == "kalshi"
            for side in ("YES", "NO"):
                a = assess_side(c, q, side, p, lo, hi, cfg, now, calibrated=platform_calibrated, forecast_fresh=fresh)
                if a.ev is not None and a.ev.net_ev_per_contract > 0:
                    evaluated.append((a, c, pid, p, lo, hi))
        conn.commit()

        # best net EV first, so the correlated-exposure cap keeps the strongest outcomes of an event
        evaluated.sort(key=lambda t: t[0].ev.net_ev_pct, reverse=True)
        for a, c, pid, p, lo, hi in evaluated:
            reasons = list(a.reasons)
            if not ok_partition:
                pass  # per-contract probabilities remain valid; recorded in the census instead
            if a.passes:
                if store.open_paper_trades(conn, c.platform, c.market_id):
                    reasons.append("ALREADY_OPEN")
                elif store.open_paper_exposure(conn, c.platform, c.event_key) + float(a.ev.total_cost) > cfg.max_event_exposure_usd:
                    reasons.append("EVENT_CAP")
            decision = "SIGNAL" if not reasons else "REJECTED"
            for r in reasons:
                summary.reject_reasons[r] = summary.reject_reasons.get(r, 0) + 1
            conf = confidence_label(cal, bundle, lead, a)
            sig = {
                "signal_key": f"{c.platform}|{c.market_id}|{_decision_bucket(now, cfg.decision_bucket_minutes)}|{a.side}",
                "prediction_id": pid, "scan_id": scan_id, "platform": c.platform, "market_id": c.market_id,
                "created_at": now.isoformat(), "side": a.side, "decision": decision, "reasons": ",".join(reasons) or None,
                "qty": float(a.ev.filled_qty), "best_price": float(a.ev.best_price), "avg_price": float(a.ev.avg_price),
                "fee": float(a.ev.fee), "total_cost": float(a.ev.total_cost), "p_win": float(a.ev.p_win),
                "gross_ev": float(a.ev.gross_ev_per_contract), "net_ev": float(a.ev.net_ev_per_contract),
                "net_ev_pct": float(a.ev.net_ev_pct), "conservative_net_ev": float(a.conservative_net_ev),
                "break_even_prob": float(a.ev.break_even_prob),
                "spread": float(a.spread) if a.spread is not None else None,
                "liquidity_usd": float(a.depth_usd) if a.depth_usd is not None else None,
                "confidence": conf, "stale": 0,
            }
            sid, created = store.insert_signal(conn, sig)
            if created and decision == "SIGNAL":
                summary.signals += 1
                _, t_created = store.insert_paper_trade(conn, {
                    "signal_id": sid, "platform": c.platform, "market_id": c.market_id, "side": a.side,
                    "qty": float(a.ev.filled_qty), "total_cost": float(a.ev.total_cost), "p_win": float(a.ev.p_win),
                    "opened_at": now.isoformat(),
                })
                summary.paper_trades += 1 if t_created else 0
                new_signals.append({
                    **sig, "signal_id": sid, "station_icao": station.icao, "station_label": head.station_label,
                    "metric": c.metric, "target_date": c.target_date.isoformat(), "bin_label": c.bin.label,
                })
            conn.commit()

    if new_signals:
        try:
            summary.alerts = deliver_signal_alerts(conn, cfg, new_signals, now, sender)
        except Exception as exc:
            summary.errors.append(f"alerts failed: {type(exc).__name__}")

    summary.status = "ok" if not summary.errors else ("partial" if any(sources_ok.values()) else "failed")
    store.finish_scan(
        conn, scan_id, datetime.now(timezone.utc) if now is None else now, summary.status, seen=summary.contracts_seen,
        modeled=summary.contracts_modeled, signals=summary.signals, alerts=summary.alerts.get("sent", 0),
        census={**summary.census, "skipped_events": summary.skipped_events, "reject_reasons": summary.reject_reasons},
        errors=summary.errors,
    )
    return summary
