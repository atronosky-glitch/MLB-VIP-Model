"""The Odds API client (v4) — used for WNBA odds only.

SportsGameOdds v2 (``src/api_client.py``) does not offer WNBA at any plan
tier (verified 2026-08-19 against their own pricing page). The Odds API
(the-odds-api.com) does, on its free tier, for game markets and player
props. Schema verified live 2026-08-19 against real WNBA games:

- ``GET /v4/sports`` — sport catalog, confirms ``basketball_wnba`` is active.
- ``GET /v4/sports/basketball_wnba/odds`` — games array, each with
  ``bookmakers[].markets[].outcomes[]``. ``h2h`` outcomes have
  ``name``/``price``; ``spreads``/``totals`` outcomes add a signed/unsigned
  ``point``. American odds are plain ints when ``oddsFormat=american``.
  Cost: ``len(markets) * len(regions)`` credits per call (confirmed: 3
  credits for markets=h2h,spreads,totals, regions=us).
- ``GET /v4/sports/basketball_wnba/events`` — free (0 credits) event list.
- ``GET /v4/sports/basketball_wnba/events/{id}/odds`` — per-event player
  props (``player_points``/``player_rebounds``/``player_assists``/
  ``player_threes`` confirmed live). Outcomes carry the player's name in
  ``description`` (over/under name is "Over"/"Under"), not a stable player
  ID — see ``src/wnba_odds_parser.py`` for how identity is resolved.

Unlike ``SPORTSODDS_API_KEY``, ``THE_ODDS_API_KEY`` is optional at import
time: MLB/NFL must keep working with no WNBA key configured at all. The
key is only required when a WNBA fetch is actually attempted.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests
from requests.exceptions import ConnectionError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
_ENV_VAR = "THE_ODDS_API_KEY"

# Real bug, found 2026-08-22 while answering an operator question about
# whether every run pulls fresh data: every call site that constructs
# OddsAPIClient() with no max_cache_age gets one (None = the cache never
# expires by age — see _get() below, which returns any existing cache
# file unconditionally when max_cache_age is None). That's harmless for
# the odds-fetch calls, whose commenceTimeFrom/To params are recomputed
# from datetime.now() on every call and so naturally produce a fresh
# cache key each time -- but get_events() (WNBA schedule discovery, and
# the props path's own event lookup) takes no time-varying params at
# all, so once a real response is cached, EVERY future call would keep
# reading that same now-frozen file forever, with no live call ever
# made again. Reproduced locally: a cache file from 2026-08-20 was still
# being served unconditionally two days later. get_events() is free (0
# credits, confirmed live), so there's no cost reason for this to ever
# be stale -- both call sites (src/worker.py::_discover_wnba_game_times,
# src/sports/wnba.py::fetch_and_parse_props) pass this explicitly.
EVENTS_CACHE_TTL_SECONDS = 300


class OddsAPIKeyError(RuntimeError):
    """Raised when THE_ODDS_API_KEY is required but not configured."""


def _mask_key(key: str) -> str:
    """Return a masked key (prefix + suffix only). Never the full key."""
    if not key:
        return "<unset>"
    if len(key) <= 8:
        return key[:2] + "..." + f"({len(key)} chars)"
    return key[:4] + "..." + key[-2:]


def _get_api_key() -> str:
    key = os.getenv(_ENV_VAR)
    if not key:
        raise OddsAPIKeyError(
            f"{_ENV_VAR} not found in .env file. WNBA odds require a free "
            f"key from https://the-odds-api.com/ — add it as "
            f"{_ENV_VAR}=your_key_here. MLB/NFL do not need this key."
        )
    return key


class OddsAPIClient:
    """Lightweight client for The Odds API v4.

    Caches every successful response as JSON, mirroring
    ``SportsGameOddsClient``'s cache-first behavior, since this provider's
    free tier has a hard 500-credits/month budget and nearly every request
    costs real credits (only ``/events`` is free).
    """

    MIN_API_INTERVAL: float = 1.0

    def __init__(self, cache_dir: str | Path = "data/_odds_api_cache",
                 max_cache_age: float | None = None, api_key: str | None = None):
        self.api_key = api_key or _get_api_key()
        self.session = requests.Session()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_api_call: float = 0.0
        self.max_cache_age = max_cache_age
        self.last_quota: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_sports(self) -> tuple[list, bool]:
        return self._get("/sports", params={})

    def get_events(self, sport_key: str = "basketball_wnba") -> tuple[list, bool]:
        """Free endpoint — event list with no odds. Never costs credits."""
        return self._get(f"/sports/{sport_key}/events", params={})

    def get_odds(
        self,
        sport_key: str = "basketball_wnba",
        regions: str = "us",
        markets: str = "h2h,spreads,totals",
        odds_format: str = "american",
        commence_time_from: str | None = None,
        commence_time_to: str | None = None,
        bookmakers: str | None = None,
    ) -> tuple[list, bool]:
        """Game-level odds for every upcoming event. Costs
        ``len(markets.split(',')) * len(regions.split(','))`` credits —
        or, when *bookmakers* is given, ``len(markets.split(','))`` (up to
        10 explicitly named books count as a single region for quota
        purposes, per The Odds API's docs — confirmed live 2026-08-26:
        a 3-market ``bookmakers=pinnacle`` call cost exactly 3 credits,
        identical to a single-region call).

        *bookmakers* takes priority over *regions* (also per the docs —
        confirmed live) and is how Pinnacle is actually reached: it is
        classified under the ``eu`` region, never ``us``, so every
        existing ``regions="us"`` caller in this codebase has never
        fetched it. Passing *bookmakers* omits *regions* from the
        request entirely rather than sending both, matching the
        documented precedence instead of relying on the API to resolve
        an ambiguous combination.

        *commence_time_from*/*commence_time_to* are real, documented
        params (ISO 8601, e.g. ``2026-08-22T00:00:00Z``) — without them
        this endpoint returns every event currently listed for the sport,
        which for a full-season sport like NFL means months out (verified
        live 2026-08-22: an unbounded call returned 272 games spanning
        Sept 2026 through Jan 2027, not "the near-term slate"). WNBA
        callers have gotten away without this so far only because retail
        books don't post WNBA lines far in advance in practice (also
        verified live the same day: unbounded WNBA call returned exactly
        the next ~24h of real games) — still worth passing explicitly for
        any sport where that assumption might not hold.
        """
        params = {"markets": markets, "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions
        if commence_time_from:
            params["commenceTimeFrom"] = commence_time_from
        if commence_time_to:
            params["commenceTimeTo"] = commence_time_to
        return self._get(f"/sports/{sport_key}/odds", params=params)

    def get_event_odds(
        self,
        event_id: str,
        sport_key: str = "basketball_wnba",
        regions: str = "us",
        markets: str = "player_points,player_rebounds,player_assists,player_threes",
        odds_format: str = "american",
        bookmakers: str | None = None,
    ) -> tuple[dict, bool]:
        """Per-event odds (used for player props). Same credit formula —
        and same *bookmakers*-takes-priority-over-*regions* behavior — as
        get_odds; see that docstring."""
        params = {"markets": markets, "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions
        return self._get(f"/sports/{sport_key}/events/{event_id}/odds", params=params)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cache_path(self, endpoint: str, params: dict | None = None) -> Path:
        parts = [endpoint.replace("/", "_")]
        if params:
            for k, v in sorted(params.items()):
                if v is not None:
                    parts.append(f"{k}_{v}")
        # Same fix as api_client.py's _cache_path (found live 2026-08-20):
        # ISO timestamps (now used in commenceTimeFrom/commenceTimeTo)
        # contain ':', which Windows rejects outright in filenames — not
        # just '?'/'&'.
        safe = "_".join(parts).replace("?", "")
        for ch in ("&", ":", "/", "\\", "*", '"', "<", ">", "|"):
            safe = safe.replace(ch, "_")
        safe = safe[:200]
        return self.cache_dir / f"{safe}.json"

    def _get(self, endpoint: str, params: dict | None = None):
        cache_path = self._cache_path(endpoint, params)

        if cache_path.exists():
            if self.max_cache_age is not None:
                cache_age = time.time() - cache_path.stat().st_mtime
                if cache_age <= self.max_cache_age:
                    with open(cache_path, encoding="utf-8") as fh:
                        return json.load(fh), True
            else:
                with open(cache_path, encoding="utf-8") as fh:
                    return json.load(fh), True

        elapsed = time.monotonic() - self._last_api_call
        if elapsed < self.MIN_API_INTERVAL:
            time.sleep(self.MIN_API_INTERVAL - elapsed)

        p = dict(params or {})
        p["apiKey"] = self.api_key
        resp = self._request_with_retry(f"{BASE_URL}{endpoint}", params=p)
        self._last_api_call = time.monotonic()

        for h in ("x-requests-remaining", "x-requests-used", "x-requests-last"):
            if h in resp.headers:
                self.last_quota[h] = resp.headers[h]

        if resp.status_code == 401:
            logger.critical("The Odds API rejected the configured key (HTTP 401)")
            raise OddsAPIKeyError("Invalid THE_ODDS_API_KEY (HTTP 401 from The Odds API)")
        resp.raise_for_status()
        data = resp.json()

        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)

        return data, False

    def _request_with_retry(self, url: str, params: dict, max_retries: int = 3, timeout: int = 30):
        retry_statuses = {429, 500, 502, 503, 504}
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=timeout)
                if resp.status_code in retry_statuses and attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("Retry %d/%d after HTTP %d — sleeping %ds",
                                    attempt + 1, max_retries, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                return resp
            except (ConnectionError, requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = 2 ** attempt
                    logger.warning("Retry %d/%d after %s — sleeping %ds",
                                    attempt + 1, max_retries, type(exc).__name__, wait)
                    time.sleep(wait)
        raise last_exc  # type: ignore[misc]
