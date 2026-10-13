"""Shared builders and fakes for the Weather Edge tests (real captured fixtures + synthetic ensembles)."""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.execution.base import OrderLevel
from src.weather_edge import kalshi_source as ks
from src.weather_edge.forecast import ForecastUnavailable
from src.weather_edge.models import ForecastBundle, MemberSet, Quote
from src.weather_edge.registry import STATIONS

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)      # 10:00 local in New York
D = Decimal


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def kalshi_series(ticker: str = "KXHIGHNY") -> dict:
    return next(s for s in load("kalshi_series_list.json") if s["ticker"] == ticker)


def kalshi_markets(file: str = "kalshi_markets_KXHIGHNY_open.json") -> list[dict]:
    data = load(file)
    return data if isinstance(data, list) else data["markets"]


def kalshi_contracts(now: datetime = NOW, target: date | None = date(2026, 10, 12)):
    out = [ks.parse_market(m, kalshi_series(), now) for m in kalshi_markets()]
    return [c for c in out if target is None or c.target_date == target]


def synthetic_hourly(start: date, days: int, centre: float, spread: float, n_gefs=31, n_ecmwf=51, seed=7) -> dict:
    """Hourly ensemble payload whose per-member daily HIGH is ``centre + offset`` (low is 16F lower)."""
    rnd = random.Random(seed)
    times = []
    for d in range(days):
        day = start + timedelta(days=d)
        times += [f"{day.isoformat()}T{h:02d}:00" for h in range(24)]
    hourly = {"time": times}
    for family, n in (("ncep_gefs_seamless", n_gefs), ("ecmwf_ifs025_ensemble", n_ecmwf)):
        for m in range(n):
            off = rnd.gauss(0, spread)
            col = f"temperature_2m_{family}" if m == 0 else f"temperature_2m_member{m:02d}_{family}"
            hourly[col] = [round(centre - 8 + off + 8 * math.cos((h - 15) / 24 * 2 * math.pi), 2)
                           for _ in range(days) for h in range(24)]
    return hourly


def bundle(centre: float, spread: float = 1.5, *, icao="KNYC", metric="high", target=date(2026, 10, 12),
           fetched_at=NOW, nws=None, observed=None, same_day=False) -> ForecastBundle:
    rnd = random.Random(3)
    vals = [centre + rnd.gauss(0, spread) for _ in range(80)]
    return ForecastBundle(icao, metric, target, fetched_at, (MemberSet("gefs", tuple(vals[:30])), MemberSet("ecmwf", tuple(vals[30:]))),
                          nws_point=nws if nws is not None else centre, observed_so_far=observed, same_day=same_day)


class FakeForecasts:
    def __init__(self, centre=73.0, spread=1.5, fetched_at=NOW, fail=False, **kw):
        self.centre, self.spread, self.fetched_at, self.fail, self.kw = centre, spread, fetched_at, fail, kw
        self.calls = 0

    def bundle(self, st, metric, target):
        self.calls += 1
        if self.fail:
            raise ForecastUnavailable("simulated outage")
        return bundle(self.centre, self.spread, icao=st.icao, metric=metric, target=target, fetched_at=self.fetched_at, **self.kw)


class FakeKalshi:
    def __init__(self, contracts, book=None, fail_discover=False, fail_book=False, books=None):
        self.contracts, self.book = contracts, book or load("kalshi_orderbook.json")
        self.books = books or {}
        self.fail_discover, self.fail_book = fail_discover, fail_book
        self.book_calls = 0

    def discover(self, fetched_at=None):
        if self.fail_discover:
            raise RuntimeError("kalshi down")
        return list(self.contracts), {"series_scanned": 1}

    def get_orderbook(self, ticker):
        self.book_calls += 1
        if self.fail_book:
            raise RuntimeError("book down")
        return self.books.get(ticker, self.book)


class FakePoly:
    def __init__(self, contracts=(), books=None, fail_discover=False):
        self.contracts, self.books, self.fail_discover = list(contracts), books or {}, fail_discover

    def discover(self, fetched_at=None):
        if self.fail_discover:
            raise RuntimeError("polymarket down")
        return list(self.contracts), {}

    def geoblock_status(self):
        return {"blocked": True, "country": "US"}

    def get_book(self, token):
        return self.books[token]


def book(yes_bids=(), no_bids=()):
    """Kalshi-style orderbook payload: resting bids per side as [price, qty]."""
    return {"orderbook_fp": {"yes_dollars": [[str(p), str(q)] for p, q in yes_bids],
                             "no_dollars": [[str(p), str(q)] for p, q in no_bids]}}


def quote(yes_asks=(), no_asks=(), yes_bid=None, no_bid=None, depth=True, at=NOW):
    return Quote(fetched_at=at, yes_asks=tuple(OrderLevel(D(str(p)), D(str(q))) for p, q in yes_asks),
                 no_asks=tuple(OrderLevel(D(str(p)), D(str(q))) for p, q in no_asks),
                 yes_bid=None if yes_bid is None else D(str(yes_bid)), no_bid=None if no_bid is None else D(str(no_bid)),
                 depth_known=depth, liquidity_usd=D("1000"), volume=D("100"))


def seed_calibration(conn, platform="kalshi", icao="KNYC", metric="high", bucket="d0", n=60, bias=0.0, std=2.0):
    from src.weather_edge import store
    store.upsert_calibration(conn, {"platform": platform, "station_icao": icao, "metric": metric, "lead_bucket": bucket,
                                    "n": n, "bias_f": bias, "residual_std_f": std, "bias_se_f": 0.3,
                                    "fitted_at": NOW.isoformat(), "source": "test"})
    conn.commit()


def one_event_contract(cfg_bin_lo=72, cfg_bin_hi=73, platform="kalshi", status="open", **over):
    """A single hand-built Kalshi-style contract for gate tests."""
    from src.weather_edge.models import Bin, WeatherContract
    st = STATIONS["KNYC"]
    base = dict(platform=platform, market_id="T-1", event_key="EV-1", title="t", metric="high",
                target_date=date(2026, 10, 12), station_icao="KNYC", station_label="NYC (CLINYC)",
                settlement_source="The Weather Company", bin=Bin(cfg_bin_lo, cfg_bin_hi), tz=st.tz, status=status,
                fee_model="kalshi_quadratic", fee_param=D("1"), min_order=D("1"), modeled=True)
    base.update(over)
    return WeatherContract(**base)
