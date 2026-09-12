"""Kalshi Trade API provider (Stage 1: read-only).

Verified against docs.kalshi.com on 2026-09-12:
- Base URLs: production https://external-api.kalshi.com/trade-api/v2,
  demo https://external-api.demo.kalshi.co/trade-api/v2 (note the demo
  domain is ".co", not ".com" -- do not "fix" this typo-looking constant).
- Auth headers KALSHI-ACCESS-KEY / KALSHI-ACCESS-TIMESTAMP (ms) /
  KALSHI-ACCESS-SIGNATURE, RSA-PSS (SHA-256, MGF1-SHA256, salt length =
  digest length) over "{timestamp_ms}{method}{path}", where *path*
  includes the "/trade-api/v2" prefix and excludes the query string.

This file only ever issues GET requests. Order placement is a future
stage, and per docs.kalshi.com must use the newer
POST /portfolio/events/orders schema, not the legacy /portfolio/orders
endpoint most tutorials still show (Kalshi's own docs say that legacy
path "will be deprecated no earlier than May 6, 2026" -- already past).
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
from src.execution.credentials import load_rsa_private_key
from src.execution.signing import build_signed_message, sign_rsa_pss

logger = logging.getLogger(__name__)

_API_PREFIX = "/trade-api/v2"
_BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co",
    "production": "https://external-api.kalshi.com",
}
_RETRY_STATUSES = {429, 500, 502, 503, 504}


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

    # -- Stage 2: game-level market matching ---------------------------

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
