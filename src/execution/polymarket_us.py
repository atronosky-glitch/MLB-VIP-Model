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
from typing import Any

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from src.execution.base import (
    BestBidAsk, Balance, HealthCheckResult, Market, Orderbook,
    PredictionMarketProvider, RawGameEvent, mask_secret,
)
from src.execution.credentials import load_ed25519_private_key
from src.execution.signing import build_signed_message, sign_ed25519

logger = logging.getLogger(__name__)

_PUBLIC_BASE_URL = "https://gateway.polymarket.us"
_AUTH_BASE_URL = "https://api.polymarket.us"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


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

    # -- Stage 2: game-level market matching ---------------------------

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
