"""Small, provider-compliant HTTP helper for the PUBLIC read-only endpoints Weather Edge uses.

* identifies itself with a User-Agent (NWS requires one),
* hard timeouts on every request,
* retries 429 / 5xx / network errors with exponential backoff, honouring ``Retry-After``,
* a minimum interval between calls so a scan can never hammer a provider,
* never sends credentials, and never logs query strings or response bodies.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = os.environ.get(
    "WEATHER_EDGE_USER_AGENT", "weather-edge/1.0 (sports-analysis app; contact via site operator)",
)
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """A provider call failed after retries. ``status`` is the last HTTP status (None = network)."""

    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider, self.status = provider, status


class PublicHttpClient:
    def __init__(
        self, provider: str, base_url: str, *, min_interval: float = 0.2, timeout: float = 20.0,
        max_retries: int = 3, backoff: float = 1.0, session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    ):
        self.provider, self.base_url = provider, base_url.rstrip("/")
        self.min_interval, self.timeout = min_interval, timeout
        self.max_retries, self.backoff = max_retries, backoff
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        self.session.headers.setdefault("Accept", "application/json")
        self._sleep, self._clock = sleep, clock
        self._last_call = float("-inf")
        self.calls = 0

    def _pace(self) -> None:
        wait = self.min_interval - (self._clock() - self._last_call)
        if wait > 0:
            self._sleep(wait)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_status: int | None = None
        last_error = "unknown error"
        for attempt in range(self.max_retries + 1):
            self._pace()
            self._last_call = self._clock()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_status, last_error = None, type(exc).__name__
            else:
                last_status = resp.status_code
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise ProviderError(self.provider, "response was not valid JSON", 200) from None
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in _RETRY_STATUS:
                    raise ProviderError(self.provider, last_error, resp.status_code)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    self._sleep(min(float(retry_after), 30.0))
            if attempt < self.max_retries:
                delay = self.backoff * (2 ** attempt)
                logger.warning("%s request failed (%s); retry %d/%d in %.1fs",
                               self.provider, last_error, attempt + 1, self.max_retries, delay)
                self._sleep(delay)
        raise ProviderError(self.provider, f"{last_error} after {self.max_retries + 1} attempts", last_status)
