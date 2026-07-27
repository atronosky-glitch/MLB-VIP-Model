"""SportsGameOdds API client (v2).

Handles all communication with the SportsGameOdds v2 API.
Optimized for the free plan — caches responses locally and
avoids duplicate requests.
"""

import os
import json
import logging
import time
from pathlib import Path

import requests
from requests.exceptions import ConnectionError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sportsgameodds.com/v2"
API_KEY = os.getenv("SPORTSODDS_API_KEY")

if not API_KEY:
    raise RuntimeError(
        "SPORTSODDS_API_KEY not found in .env file. "
        "Create a .env file with: SPORTSODDS_API_KEY=your_key_here"
    )


class SportsGameOddsClient:
    """Lightweight client for the SportsGameOdds v2 API.

    Uses ``x-api-key`` header for authentication.
    Caches every successful response as JSON under ``cache_dir/`` so
    the same data is never fetched twice on the free plan.
    Enforces a minimum interval between live API calls (rate limiting).
    """

    # Minimum seconds between live API calls (free-plan safety)
    MIN_API_INTERVAL: float = 1.0

    def __init__(self, cache_dir: str | Path = "data/_api_cache",
                 max_cache_age: float | None = None):
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": API_KEY})
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_api_call: float = 0.0
        # Max age of cache files in seconds; None = use cached regardless of age
        self.max_cache_age = max_cache_age

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_leagues(self) -> tuple[dict, bool]:
        """Return the list of supported leagues."""
        return self._get("/leagues")

    def get_sports(self) -> tuple[dict, bool]:
        """Return the list of supported sports."""
        return self._get("/sports")

    def get_events(
        self,
        league: str = "MLB",
        date_str: str | None = None,
        event_id: str | None = None,
        odds_available: bool = True,
        include_alt_lines: bool = True,
    ) -> tuple[dict, bool]:
        """Return events (games) for *league*.

        Returns ``(data, from_cache)`` where *from_cache* is ``True``
        when the response was served from the local disk cache.

        Parameters
        ----------
        league : str
            League ID (e.g. ``"MLB"``).
        date_str : str or None
            ISO date YYYY-MM-DD.  If omitted, defaults to today.
        event_id : str or None
            Fetch a single event by ID instead of all events.
        odds_available : bool
            Only return events that have odds available.
        include_alt_lines : bool
            Include alternate lines in the response.
        """
        params: dict[str, str] = {"leagueID": league}
        if date_str:
            params["date"] = date_str
        if event_id:
            params["eventID"] = event_id
        if odds_available:
            params["oddsAvailable"] = "true"
        if include_alt_lines:
            params["includeAltLines"] = "true"

        return self._get("/events", params=params)

    def get_teams(self, league: str = "MLB") -> tuple[dict, bool]:
        """Return teams for a given league."""
        return self._get("/teams", params={"leagueID": league})

    def get_markets(self, league: str = "MLB") -> tuple[dict, bool]:
        """Return supported markets for a given league."""
        return self._get("/markets", params={"leagueID": league})

    def get_usage(self) -> tuple[dict, bool]:
        """Return current API usage and limits."""
        return self._get("/account/usage")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, endpoint: str, params: dict | None = None) -> Path:
        """Build a deterministic cache-file path from endpoint + params."""
        parts = [endpoint.replace("/", "_")]
        if params:
            for k, v in sorted(params.items()):
                if v is not None:
                    parts.append(f"{k}_{v}")
        # sanitise filename
        safe = "_".join(parts).replace("?", "").replace("&", "_")
        # Limit filename length (Windows has 255 char path limit)
        safe = safe[:200]
        return self.cache_dir / f"{safe}.json"

    def _request_with_retry(
        self,
        url: str,
        params: dict | None = None,
        max_retries: int = 3,
        timeout: int = 30,
    ) -> requests.Response:
        """Send a GET request with exponential-backoff retry.

        Retries up to *max_retries* times on connection errors, timeouts,
        and HTTP 429/5xx responses.  Sleep intervals are 1 s, 2 s, 4 s …
        """
        retry_statuses = {429, 500, 502, 503, 504}
        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
                if resp.status_code in retry_statuses and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Retry %d/%d after HTTP %d – sleeping %ds",
                        attempt + 1, max_retries, resp.status_code, wait,
                    )
                    time.sleep(wait)
                    continue
                return resp
            except (ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning(
                        "Retry %d/%d after %s – sleeping %ds",
                        attempt + 1, max_retries, type(exc).__name__, wait,
                    )
                    time.sleep(wait)

        raise last_exc  # type: ignore[misc]

    def _get(self, endpoint: str, params: dict | None = None) -> tuple[dict, bool]:
        """GET *endpoint* with optional *params*, caching the result.

        Returns ``(data, from_cache)``.
        """
        url = f"{BASE_URL}{endpoint}"
        cache_path = self._cache_path(endpoint, params)

        # Return cached data if it exists (avoid burning API calls)
        if cache_path.exists():
            # Check cache age if max_cache_age is set
            if self.max_cache_age is not None:
                cache_mtime = cache_path.stat().st_mtime
                cache_age = time.time() - cache_mtime
                if cache_age > self.max_cache_age:
                    logger.info("Cache STALE (%.0fs old) for %s %s — fetching fresh",
                                cache_age, endpoint, params)
                else:
                    logger.debug("Cache HIT (%.0fs old) for %s %s", cache_age, endpoint, params)
                    with open(cache_path, encoding="utf-8") as fh:
                        return json.load(fh), True
            else:
                logger.debug("Cache HIT for %s %s", endpoint, params)
                with open(cache_path, encoding="utf-8") as fh:
                    return json.load(fh), True

        logger.info("API call: GET %s params=%s", endpoint, params)

        # Rate limiting: enforce minimum interval between live API calls
        elapsed = time.monotonic() - self._last_api_call
        if elapsed < self.MIN_API_INTERVAL:
            wait = self.MIN_API_INTERVAL - elapsed
            logger.debug("Rate limit: sleeping %.2fs", wait)
            time.sleep(wait)

        resp = self._request_with_retry(url, params=params, timeout=30)
        self._last_api_call = time.monotonic()
        resp.raise_for_status()
        data = resp.json()

        # Write to cache
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)

        return data, False

    def clear_cache(self) -> None:
        """Delete all cached API responses (useful for debugging)."""
        import shutil
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("API cache cleared.")

    def clear_stale_cache(self, max_age_seconds: float = 3600) -> int:
        """Delete cache files older than *max_age_seconds*.

        Returns the number of files deleted.
        """
        deleted = 0
        if not self.cache_dir.exists():
            return deleted
        cutoff = time.time() - max_age_seconds
        for f in self.cache_dir.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        if deleted:
            logger.info("Cleared %d stale cache file(s) (older than %ds)",
                        deleted, max_age_seconds)
        return deleted

    def get_cache_info(self) -> dict:
        """Return summary info about the cache directory."""
        if not self.cache_dir.exists():
            return {"files": 0, "total_bytes": 0}
        files = list(self.cache_dir.glob("*.json"))
        total = sum(f.stat().st_size for f in files)
        return {"files": len(files), "total_bytes": total}
