"""Pinnacle (pinnapi.com) odds feed integration.

Fetches Pinnacle's real prematch fixtures — player props AND full-game
moneyline/spread/total ("Game" period) — and injects a "pinnacle" book
entry into every matching O/U group, so the frozen Pinnacle value model
in ``player_prop_analysis`` can compute a no-vig reference and gate
official picks.

Multi-league as of 2026-08-23 (was MLB-only before): MLB, NFL, and WNBA
are all confirmed live on this feed — see ``src/prop_config.py``'s
``PINNACLE_SPORT_ID_BY_LEAGUE``/``PINNACLE_PROP_UNITS_BY_LEAGUE`` for the
verified sport_id and market catalog per league. NFL currently has real
game events but zero specials posted this far before its 2026-09-10
season opener (real, not a bug — re-verify closer to kickoff).

Free-tier notes
---------------
* ~100 REST requests/day; hard 429 rate limit under rapid-fire calls.
  This is now shared across 3 leagues x 2 data types (props + game
  odds) = up to 6 distinct cache slots, each independently rate-limited
  and cached — worth watching pinnapi's own dashboard if usage climbs
  near the daily cap once all 3 leagues are scanning regularly.
* ONE prematch fixtures call returns ALL of that sport's fixtures for
  the day, so coverage costs one request per (league, data-type) pair
  per cache window, not one per market.
* The feed only prices Over/Under markets (both props and game
  markets).  Yes/No markets are never touched — they were never gated
  by Pinnacle in the first place (see Gate 9 in ``official_picks.py``).

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

# MLB's own unit map, kept as a module-level constant for backward
# compatibility (existing callers/tests reference this directly) — now
# just MLB's slice of the general, multi-league
# cfg.PINNACLE_PROP_UNITS_BY_LEAGUE. Any other unit in a payload is a
# team/game market (Exact Scores, Next Run, Double Result) and is
# filtered out by the category check, same as before.
UNIT_TO_MARKET_OU = dict(cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["MLB"])
MARKET_OU_TO_UNIT = {v: k for k, v in UNIT_TO_MARKET_OU.items()}

# Single request per process lifetime guard against hammering the free tier.
_FETCH_LOCK = threading.Lock()
_last_fetch_time = 0.0

# Cache dir: <repo>/data/_pinnacle_feed_cache_<league>_raw.json —
# per-league raw-payload files (added 2026-08-23) so MLB/NFL/WNBA never
# overwrite each other's cache. One raw payload per league covers both
# props and game odds (see PinnacleFeedClient._get_raw_payload).
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data"


def _cache_path_for(league: str, kind: str) -> Path:
    return _CACHE_DIR / f"_pinnacle_feed_cache_{league.lower()}_{kind}.json"


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


@dataclass(frozen=True)
class PinnacleGameOdds:
    """Pinnacle's full-game (period "Game") moneyline/spread/total prices
    for one real event — added 2026-08-23 alongside multi-league support,
    since Pinnacle's "Game" period carries these for every league the
    same way player props do, and nothing used them before this."""

    home_name: str
    away_name: str
    market_type: str          # our market_type string, e.g. "game_moneyline"
    line: float | None        # None for moneyline (no line concept)
    home_decimal: float | None
    away_decimal: float | None
    over_decimal: float | None   # spread/total only
    under_decimal: float | None  # spread/total only


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

# Kept for backward compatibility (existing callers reference this
# directly) — now just MLB's slice of cfg.PINNACLE_PROP_SUFFIXES_BY_LEAGUE.
_PLAYER_PROP_SUFFIXES = dict(cfg.PINNACLE_PROP_SUFFIXES_BY_LEAGUE["MLB"])


def _parse_player_name(special: str, unit: str | None = None, league: str = "MLB") -> str:
    """Extract the player name from Pinnacle's special-market label."""
    name = (special or "").split(" (")[0]
    suffixes = cfg.PINNACLE_PROP_SUFFIXES_BY_LEAGUE.get(league, {})
    suffix = suffixes.get(unit or "")
    if suffix and name.casefold().endswith(f" {suffix}".casefold()):
        name = name[:-(len(suffix) + 1)]
    return name.strip()


def _parse_main_events(events: list[dict], league: str = "MLB") -> dict[int, dict]:
    """Main events by event_id, restricted to *league*'s real league_name
    within whatever sport_id it shares with other real leagues (e.g.
    Basketball's sport_id also carries NBA/other basketball alongside
    WNBA — filtering here is what keeps those separate)."""
    target_name = cfg.PINNACLE_LEAGUE_NAME_BY_LEAGUE.get(league, league)
    mains = {}
    for ev in events:
        if ev.get("parent_id"):
            continue
        if ev.get("league_name") != target_name:
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


def parse_player_props(payload: dict, league: str = "MLB") -> list[PinnacleProp]:
    """Parse the pinnapi fixtures payload into PinnacleProp objects for
    *league*. Generalized 2026-08-23 from the original MLB-only
    ``parse_mlb_props`` — see ``parse_mlb_props`` below for the thin
    backward-compatible wrapper."""
    events = payload.get("events") or []
    mains = _parse_main_events(events, league)
    unit_map = cfg.PINNACLE_PROP_UNITS_BY_LEAGUE.get(league, {})

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
        if unit not in unit_map:
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
                    player_name=_parse_player_name(ev.get("special") or "", unit, league),
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


def parse_mlb_props(payload: dict) -> list[PinnacleProp]:
    """Backward-compatible MLB-only wrapper around ``parse_player_props``."""
    return parse_player_props(payload, league="MLB")


def _extract_game_period(main_event_raw: dict) -> dict | None:
    """Return the full-game ("num_0" / description "Game") period dict
    from a raw Pinnacle main event, or None if absent."""
    periods = main_event_raw.get("periods") or {}
    for pd in periods.values():
        if (pd or {}).get("description") == "Game":
            return pd
    return None


def parse_game_odds(payload: dict, league: str = "MLB") -> list[PinnacleGameOdds]:
    """Parse Pinnacle's full-game moneyline/spread/total prices for
    *league* into PinnacleGameOdds objects — one per distinct market
    (moneyline is line=None; each distinct spread/total line offered
    gets its own entry, mirroring how player props keep every distinct
    line rather than picking one "main" line).

    Added 2026-08-23 — Pinnacle's "Game" period was never used before
    this; only player props had a Pinnacle reference.
    """
    events = payload.get("events") or []
    market_types = cfg.PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE.get(league)
    if not market_types:
        return []
    target_name = cfg.PINNACLE_LEAGUE_NAME_BY_LEAGUE.get(league, league)

    results: list[PinnacleGameOdds] = []
    for ev in events:
        if ev.get("parent_id") or ev.get("league_name") != target_name:
            continue
        home_name = (ev.get("home") or "").strip()
        away_name = (ev.get("away") or "").strip()
        if not home_name or not away_name:
            continue
        period = _extract_game_period(ev)
        if period is None:
            continue

        ml = period.get("money_line") or {}
        home_ml, away_ml = ml.get("home"), ml.get("away")
        if home_ml and away_ml and float(home_ml) > 1.0 and float(away_ml) > 1.0:
            results.append(PinnacleGameOdds(
                home_name=home_name, away_name=away_name,
                market_type=market_types["moneyline"], line=None,
                home_decimal=float(home_ml), away_decimal=float(away_ml),
                over_decimal=None, under_decimal=None,
            ))

        for entry in (period.get("spreads") or {}).values():
            hdp, home_p, away_p = entry.get("hdp"), entry.get("home"), entry.get("away")
            if hdp is None or not home_p or not away_p:
                continue
            try:
                if float(home_p) <= 1.0 or float(away_p) <= 1.0:
                    continue
                results.append(PinnacleGameOdds(
                    home_name=home_name, away_name=away_name,
                    market_type=market_types["spread"], line=round(abs(float(hdp)), 2),
                    home_decimal=float(home_p), away_decimal=float(away_p),
                    over_decimal=None, under_decimal=None,
                ))
            except (TypeError, ValueError):
                continue

        for entry in (period.get("totals") or {}).values():
            points, over_p, under_p = entry.get("points"), entry.get("over"), entry.get("under")
            if points is None or not over_p or not under_p:
                continue
            try:
                if float(over_p) <= 1.0 or float(under_p) <= 1.0:
                    continue
                results.append(PinnacleGameOdds(
                    home_name=home_name, away_name=away_name,
                    market_type=market_types["total"], line=round(float(points), 2),
                    home_decimal=None, away_decimal=None,
                    over_decimal=float(over_p), under_decimal=float(under_p),
                ))
            except (TypeError, ValueError):
                continue

    return results


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
    league: str = "MLB",
) -> PinnacleProp | None:
    """Find the Pinnacle prop for a group at the exact same line.

    *league* selects which market_type<->unit map to use (added
    2026-08-23) — WNBA's market_type strings differ entirely from MLB's,
    so this can no longer hardcode MLB's reverse map.
    """
    unit_map = cfg.PINNACLE_PROP_UNITS_BY_LEAGUE.get(league, {})
    market_to_unit = {v: k for k, v in unit_map.items()}
    unit = market_to_unit.get(market_type)
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
    ou_groups: dict, event_map: dict, lookup: dict, league: str = "MLB",
) -> int:
    """Inject a 'pinnacle' book entry into matching player-prop O/U
    groups. Groups whose line has no Pinnacle counterpart are left
    untouched (they keep the market-median fallback). Returns the
    number of groups that received a Pinnacle reference.
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
            league=league,
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
# Matching game markets (moneyline/spread/total) — added 2026-08-23
# ======================================================================

def build_pinnacle_game_lookup(games: list[PinnacleGameOdds]) -> dict:
    """Index game odds by (team pair, market_type, line) — no player
    component, unlike props. line is None for moneyline."""
    lookup = {}
    for g in games:
        teams = frozenset({normalize_team_name(g.home_name), normalize_team_name(g.away_name)})
        if not teams:
            continue
        key = (teams, g.market_type, round(float(g.line), 2) if g.line is not None else None)
        lookup[key] = g
    return lookup


def match_pinnacle_game(
    lookup: dict, *, home_name: str, away_name: str, market_type: str, line,
) -> PinnacleGameOdds | None:
    """Find the Pinnacle game-odds entry for a game-market group at the
    exact same line (None for moneyline)."""
    teams = frozenset({normalize_team_name(home_name), normalize_team_name(away_name)})
    if not teams:
        return None
    norm_line = round(float(line), 2) if line is not None else None

    exact = lookup.get((teams, market_type, norm_line))
    if exact is not None:
        return exact

    for (p_teams, p_market_type, p_line), game in lookup.items():
        if p_market_type != market_type:
            continue
        if norm_line is None or p_line is None:
            if norm_line != p_line:
                continue
        elif abs(p_line - norm_line) > 1e-6:
            continue
        if _match_teams(teams, p_teams):
            return game
    return None


def inject_pinnacle_game_reference(ou_groups: dict, event_map: dict, lookup: dict) -> int:
    """Inject a 'pinnacle' book entry into matching game-market O/U
    groups (moneyline/spread/total — grouped the same way player props
    are, with AWAY mapped to the 'over' slot and HOME to 'under', per
    each league's MarketConfig.internal_side_map). Returns the number
    of groups that received a Pinnacle reference."""
    injected = 0
    for gdata in ou_groups.values():
        # Game-level markets always carry player_id="GAME" (set in
        # src/player_prop_parser.py and src/odds_api_game_parser.py,
        # regardless of provider) — the one reliable way to tell a game
        # market apart from a real player prop.
        if gdata.get("player_id") != "GAME":
            continue
        ev = event_map.get(gdata.get("event_id", "")) or {}
        pin = match_pinnacle_game(
            lookup,
            home_name=ev.get("home_name", ""),
            away_name=ev.get("away_name", ""),
            market_type=gdata.get("market_type", ""),
            line=gdata.get("line"),
        )
        if pin is None:
            continue
        if pin.line is None:  # moneyline
            over_dec, under_dec = pin.away_decimal, pin.home_decimal
        elif pin.market_type.endswith("_total_ou"):
            over_dec, under_dec = pin.over_decimal, pin.under_decimal
        else:  # spread/run line: AWAY=over, HOME=under, same convention as moneyline
            over_dec, under_dec = pin.away_decimal, pin.home_decimal
        for side, decimal in (("over", over_dec), ("under", under_dec)):
            if decimal is None or "pinnacle" in gdata[side]:
                continue
            gdata[side]["pinnacle"] = {
                "price": decimal_to_american(decimal),
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
        # None (the real-usage default) means "compute per-league,
        # per-data-type" — see _resolve_cache_path. An explicit path
        # (mainly for tests) pins every call to that one file, same as
        # before multi-league support existed.
        self._explicit_cache_path = Path(cache_path) if cache_path else None
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

    def _resolve_cache_path(self, league: str, kind: str) -> Path:
        return self._explicit_cache_path or _cache_path_for(league, kind)

    def _load_cache(self, cache_path: Path) -> dict | None:
        try:
            if not cache_path.exists():
                return None
            raw = cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "fetched_at" in data and "raw" in data:
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save_raw_cache(self, cache_path: Path, raw_payload: dict) -> None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": time.time(), "raw": raw_payload}
            tmp = cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(cache_path)
        except OSError:
            logger.warning("Could not write Pinnacle feed cache to %s", cache_path)

    # -- fetch ---------------------------------------------------------

    def _respect_rate_limit(self) -> bool:
        global _last_fetch_time
        with _FETCH_LOCK:
            now = time.time()
            if now - _last_fetch_time < self.min_interval:
                return False
            _last_fetch_time = now
            return True

    def _fetch_raw(self, sport_id: int) -> dict | None:
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
                params={"include_specials": 1, "sport_id": sport_id},
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

    def _get_raw_payload(self, league: str, allow_fetch: bool) -> dict | None:
        """Fetch-or-reuse the raw fixtures payload for *league*, cached
        as a whole (not split by props/game-odds).

        A single ``/prematch/fixtures`` call already returns BOTH player
        props and full-game odds in one response — fetching it once per
        league and letting ``get_player_props``/``get_game_odds`` both
        parse from the same cached payload avoids a real bug caught
        before shipping (2026-08-23): fetching props and game-odds as
        two separate live calls would hit the same global rate limiter
        back-to-back, and the second call would almost always be
        silently rate-limited into returning nothing.
        """
        cache_path = self._resolve_cache_path(league, "raw")
        cache = self._load_cache(cache_path)
        if cache is not None and time.time() - cache["fetched_at"] < self.ttl:
            return cache.get("raw")

        if not allow_fetch:
            return None

        sport_id = cfg.PINNACLE_SPORT_ID_BY_LEAGUE.get(league)
        if sport_id is None:
            logger.warning("Pinnacle feed: no sport_id configured for league %r", league)
            return None

        if not self._respect_rate_limit():
            logger.info("Pinnacle feed rate-limited (min interval); skipping fetch")
            return None

        raw = self._fetch_raw(sport_id)
        if raw is None:
            return None
        try:
            self._save_raw_cache(cache_path, raw)
        except Exception:
            logger.warning("Could not cache raw Pinnacle payload for %s", league, exc_info=True)
        return raw

    def get_player_props(self, league: str = "MLB", allow_fetch: bool = True) -> list[PinnacleProp] | None:
        """Return parsed Pinnacle player props for *league*.

        Uses a fresh disk cache when available.  Triggers a live fetch
        only when ``allow_fetch`` is True.  Returns None on any failure
        (including "no key configured", "rate limited", or "genuinely
        nothing posted yet") so callers can fall back to the
        market-median model rather than distinguish those cases.
        """
        raw = self._get_raw_payload(league, allow_fetch)
        if raw is None:
            return None
        try:
            props = parse_player_props(raw, league)
        except Exception:
            logger.exception("Pinnacle feed parse error")
            return None
        logger.info("Pinnacle feed: %d %s props parsed", len(props), league)
        return props

    def get_mlb_props(self, allow_fetch: bool = True) -> list[PinnacleProp] | None:
        """Backward-compatible MLB-only wrapper around ``get_player_props``."""
        return self.get_player_props(league="MLB", allow_fetch=allow_fetch)

    def get_game_odds(self, league: str = "MLB", allow_fetch: bool = True) -> list[PinnacleGameOdds] | None:
        """Return parsed Pinnacle full-game moneyline/spread/total odds
        for *league*. Shares the same cached raw payload as
        ``get_player_props`` (see ``_get_raw_payload``) — one real fetch
        per league covers both, since pinnapi's single fixtures response
        already carries both data types."""
        raw = self._get_raw_payload(league, allow_fetch)
        if raw is None:
            return None
        try:
            games = parse_game_odds(raw, league)
        except Exception:
            logger.exception("Pinnacle feed parse error")
            return None
        logger.info("Pinnacle feed: %d %s game-odds entries parsed", len(games), league)
        return games
