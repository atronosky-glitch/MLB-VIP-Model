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


# ======================================================================
# Fetch-status classification (added 2026-08-23)
# ======================================================================
# The raw-payload fetch used to collapse every failure mode into a bare
# None, on the theory that the caller only ever needed "did it work or
# not" to decide on the LOO-consensus fallback. That's still true for the
# fallback decision itself, but it made "Pinnacle genuinely has no props
# posted yet" indistinguishable from "the feed is down"/"rate limited"/
# "auth failed"/"parse error" in logs and health checks — exactly the
# ambiguity that made a real, temporary props=0 response look alarming.
# PinnacleFeedClient now tracks the LAST raw-fetch outcome as an instance
# attribute (self.last_fetch_status) rather than changing get_player_props/
# get_game_odds's return type, so every existing caller/test keeps working
# unchanged; only callers that care about the distinction read the attribute.

PINNACLE_STATUS_OK = "ok"                                  # fresh live fetch succeeded
PINNACLE_STATUS_CACHED = "cached"                           # served from a fresh disk cache, no live call made
PINNACLE_STATUS_NO_API_KEY = "no_api_key"
PINNACLE_STATUS_RATE_LIMITED_LOCAL = "rate_limited_local"   # our own min-interval throttle, not pinnapi's
PINNACLE_STATUS_AUTH_FAILURE = "auth_failure"               # HTTP 401/403
PINNACLE_STATUS_HTTP_ERROR = "http_error"                   # any other non-200 (429, 5xx, ...)
PINNACLE_STATUS_NETWORK_ERROR = "network_error"             # timeout / connection failure
PINNACLE_STATUS_PARSE_ERROR = "parse_error"                 # unparseable JSON or a parser exception
PINNACLE_STATUS_LEAGUE_NOT_CONFIGURED = "league_not_configured"
PINNACLE_STATUS_NO_PROPS_POSTED = "no_props_currently_posted"  # raw fetch OK; zero Player Props specials right now


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
    # Unix epoch seconds from pinnapi's own "last" field on the special
    # sub-event — when PINNACLE's own backend last updated this specific
    # prop. Live-verified 2026-08-23: every event in one payload shares
    # the same value (a payload-wide refresh timestamp, not a genuine
    # per-price one), consistently under ~30s old in normal operation.
    # None if pinnapi omits the field (never guessed/reconstructed).
    last_updated: float | None = None
    # Which real feed actually produced this quote — added 2026-08-26 so
    # diagnostics can show provenance even though the downstream engine
    # always sees a book literally named "pinnacle" regardless of source
    # (see src/odds_api_pinnacle_feed.py for the other producer of this
    # same dataclass). Defaults to "direct_pinnapi" since every call site
    # in this module predates the second source.
    source: str = "direct_pinnapi"


@dataclass(frozen=True)
class PinnacleGameOdds:
    """Pinnacle's full-game (period "Game") moneyline/spread/total prices
    for one real event — added 2026-08-23 alongside multi-league support,
    since Pinnacle's "Game" period carries these for every league the
    same way player props do, and nothing used them before this."""

    home_name: str
    away_name: str
    market_type: str          # our market_type string, e.g. "game_moneyline"
    line: float | None
    # None for moneyline (no line concept). For totals, the positive points
    # value. For spreads, the SIGNED hdp exactly as pinnapi returns it —
    # Pinnacle's own convention, home-team perspective (positive = home
    # underdog/receiving, negative = home favorite/laying). NOT abs-valued:
    # pinnapi genuinely offers both hdp=+1.5 and hdp=-1.5 as distinct real
    # alt-lines for the same game (confirmed live 2026-08-23 — collapsing
    # them to abs(hdp) silently let one overwrite the other in the lookup,
    # producing nonsensical ~85% "EV" from comparing two different real
    # bets). Callers must convert a group's own signed away-side raw_line
    # to this same home-perspective sign (target_hdp = -away_raw_line)
    # before looking up a spread entry — see inject_pinnacle_game_reference.
    home_decimal: float | None
    away_decimal: float | None
    over_decimal: float | None   # spread/total only
    under_decimal: float | None  # spread/total only
    # Unix epoch seconds from pinnapi's own "last" field on the main
    # event — see PinnacleProp.last_updated for the same field's meaning
    # and caveats (payload-wide, not genuinely per-price).
    last_updated: float | None = None
    # See PinnacleProp.source's docstring — same provenance field, same default.
    source: str = "direct_pinnapi"


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


def _parse_last(value) -> float | None:
    """Parse pinnapi's "last" field (Unix epoch seconds) into a float,
    never guessing when it's absent or malformed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _payload_has_player_prop_specials(payload: dict, league: str) -> bool:
    """Cheap existence check — does this raw payload contain ANY "Player
    Props" sub-event for *league*, without fully parsing them? Used only
    to decide the cache's effective TTL (see
    PinnacleFeedClient._get_raw_payload); a full parse still happens via
    parse_player_props for the real prop objects. Filters by the PARENT
    (main) event's league, same as parse_player_props itself — a
    special's own league_name field is not what real filtering uses."""
    events = payload.get("events") or []
    mains = _parse_main_events(events, league)
    for ev in events:
        parent_id = ev.get("parent_id")
        if not parent_id or parent_id not in mains:
            continue
        if (ev.get("special_category") or "") == "Player Props":
            return True
    return False


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
                    last_updated=_parse_last(ev.get("last")),
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
        event_last = _parse_last(ev.get("last"))

        ml = period.get("money_line") or {}
        home_ml, away_ml = ml.get("home"), ml.get("away")
        if home_ml and away_ml and float(home_ml) > 1.0 and float(away_ml) > 1.0:
            results.append(PinnacleGameOdds(
                home_name=home_name, away_name=away_name,
                market_type=market_types["moneyline"], line=None,
                home_decimal=float(home_ml), away_decimal=float(away_ml),
                over_decimal=None, under_decimal=None,
                last_updated=event_last,
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
                    market_type=market_types["spread"], line=round(float(hdp), 2),
                    home_decimal=float(home_p), away_decimal=float(away_p),
                    over_decimal=None, under_decimal=None,
                    last_updated=event_last,
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
                    last_updated=event_last,
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


def _is_stale(last_updated: float | None) -> bool:
    """Whether a Pinnacle quote's own "last" timestamp is older than
    ``cfg.PINNACLE_MAX_STALENESS_SECONDS``. A missing timestamp is NOT
    treated as stale (pinnapi may omit it; conservative default is to
    still use the quote — the same "graceful degradation" stance the
    rest of this feed already takes, never inventing a reason to distrust
    real data that lacks a field)."""
    if last_updated is None:
        return False
    return (time.time() - last_updated) > cfg.PINNACLE_MAX_STALENESS_SECONDS


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
) -> tuple[int, int]:
    """Inject a 'pinnacle' book entry into matching player-prop O/U
    groups. Groups whose line has no Pinnacle counterpart are left
    untouched (they keep the market-median fallback). A match whose own
    "last" timestamp is older than ``cfg.PINNACLE_MAX_STALENESS_SECONDS``
    is treated the same as no match at all — never injected, so the group
    falls back to LOO consensus rather than anchoring on a stale sharp
    price (added 2026-08-23, per operator directive: a stale Pinnacle
    quote must never override fresher multi-book consensus).

    Returns ``(injected, stale_skipped)`` — groups that received a real
    reference, and groups where a match existed but was too old to use.
    """
    injected = 0
    stale_skipped = 0
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
        if _is_stale(pin.last_updated):
            stale_skipped += 1
            logger.warning(
                "PINNACLE_STALE_SKIPPED player=%s market=%s line=%s age_s=%.0f",
                gdata.get("player_name", ""), gdata.get("market_type", ""),
                gdata.get("line"), time.time() - pin.last_updated,
            )
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
                "pinnacle_source": pin.source,
                "pinnacle_last_updated": pin.last_updated,
            }
        injected += 1
    return injected, stale_skipped


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


def inject_pinnacle_game_reference(
    ou_groups: dict, event_map: dict, lookup: dict,
) -> tuple[int, int]:
    """Inject a 'pinnacle' book entry into matching game-market O/U
    groups (moneyline/spread/total — grouped the same way player props
    are, with AWAY mapped to the 'over' slot and HOME to 'under', per
    each league's MarketConfig.internal_side_map). A match whose own
    "last" timestamp is older than ``cfg.PINNACLE_MAX_STALENESS_SECONDS``
    is treated the same as no match — never injected, so the group falls
    back to LOO consensus instead of a stale sharp price (2026-08-23).

    Returns ``(injected, stale_skipped)``.
    """
    injected = 0
    stale_skipped = 0
    for gdata in ou_groups.values():
        # Game-level markets always carry player_id="GAME" (set in
        # src/player_prop_parser.py and src/odds_api_game_parser.py,
        # regardless of provider) — the one reliable way to tell a game
        # market apart from a real player prop.
        if gdata.get("player_id") != "GAME":
            continue
        ev = event_map.get(gdata.get("event_id", "")) or {}
        market_type = gdata.get("market_type", "")
        is_spread = market_type != "game_moneyline" and not market_type.endswith("_total_ou")
        if is_spread:
            # This group's own `line` is stored unsigned (abs value), but
            # Pinnacle's hdp is signed from the HOME team's perspective and
            # pinnapi genuinely offers both directions as distinct real
            # alt-lines (e.g. hdp=+1.5 AND hdp=-1.5 for the same game — not
            # duplicates). Convert using the group's own signed away-side
            # raw_line (negative = away favorite): target_hdp =
            # -away_raw_line. If unavailable, skip rather than guess a
            # direction — an unsigned lookup can silently match the WRONG
            # entry, comparing two different real bets (caught live
            # 2026-08-23: produced a nonsensical ~85% "EV").
            away_raw_line = (gdata.get("side_raw_line") or {}).get("over")
            if away_raw_line is None:
                continue
            lookup_line = -away_raw_line
        else:
            lookup_line = gdata.get("line")
        pin = match_pinnacle_game(
            lookup,
            home_name=ev.get("home_name", ""),
            away_name=ev.get("away_name", ""),
            market_type=market_type,
            line=lookup_line,
        )
        if pin is None:
            continue
        if _is_stale(pin.last_updated):
            stale_skipped += 1
            logger.warning(
                "PINNACLE_STALE_SKIPPED player=GAME market=%s line=%s age_s=%.0f",
                market_type, gdata.get("line"), time.time() - pin.last_updated,
            )
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
                "pinnacle_source": pin.source,
                "pinnacle_last_updated": pin.last_updated,
            }
        injected += 1
    return injected, stale_skipped


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
        # Last raw-fetch outcome, per league — read after calling
        # get_player_props/get_game_odds to distinguish WHY a call
        # returned nothing (see the PINNACLE_STATUS_* constants above).
        self.last_fetch_status: dict[str, str] = {}
        # Last player-props-specific outcome, per league — PINNACLE_STATUS_OK
        # or PINNACLE_STATUS_NO_PROPS_POSTED when the raw fetch itself
        # succeeded but zero Player Props specials exist right now, or
        # whatever last_fetch_status holds when the raw fetch itself failed.
        self.last_props_status: dict[str, str] = {}

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

    def _fetch_raw(self, sport_id: int) -> tuple[dict | None, str]:
        """Returns ``(payload_or_None, status)`` — status is one of the
        ``PINNACLE_STATUS_*`` constants, always set even on failure, so
        callers can distinguish auth/rate-limit/network/parse failures
        from each other and from "genuinely nothing posted" (added
        2026-08-23 — this used to collapse every failure into a bare
        None with only a log line differentiating them)."""
        if not self.api_key:
            logger.warning(
                "Pinnacle feed disabled: %s env var not set",
                cfg.PINNACLE_FEED_API_KEY_ENV,
            )
            return None, PINNACLE_STATUS_NO_API_KEY
        url = f"{cfg.PINNACLE_FEED_BASE_URL}/kit/v1/prematch/fixtures"
        try:
            resp = self.session.get(
                url,
                params={"include_specials": 1, "sport_id": sport_id},
                timeout=cfg.PINNACLE_FEED_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("Pinnacle feed request failed: %s", exc)
            return None, PINNACLE_STATUS_NETWORK_ERROR
        if resp.status_code in (401, 403):
            logger.warning(
                "Pinnacle feed auth failure HTTP %s (body=%.200s)", resp.status_code, resp.text
            )
            return None, PINNACLE_STATUS_AUTH_FAILURE
        if resp.status_code != 200:
            logger.warning(
                "Pinnacle feed HTTP %s (body=%.200s)", resp.status_code, resp.text
            )
            return None, PINNACLE_STATUS_HTTP_ERROR
        try:
            return json.loads(resp.content.decode("utf-8", errors="replace")), PINNACLE_STATUS_OK
        except ValueError:
            logger.warning("Pinnacle feed returned unparseable JSON")
            return None, PINNACLE_STATUS_PARSE_ERROR

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

        Sets ``self.last_fetch_status[league]`` to a PINNACLE_STATUS_*
        constant on every call, including cache hits (added 2026-08-23).
        A cached payload with zero player-prop specials for *league* uses
        a shorter effective TTL (``cfg.PINNACLE_PROPS_EMPTY_RECHECK_SECONDS``)
        than a normal payload, so a genuinely-empty props response doesn't
        block us from noticing newly-posted props for up to the full
        cache window — real props appearing closer to game time get
        picked up promptly instead of waiting out a stale "nothing here"
        cache entry.
        """
        cache_path = self._resolve_cache_path(league, "raw")
        cache = self._load_cache(cache_path)
        if cache is not None:
            age = time.time() - cache["fetched_at"]
            effective_ttl = self.ttl
            if age >= cfg.PINNACLE_PROPS_EMPTY_RECHECK_SECONDS and not _payload_has_player_prop_specials(
                cache.get("raw") or {}, league
            ):
                effective_ttl = cfg.PINNACLE_PROPS_EMPTY_RECHECK_SECONDS
            if age < effective_ttl:
                self.last_fetch_status[league] = PINNACLE_STATUS_CACHED
                return cache.get("raw")

        if not allow_fetch:
            self.last_fetch_status[league] = PINNACLE_STATUS_CACHED if cache else PINNACLE_STATUS_HTTP_ERROR
            return cache.get("raw") if cache else None

        sport_id = cfg.PINNACLE_SPORT_ID_BY_LEAGUE.get(league)
        if sport_id is None:
            logger.warning("Pinnacle feed: no sport_id configured for league %r", league)
            self.last_fetch_status[league] = PINNACLE_STATUS_LEAGUE_NOT_CONFIGURED
            return None

        if not self._respect_rate_limit():
            logger.info("Pinnacle feed rate-limited (min interval); skipping fetch")
            self.last_fetch_status[league] = PINNACLE_STATUS_RATE_LIMITED_LOCAL
            # Serve a stale cache rather than nothing if one exists — a
            # locally-throttled call is not the same as "no data at all".
            return cache.get("raw") if cache else None

        raw, status = self._fetch_raw(sport_id)
        self.last_fetch_status[league] = status
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
        only when ``allow_fetch`` is True.  Returns None on a genuine
        fetch/parse FAILURE, and an empty list ``[]`` when the fetch
        itself succeeded but Pinnacle simply has no player-prop specials
        posted for this league right now — these are deliberately
        different return shapes (added 2026-08-23) so a caller that wants
        to distinguish "broken" from "genuinely nothing yet" can, without
        needing to know pinnapi's response shape; either way, callers
        that only care about "do I have real data" can keep treating both
        as falsy exactly as before. Read ``self.last_props_status`` right
        after this call for the specific PINNACLE_STATUS_* reason.
        """
        raw = self._get_raw_payload(league, allow_fetch)
        if raw is None:
            self.last_props_status[league] = self.last_fetch_status.get(league, PINNACLE_STATUS_HTTP_ERROR)
            return None
        try:
            props = parse_player_props(raw, league)
        except Exception:
            logger.exception("Pinnacle feed parse error")
            self.last_props_status[league] = PINNACLE_STATUS_PARSE_ERROR
            return None
        self.last_props_status[league] = (
            PINNACLE_STATUS_OK if props else PINNACLE_STATUS_NO_PROPS_POSTED
        )
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
        already carries both data types. Read
        ``self.last_fetch_status`` right after this call for the
        specific PINNACLE_STATUS_* reason on a None/empty result."""
        raw = self._get_raw_payload(league, allow_fetch)
        if raw is None:
            return None
        try:
            games = parse_game_odds(raw, league)
        except Exception:
            logger.exception("Pinnacle feed parse error")
            self.last_fetch_status[league] = PINNACLE_STATUS_PARSE_ERROR
            return None
        logger.info("Pinnacle feed: %d %s game-odds entries parsed", len(games), league)
        return games
