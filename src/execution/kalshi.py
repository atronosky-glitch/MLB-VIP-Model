"""Kalshi Trade API provider (Stage 1: read-only).

Verified against docs.kalshi.com on 2026-09-12:
- Base URLs: production https://external-api.kalshi.com/trade-api/v2,
  demo https://external-api.demo.kalshi.co/trade-api/v2 (note the demo
  domain is ".co", not ".com" -- do not "fix" this typo-looking constant).
- Auth headers KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP (ms) /
  KALSHI-ACCESS-SIGNATURE, RSA-PSS (SHA-256, MGF1-SHA256, salt length =
  digest length) over "{timestamp_ms}{method}{path}", where *path*
  includes the "/trade-api/v2" prefix and excludes the query string.

Stage 1-2B only ever issue GET requests. Stage 4.1 verified the exact
Create Order schema directly from Kalshi's own official `kalshi-python`
SDK (PyPI, version 2.1.4, generated from OpenAPI document version
2.0.0, published by Kalshi under github.com/Kalshi) -- the single
strongest available source per this project's own verification
priority (SDK/OpenAPI over scraped docs). That SDK's
`portfolio_api.py` shows the CURRENT order-creation resource path is
`POST /portfolio/orders` with the `CreateOrderRequest` model (ticker,
client_order_id, side "yes"/"no", action "buy"/"sell", count>=1,
type "limit"/"market", yes_price/no_price as INTEGER CENTS 1-99,
expiration_ts, sell_position_floor, buy_max_cost). An EARLIER research
pass (Stage 1/4) had flagged a distinct `/portfolio/events/orders`
V2 endpoint from a docs-site fetch as the target -- the SDK shows no
such path anywhere (confirmed by grepping every resource_path in
portfolio_api.py and events_api.py), so that endpoint appears to be
either deprecated, a different specialized (multi-market "event"
order) product, or a fetch artifact; `/portfolio/orders` is what this
file now targets, backed by the versioned SDK rather than a
JS-rendered docs page. See _submit_authorized_order's docstring for
the full citation and remaining open questions (exact success HTTP
status code, exact client_order_id length/charset limits) that only
live verification against a real account can close out.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from src.execution.base import (
    BestBidAsk, Balance, FeeEstimate, HealthCheckResult, Market,
    NormalizedOrderBook, Orderbook, OrderLevel,
    PredictionMarketProvider, ProviderCapabilities, RawGameEvent, mask_secret,
)
from src.execution.credentials import load_rsa_private_key
from src.execution.signing import build_signed_message, sign_rsa_pss

logger = logging.getLogger(__name__)

_API_PREFIX = "/trade-api/v2"
_BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co",
    "production": "https://external-api.kalshi.com",
}
_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Kalshi's standard (non-margin) taker fee, confirmed against multiple
# 2026 sources describing Kalshi's public maker/taker framework:
# fee = ceil_to_cent(0.07 * C * P * (1-P)). Peaks at 1.75c/contract at
# P=$0.50. Maker fee (25% of taker, on markets that have one) is
# documented but not implemented here -- this stage only analyzes
# taker (immediate/marketable) fills. No per-order dollar cap could be
# confirmed from a primary source -- not invented. Marked as an
# estimate (not confirmed against a primary-source page for this exact
# simple formula, only multiple consistent secondary sources) rather
# than an exact guarantee.
_TAKER_FEE_COEFFICIENT = Decimal("0.07")


def _kalshi_taker_fee(contracts: Decimal, price: Decimal) -> Decimal:
    raw_fee = _TAKER_FEE_COEFFICIENT * contracts * price * (Decimal("1") - price)
    cents = (raw_fee * 100).to_integral_value(rounding=ROUND_CEILING)
    return cents / 100


def build_kalshi_order_payload(
    ticker: str, side: str, count: int, limit_price: Decimal, client_order_id: str,
    ioc_emulation_seconds: int = 5,
) -> dict[str, Any]:
    """Pure payload construction (no I/O) -- the exact CreateOrderRequest
    body _submit_authorized_order sends, extracted so it can be
    inspected/tested/dry-run-printed (e.g. by `provider-diagnostics`)
    without ever touching the network. Schema per the official
    kalshi-python SDK (v2.1.4, OpenAPI doc v2.0.0) -- see the module
    docstring for the full citation. side must already be lowercase
    "yes"/"no"; price is converted from a Decimal dollar fraction to
    the required integer cents (1-99 inclusive)."""
    price_cents = int((limit_price * 100).to_integral_value(rounding=ROUND_HALF_UP))
    price_cents = max(1, min(99, price_cents))

    body: dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": side,
        "action": "buy",
        "count": count,
        "type": "limit",
        "expiration_ts": int(time.time()) + ioc_emulation_seconds,
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    return body


class KalshiProvider(PredictionMarketProvider):
    name = "kalshi"

    def __init__(self, config: Any) -> None:
        env = config.kalshi_env
        if env not in _BASE_URLS:
            raise ValueError(f"kalshi_env must be 'demo' or 'production', got {env!r}")
        self._base_url = _BASE_URLS[env]
        self._api_key_id = config.kalshi_api_key_id
        self._private_key = load_rsa_private_key(config.kalshi_private_key_path)
        self.session = requests.Session()
        logger.info(
            "KalshiProvider initialized: env=%s key_id=%s",
            env, mask_secret(self._api_key_id),
        )

    # -- signing -----------------------------------------------------

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = build_signed_message(timestamp_ms, method, path)
        signature = sign_rsa_pss(self._private_key, message)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def _get(self, endpoint: str, params: dict[str, Any] | None = None, max_retries: int = 3) -> dict:
        """GET *endpoint* (relative to the API prefix) with retry.

        Mirrors src/api_client.py's _request_with_retry: exponential
        backoff on {429,500,502,503,504}/connection errors/timeouts,
        fail-fast on 401/403.
        """
        path = f"{_API_PREFIX}{endpoint}"
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            headers = self._signed_headers("GET", path)
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
                if resp.status_code in (401, 403):
                    raise PermissionError(
                        f"Kalshi rejected credentials (HTTP {resp.status_code}) for key_id="
                        f"{mask_secret(self._api_key_id)}"
                    )
                if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Kalshi retry %d/%d after HTTP %d - sleeping %ds",
                        attempt + 1, max_retries, resp.status_code, wait,
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (RequestsConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Kalshi retry %d/%d after %s - sleeping %ds",
                        attempt + 1, max_retries, type(exc).__name__, wait,
                    )
                    time.sleep(wait)

        raise last_exc  # type: ignore[misc]

    # -- read-only interface ------------------------------------------

    def get_balance(self) -> Balance:
        body = self._get("/portfolio/balance")
        dollars = float(body.get("balance_dollars", body.get("balance", 0) / 100))
        return Balance(currency="USD", available=dollars, raw=body)

    def get_markets(self, **filters: Any) -> list[Market]:
        body = self._get("/markets", params=filters or None)
        return [
            Market(
                id=m.get("ticker", ""),
                title=m.get("yes_sub_title") or m.get("title") or "",
                status=m.get("status", ""),
                raw=m,
            )
            for m in body.get("markets", [])
        ]

    def get_market(self, market_id: str) -> Market:
        body = self._get(f"/markets/{market_id}")
        m = body.get("market", body)
        return Market(
            id=m.get("ticker", market_id),
            title=m.get("yes_sub_title") or m.get("title") or "",
            status=m.get("status", ""),
            raw=m,
        )

    def get_orderbook(self, market_id: str) -> Orderbook:
        """Note: Kalshi's orderbook only ever returns *bids* for each
        side (a yes-bid at X is equivalent to a no-ask at 1-X). This
        method deliberately does NOT perform that price transform --
        bids/asks here map directly to yes_dollars/no_dollars as Kalshi
        returns them. A future stage that needs a single unified book
        for EV/liquidity math should do that transform there, against
        real requirements, not guessed here."""
        body = self._get(f"/markets/{market_id}/orderbook")
        book = body.get("orderbook_fp", body.get("orderbook", {})) or {}
        bids = [(float(p), float(s)) for p, s in (book.get("yes_dollars") or [])]
        asks = [(float(p), float(s)) for p, s in (book.get("no_dollars") or [])]
        return Orderbook(market_id=market_id, bids=bids, asks=asks, raw=body)

    def get_best_bid_ask(self, market_id: str) -> BestBidAsk:
        book = self.get_orderbook(market_id)
        best_bid = book.bids[0][0] if book.bids else None
        best_ask = book.asks[0][0] if book.asks else None
        return BestBidAsk(market_id=market_id, best_bid=best_bid, best_ask=best_ask, raw=book.raw)

    def health_check(self) -> HealthCheckResult:
        try:
            self.get_balance()
            return HealthCheckResult(
                provider=self.name, ok=True, detail="balance fetched successfully",
                checked_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return HealthCheckResult(
                provider=self.name, ok=False, detail=f"{type(exc).__name__}: {exc}",
                checked_at=datetime.now(timezone.utc),
            )

    # -- Stage 2B: executable pricing -----------------------------------

    def estimate_fees(self, side: str, price: Decimal, quantity: Decimal) -> FeeEstimate:
        fee = _kalshi_taker_fee(quantity, price)
        return FeeEstimate(
            fee=fee, fee_estimate=True,
            detail="Kalshi taker fee: ceil_to_cent(0.07*C*P*(1-P)); confirmed via multiple "
                   "consistent 2026 sources, not a single unambiguous primary-source page "
                   "for this exact (non-margin) formula, hence marked as an estimate",
        )

    def normalize_orderbook(self, raw: Orderbook) -> NormalizedOrderBook:
        """Kalshi's orderbook only ever returns BIDS for each side --
        raw.bids is yes_dollars (yes-side bids), raw.asks is Stage 1's
        name for no_dollars (no-side bids, despite the "asks" name it
        was given there). yes_asks/no_asks are synthesized via 1-P:
        this is not a hand-wavy no-arbitrage assumption but a mechanism
        fact -- a resting order to buy NO at price P *is* a resting
        order to sell YES at (1-P), since Kalshi's yes_dollars/
        no_dollars are two views into one shared book, not two
        independently-liquid books."""
        yes_bids = sorted(
            (OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in raw.bids),
            key=lambda lvl: lvl.price, reverse=True,
        )
        no_bids = sorted(
            (OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in raw.asks),
            key=lambda lvl: lvl.price, reverse=True,
        )
        yes_asks = sorted(
            (OrderLevel(Decimal("1") - lvl.price, lvl.quantity) for lvl in no_bids),
            key=lambda lvl: lvl.price,
        )
        no_asks = sorted(
            (OrderLevel(Decimal("1") - lvl.price, lvl.quantity) for lvl in yes_bids),
            key=lambda lvl: lvl.price,
        )
        return NormalizedOrderBook(
            market_id=raw.market_id,
            yes_bids=list(yes_bids), yes_asks=list(yes_asks),
            no_bids=list(no_bids), no_asks=list(no_asks),
            timestamp=datetime.now(timezone.utc),
        )

    # -- Stage 2A: game-level market matching ---------------------------

    _TITLE_SEPARATORS = (" vs. ", " vs ", " @ ")

    def parse_game_event(self, market: Market) -> RawGameEvent | None:
        """UNCONFIRMED against real Kalshi data (their /markets endpoint
        needs signed auth not available in a research context) -- this
        is a narrow first pass over likely title separators, returning
        None for anything that doesn't match rather than guessing
        further. Verify against a real demo-account market before
        trusting this provider's matching (see the Stage 2 plan)."""
        title = market.title or ""
        away = home = None
        for sep in self._TITLE_SEPARATORS:
            if sep in title:
                away, _, home = title.partition(sep)
                break
        if away is None or home is None:
            return None
        away, home = away.strip(), home.strip()
        if not away or not home:
            return None

        event_start_time = None
        raw = market.raw or {}
        for key in ("close_time", "expiration_time", "expected_expiration_time"):
            value = raw.get(key)
            if value:
                event_start_time = _parse_kalshi_timestamp(value)
                if event_start_time:
                    break

        return RawGameEvent(
            home_team=home,
            away_team=away,
            market_type="moneyline",
            side=None,
            line=None,
            event_start_time=event_start_time,
        )

    # -- Stage 4.1: live execution -- schema verified from the official SDK --
    #
    # Source: PyPI package `kalshi-python` 2.1.4, generated from Kalshi's
    # own OpenAPI document version 2.0.0 (github.com/Kalshi), downloaded
    # and inspected directly (kalshi_python/api/portfolio_api.py,
    # kalshi_python/models/{create_order_request,create_order_response,
    # order,fill,position,get_balance_response}.py). This is the
    # strongest available source per this project's own priority order
    # (SDK/OpenAPI over a JS-rendered docs page) and supersedes the
    # earlier, less-certain `/portfolio/events/orders` finding -- see
    # the module docstring.
    #
    # Confirmed CreateOrderRequest fields: ticker (str), client_order_id
    # (optional str -- a REAL idempotency key, unlike Polymarket US),
    # side ("yes"/"no"), action ("buy"/"sell"), count (int >= 1),
    # type ("limit"/"market"), yes_price/no_price (INTEGER CENTS, 1-99
    # inclusive -- NOT a Decimal dollar fraction), expiration_ts
    # (optional int, unix seconds), sell_position_floor, buy_max_cost.
    # Confirmed CreateOrderResponse: {"order": Order}; Order carries
    # order_id, client_order_id, ticker, side, action, type, status
    # (enum: "resting"/"canceled"/"executed"/"pending"), yes_price,
    # no_price, count, remaining_count, expiration_time, created_time,
    # updated_time.
    #
    # Confirmed read endpoints (also from the SDK): GET
    # /portfolio/orders/{order_id} (single order), GET /portfolio/orders
    # (list; query params ticker/event_ticker/min_ts/max_ts/status/
    # limit/cursor -- NOT filterable by client_order_id server-side, so
    # reconciliation-by-client_order_id means listing by ticker+time
    # window and scanning results), GET /portfolio/fills (query params
    # include order_id directly), GET /portfolio/positions.
    #
    # STILL UNCONFIRMED without a live account (flagged, not guessed):
    # the exact success HTTP status code (assumed 200/201, the standard
    # REST convention -- both are accepted defensively below); whether
    # Kalshi enforces a client_order_id length/charset limit narrower
    # than this project's ~43-character token_urlsafe id; and whether
    # "executed" with remaining_count > 0 can occur (partial fill still
    # reported as "executed" vs. a separate partial state) -- handled
    # defensively by deriving fill status from count/remaining_count
    # rather than trusting the status string alone.
    #
    # Kalshi has NO native time-in-force field. IOC is emulated via a
    # short expiration_ts (LIVE_ORDER_MODE=IOC_LIMIT's "safest available
    # short-lived mechanism" per the original Stage 4 plan) -- an order
    # that doesn't fill before expiration_ts is expected to cancel
    # itself, but this specific behavior is also unverified live.

    _IOC_EMULATION_SECONDS = 5

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_preview=False,             # no preview endpoint found in the SDK
            supports_client_idempotency=True,   # client_order_id confirmed in CreateOrderRequest
            supports_ioc=False,                 # no native TIF field -- emulated via expiration_ts
            supports_fok=False,
            supports_order_lookup=True,         # GET /portfolio/orders/{id} and /portfolio/orders confirmed
            supports_fill_lookup=True,           # GET /portfolio/fills confirmed, filterable by order_id
            supports_cancel=True,                # POST /portfolio/orders/{id} (cancel) path exists in the SDK
            supports_modify=True,                # POST /portfolio/orders/{id}/amend path exists in the SDK
        )

    def _submit_authorized_order(
        self, authorization: Any, quantity: Decimal, limit_price: Decimal,
    ) -> "LiveSubmissionOutcome":
        from src.execution.base import LiveSubmissionOutcome

        if not KALSHI_LIVE_SCHEMA_VERIFIED:
            raise KalshiLiveSchemaUnverifiedError(
                "Kalshi live order submission is blocked: KALSHI_LIVE_SCHEMA_VERIFIED=False."
            )

        side = authorization.side.lower()
        if side not in ("yes", "no"):
            return LiveSubmissionOutcome(
                outcome="AMBIGUOUS", provider_order_id=None,
                detail=f"unrecognized side {authorization.side!r}, refusing to guess yes/no", raw_reference=None,
            )

        count = int(quantity)  # truncates toward zero; quantity is always a non-negative whole contract count here
        if count < 1:
            return LiveSubmissionOutcome(
                outcome="AMBIGUOUS", provider_order_id=None,
                detail=f"quantity {quantity} rounds to zero whole contracts", raw_reference=None,
            )

        body = build_kalshi_order_payload(
            ticker=authorization.provider_market_id, side=side, count=count, limit_price=limit_price,
            client_order_id=authorization.approval_id, ioc_emulation_seconds=self._IOC_EMULATION_SECONDS,
        )

        path = f"{_API_PREFIX}/portfolio/orders"
        try:
            headers = self._signed_headers("POST", path)
            resp = self.session.post(f"{self._base_url}{path}", json=body, headers=headers, timeout=30)
        except (RequestsConnectionError, requests.exceptions.Timeout) as exc:
            return LiveSubmissionOutcome(
                outcome="AMBIGUOUS", provider_order_id=None,
                detail=f"transport failure before a response was received: {type(exc).__name__}", raw_reference=None,
            )
        except Exception as exc:
            return LiveSubmissionOutcome(
                outcome="AMBIGUOUS", provider_order_id=None,
                detail=f"unexpected error submitting order: {type(exc).__name__}", raw_reference=None,
            )

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except ValueError:
                return LiveSubmissionOutcome(
                    outcome="AMBIGUOUS", provider_order_id=None,
                    detail=f"HTTP {resp.status_code} with an unparseable body", raw_reference=None,
                )
            order = data.get("order") or {}
            order_id = order.get("order_id")
            if not order_id:
                return LiveSubmissionOutcome(
                    outcome="AMBIGUOUS", provider_order_id=None,
                    detail=f"HTTP {resp.status_code} response had no order_id", raw_reference=None,
                )
            order_count = order.get("count")
            remaining = order.get("remaining_count")
            quantity_filled = (
                Decimal(str(order_count - remaining)) if order_count is not None and remaining is not None else None
            )
            avg_price = None
            if quantity_filled and quantity_filled > 0:
                filled_price_cents = order.get("yes_price") if side == "yes" else order.get("no_price")
                avg_price = Decimal(str(filled_price_cents)) / 100 if filled_price_cents is not None else None
            return LiveSubmissionOutcome(
                outcome="CONFIRMED", provider_order_id=str(order_id), detail="order accepted",
                raw_reference=f"HTTP {resp.status_code}", quantity_filled=quantity_filled,
                average_fill_price=avg_price, order_state=order.get("status"),
            )

        if resp.status_code in (400, 422):
            return LiveSubmissionOutcome(
                outcome="REJECTED", provider_order_id=None,
                detail=f"provider rejected the order: HTTP {resp.status_code}",
                raw_reference=_sanitize_kalshi_error_body(resp),
            )
        if resp.status_code in (401, 403):
            return LiveSubmissionOutcome(
                outcome="REJECTED", provider_order_id=None,
                detail=f"authentication/authorization failure: HTTP {resp.status_code}", raw_reference=None,
            )

        return LiveSubmissionOutcome(
            outcome="AMBIGUOUS", provider_order_id=None,
            detail=f"ambiguous provider response: HTTP {resp.status_code}",
            raw_reference=_sanitize_kalshi_error_body(resp),
        )

    # -- Stage 4.1: read-only reconciliation surface ---------------------

    def get_order_by_id(self, order_id: str) -> dict | None:
        try:
            body = self._get(f"/portfolio/orders/{order_id}")
        except Exception:
            return None
        return body.get("order")

    def get_recent_orders(self, **filters: Any) -> list[dict] | None:
        params = {k: v for k, v in filters.items() if v is not None}
        try:
            body = self._get("/portfolio/orders", params=params or None)
        except Exception:
            return None
        return body.get("orders", [])

    def get_fills(self, **filters: Any) -> list[dict] | None:
        params = {k: v for k, v in filters.items() if v is not None}
        try:
            body = self._get("/portfolio/fills", params=params or None)
        except Exception:
            return None
        return body.get("fills", [])

    def get_positions(self, **filters: Any) -> list[dict] | None:
        params = {k: v for k, v in filters.items() if v is not None}
        try:
            body = self._get("/portfolio/positions", params=params or None)
        except Exception:
            return None
        return body.get("positions", [])


_SENSITIVE_BODY_PATTERNS = ("key", "secret", "signature", "token", "password", "credential", "auth")


def _sanitize_kalshi_error_body(resp: "requests.Response") -> str:
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code} (non-JSON body, {len(resp.content)} bytes)"
    if isinstance(data, dict):
        redacted = {
            k: ("<redacted>" if any(p in k.lower() for p in _SENSITIVE_BODY_PATTERNS) else v)
            for k, v in data.items()
        }
        return f"HTTP {resp.status_code}: {redacted}"
    return f"HTTP {resp.status_code}"


class KalshiLiveSchemaUnverifiedError(Exception):
    """Raised only if KALSHI_LIVE_SCHEMA_VERIFIED is ever manually set
    back to False -- kept as a distinct, documented exception type
    (rather than removed) so a future rollback has a clear, specific
    error to raise instead of silently falling through to base.py's
    generic NotImplementedError."""


# Deliberately NOT an environment variable -- no .env edit can flip
# this. Set True in Stage 4.1 after the exact CreateOrderRequest/
# CreateOrderResponse schema was confirmed directly from Kalshi's
# official `kalshi-python` PyPI SDK (v2.1.4, OpenAPI doc v2.0.0) --
# see _submit_authorized_order's docstring for the full citation.
# This still does NOT enable live trading by itself: LIVE_TRADING_ENABLED,
# KALSHI_LIVE_ENABLED, REQUIRE_HUMAN_APPROVAL, and a real human approval
# are all independently required (src/execution/live/service.py::
# _can_attempt_live_trade). No production order has been submitted --
# this environment has no Kalshi credentials configured at all.
KALSHI_LIVE_SCHEMA_VERIFIED = True


def _parse_kalshi_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of a Kalshi timestamp field -- format
    unconfirmed (could be ISO8601 or Unix seconds); tries both."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
