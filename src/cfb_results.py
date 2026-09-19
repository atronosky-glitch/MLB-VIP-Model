"""Verified CFB (college football) result ingestion via ESPN's public
scoreboard API.

Free, keyless, same shape as ``src.nfl_results`` at the analogous URL
(``.../football/college-football`` instead of ``.../football/nfl``) —
confirmed live 2026-09-19 against real in-progress games (e.g. Georgia
Bulldogs at Arkansas Razorbacks, real scores returned).

Much simpler than ``nfl_results.py``: CFB has no player props (see
``src/sports/cfb.py``'s module docstring), so this only ever needs to
resolve final scores for ``event_results`` — the generic, sport-agnostic
``src.game_settlement`` module (already shared by MLB/NFL/WNBA) does the
actual moneyline/spread/total/team-total grading from there. No
boxscore/athlete-stat extraction exists in this file at all, unlike
``nfl_results.py``, because there's nothing here that would ever use it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from database.db_manager import save_event_result

logger = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
RESULT_SOURCE = "ESPN CFB"

_VOID_STATUS_NAMES = frozenset({
    "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_SUSPENDED",
})


def normalize_name(value: str | None) -> str:
    value = (value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class ESPNCFBClient:
    """Small, retry-free client for ESPN's public CFB scoreboard API."""

    def __init__(self, session: requests.Session | None = None, timeout: int = 20):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.timeout = timeout

    def fetch_scoreboard(self, date_value: str) -> list[dict]:
        """*date_value* is an ISO date (YYYY-MM-DD); ESPN wants YYYYMMDD.

        Unlike NFL's ~16-game slate, a CFB Saturday can have 60+ games —
        ESPN's scoreboard endpoint returns them all in one call (verified
        live: no pagination needed for a single day's request), so no
        extra "groups"/limit handling is required here.
        """
        response = self.session.get(
            f"{BASE_URL}/scoreboard",
            params={"dates": date_value.replace("-", "")},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json().get("events", [])


def _match_scoreboard_event(
    events: list[dict], away_team: str, home_team: str, start_time: str | None,
) -> dict | None:
    away = normalize_name(away_team)
    home = normalize_name(home_team)
    target_time = _parse_time(start_time)
    matches = []
    for event in events:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        ev_away = ev_home = ""
        for c in competitors:
            name = normalize_name((c.get("team") or {}).get("displayName"))
            if c.get("homeAway") == "away":
                ev_away = name
            elif c.get("homeAway") == "home":
                ev_home = name
        if ev_away != away or ev_home != home:
            continue
        event_time = _parse_time(event.get("date"))
        if target_time and event_time and abs((event_time - target_time).total_seconds()) > 18 * 3600:
            continue
        matches.append(event)
    return matches[0] if len(matches) == 1 else None


def ingest_results_for_recommendations(
    conn, recommendations: list[dict], client: ESPNCFBClient | None = None,
) -> dict:
    """Fetch final CFB scores and persist them as event_results for
    unresolved game-level recommendations. Game-level only — see module
    docstring; any non-game-level market_type is simply not something
    this league's scanner ever produces, so there's no filtering step
    equivalent to NFL's ``_SUPPORTED_BASE_MARKETS`` needed here.
    """
    client = client or ESPNCFBClient()

    def matchup_teams(rec: dict) -> tuple[str, str]:
        away = rec.get("away_team") or ""
        home = rec.get("home_team") or ""
        if (not away or not home) and " @ " in (rec.get("matchup") or ""):
            away, home = rec["matchup"].split(" @ ", 1)
        return away, home

    by_date: dict[str, list[dict]] = {}
    for rec in recommendations:
        parsed = _parse_time(rec.get("event_start_time"))
        if parsed:
            by_date.setdefault(parsed.date().isoformat(), []).append(rec)

    stats = {
        "recommendations": len(recommendations), "games_final": 0,
        "unresolved": 0, "errors": 0,
        "unresolved_reasons": {
            "missing_start_time": 0,
            "missing_matchup": 0,
            "scoreboard_fetch_error": 0,
            "game_matching_failure": 0,
            "game_not_final": 0,
        },
    }
    reasons = stats["unresolved_reasons"]
    reasons["missing_start_time"] = sum(1 for rec in recommendations if not rec.get("event_start_time"))
    stats["unresolved"] = reasons["missing_start_time"]

    seen_events: set[str] = set()

    for date_value, date_recs in by_date.items():
        try:
            scoreboard = client.fetch_scoreboard(date_value)
        except Exception:
            logger.exception("ESPN CFB scoreboard fetch failed date=%s", date_value)
            stats["errors"] += 1
            reasons["scoreboard_fetch_error"] += len(date_recs)
            continue

        for rec in date_recs:
            event_id = rec.get("event_id")
            if not rec.get("event_start_time"):
                continue  # already counted above
            if event_id in seen_events:
                continue
            away, home = matchup_teams(rec)
            if not away or not home:
                stats["unresolved"] += 1
                reasons["missing_matchup"] += 1
                seen_events.add(event_id)
                continue
            sb_event = _match_scoreboard_event(scoreboard, away, home, rec.get("event_start_time"))
            if not sb_event:
                stats["unresolved"] += 1
                reasons["game_matching_failure"] += 1
                seen_events.add(event_id)
                continue
            seen_events.add(event_id)

            status = ((sb_event.get("status") or {}).get("type") or {})
            if not status.get("completed"):
                status_name = (status.get("name") or "").upper()
                if status_name in _VOID_STATUS_NAMES:
                    save_event_result(conn, event_id, final_status=status_name, result_source=RESULT_SOURCE)
                    stats["games_final"] += 1
                else:
                    stats["unresolved"] += 1
                    reasons["game_not_final"] += 1
                continue

            competitors = (sb_event.get("competitions") or [{}])[0].get("competitors") or []
            away_score = home_score = None
            for c in competitors:
                score = c.get("score")
                if c.get("homeAway") == "away" and score is not None:
                    away_score = int(score)
                elif c.get("homeAway") == "home" and score is not None:
                    home_score = int(score)
            save_event_result(
                conn, event_id, final_status="FINAL",
                away_score=away_score, home_score=home_score, result_source=RESULT_SOURCE,
            )
            stats["games_final"] += 1

    return stats
