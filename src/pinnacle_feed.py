"""Pinnacle (pinnapi.com) player-prop feed integration.

Fetches Pinnacle's prematch baseball fixtures (player props included) and
injects a "pinnacle" book entry into every O/U group at the exact same
line, so the frozen Pinnacle value model in ``player_prop_analysis`` can
compute a no-vig reference and gate official picks.

Free-tier notes
---------------
* ~100 REST requests/day; hard 429 rate limit under rapid-fire calls.
* ONE prematch fixtures call returns ALL of the day's MLB player props,
  so prop coverage costs one request per scan, not one per market.
* The feed only prices Over/Under props (``special_units`` such as
  ``TotalBases``, ``Strikeouts``).  Yes/No markets are never touched.

Graceful degradation
--------------------
Any fetch/parse failure returns ``None``.  The scanner then falls back to
the existing market-median consensus, so a dead feed never blocks a scan
and never injects stale prices into the value model.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import prop_config as cfg
from .market_analysis import decimal_to_american

load_dotenv()

logger = logging.getLogger(__name__)

# pinnapi special unit -> SGO O/U market_type (registry strings)
# Verified against a live prematch fixtures fetch (include_specials=1):
# Pinnacle's MLB player-prop board carries exactly these six units.
# Any other unit in the payload is a team/game market (Exact Scores,
# Next Run, Double Result) and is filtered out by the category check.
UNIT_TO_MARKET_OU = {
    "Strikeouts": "pitching_strikeouts_ou",
    "HitsAllowed": "pitching_hits_ou",
    "EarnedRuns": "pitching_earnedRuns_ou",
    "PitchingOuts": "pitching_outs_ou",
    "TotalBases": "batting_totalBases_ou",
    "HomeRuns": "batting_homeRuns_ou",
}
MARKET_OU_TO_UNIT = {v: k for k, v in UNIT_TO_MARKET_OU.items()}

# Single request per process lifetime guard against hammering the free tier.
_FETCH_LOCK = threading.Lock()
_last_fetch_time = 0.0

# Default disk cache location: <repo>/data/_pinnacle_feed_cache.json
_DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "_pinnacle_feed_cache.json"
)


@dataclass(frozen=True)
class PinnacleProp:
    """One Pinnacle Over/Under player prop at a single line."""

    home_name: str
    away_name: str
    player_name: str
    unit: str
    line: float
    over_decimal: float
    under_decimal: float
    over_american: int
    under_american: int


# ======================================================================
# Name normalization (used on both the SGO side and the Pinnacle side)
# ======================================================================

def _repair_mojibake(text: str) -> str:
    """Recover accents from latin-1 bytes that were decoded as UTF-8."""
    if "\ufffd" not in text:
        return text
    try:
        return text.encode("latin-1", errors="replace").decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def normalize_name(text: str) -> str:
    """Lowercase, strip diacritics and collapse whitespace."""
    text = _repair_mojibake(text or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


def normalize_team_name(text: str) -> str:
    """Team names may be 'Royals' on one side and 'Kansas City Royals' on the other."""
    return normalize_name(text)


def _token_overlap(a: str, b: str) -> float:
    """Fraction of the shorter token set that appears in the longer one."""
    ta = {t for t in a.split() if t}
    tb = {t for t in b.split() if t}
    if not ta or not tb:
        return 0.0
    smaller, larger = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(smaller & larger) / len(smaller)


# ======================================================================
# Parsing the pinnapi fixtures payload
# ======================================================================

_PLAYER_PROP_SUFFIXES = {
    "Strikeouts": "Total Strikeouts",
    "HitsAllowed": "Hits Allowed",
    "EarnedRuns": "Earned Runs",
    "PitchingOuts": "Pitching Outs",
    "TotalBases": "Total Bases",
    "HomeRuns": "Home Runs",
}


def _parse_player_name(special: str, unit: str | None = None) -> str:
    """Extract the player name from Pinnacle's special-market label."""
    name = (special or "").split(" (")[0]
    suffix = _PLAYER_PROP_SUFFIXES.get(unit or "")
    if suffix and name.casefold().endswith(f" {suffix}".casefold()):
        name = name[:-(len(suffix) + 1)]
    return name.strip()


def _parse_main_events(events: list[dict]) -> dict[int, dict]:
    """Main events by event_id, restricted to the configured league."""
    mains = {}
    for ev in events:
        if ev.get("parent_id"):
            continue
        if ev.get("league_name") != cfg.PINNACLE_FEED_LEAGUE:
            continue
        eid = ev.get("event_id")
        if not eid:
            continue
        mains[eid] = {
            "home_name": (ev.get("home") or "").strip(),
            "away_name": (ev.get("away") or "").strip(),
        }
    return mains


def _extract_total_market(markets: dict) -> dict | None:
    """Return the Over/Under market (with both prices at the same points)."""
    for bucket in (markets or {}).values():
        if not isinstance(bucket, list):
            continue
        for mk in bucket:
            if (mk or {}).get("type") != "total":
                continue
            prices = mk.get("prices") or []
            over = next((p for p in prices if p.get("name") == "Over"), None)
            under = next((p for p in prices if p.get("name") == "Under"), None)
            if not over or not under:
                continue
            try:
                line = float(over.get("points"))
                if abs(line - float(under.get("points"))) > 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            return {"line": line, "over": over.get("price"), "under": under.get("price")}
    return None


def parse_mlb_props(payload: dict) -> list[PinnacleProp]:
    """Parse the pinnapi fixtures payload into PinnacleProp objects."""
    events = payload.get("events") or []
    mains = _parse_main_events(events)

    props: list[PinnacleProp] = []
    for ev in events:
        parent_id = ev.get("parent_id")
        if not parent_id:
            continue
        main = mains.get(parent_id)
        if main is None:
            continue
        if (ev.get("special_category") or "") != "Player Props":
            continue
        unit = ev.get("special_units")
        if unit not in UNIT_TO_MARKET_OU:
            continue
        market = _extract_total_market(ev.get("special_markets"))
        if market is None:
            continue
        over_dec = float(market["over"])
        under_dec = float(market["under"])
        if over_dec <= 1.0 or under_dec <= 1.0:
            continue
        try:
            props.append(
                PinnacleProp(
                    home_name=main["home_name"],
                    away_name=main["away_name"],
                    player_name=_parse_player_name(ev.get("special") or "", unit),
                    unit=unit,
                    line=market["line"],
                    over_decimal=over_dec,
                    under_decimal=under_dec,
                    over_american=decimal_to_american(over_dec),
                    under_american=decimal_to_american(under_dec),
                )
            )
        except ValueError:
            continue
    return props


# ======================================================================
# Matching against SGO prop groups
# ======================================================================

def build_pinnacle_lookup(props: list[PinnacleProp]) -> dict:
    """Index props by (team pair, player, unit, line)."""
    lookup = {}
    for p in props:
        teams = frozenset({normalize_team_name(p.home_name), normalize_team_name(p.away_name)})
        player = normalize_name(p.player_name)
        if not teams or not player:
            continue
        key = (teams, player, p.unit, round(float(p.line), 2))
        # Prefer the most recently seen prop for a duplicate key.
        lookup[key] = p
    return lookup


def _match_teams(sgo_teams: frozenset, pinn_teams: frozenset) -> bool:
    if sgo_teams == pinn_teams:
        return True
    sgo_list = list(sgo_teams)
    pinn_list = list(pinn_teams)
    if len(sgo_list) != len(pinn_list):
        return False
    used = set()
    for s in sgo_list:
        matched = False
        for i, p in enumerate(pinn_list):
            if i in used:
                continue
            if s in p or p in s or _token_overlap(s, p) >= 1.0:
                used.add(i)
                matched = True
                break
        if not matched:
            return False
    return True


def match_pinnacle(
    lookup: dict,
    *,
    home_name: str,
    away_name: str,
    player_name: str,
    market_type: str,
    line,
) -> PinnacleProp | None:
    """Find the Pinnacle prop for an SGO group at the exact same line."""
    unit = MARKET_OU_TO_UNIT.get(market_type)
    if unit is None or line is None:
        return None
    sgo_teams = frozenset(
        {normalize_team_name(home_name), normalize_team_name(away_name)}
    )
    player = normalize_name(player_name)
    if not sgo_teams or not player:
        return None

    exact = lookup.get((sgo_teams, player, unit, round(float(line), 2)))
    if exact is not None:
        return exact

    # Fuzzy fallback: same player+unit at the same line, team names may be
    # short vs long or have minor spelling differences.
    for (p_teams, p_player, p_unit, p_line), prop in lookup.items():
        if p_unit != unit or p_player != player or abs(p_line - float(line)) > 1e-6:
            continue
        if _match_teams(sgo_teams, p_teams):
            return prop
    return None


def inject_pinnacle_reference(
    ou_groups: dict, event_map: dict, lookup: dict
) -> int:
    """Inject a 'pinnacle' book entry into matching O/U groups.

    Groups whose SGO line has no Pinnacle counterpart are left untouched
    (they keep the market-median fallback).  Returns the number of groups
    that received a Pinnacle reference.
    """
    injected = 0
    for gdata in ou_groups.values():
        ev = event_map.get(gdata.get("event_id", "")) or {}
        pin = match_pinnacle(
            lookup,
            home_name=ev.get("home_name", ""),
            away_name=ev.get("away_name", ""),
            player_name=gdata.get("player_name", ""),
            market_type=gdata.get("market_type", ""),
            line=gdata.get("line"),
        )
        if pin is None:
            continue
        sides = (
            ("over", pin.over_american, pin.over_decimal),
            ("under", pin.under_american, pin.under_decimal),
        )
        for side, american, decimal in sides:
            if "pinnacle" in gdata[side]:
                continue
            gdata[side]["pinnacle"] = {
                "price": american,
                "decimal_odds": round(float(decimal), 4),
                "line": gdata.get("line"),
                "validation_status": "VALID",
            }
        injected += 1
    return injected


# ======================================================================
# Feed client with disk cache + rate limit
# ======================================================================

class PinnacleFeedClient:
    """Fetch and cache the pinnapi prematch fixtures payload."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_path: str | Path | None = None,
        ttl_seconds: float | None = None,
        min_interval_seconds: float | None = None,
    ):
        self.api_key = api_key or os.getenv(cfg.PINNACLE_FEED_API_KEY_ENV) or ""
        self.cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self.ttl = (
            float(ttl_seconds)
            if ttl_seconds is not None
            else float(cfg.PINNACLE_FEED_CACHE_TTL_SECONDS)
        )
        self.min_interval = (
            float(min_interval_seconds)
            if min_interval_seconds is not None
            else float(cfg.PINNACLE_FEED_MIN_INTERVAL_SECONDS)
        )
        self.session = requests.Session()
        self.session.headers.update({"x-portal-apikey": self.api_key})

    # -- cache --------------------------------------------------------

    def _load_cache(self) -> dict | None:
        try:
            if not self.cache_path.exists():
                return None
            raw = self.cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "fetched_at" in data and "props" in data:
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save_cache(self, props: list[dict]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": time.time(), "props": props}
            tmp = self.cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError:
            logger.warning("Could not write Pinnacle feed cache to %s", self.cache_path)

    # -- fetch ---------------------------------------------------------

    def _respect_rate_limit(self) -> bool:
        global _last_fetch_time
        with _FETCH_LOCK:
            now = time.time()
            if now - _last_fetch_time < self.min_interval:
                return False
            _last_fetch_time = now
            return True

    def _fetch_raw(self) -> dict | None:
        if not self.api_key:
            logger.warning(
                "Pinnacle feed disabled: %s env var not set",
                cfg.PINNACLE_FEED_API_KEY_ENV,
            )
            return None
        url = f"{cfg.PINNACLE_FEED_BASE_URL}/kit/v1/prematch/fixtures"
        try:
            resp = self.session.get(
                url,
                params={"include_specials": 1, "sport_id": cfg.PINNACLE_FEED_SPORT_ID},
                timeout=cfg.PINNACLE_FEED_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("Pinnacle feed request failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "Pinnacle feed HTTP %s (body=%.200s)", resp.status_code, resp.text
            )
            return None
        try:
            return json.loads(resp.content.decode("utf-8", errors="replace"))
        except ValueError:
            logger.warning("Pinnacle feed returned unparseable JSON")
            return None

    # -- public API ----------------------------------------------------

    def get_mlb_props(self, allow_fetch: bool = True) -> list[PinnacleProp] | None:
        """Return parsed Pinnacle props.

        Uses a fresh disk cache when available.  Triggers a live fetch only
        when ``allow_fetch`` is True.  Returns None on any failure so callers
        can fall back to the market-median model.
        """
        cache = self._load_cache()
        if cache is not None and time.time() - cache["fetched_at"] < self.ttl:
            return [PinnacleProp(**p) for p in cache["props"] if "unit" in p]

        if not allow_fetch:
            return None

        if not self._respect_rate_limit():
            logger.info("Pinnacle feed rate-limited (min interval); skipping fetch")
            return None

        raw = self._fetch_raw()
        if raw is None:
            # Never inject stale prices into the value model.
            return None
        try:
            props = parse_mlb_props(raw)
        except Exception:
            logger.exception("Pinnacle feed parse error")
            return None
        self._save_cache([p.__dict__ for p in props])
        logger.info("Pinnacle feed: %d MLB props parsed", len(props))
        return props
