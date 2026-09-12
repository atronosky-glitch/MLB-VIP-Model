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
from decimal import Decimal
from typing import Any, NamedTuple


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


class OrderLevel(NamedTuple):
    """One price level in a NormalizedOrderBook. A NamedTuple (not a
    plain tuple) specifically so real money math can never mix up which
    slot is price vs. quantity -- Stage 1's Orderbook.bids/.asks (plain
    float tuples) are untouched; this is Stage 2B's Decimal-based type."""
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class NormalizedOrderBook:
    """Fully-normalized, Decimal, four-sided book for a binary
    (YES/NO) contract -- built by each provider's normalize_orderbook()
    from its own Stage 1 Orderbook. Named NormalizedOrderBook rather
    than the more obvious "OrderBook" specifically to avoid a one-letter
    capitalization collision with Stage 1's existing Orderbook class,
    since both are legitimately imported side by side in evaluator.py.

    yes_asks/no_asks may be synthesized via the 1-P transform rather
    than directly observed -- see each provider's normalize_orderbook
    docstring for whether that's confirmed-valid (Kalshi) or a
    documented-but-unconfirmed default (Polymarket US) for that venue.
    """
    market_id: str
    yes_bids: list[OrderLevel]
    yes_asks: list[OrderLevel]
    no_bids: list[OrderLevel]
    no_asks: list[OrderLevel]
    timestamp: datetime


@dataclass(frozen=True)
class FeeEstimate:
    """A provider's estimate_fees() result. fee_estimate=True flags
    that the exact fee cannot be known before execution (e.g. a maker
    fill's exact resting-time-dependent rate) and this is the most
    conservative reasonable estimate, not a guarantee."""
    fee: Decimal
    fee_estimate: bool
    detail: str


class PredictionMarketProvider(ABC):
    """Read-only interface every prediction-market provider implements.

    Order placement (place_order/cancel_order) is intentionally NOT part
    of this stage -- see module docstring.
    """

    name: str

    # Explicit, always-False-in-this-stage marker (item 17): no
    # provider supports live execution yet -- place_order/cancel_order
    # below always raise NotImplementedError regardless of this flag,
    # but the flag itself gives calling code (dashboards, future
    # approval UI) an explicit thing to check/display rather than
    # inferring "no execution" from an absence of behavior.
    supports_live_execution: bool = False

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

    @abstractmethod
    def normalize_orderbook(self, raw: Orderbook) -> NormalizedOrderBook:
        """Convert this provider's Stage 1 Orderbook into a fully
        normalized, Decimal, four-sided (yes_bids/yes_asks/no_bids/
        no_asks) book. Stage 2B (src/execution/orderbook_math.py etc.)
        operates only on this normalized shape -- never on Stage 1's
        raw Orderbook directly."""
        ...

    @abstractmethod
    def estimate_fees(self, side: str, price: Decimal, quantity: Decimal) -> FeeEstimate:
        """Estimate the taker fee for buying *quantity* contracts of
        *side* ("YES"/"NO") at *price*, using this provider's current,
        documented fee formula. TAKER fills only -- this stage's whole
        premise is "can I execute right now," so maker/resting-order
        fees are out of scope."""
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
