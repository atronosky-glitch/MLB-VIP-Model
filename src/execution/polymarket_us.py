"""Polymarket US provider (Stage 1: read-only).

Polymarket US is a distinct, KYC-gated, regulated US product
(docs.polymarket.us) -- NOT the international polymarket.com CLOB.
Verified against docs.polymarket.us on 2026-09-12:

- Public market data (no auth): https://gateway.polymarket.us
    GET /v1/markets                    -- list markets
    GET /v1/market/slug/{slug}          -- single market by slug
    GET /v1/markets/{slug}/book          -- order book
    GET /v1/markets/{slug}/bbo            -- best bid/offer
- Authenticated account data: https://api.polymarket.us
    GET /v1/account/balances
  Auth headers: X-PM-Access-Key / X-PM-Timestamp (ms) / X-PM-Signature,
  Ed25519 (not RSA) over "{timestamp_ms}{method}{path}".

Unlike Kalshi, there is no sandbox/demo environment for Polymarket US --
production API access requires completed KYC. health_check() here
necessarily checks live production credentials (read-only, GET-only);
there is deliberately no env/demo branching in this provider.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from src.execution.base import (
    BestBidAsk, Balance, FeeEstimate, HealthCheckResult, LiveSubmissionOutcome, Market,
    NormalizedOrderBook, Orderbook, OrderLevel,
    PredictionMarketProvider, ProviderCapabilities, RawGameEvent, mask_secret,
)
from src.execution.credentials import load_ed25519_private_key
from src.execution.signing import build_signed_message, sign_ed25519

logger = logging.getLogger(__name__)

_PUBLIC_BASE_URL = "https://gateway.polymarket.us"
_AUTH_BASE_URL = "https://api.polymarket.us"
_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Confirmed directly from the primary source, docs.polymarket.us/fees
# (2026-09-12): taker fee = round_half_even_to_cent(0.06*C*P*(1-P)).
# Worked examples from that page: at P=$0.50, C=1000, taker fee = $15.00;
# banker's rounding examples $0.025->$0.02 (down to even), $0.035->$0.04
# (up to even) -- explicitly NOT the same rounding rule as Kalshi's
# round-up. Maker rebate (0.0125*C*P*(1-P), paid TO the maker) is
# documented but not implemented -- this stage only analyzes taker
# (immediate/marketable) fills.
_TAKER_FEE_COEFFICIENT = Decimal("0.06")


def _polymarket_taker_fee(contracts: Decimal, price: Decimal) -> Decimal:
    raw_fee = _TAKER_FEE_COEFFICIENT * contracts * price * (Decimal("1") - price)
    cents = (raw_fee * 100).to_integral_value(rounding=ROUND_HALF_EVEN)
    return cents / 100


class PolymarketUSProvider(PredictionMarketProvider):
    name = "polymarket_us"

    def __init__(self, config: Any) -> None:
        self._api_key_id = config.polymarket_us_api_key_id
        self._private_key = load_ed25519_private_key(config.polymarket_us_private_key_path)
        self.session = requests.Session()
        logger.info(
            "PolymarketUSProvider initialized: key_id=%s", mask_secret(self._api_key_id),
        )

    # -- signing -----------------------------------------------------

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = build_signed_message(timestamp_ms, method, path)
        signature = sign_ed25519(self._private_key, message)
        return {
            "X-PM-Access-Key": self._api_key_id,
            "X-PM-Timestamp": timestamp_ms,
            "X-PM-Signature": signature,
        }

    def _get(
        self, base_url: str, path: str, *, authenticated: bool,
        params: dict[str, Any] | None = None, max_retries: int = 3,
    ) -> dict:
        """GET *path* with retry. Mirrors src/api_client.py's backoff
        shape. *authenticated* controls whether Ed25519-signed headers
        are attached at all -- public market-data calls carry none."""
        url = f"{base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            headers = self._signed_headers("GET", path) if authenticated else {}
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=30)
                if resp.status_code in (401, 403):
                    raise PermissionError(
                        f"Polymarket US rejected credentials (HTTP {resp.status_code}) for key_id="
                        f"{mask_secret(self._api_key_id)}"
                    )
                if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Polymarket US retry %d/%d after HTTP %d - sleeping %ds",
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
                        "Polymarket US retry %d/%d after %s - sleeping %ds",
                        attempt + 1, max_retries, type(exc).__name__, wait,
                    )
                    time.sleep(wait)

        raise last_exc  # type: ignore[misc]

    def _public_get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        return self._get(_PUBLIC_BASE_URL, path, authenticated=False, params=params)

    def _authenticated_get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        return self._get(_AUTH_BASE_URL, path, authenticated=True, params=params)

    # -- read-only interface (public market data) ---------------------

    def get_markets(self, **filters: Any) -> list[Market]:
        body = self._public_get("/v1/markets", params=filters or None)
        return [
            Market(
                id=m.get("slug", m.get("id", "")),
                title=m.get("title") or m.get("question") or "",
                status="closed" if m.get("closed") else ("active" if m.get("active") else "unknown"),
                raw=m,
            )
            for m in body.get("markets", [])
        ]

    def get_market(self, market_id: str) -> Market:
        body = self._public_get(f"/v1/market/slug/{market_id}")
        m = body.get("market", body)
        return Market(
            id=m.get("slug", market_id),
            title=m.get("title") or m.get("question") or "",
            status="closed" if m.get("closed") else ("active" if m.get("active") else "unknown"),
            raw=m,
        )

    def get_orderbook(self, market_id: str) -> Orderbook:
        body = self._public_get(f"/v1/markets/{market_id}/book")
        market_data = body.get("marketData", {}) or {}
        bids = [
            (float(e["px"]["value"]), float(e["qty"]))
            for e in market_data.get("bids", [])
        ]
        asks = [
            (float(e["px"]["value"]), float(e["qty"]))
            for e in market_data.get("offers", [])
        ]
        return Orderbook(market_id=market_id, bids=bids, asks=asks, raw=body)

    def get_best_bid_ask(self, market_id: str) -> BestBidAsk:
        body = self._public_get(f"/v1/markets/{market_id}/bbo")
        market_data = body.get("marketData", {}) or {}
        best_bid_raw = market_data.get("bestBid")
        best_ask_raw = market_data.get("bestAsk")
        best_bid = float(best_bid_raw["value"]) if best_bid_raw else None
        best_ask = float(best_ask_raw["value"]) if best_ask_raw else None
        return BestBidAsk(market_id=market_id, best_bid=best_bid, best_ask=best_ask, raw=body)

    # -- read-only interface (authenticated account data) --------------

    def get_balance(self) -> Balance:
        body = self._authenticated_get("/v1/account/balances")
        balances = body.get("balances", [])
        if not balances:
            return Balance(currency="USD", available=0.0, raw=body)
        first = balances[0]
        return Balance(
            currency=first.get("currency", "USD"),
            available=float(first.get("currentBalance", 0)),
            raw=body,
        )

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
        fee = _polymarket_taker_fee(quantity, price)
        return FeeEstimate(
            fee=fee, fee_estimate=False,
            detail="Polymarket US taker fee: round_half_even_to_cent(0.06*C*P*(1-P)); "
                   "confirmed directly against docs.polymarket.us/fees's own worked examples",
        )

    def normalize_orderbook(self, raw: Orderbook) -> NormalizedOrderBook:
        """UNCONFIRMED whether Polymarket US models NO as genuinely
        separate, independently-tradeable liquidity (as the
        international Polymarket CLOB does with separate outcome
        tokens) or as a single binary book where NO = 1-YES (as
        Kalshi's docs explicitly confirm for Kalshi specifically).
        docs.polymarket.us describes one instrument per game outcome
        (matching a live /v1/markets fetch that showed one row per
        game, not one per team) which is consistent with -- but does
        not prove -- the single-book structure. raw.bids/raw.asks are
        treated as the YES side directly (Polymarket's book is already
        a normal bidirectional book, unlike Kalshi's bids-only-per-side
        quirk), and NO is synthesized via 1-P as the documented,
        conservative default. Verify via `inspect-markets --raw`
        against real data -- flagged the same way Stage 2A flagged
        Kalshi's title-parsing as unconfirmed."""
        yes_bids = sorted(
            (OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in raw.bids),
            key=lambda lvl: lvl.price, reverse=True,
        )
        yes_asks = sorted(
            (OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in raw.asks),
            key=lambda lvl: lvl.price,
        )
        no_bids = sorted(
            (OrderLevel(Decimal("1") - lvl.price, lvl.quantity) for lvl in yes_asks),
            key=lambda lvl: lvl.price, reverse=True,
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

    def parse_game_event(self, market: Market) -> RawGameEvent | None:
        """Polymarket US's slug grammar, confirmed live 2026-09-12:
        "{prefix}-{league}-{away_abbr}-{home_abbr}-{YYYY}-{MM}-{DD}"
        (e.g. "aec-nfl-lac-ten-2025-11-02" for "Los Angeles vs. Tennessee",
        away-then-home matching this repo's own matchup convention).

        market_type/side are NOT confirmed against real non-moneyline
        data yet -- this always reports "moneyline", matching Stage 2's
        deliberately narrow scope (see src/execution/matching.py's module
        docstring). Revisit once spread/total markets are verified live.
        """
        parts = (market.id or "").split("-")
        if len(parts) < 7:
            return None
        _prefix, _league_token, away_abbr, home_abbr, year, month, day = parts[:7]
        try:
            event_date = datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
        except ValueError:
            return None
        return RawGameEvent(
            home_team=home_abbr,
            away_team=away_abbr,
            market_type="moneyline",
            side=None,
            line=None,
            event_start_time=event_date,
        )

    # -- Stage 4: live execution -----------------------------------------
    #
    # POST /v1/orders, confirmed directly from docs.polymarket.us
    # (2026-09-12). Confirmed request fields used here: marketSlug,
    # type (ORDER_TYPE_LIMIT), price {value, currency}, quantity,
    # outcomeSide (YES/NO), action (BUY/SELL), tif (TimeInForce enum --
    # the exact "TIME_IN_FORCE_IOC" string form was inferred from the
    # single confirmed sibling value "TIME_IN_FORCE_GOOD_TILL_DATE" and
    # is flagged for live verification, same posture as everything else
    # in this repo marked "best-effort"), manualOrderIndicator (confirmed
    # via the original Stage 1 research: MANUAL_ORDER_INDICATOR_MANUAL /
    # _AUTOMATIC -- MANUAL is used since a human explicitly approved
    # every order this method ever sends). Confirmed response fields:
    # top-level "id" (exchange-assigned order id).
    #
    # Confirmed limitations (not guessed around): Polymarket US has NO
    # client-supplied idempotency key and NO order-preview endpoint.
    # This method therefore NEVER retries internally -- exactly one
    # HTTP POST per call, full stop. LiveExecutionService is the one
    # that decides whether an ambiguous outcome ever gets a second
    # attempt (governed by MAX_FINANCIAL_POST_ATTEMPTS_PER_APPROVAL,
    # which defaults to and is validated to stay 1).

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_preview=False,             # confirmed absent from current docs
            supports_client_idempotency=False,  # confirmed absent from current docs
            supports_ioc=True,                  # tif: TIME_IN_FORCE_IOC (best-effort exact string)
            supports_fok=True,                  # tif: TIME_IN_FORCE_FOK (best-effort exact string)
            supports_order_lookup=False,        # not implemented this stage -- see module docstring
            supports_fill_lookup=False,
            supports_cancel=False,
            supports_modify=False,
        )

    def _submit_authorized_order(
        self, authorization: Any, quantity: Decimal, limit_price: Decimal,
    ) -> LiveSubmissionOutcome:
        """Exactly one HTTP POST, no internal retry. Distinguishes
        CONFIRMED (order id returned) / REJECTED (a synchronous
        validation or auth failure, proven no order exists) / AMBIGUOUS
        (timeout, connection error, or any response this code can't
        positively classify -- treated as "order may exist," never
        auto-retried)."""
        path = "/v1/orders"
        body = {
            "marketSlug": authorization.provider_market_id,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(limit_price), "currency": "USD"},
            "quantity": float(quantity),
            "tif": "TIME_IN_FORCE_IOC",
            "outcomeSide": authorization.side,
            "action": "BUY",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
            "synchronousExecution": True,
        }

        try:
            headers = self._signed_headers("POST", path)
            headers["Content-Type"] = "application/json"
            resp = self.session.post(f"{_AUTH_BASE_URL}{path}", json=body, headers=headers, timeout=30)
        except (RequestsConnectionError, requests.exceptions.Timeout) as exc:
            return LiveSubmissionOutcome(
                outcome="AMBIGUOUS", provider_order_id=None,
                detail=f"transport failure before a response was received: {type(exc).__name__}",
                raw_reference=None,
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
            order_id = data.get("id")
            if not order_id:
                return LiveSubmissionOutcome(
                    outcome="AMBIGUOUS", provider_order_id=None,
                    detail=f"HTTP {resp.status_code} response had no order id", raw_reference=None,
                )
            # synchronousExecution=True was requested, so a confirmed
            # response SHOULD carry immediate fill data via
            # executions[0].order -- extracted defensively (confirmed
            # field names: avgPx, cumQuantity, state; never guessed if
            # missing or unparseable, since "we don't know" is safer
            # than a wrong fill amount).
            qty_filled, avg_px, order_state = None, None, None
            executions = data.get("executions") or []
            if executions and isinstance(executions, list):
                order_data = (executions[0] or {}).get("order") or {}
                try:
                    if order_data.get("cumQuantity") is not None:
                        qty_filled = Decimal(str(order_data["cumQuantity"]))
                    if order_data.get("avgPx") is not None:
                        avg_px = Decimal(str(order_data["avgPx"]))
                    order_state = order_data.get("state")
                except (TypeError, ValueError, ArithmeticError):
                    qty_filled, avg_px, order_state = None, None, None
            return LiveSubmissionOutcome(
                outcome="CONFIRMED", provider_order_id=str(order_id),
                detail="order accepted", raw_reference=f"HTTP {resp.status_code}",
                quantity_filled=qty_filled, average_fill_price=avg_px, order_state=order_state,
            )

        if resp.status_code in (400, 422):
            # A synchronous validation rejection -- proven no order was
            # created (these codes are returned before order matching).
            return LiveSubmissionOutcome(
                outcome="REJECTED", provider_order_id=None,
                detail=f"provider rejected the order: HTTP {resp.status_code}",
                raw_reference=_sanitize_error_body(resp),
            )

        if resp.status_code in (401, 403):
            # Authentication failure -- the request never reached order
            # processing, so no order could have been created.
            return LiveSubmissionOutcome(
                outcome="REJECTED", provider_order_id=None,
                detail=f"authentication/authorization failure: HTTP {resp.status_code}", raw_reference=None,
            )

        # 409/429/5xx/anything else: cannot prove an order was or was
        # not created. Fail closed to AMBIGUOUS rather than guess.
        return LiveSubmissionOutcome(
            outcome="AMBIGUOUS", provider_order_id=None,
            detail=f"ambiguous provider response: HTTP {resp.status_code}",
            raw_reference=_sanitize_error_body(resp),
        )


_SENSITIVE_BODY_PATTERNS = ("key", "secret", "signature", "token", "password", "credential", "auth")


def _sanitize_error_body(resp: "requests.Response") -> str:
    """A short, credential-redacted summary of an error response --
    never the raw body, never headers (which would include the
    Ed25519 signature)."""
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
