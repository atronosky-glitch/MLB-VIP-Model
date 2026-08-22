"""Canonical player identity resolution.

Player props from The Odds API carry only a free-text name (no stable
player ID) — see ``src/wnba_odds_parser.py``. This module resolves that
name into a canonical, stable player identity by matching against a real
roster source (ESPN's public API, which does provide stable numeric player
IDs), scoped to the two teams actually playing in the game (never a
league-wide fuzzy search, which would be far more prone to collisions).

A prop can only become a trustworthy recommendation once its player
identity is resolved with HIGH or MEDIUM confidence; LOW/UNRESOLVED must
be excluded — never guessed. See ``resolve_player_identity()``.

Confidence levels
------------------
HIGH       — exact normalized-name match against exactly one roster player.
MEDIUM     — match after stripping a suffix (Jr/Sr/II/III/IV) or via a
             single unambiguous first-initial + last-name match, still
             exactly one candidate.
LOW        — multiple candidates matched (ambiguous) or a weak partial match.
UNRESOLVED — no candidate matched at all.

Only HIGH and MEDIUM should be treated as usable for a recommendation.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNRESOLVED = "UNRESOLVED"

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

ESPN_ROSTER_URL_TEMPLATE = "https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{team_id}/roster"
ESPN_TEAMS_URL_TEMPLATE = "https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams"

# league -> ESPN's sport path segment, verified live 2026-08-19 (WNBA),
# 2026-08-22 (MLB/NFL).
_ESPN_SPORT_PATH = {
    "WNBA": "basketball/wnba",
    "MLB": "baseball/mlb",
    "NFL": "football/nfl",
}


def normalize_name(value: str | None) -> str:
    """Normalize a player name for matching: strip accents, punctuation,
    and casing differences.

    Unicode NFKD decomposition + combining-mark removal handles accents
    (e.g. "Dzanan" / "Ðžanan", "Ivana Dojkić" -> "ivana dojkic") that a
    plain ``[^a-z0-9]`` regex would silently mangle instead of transliterate.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.casefold()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return re.sub(r"\s+", " ", cleaned)


def _strip_suffix(normalized: str) -> str:
    """Remove a trailing generational suffix token, if present."""
    parts = normalized.split(" ")
    if len(parts) > 1 and parts[-1] in _SUFFIXES:
        return " ".join(parts[:-1])
    return normalized


def _initial_last_key(normalized: str) -> str | None:
    """Return "f lastname" (first-initial + last name) for loose matching."""
    parts = normalized.split(" ")
    if len(parts) < 2:
        return None
    return f"{parts[0][0]} {parts[-1]}"


@dataclass
class RosterPlayer:
    provider_player_id: str
    display_name: str
    normalized_name: str
    team_id: str
    team_name: str


@dataclass
class IdentityResolution:
    canonical_player_id: str | None
    display_name: str
    confidence: str
    method: str
    team_id: str = ""
    team_name: str = ""


class ESPNRosterClient:
    """Small, cached client for ESPN's free public roster API."""

    def __init__(self, session: requests.Session | None = None, timeout: int = 20,
                 cache_ttl_seconds: float = 6 * 3600):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._team_cache: dict[str, tuple[float, list[dict]]] = {}
        self._roster_cache: dict[str, tuple[float, list[RosterPlayer]]] = {}

    def get_teams(self, league: str) -> list[dict]:
        sport_path = _ESPN_SPORT_PATH.get(league.upper())
        if not sport_path:
            raise ValueError(f"No ESPN roster source configured for league {league!r}")
        cached = self._team_cache.get(league)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        resp = self.session.get(
            ESPN_TEAMS_URL_TEMPLATE.format(sport_path=sport_path), timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        result = [t["team"] for t in teams if "team" in t]
        self._team_cache[league] = (now, result)
        return result

    def get_roster(self, league: str, team_id: str) -> list[RosterPlayer]:
        sport_path = _ESPN_SPORT_PATH.get(league.upper())
        if not sport_path:
            raise ValueError(f"No ESPN roster source configured for league {league!r}")
        cache_key = f"{league}:{team_id}"
        cached = self._roster_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        resp = self.session.get(
            ESPN_ROSTER_URL_TEMPLATE.format(sport_path=sport_path, team_id=team_id),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        team_name = (data.get("team") or {}).get("displayName", "")
        # ESPN's roster shape differs by sport, verified live 2026-08-22:
        # WNBA's "athletes" is a flat list of athlete objects directly;
        # MLB/NFL's is a list of position-group objects (e.g.
        # {"position": "Pitchers", "items": [...]}), with the actual
        # athlete objects nested under "items". Flatten both into the
        # same shape rather than assuming WNBA's was universal.
        raw_entries = data.get("athletes") or []
        flat_athletes = []
        for entry in raw_entries:
            if isinstance(entry, dict) and "items" in entry:
                flat_athletes.extend(entry.get("items") or [])
            else:
                flat_athletes.append(entry)
        players = []
        for athlete in flat_athletes:
            display_name = athlete.get("displayName") or athlete.get("fullName") or ""
            if not display_name or not athlete.get("id"):
                continue
            players.append(RosterPlayer(
                provider_player_id=str(athlete["id"]),
                display_name=display_name,
                normalized_name=normalize_name(display_name),
                team_id=str(team_id),
                team_name=team_name,
            ))
        self._roster_cache[cache_key] = (now, players)
        return players

    def find_team_id(self, league: str, team_display_name: str) -> str | None:
        """Exact match only — team names are a small, well-known set."""
        for team in self.get_teams(league):
            if team.get("displayName") == team_display_name:
                return str(team.get("id"))
        return None


def resolve_player_identity(
    raw_name: str,
    *,
    league: str,
    home_team: str,
    away_team: str,
    client: ESPNRosterClient,
) -> IdentityResolution:
    """Resolve *raw_name* against the two teams actually playing.

    Scoping to the specific game's rosters (rather than a league-wide
    search) is the main safety mechanism against name collisions — WNBA
    has ~180 active players, but any two teams' combined rosters are
    ~24-30, where a same-normalized-name collision is very unlikely.
    """
    target = normalize_name(raw_name)
    if not target:
        return IdentityResolution(None, raw_name, CONFIDENCE_UNRESOLVED, "empty_name")

    try:
        home_id = client.find_team_id(league, home_team)
        away_id = client.find_team_id(league, away_team)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Roster team lookup failed for %s/%s: %s", home_team, away_team, exc)
        return IdentityResolution(None, raw_name, CONFIDENCE_UNRESOLVED, "team_lookup_failed")

    roster: list[RosterPlayer] = []
    for team_id in (home_id, away_id):
        if not team_id:
            continue
        try:
            roster.extend(client.get_roster(league, team_id))
        except requests.RequestException as exc:
            logger.warning("Roster fetch failed for team %s: %s", team_id, exc)

    if not roster:
        return IdentityResolution(None, raw_name, CONFIDENCE_UNRESOLVED, "no_roster_data")

    # 1. Exact normalized-name match.
    exact = [p for p in roster if p.normalized_name == target]
    if len(exact) == 1:
        p = exact[0]
        return IdentityResolution(
            f"ESPN_{league.upper()}_{p.provider_player_id}", p.display_name,
            CONFIDENCE_HIGH, "espn_roster_exact_match", p.team_id, p.team_name,
        )
    if len(exact) > 1:
        return IdentityResolution(None, raw_name, CONFIDENCE_LOW, "multiple_exact_matches")

    # 2. Suffix-stripped match (e.g. incoming "X Y Jr" vs roster "X Y").
    target_stripped = _strip_suffix(target)
    suffix_matches = [p for p in roster if _strip_suffix(p.normalized_name) == target_stripped]
    if len(suffix_matches) == 1:
        p = suffix_matches[0]
        return IdentityResolution(
            f"ESPN_{league.upper()}_{p.provider_player_id}", p.display_name,
            CONFIDENCE_MEDIUM, "espn_roster_suffix_stripped_match", p.team_id, p.team_name,
        )
    if len(suffix_matches) > 1:
        return IdentityResolution(None, raw_name, CONFIDENCE_LOW, "multiple_suffix_matches")

    # 3. First-initial + last-name match (handles "A. Wilson" vs "A'ja Wilson").
    target_key = _initial_last_key(target)
    if target_key:
        initial_matches = [p for p in roster if _initial_last_key(p.normalized_name) == target_key]
        if len(initial_matches) == 1:
            p = initial_matches[0]
            return IdentityResolution(
                f"ESPN_{league.upper()}_{p.provider_player_id}", p.display_name,
                CONFIDENCE_MEDIUM, "espn_roster_initial_last_match", p.team_id, p.team_name,
            )
        if len(initial_matches) > 1:
            return IdentityResolution(None, raw_name, CONFIDENCE_LOW, "multiple_initial_matches")

    return IdentityResolution(None, raw_name, CONFIDENCE_UNRESOLVED, "no_roster_match")
