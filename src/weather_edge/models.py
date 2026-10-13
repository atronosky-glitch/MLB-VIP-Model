"""Plain data types shared across the Weather Edge package (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from src.execution.base import OrderLevel


@dataclass(frozen=True)
class Bin:
    """The set of WHOLE-degree reported values that make a contract resolve YES.

    ``lo``/``hi`` are inclusive integers; ``None`` means unbounded on that side.
    Kalshi and Polymarket both settle temperature markets on a reported whole-degree value, so
    "77° or above" is lo=77/hi=None and "75° to 76°" is lo=75/hi=76.
    """

    lo: int | None
    hi: int | None

    def __post_init__(self):
        if self.lo is None and self.hi is None:
            raise ValueError("a bin must be bounded on at least one side")
        if self.lo is not None and self.hi is not None and self.lo > self.hi:
            raise ValueError(f"empty bin: {self.lo} > {self.hi}")

    def contains(self, value: int) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)

    @property
    def label(self) -> str:
        if self.lo is None:
            return f"{self.hi}° or below"
        if self.hi is None:
            return f"{self.lo}° or above"
        return f"{self.lo}°" if self.lo == self.hi else f"{self.lo}° to {self.hi}°"


@dataclass(frozen=True)
class Quote:
    """Executable prices for ONE contract, in dollars per $1-payout share.

    ``yes_asks`` / ``no_asks`` are what a buyer pays, best (lowest) price first. ``depth_known``
    is False when only the top of book was available (market-list payload), in which case the
    asks hold a single level and a larger order is NOT assumed fillable beyond it.
    """

    fetched_at: datetime
    yes_asks: tuple[OrderLevel, ...] = ()
    no_asks: tuple[OrderLevel, ...] = ()
    yes_bid: Decimal | None = None
    no_bid: Decimal | None = None
    depth_known: bool = False
    liquidity_usd: Decimal | None = None
    volume: Decimal | None = None

    @property
    def yes_ask(self) -> Decimal | None:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def no_ask(self) -> Decimal | None:
        return self.no_asks[0].price if self.no_asks else None

    @property
    def yes_spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def yes_mid(self) -> Decimal | None:
        """Market-implied probability of YES: the midpoint when both sides quote, else None.
        (Never the last trade, which may be stale.)"""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2


@dataclass(frozen=True)
class WeatherContract:
    platform: str                      # "kalshi" | "polymarket"
    market_id: str                     # Kalshi ticker / Polymarket market id
    event_key: str                     # Kalshi event ticker / Polymarket event slug
    title: str
    metric: str                        # "high" | "low" | "rain" | "snow" | "other"
    target_date: date | None
    station_icao: str | None           # settlement station if known AND in the registry
    station_label: str                 # station as the market's own rules name it
    settlement_source: str             # e.g. "The Weather Company", "NOAA (weather.gov/wrh/timeseries)"
    bin: Bin | None
    unit: str = "F"
    tz: str | None = None
    status: str = "open"               # open | closed | settled
    close_time: datetime | None = None
    fee_model: str = ""                # "kalshi_quadratic" | "polymarket_curve"
    fee_param: Decimal = Decimal("0")  # Kalshi multiplier / Polymarket taker rate
    min_order: Decimal = Decimal("1")
    modeled: bool = False
    unmodeled_reason: str | None = None
    rules_text: str = ""
    yes_token: str | None = None       # Polymarket outcome token ids
    no_token: str | None = None
    quote: Quote | None = None
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def group_key(self) -> tuple:
        """Contracts sharing this key are mutually exclusive outcomes of ONE weather event."""
        return (self.platform, self.event_key)


@dataclass(frozen=True)
class MemberSet:
    """Daily-extreme values (°F) from every ensemble member of one model family."""

    family: str                        # "gefs" | "ecmwf"
    values: tuple[float, ...]


@dataclass(frozen=True)
class ForecastBundle:
    station_icao: str
    metric: str
    target_date: date
    fetched_at: datetime
    members: tuple[MemberSet, ...]
    nws_point: float | None = None     # NWS deterministic daily extreme, if available
    observed_so_far: float | None = None
    same_day: bool = False
    source_note: str = ""

    @property
    def all_values(self) -> list[float]:
        return [v for m in self.members for v in m.values]
