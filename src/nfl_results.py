"""Verified NFL result ingestion via ESPN's public scoreboard/summary API.

Free, keyless, no rate-limit documented (used the same way MLB StatsAPI is
used for MLB — a public results endpoint kept separate from the paid
SportsGameOdds odds feed). Schema verified live 2026-08-19 against a real
completed game (event 401873272, CIN 16 - DET 14):

- ``GET /scoreboard?dates=YYYYMMDD`` — per-event ``status.type.completed``,
  ``competitions[0].competitors[].team.displayName`` /
  ``.homeAway`` / ``.score``.
- ``GET /summary?event={id}`` — ``boxscore.players[].statistics[]`` grouped
  by category (``passing``/``rushing``/``receiving``/``kicking``/
  ``kickReturns``/``puntReturns``/...), each with a ``labels`` array and
  per-athlete ``stats`` array in the same order. Athlete identity via
  ``athlete.id``/``athlete.displayName``.

Only markets this adapter can verify from that boxscore are auto-settled
(passing/rushing/receiving yards+touchdowns+interceptions, receptions,
field goals made, anytime touchdown). Matches MLB's own current scope —
game-level markets (moneyline/spread/total) are not auto-settled by
``mlb_results.py`` either (see ``automatic_grading.py``, which only grades
from ``player_stat_results``), so this is parity, not a gap specific to
NFL. Provider identity bridging is conservative, mirroring
``mlb_results.py``: games require an exact normalized team-pair match,
players require exactly one exact normalized name within that game's
boxscore. Ambiguous or missing facts are unresolved, never inferred.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from database.db_manager import save_event_result, save_player_stat_result

logger = logging.getLogger(__name__)

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
RESULT_SOURCE = "ESPN NFL"

# market base name (market_type with _ou/_yn stripped) -> (boxscore
# category name, column label). Verified against a real boxscore response.
_SIMPLE_STAT_FIELDS = {
    "passing_yards": ("passing", "YDS"),
    "passing_touchdowns": ("passing", "TD"),
    "passing_interceptions": ("passing", "INT"),
    "rushing_yards": ("rushing", "YDS"),
    "receiving_yards": ("receiving", "YDS"),
    "receiving_receptions": ("receiving", "REC"),
}
# "made/attempted" style columns (e.g. FG = "2/3") — value is the made count.
_SPLIT_STAT_FIELDS = {
    "field_goals_made": ("kicking", "FG"),
}
# Anytime touchdown sums the TD column across every scoring category a
# skill player could appear in — rushing, receiving, and both return types.
_TOUCHDOWN_CATEGORIES = ("rushing", "receiving", "kickReturns", "puntReturns")


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


class ESPNNFLClient:
    """Small, retry-free client for ESPN's public NFL scoreboard/summary API."""

    def __init__(self, session: requests.Session | None = None, timeout: int = 20):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0")
        self.timeout = timeout

    def fetch_scoreboard(self, date_value: str) -> list[dict]:
        """*date_value* is an ISO date (YYYY-MM-DD); ESPN wants YYYYMMDD."""
        response = self.session.get(
            f"{BASE_URL}/scoreboard",
            params={"dates": date_value.replace("-", "")},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json().get("events", [])

    def fetch_summary(self, event_id: str) -> dict:
        response = self.session.get(
            f"{BASE_URL}/summary",
            params={"event": event_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


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


def _iter_athlete_categories(summary: dict):
    """Yield (athlete_id, athlete_name, category_name, labels, stats) rows."""
    box = summary.get("boxscore") or {}
    for team_block in box.get("players") or []:
        for category in team_block.get("statistics") or []:
            cat_name = category.get("name")
            labels = category.get("labels") or []
            for entry in category.get("athletes") or []:
                athlete = entry.get("athlete") or {}
                yield athlete.get("id"), athlete.get("displayName"), cat_name, labels, entry.get("stats") or []


def _find_player_categories(summary: dict, player_name: str) -> tuple[str, dict] | None:
    """Return (athlete_id, {category_name: (labels, stats)}) for one exact name match."""
    target = normalize_name(player_name)
    by_id: dict[str, dict] = {}
    for athlete_id, name, cat_name, labels, stats in _iter_athlete_categories(summary):
        if normalize_name(name) != target:
            continue
        by_id.setdefault(athlete_id, {})[cat_name] = (labels, stats)
    if len(by_id) != 1:
        return None
    athlete_id = next(iter(by_id))
    return athlete_id, by_id[athlete_id]


def _lookup(categories: dict, cat_name: str, label: str) -> str | None:
    spec = categories.get(cat_name)
    if not spec:
        return None
    labels, stats = spec
    if label not in labels:
        return None
    idx = labels.index(label)
    if idx >= len(stats):
        return None
    return stats[idx]


def extract_stat_fact(summary: dict, recommendation: dict) -> dict | None:
    """Extract one verified numeric fact for a recommendation."""
    match = _find_player_categories(summary, recommendation.get("player_name"))
    if not match:
        return None
    player_id, categories = match
    market_type = recommendation.get("market_type", "")
    base_market = market_type.removesuffix("_ou").removesuffix("_yn")

    if base_market == "anytime_touchdown":
        total = 0
        found_any_category = False
        for cat_name in _TOUCHDOWN_CATEGORIES:
            raw = _lookup(categories, cat_name, "TD")
            if raw is None:
                continue
            found_any_category = True
            try:
                total += int(raw)
            except (TypeError, ValueError):
                return None
        if not found_any_category:
            return None
        value = float(total)
    elif base_market in _SIMPLE_STAT_FIELDS:
        cat_name, label = _SIMPLE_STAT_FIELDS[base_market]
        raw = _lookup(categories, cat_name, label)
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
    elif base_market in _SPLIT_STAT_FIELDS:
        cat_name, label = _SPLIT_STAT_FIELDS[base_market]
        raw = _lookup(categories, cat_name, label)
        if raw is None or "/" not in str(raw):
            return None
        made_str = str(raw).split("/", 1)[0]
        try:
            value = float(made_str)
        except ValueError:
            return None
    else:
        return None

    return {
        "value": value,
        "player_id": str(player_id),
        "player_name": recommendation.get("player_name"),
        "source": RESULT_SOURCE,
    }


_SUPPORTED_BASE_MARKETS = frozenset(
    set(_SIMPLE_STAT_FIELDS) | set(_SPLIT_STAT_FIELDS) | {"anytime_touchdown"}
)


def ingest_results_for_recommendations(
    conn, recommendations: list[dict], client: ESPNNFLClient | None = None,
) -> dict:
    """Fetch final NFL facts and persist them for unresolved recommendations."""
    client = client or ESPNNFLClient()

    def matchup_teams(rec: dict) -> tuple[str, str]:
        away = rec.get("away_team") or ""
        home = rec.get("home_team") or ""
        if (not away or not home) and " @ " in (rec.get("matchup") or ""):
            away, home = rec["matchup"].split(" @ ", 1)
        return away, home

    by_date: dict[str, list[dict]] = {}
    for rec in recommendations:
        base_market = (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn")
        if base_market not in _SUPPORTED_BASE_MARKETS:
            continue
        parsed = _parse_time(rec.get("event_start_time"))
        if parsed:
            by_date.setdefault(parsed.date().isoformat(), []).append(rec)

    stats = {
        "recommendations": len(recommendations), "games_final": 0,
        "facts_saved": 0, "unresolved": 0, "errors": 0,
        "unresolved_reasons": {
            "unsupported_or_research_market": 0,
            "missing_start_time": 0,
            "missing_matchup": 0,
            "scoreboard_fetch_error": 0,
            "game_matching_failure": 0,
            "game_not_final": 0,
            "summary_fetch_error": 0,
            "player_fact_missing_or_ambiguous": 0,
        },
    }
    reasons = stats["unresolved_reasons"]
    reasons["unsupported_or_research_market"] = sum(
        1 for rec in recommendations
        if (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") not in _SUPPORTED_BASE_MARKETS
    )
    reasons["missing_start_time"] += sum(
        1 for rec in recommendations
        if (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") in _SUPPORTED_BASE_MARKETS
        and not rec.get("event_start_time")
    )
    stats["unresolved"] = reasons["unsupported_or_research_market"] + reasons["missing_start_time"]

    for date_value, date_recs in by_date.items():
        try:
            scoreboard = client.fetch_scoreboard(date_value)
        except Exception:
            logger.exception("ESPN NFL scoreboard fetch failed date=%s", date_value)
            stats["errors"] += 1
            reasons["scoreboard_fetch_error"] += len(date_recs)
            continue

        summaries: dict[str, dict] = {}
        for rec in date_recs:
            event_id = rec.get("event_id")
            if event_id in summaries:
                continue
            away, home = matchup_teams(rec)
            if not rec.get("event_start_time"):
                stats["unresolved"] += 1
                reasons["missing_start_time"] += 1
                continue
            if not away or not home:
                stats["unresolved"] += 1
                reasons["missing_matchup"] += 1
                continue
            sb_event = _match_scoreboard_event(scoreboard, away, home, rec.get("event_start_time"))
            if not sb_event:
                stats["unresolved"] += 1
                reasons["game_matching_failure"] += 1
                continue
            status = ((sb_event.get("status") or {}).get("type") or {})
            if not status.get("completed"):
                # status.type.name carries ESPN's STATUS_* vocabulary (same
                # field already read above for "completed") — a void game
                # never becomes completed, so it would stay UNRESOLVED
                # forever without this. Anything unrecognized stays
                # unresolved rather than guessed.
                status_name = (status.get("name") or "").upper()
                if status_name in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_SUSPENDED"):
                    save_event_result(conn, event_id, final_status=status_name, result_source=RESULT_SOURCE)
                    stats["games_final"] += 1
                else:
                    stats["unresolved"] += 1
                    reasons["game_not_final"] += 1
                continue
            try:
                summary = client.fetch_summary(sb_event.get("id"))
            except Exception:
                logger.exception("ESPN NFL summary fetch failed event=%s", sb_event.get("id"))
                stats["errors"] += 1
                reasons["summary_fetch_error"] += 1
                continue
            summaries[event_id] = summary
            stats["games_final"] += 1

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

        for rec in date_recs:
            summary = summaries.get(rec.get("event_id"))
            fact = extract_stat_fact(summary, rec) if summary else None
            if not fact:
                stats["unresolved"] += 1
                reasons["player_fact_missing_or_ambiguous"] += 1
                continue
            save_player_stat_result(
                conn, rec["event_id"], rec["player_id"], rec["market_type"],
                player_name=rec.get("player_name"), final_stat_value=fact["value"],
                result_source=RESULT_SOURCE, result_status="FINAL",
            )
            stats["facts_saved"] += 1

    return stats
