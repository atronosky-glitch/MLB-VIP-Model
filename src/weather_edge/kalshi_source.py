"""Kalshi weather markets via the PUBLIC market-data API (no credentials, GET only).

Verified against the live API on 2026-10-12:

* ``GET /series?category=Climate and Weather`` lists weather series with ``fee_type`` /
  ``fee_multiplier`` / ``settlement_sources``.
* ``GET /markets?series_ticker=...&status=open`` returns one market per outcome with
  ``strike_type`` (``between`` | ``greater`` | ``less``), ``floor_strike`` / ``cap_strike``, dollar
  bid/ask + sizes, and the legal ``rules_primary`` text naming the settlement station and source.
* ``GET /markets/{ticker}/orderbook`` returns ``orderbook_fp.yes_dollars`` / ``no_dollars``: the
  resting BIDS for each side (ascending). A YES buyer pays ``1 - no_bid``.
* Settled markets carry ``result`` ("yes"/"no") and ``expiration_value`` (the official value).
* ``GET /series/{s}/markets/{t}/candlesticks`` gives hourly OHLC bid/ask history.

Temperature markets settle on whole-degree values reported by **The Weather Company** (not NWS);
rain markets settle on the NWS Climatological Report.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from src.execution.base import OrderLevel

from .http import PublicHttpClient
from .models import Bin, Quote, WeatherContract
from .registry import station_for_cli

logger = logging.getLogger(__name__)

KALSHI_BASE = os.environ.get("WEATHER_EDGE_KALSHI_BASE", "https://external-api.kalshi.com/trade-api/v2")
WEATHER_CATEGORY = "Climate and Weather"

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")
_STATION_RE = re.compile(r"recorded at (.+?) \((CLI[A-Z0-9]{3,4})\)")
_SOURCE_RE = re.compile(r"according to (.+?)(?:,|\.| then)")
_ABOVE_RE = re.compile(r"^(-?\d+)\s*°?\s*(?:F\s*)?or above$", re.I)
_BELOW_RE = re.compile(r"^(-?\d+)\s*°?\s*(?:F\s*)?or below$", re.I)
_RANGE_RE = re.compile(r"^(-?\d+)\s*°?\s*(?:F\s*)?to\s*(-?\d+)\s*°?$", re.I)

_STATUS_MAP = {
    "initialized": "open", "inactive": "open", "active": "open", "open": "open",
    "closed": "closed", "determined": "closed", "disputed": "closed",
    "settled": "settled", "finalized": "settled",
}


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def classify_series(series: dict) -> str:
    """Cheap pre-filter on series metadata: which weather metric a series is about."""
    title = (series.get("title") or "").lower()
    if "snow" in title:
        return "snow"
    if "rain" in title or "precip" in title:
        return "rain"
    if re.search(r"\b(low|lowest|min|minimum)\b", title):
        return "low"
    if re.search(r"\b(high|highest|max|maximum)\b", title) and "temp" in title.replace("temperature", "temp"):
        return "high"
    return "other"


def event_date(event_ticker: str) -> date | None:
    m = _EVENT_DATE_RE.search(event_ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def bin_from_subtitle(text: str | None) -> Bin | None:
    t = (text or "").replace("°", "°").strip()
    for regex, build in (
        (_ABOVE_RE, lambda g: Bin(int(g[0]), None)),
        (_BELOW_RE, lambda g: Bin(None, int(g[0]))),
        (_RANGE_RE, lambda g: Bin(int(g[0]), int(g[1]))),
    ):
        m = regex.match(t)
        if m:
            return build(m.groups())
    return None


def bin_from_strikes(market: dict) -> Bin | None:
    kind, floor, cap = market.get("strike_type"), market.get("floor_strike"), market.get("cap_strike")
    try:
        if kind == "between" and floor is not None and cap is not None:
            return Bin(int(floor), int(cap))
        if kind == "greater" and floor is not None:
            return Bin(int(floor) + 1, None)      # ">76" on whole degrees means 77 or more
        if kind == "less" and cap is not None:
            return Bin(None, int(cap) - 1)        # "<69" means 68 or less
    except (TypeError, ValueError):
        return None
    return None


def quote_from_market(market: dict, fetched_at: datetime) -> Quote:
    """Top-of-book quote from a market-list payload (depth NOT known)."""
    yes_ask, no_ask = _dec(market.get("yes_ask_dollars")), _dec(market.get("no_ask_dollars"))
    yes_bid, no_bid = _dec(market.get("yes_bid_dollars")), _dec(market.get("no_bid_dollars"))
    yes_sz, no_sz = _dec(market.get("yes_ask_size_fp")), _dec(market.get("no_ask_size_fp"))

    def level(price, size):
        if price is None or not (0 < price < 1):
            return ()
        # A size of 0/None means "unknown", not "free": assume a single contract is available.
        return (OrderLevel(price, size if size and size > 0 else Decimal("1")),)

    liq = _dec(market.get("liquidity_dollars"))
    return Quote(
        fetched_at=fetched_at, yes_asks=level(yes_ask, yes_sz), no_asks=level(no_ask, no_sz),
        yes_bid=yes_bid if yes_bid and yes_bid > 0 else None, no_bid=no_bid if no_bid and no_bid > 0 else None,
        depth_known=False, liquidity_usd=liq, volume=_dec(market.get("volume_24h_fp")),
    )


def quote_from_orderbook(book: dict, fetched_at: datetime, base: Quote | None = None) -> Quote:
    """Full-depth quote from ``/orderbook``. Resting NO bids are YES asks (price ``1 - bid``)."""
    ob = book.get("orderbook_fp") or book.get("orderbook") or {}
    yes_bids = sorted(((_dec(p), _dec(q)) for p, q in ob.get("yes_dollars") or []), key=lambda x: x[0] or 0, reverse=True)
    no_bids = sorted(((_dec(p), _dec(q)) for p, q in ob.get("no_dollars") or []), key=lambda x: x[0] or 0, reverse=True)
    yes_asks = tuple(OrderLevel(1 - p, q) for p, q in no_bids if p and q and 0 < p < 1 and q > 0)
    no_asks = tuple(OrderLevel(1 - p, q) for p, q in yes_bids if p and q and 0 < p < 1 and q > 0)
    return Quote(
        fetched_at=fetched_at, yes_asks=yes_asks, no_asks=no_asks,
        yes_bid=yes_bids[0][0] if yes_bids else None, no_bid=no_bids[0][0] if no_bids else None,
        depth_known=True, liquidity_usd=base.liquidity_usd if base else None, volume=base.volume if base else None,
    )


def parse_market(market: dict, series: dict | None, fetched_at: datetime) -> WeatherContract:
    """Turn one Kalshi market payload into a WeatherContract (never raises on bad input: anything
    that cannot be matched to a verified station and bin is returned ``modeled=False`` with the reason)."""
    ticker = market.get("ticker", "")
    event_key = market.get("event_ticker", "")
    series = series or {}
    metric = classify_series(series) if series else "other"
    rules = market.get("rules_primary") or ""
    station_label, cli, source = "", None, ""
    sm = _STATION_RE.search(rules)
    if sm:
        station_label, cli = sm.group(1).strip(), sm.group(2)
    srcm = _SOURCE_RE.search(rules)
    source = srcm.group(1).strip() if srcm else (
        (series.get("settlement_sources") or [{}])[0].get("name", "") if series.get("settlement_sources") else "")
    station = station_for_cli(cli)
    target = event_date(event_key)
    strike_bin = bin_from_strikes(market)
    sub_bin = bin_from_subtitle(market.get("yes_sub_title"))
    fee_type, fee_mult = series.get("fee_type"), _dec(series.get("fee_multiplier"))

    reason = None
    if metric not in ("high", "low"):
        reason = f"{metric} markets are discovered but not modeled yet"
    elif target is None:
        reason = "event date could not be parsed"
    elif strike_bin is None:
        reason = "strike definition not recognized"
    elif sub_bin is not None and sub_bin != strike_bin:
        reason = f"strikes ({strike_bin.label}) disagree with market label ({sub_bin.label})"
    elif station is None:
        reason = f"settlement station {cli or 'unknown'} is not in the verified registry"
    elif fee_type != "quadratic" or fee_mult is None:
        reason = f"unsupported fee schedule {fee_type!r}"
    elif "celsius" in rules.lower():
        reason = "Celsius market (only °F is modeled)"

    close_time = None
    if market.get("close_time"):
        try:
            close_time = datetime.fromisoformat(market["close_time"].replace("Z", "+00:00"))
        except ValueError:
            pass
    return WeatherContract(
        platform="kalshi", market_id=ticker, event_key=event_key, title=market.get("title", ""),
        metric=metric, target_date=target, station_icao=station.icao if station else None,
        station_label=f"{station_label} ({cli})" if cli else station_label, settlement_source=source,
        bin=strike_bin, unit="F", tz=station.tz if station else None,
        status=_STATUS_MAP.get(str(market.get("status", "")).lower(), "open"), close_time=close_time,
        fee_model="kalshi_quadratic", fee_param=fee_mult if fee_mult is not None else Decimal("1"),
        min_order=Decimal("1"), modeled=reason is None, unmodeled_reason=reason, rules_text=rules,
        quote=quote_from_market(market, fetched_at),
        raw={"strike_type": market.get("strike_type"), "series": (event_key or "").split("-")[0]},
    )


def settlement_result(market: dict) -> tuple[str | None, float | None]:
    """(result, official value) for a finalized market; ``(None, None)`` until it is settled."""
    result = (market.get("result") or "").lower()
    if result not in ("yes", "no"):
        return None, None
    value = _dec(market.get("expiration_value"))
    return result, float(value) if value is not None else None


class KalshiWeatherClient:
    def __init__(self, http: PublicHttpClient | None = None):
        self.http = http or PublicHttpClient("kalshi", KALSHI_BASE, min_interval=0.4)

    def list_weather_series(self) -> list[dict]:
        return self.http.get_json("/series", {"category": WEATHER_CATEGORY}).get("series", [])

    def list_markets(self, series_ticker: str, status: str = "open", limit: int = 1000, max_pages: int = 5) -> list[dict]:
        out: list[dict] = []
        cursor = ""
        for _ in range(max_pages):
            params = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            page = self.http.get_json("/markets", params)
            out.extend(page.get("markets", []))
            cursor = page.get("cursor") or ""
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> dict:
        return self.http.get_json(f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str) -> dict:
        return self.http.get_json(f"/markets/{ticker}/orderbook")

    def get_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_minutes: int = 60) -> list[dict]:
        data = self.http.get_json(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_minutes},
        )
        return data.get("candlesticks", [])

    def discover(self, fetched_at: datetime | None = None) -> tuple[list[WeatherContract], dict]:
        """All open temperature contracts plus a census of what was seen but not modeled."""
        fetched_at = fetched_at or datetime.now(timezone.utc)
        census = {"series_seen": 0, "series_scanned": 0, "series_failed": 0}
        contracts: list[WeatherContract] = []
        all_series = self.list_weather_series()
        census["series_seen"] = len(all_series)
        for s in all_series:
            if s.get("frequency") != "daily" or classify_series(s) not in ("high", "low"):
                continue
            census["series_scanned"] += 1
            try:
                markets = self.list_markets(s["ticker"])
            except Exception as exc:  # one failed series must not hide the rest
                census["series_failed"] += 1
                logger.warning("Kalshi series %s failed: %s", s.get("ticker"), type(exc).__name__)
                continue
            contracts.extend(parse_market(m, s, fetched_at) for m in markets)
        return contracts, census
