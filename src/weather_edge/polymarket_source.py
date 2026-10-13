"""Polymarket weather markets via the PUBLIC Gamma (discovery) and CLOB (order book) APIs.

Verified live on 2026-10-12 (read-only, no credentials):

* ``GET gamma-api.polymarket.com/events?tag_slug=weather&active=true&closed=false`` lists events.
  Daily temperature events are titled ``"Highest|Lowest temperature in <City> on <Month> <D>?"``; each
  has one market per bin (``groupItemTitle`` like ``"56-57°F"`` / ``"55°F or below"``), is a
  ``negRisk`` group, and names its settlement station in ``resolutionSource`` (``...?site=klga``).
* Each market carries ``clobTokenIds`` (``[yes_token, no_token]`` as a JSON string),
  ``orderMinSize``, ``acceptingOrders`` and ``feeSchedule`` (weather taker fee ``rate`` 0.05).
* ``GET clob.polymarket.com/book?token_id=`` returns bids and asks as strings; the API does NOT
  guarantee sort order, so levels are sorted here (asks ascending, bids descending).

The Gamma text sometimes contains double-encoded degree signs (``Â°``); the parsers tolerate that.

Trading from the US is geoblocked by Polymarket for polymarket.com. This module only READS public
data and never attempts to bypass any restriction; ``geoblock_status`` reports the provider's own
answer so the UI can say plainly that these prices are informational for ineligible users.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from src.execution.base import OrderLevel

from .http import PublicHttpClient
from .models import Bin, Quote, WeatherContract
from .registry import station_by_icao

logger = logging.getLogger(__name__)

GAMMA_BASE = os.environ.get("WEATHER_EDGE_POLYMARKET_GAMMA", "https://gamma-api.polymarket.com")
CLOB_BASE = os.environ.get("WEATHER_EDGE_POLYMARKET_CLOB", "https://clob.polymarket.com")
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"

_TITLE_RE = re.compile(r"^(Highest|Lowest) temperature in (.+?) on ([A-Za-z]+) (\d{1,2})\?$")
_SITE_RE = re.compile(r"[?&]site=([A-Za-z0-9]{3,4})", re.I)
_ABOVE_RE = re.compile(r"^\s*(-?\d+)\D*F?\s+or\s+(?:higher|above)\s*$", re.I)
_BELOW_RE = re.compile(r"^\s*(-?\d+)\D*F?\s+or\s+(?:below|lower)\s*$", re.I)
_RANGE_RE = re.compile(r"^\s*(-?\d+)\s*[-–]\s*(-?\d+)\D*F?\s*$", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


_UNIT_RE = re.compile(r"(?<![A-Za-z])([CF])(?![A-Za-z])")


def bin_from_label(label: str | None) -> Bin | None:
    text = label or ""
    unit = _UNIT_RE.search(text)
    if unit and unit.group(1) == "C":
        return None          # Celsius markets are never read as Fahrenheit bins
    for regex, build in (
        (_ABOVE_RE, lambda g: Bin(int(g[0]), None)),
        (_BELOW_RE, lambda g: Bin(None, int(g[0]))),
        (_RANGE_RE, lambda g: Bin(int(g[0]), int(g[1]))),
    ):
        m = regex.match(text)
        if m:
            try:
                return build(m.groups())
            except ValueError:
                return None
    return None


def parse_event_title(title: str | None) -> tuple[str, str, int, int] | None:
    """``(metric, city, month, day)`` for a daily temperature event title, else ``None``."""
    m = _TITLE_RE.match((title or "").strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(3).lower())
    if month is None:
        return None
    return ("high" if m.group(1) == "Highest" else "low"), m.group(2), month, int(m.group(4))


def station_from_source(url: str | None) -> str | None:
    m = _SITE_RE.search(url or "")
    return m.group(1).upper() if m else None


def classify_event(event: dict) -> str:
    """Cheap event-level census label for things we discover but do not model."""
    title = (event.get("title") or "").lower()
    if parse_event_title(event.get("title")) is not None:
        return "temperature_daily"
    if "temperature" in title:
        return "temperature_other"
    if any(w in title for w in ("rain", "precip", "snow")):
        return "precipitation"
    if any(w in title for w in ("hurricane", "storm", "tornado", "wildfire")):
        return "storm"
    return "other"


def _event_date(event: dict, parsed: tuple[str, str, int, int]) -> date | None:
    raw = event.get("eventDate")
    if raw:
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            pass
    end = event.get("endDate") or ""
    try:
        year = int(end[:4])
        return date(year, parsed[2], parsed[3])
    except (ValueError, TypeError):
        return None


def quote_from_gamma(market: dict, fetched_at: datetime) -> Quote:
    """Top-of-book from the Gamma payload (YES side only; depth NOT known)."""
    yes_ask, yes_bid = _dec(market.get("bestAsk")), _dec(market.get("bestBid"))
    size = _dec(market.get("orderMinSize")) or Decimal("5")
    yes_asks = (OrderLevel(yes_ask, size),) if yes_ask and 0 < yes_ask < 1 else ()
    no_asks = (OrderLevel(1 - yes_bid, size),) if yes_bid and 0 < yes_bid < 1 else ()
    return Quote(
        fetched_at=fetched_at, yes_asks=yes_asks, no_asks=no_asks,
        yes_bid=yes_bid if yes_bid and yes_bid > 0 else None, no_bid=None, depth_known=False,
        liquidity_usd=_dec(market.get("liquidityNum") or market.get("liquidity")),
        volume=_dec(market.get("volume24hr")),
    )


def _levels(raw: list[dict] | None, *, descending: bool) -> list[tuple[Decimal, Decimal]]:
    out = []
    for lv in raw or []:
        p, s = _dec(lv.get("price")), _dec(lv.get("size"))
        if p is not None and s is not None and 0 < p < 1 and s > 0:
            out.append((p, s))
    out.sort(key=lambda x: x[0], reverse=descending)
    return out


def quote_from_books(yes_book: dict | None, no_book: dict | None, fetched_at: datetime, base: Quote | None = None) -> Quote:
    """Full-depth quote from the YES-token and NO-token CLOB books (each side priced from its own book)."""
    yes_asks = tuple(OrderLevel(p, s) for p, s in _levels((yes_book or {}).get("asks"), descending=False))
    no_asks = tuple(OrderLevel(p, s) for p, s in _levels((no_book or {}).get("asks"), descending=False))
    yes_bids = _levels((yes_book or {}).get("bids"), descending=True)
    no_bids = _levels((no_book or {}).get("bids"), descending=True)
    return Quote(
        fetched_at=fetched_at, yes_asks=yes_asks, no_asks=no_asks,
        yes_bid=yes_bids[0][0] if yes_bids else None, no_bid=no_bids[0][0] if no_bids else None,
        depth_known=True, liquidity_usd=base.liquidity_usd if base else None, volume=base.volume if base else None,
    )


def parse_event(event: dict, fetched_at: datetime) -> list[WeatherContract]:
    """All bin contracts of one Polymarket event. Non-temperature or unverifiable events produce
    ``modeled=False`` contracts (with the reason) rather than being silently dropped."""
    parsed = parse_event_title(event.get("title"))
    event_key = event.get("slug") or event.get("ticker") or str(event.get("id", ""))
    markets = event.get("markets") or []
    out: list[WeatherContract] = []
    icao = station_from_source(event.get("resolutionSource"))
    station = station_by_icao(icao) if icao else None
    target = _event_date(event, parsed) if parsed else None
    for m in markets:
        label = m.get("groupItemTitle") or m.get("question")
        b = bin_from_label(m.get("groupItemTitle")) if parsed else None
        tokens = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        fee = m.get("feeSchedule") or {}
        rate = _dec(fee.get("rate"))
        reason = None
        if parsed is None:
            reason = "not a daily high/low temperature market"
        elif target is None:
            reason = "event date could not be parsed"
        elif b is None:
            is_c = _UNIT_RE.search(label or "") is not None and _UNIT_RE.search(label or "").group(1) == "C"
            reason = "Celsius market (only °F is modeled)" if is_c else f"bin label {label!r} not recognized"
        elif station is None:
            reason = f"settlement station {icao or 'unknown'} is not in the verified registry"
        elif len(tokens) != 2:
            reason = "market has no YES/NO token pair"
        elif not m.get("feesEnabled", True) or rate is None or fee.get("takerOnly") is not True:
            reason = "fee schedule not understood"
        elif "fahrenheit" not in (event.get("description") or "").lower():
            reason = "settlement unit is not Fahrenheit"
        close_time = None
        if m.get("endDate"):
            try:
                close_time = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            except ValueError:
                pass
        if m.get("closed") or m.get("archived"):
            status = "closed"
        elif m.get("active") and m.get("acceptingOrders"):
            status = "open"
        else:
            status = "closed"
        out.append(WeatherContract(
            platform="polymarket", market_id=str(m.get("conditionId") or m.get("id")),
            event_key=event_key, title=m.get("question") or event.get("title") or "",
            metric=parsed[0] if parsed else "other", target_date=target,
            station_icao=station.icao if station else None,
            station_label=f"{station.name} ({station.icao})" if station else (icao or ""),
            settlement_source="NOAA (weather.gov observations)", bin=b, unit="F",
            tz=station.tz if station else None, status=status, close_time=close_time,
            fee_model="polymarket_curve", fee_param=rate if rate is not None else Decimal("0.05"),
            min_order=_dec(m.get("orderMinSize")) or Decimal("5"), modeled=reason is None,
            unmodeled_reason=reason, rules_text=event.get("description") or "",
            yes_token=tokens[0] if len(tokens) == 2 else None, no_token=tokens[1] if len(tokens) == 2 else None,
            quote=quote_from_gamma(m, fetched_at),
            raw={"neg_risk": bool(m.get("negRisk")), "tick": m.get("orderPriceMinTickSize"),
                 "label": label, "slug": m.get("slug")},
        ))
    return out


class PolymarketWeatherClient:
    def __init__(self, gamma: PublicHttpClient | None = None, clob: PublicHttpClient | None = None):
        self.gamma = gamma or PublicHttpClient("polymarket-gamma", GAMMA_BASE, min_interval=0.25)
        self.clob = clob or PublicHttpClient("polymarket-clob", CLOB_BASE, min_interval=0.15)

    def list_weather_events(self, page_size: int = 100, max_pages: int = 6) -> list[dict]:
        events: list[dict] = []
        for page in range(max_pages):
            batch = self.gamma.get_json("/events", {
                "tag_slug": "weather", "active": "true", "closed": "false",
                "limit": page_size, "offset": page * page_size,
            })
            if not isinstance(batch, list) or not batch:
                break
            events.extend(batch)
            if len(batch) < page_size:
                break
        return events

    def get_event(self, slug: str) -> dict | None:
        """One event by slug (works for closed/resolved events too, which carry final outcomePrices)."""
        data = self.gamma.get_json("/events", {"slug": slug})
        return data[0] if isinstance(data, list) and data else None

    def get_book(self, token_id: str) -> dict:
        return self.clob.get_json("/book", {"token_id": token_id})

    def discover(self, fetched_at: datetime | None = None) -> tuple[list[WeatherContract], dict]:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        events = self.list_weather_events()
        census: dict[str, int] = {"events_seen": len(events)}
        contracts: list[WeatherContract] = []
        for ev in events:
            kind = classify_event(ev)
            census[kind] = census.get(kind, 0) + 1
            if kind == "temperature_daily":
                contracts.extend(parse_event(ev, fetched_at))
        return contracts, census

    def geoblock_status(self) -> dict | None:
        """Polymarket's own answer about this server's IP (``None`` when it cannot be determined)."""
        try:
            data = PublicHttpClient("polymarket-geoblock", GEOBLOCK_URL, max_retries=0).get_json(GEOBLOCK_URL)
        except Exception:
            return None
        return {"blocked": bool(data.get("blocked")), "country": data.get("country")} if isinstance(data, dict) else None


def settlement_result(market: dict) -> str | None:
    """'yes' / 'no' once UMA has resolved the market with a clean 1/0 price pair; else None."""
    if not market.get("closed") or str(market.get("umaResolutionStatus", "")).lower() != "resolved":
        return None
    prices = [_dec(p) for p in _json_list(market.get("outcomePrices"))]
    if len(prices) != 2 or None in prices:
        return None
    if prices[0] == 1 and prices[1] == 0:
        return "yes"
    if prices[0] == 0 and prices[1] == 1:
        return "no"
    return None
