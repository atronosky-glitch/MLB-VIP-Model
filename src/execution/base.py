"""Provider-agnostic prediction-market interface (Stage 1: read-only).

Concrete providers (``src.execution.kalshi``, ``src.execution.polymarket_us``)
implement this so nothing above them (a future market matcher, risk engine,
or order-preparation step) needs to know which venue it's talking to.

Stage 1 only covers read-only market discovery and account health --
``place_order``/``cancel_order`` are declared here as concrete methods that
raise ``NotImplementedError`` rather than as abstract methods, so a later
stage can add order support to one provider at a time without forcing every
other subclass to grow stub implementations first, and so nothing built in
this stage can reach a write path by accident.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def mask_secret(value: str) -> str:
    """Return a masked version of a key/id/secret -- never the full value.

    Mirrors src/api_client.py's _mask_key so credential-adjacent strings
    (key IDs, not private key material -- that's never logged at all, see
    src/execution/credentials.py) are safe to include in log lines.
    """
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return value[:2] + "..." + f"({len(value)} chars)"
    return value[:4] + "..." + value[-2:]


@dataclass(frozen=True)
class Balance:
    currency: str
    available: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Market:
    id: str
    title: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Orderbook:
    market_id: str
    bids: list[tuple[float, float]]  # (price, size), best first
    asks: list[tuple[float, float]]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class BestBidAsk:
    market_id: str
    best_bid: float | None
    best_ask: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class HealthCheckResult:
    provider: str
    ok: bool
    detail: str
    checked_at: datetime


@dataclass(frozen=True)
class RawGameEvent:
    """Best-effort extraction of a game-level market's identity from a
    provider's title/slug/raw payload -- see parse_game_event below.

    Deliberately does NOT carry provider/market_id/raw_market -- those
    are already on the Market that was parsed, so a caller combining
    this with its Market has everything without duplication. Kept in
    base.py (not src/execution/matching.py) specifically so this
    interface has no dependency on the matching module -- matching.py
    depends on base.py, not the other way around.
    """
    home_team: str | None
    away_team: str | None
    market_type: str | None  # normalized to "moneyline" / "spread" / "total"
    side: str | None
    line: float | None
    event_start_time: datetime | None


class PredictionMarketProvider(ABC):
    """Read-only interface every prediction-market provider implements.

    Order placement (place_order/cancel_order) is intentionally NOT part
    of this stage -- see module docstring.
    """

    name: str

    @abstractmethod
    def get_balance(self) -> Balance:
        ...

    @abstractmethod
    def get_markets(self, **filters: Any) -> list[Market]:
        ...

    @abstractmethod
    def get_market(self, market_id: str) -> Market:
        ...

    @abstractmethod
    def get_orderbook(self, market_id: str) -> Orderbook:
        ...

    @abstractmethod
    def get_best_bid_ask(self, market_id: str) -> BestBidAsk:
        ...

    @abstractmethod
    def health_check(self) -> HealthCheckResult:
        ...

    def parse_game_event(self, market: Market) -> RawGameEvent | None:
        """Best-effort extraction of (teams, market_type, side, line,
        date) from this provider's title/slug/raw payload for a
        game-level market (Stage 2 -- see src/execution/matching.py).
        Concrete default returns None (not abstract); a provider that
        hasn't implemented this yet simply contributes no candidates
        rather than every subclass needing a stub. Must never raise --
        anything unparseable is None, not a match candidate."""
        return None

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__}.place_order is not available until a "
            "later execution-layer stage"
        )

    def cancel_order(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            f"{type(self).__name__}.cancel_order is not available until a "
            "later execution-layer stage"
        )
