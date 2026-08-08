"""Verified MLB StatsAPI result ingestion.

The MLB StatsAPI is used only for final-result facts. Provider identity
bridging is conservative: games require an exact normalized team pair and
players require one exact normalized name within that game's box score.
Ambiguous or missing facts are unresolved, never inferred as zero.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from database.db_manager import save_event_result, save_player_stat_result

logger = logging.getLogger(__name__)

BASE_URL = "https://statsapi.mlb.com/api"
RESULT_SOURCE = "MLB StatsAPI"
_MARKET_FIELDS = {
    "pitching_strikeouts": ("pitching", "strikeOuts"),
    "pitching_hits": ("pitching", "hits"),
    "pitching_basesOnBalls": ("pitching", "baseOnBalls"),
    "pitching_earnedRuns": ("pitching", "earnedRuns"),
    "pitching_outs": ("pitching", "outs"),
    "batting_hits": ("batting", "hits"),
    "batting_totalBases": ("batting", "totalBases"),
    "batting_homeRuns": ("batting", "homeRuns"),
    "batting_stolenBases": ("batting", "stolenBases"),
}


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


class MLBStatsClient:
    """Small, retry-free client for the public MLB StatsAPI."""

    def __init__(self, session: requests.Session | None = None, timeout: int = 20):
        self.session = session or requests.Session()
        self.timeout = timeout

    def fetch_schedule(self, date_value: str) -> list[dict]:
        response = self.session.get(
            f"{BASE_URL}/v1/schedule",
            params={"sportId": 1, "date": date_value},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return [game for day in payload.get("dates", []) for game in day.get("games", [])]

    def fetch_game_feed(self, game_pk: int | str) -> dict:
        response = self.session.get(
            f"{BASE_URL}/v1.1/game/{game_pk}/feed/live",
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def _match_schedule_game(
    games: list[dict], away_team: str, home_team: str, start_time: str | None,
) -> dict | None:
    away = normalize_name(away_team)
    home = normalize_name(home_team)
    target_time = _parse_time(start_time)
    matches = []
    for game in games:
        teams = game.get("teams", {})
        game_away = normalize_name(((teams.get("away") or {}).get("team") or {}).get("name"))
        game_home = normalize_name(((teams.get("home") or {}).get("team") or {}).get("name"))
        if game_away != away or game_home != home:
            continue
        game_time = _parse_time(game.get("gameDate"))
        if target_time and game_time and abs((game_time - target_time).total_seconds()) > 18 * 3600:
            continue
        matches.append(game)
    return matches[0] if len(matches) == 1 else None


def _iter_players(feed: dict):
    for team_side in ("away", "home"):
        team = ((feed.get("liveData") or {}).get("boxscore") or {}).get("teams", {}).get(team_side, {})
        for player in (team.get("players") or {}).values():
            person = player.get("person") or {}
            yield team_side, person, player


def _find_player(feed: dict, player_name: str) -> tuple[str, dict, dict] | None:
    target = normalize_name(player_name)
    matches = [item for item in _iter_players(feed) if normalize_name(item[1].get("fullName")) == target]
    return matches[0] if len(matches) == 1 else None


def extract_stat_fact(feed: dict, recommendation: dict) -> dict | None:
    """Extract one verified numeric fact for a recommendation."""
    match = _find_player(feed, recommendation.get("player_name"))
    if not match:
        return None
    _team_side, person, player = match
    market_type = recommendation.get("market_type", "")
    base_market = market_type.removesuffix("_ou").removesuffix("_yn")

    if base_market == "pitching_win":
        decisions = (feed.get("liveData") or {}).get("decisions") or {}
        winner_id = ((decisions.get("winner") or {}).get("id"))
        value = 1 if winner_id is not None and str(winner_id) == str(person.get("id")) else 0
        stats = (player.get("stats") or {}).get("pitching")
        if not stats:
            return None
    else:
        field_spec = _MARKET_FIELDS.get(base_market)
        if not field_spec:
            return None
        stats = (player.get("stats") or {}).get(field_spec[0]) or {}
        value = stats.get(field_spec[1])
        if value is None:
            return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return {
        "value": numeric,
        "player_id": str(person.get("id")),
        "player_name": person.get("fullName"),
        "source": RESULT_SOURCE,
    }


def ingest_results_for_recommendations(conn, recommendations: list[dict], client: MLBStatsClient | None = None) -> dict:
    """Fetch final MLB facts and persist them for unresolved recommendations."""
    client = client or MLBStatsClient()
    def matchup_teams(rec: dict) -> tuple[str, str]:
        away = rec.get("away_team") or ""
        home = rec.get("home_team") or ""
        if (not away or not home) and " @ " in (rec.get("matchup") or ""):
            away, home = rec["matchup"].split(" @ ", 1)
        return away, home
    by_date: dict[str, list[dict]] = {}
    for rec in recommendations:
        base_market = (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn")
        if base_market != "pitching_win" and base_market not in _MARKET_FIELDS:
            # Keep unsupported registry markets visible but do not call the
            # external provider for facts the adapter cannot verify.
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
            "schedule_fetch_error": 0,
            "game_matching_failure": 0,
            "game_not_final": 0,
            "game_feed_error": 0,
            "player_fact_missing_or_ambiguous": 0,
        },
    }
    reasons = stats["unresolved_reasons"]
    reasons["unsupported_or_research_market"] = sum(
        1 for rec in recommendations
        if (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") != "pitching_win"
        and (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") not in _MARKET_FIELDS
    )
    reasons["missing_start_time"] += sum(
        1 for rec in recommendations
        if (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") == "pitching_win"
        or (rec.get("market_type") or "").removesuffix("_ou").removesuffix("_yn") in _MARKET_FIELDS
        if not rec.get("event_start_time")
    )
    stats["unresolved"] = reasons["unsupported_or_research_market"] + reasons["missing_start_time"]
    for date_value, date_recs in by_date.items():
        try:
            schedule = client.fetch_schedule(date_value)
        except Exception:
            logger.exception("MLB StatsAPI schedule fetch failed date=%s", date_value)
            stats["errors"] += 1
            reasons["schedule_fetch_error"] += len(date_recs)
            continue
        feeds: dict[str, dict] = {}
        for rec in date_recs:
            event_id = rec.get("event_id")
            if event_id in feeds:
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
            game = _match_schedule_game(schedule, away, home, rec.get("event_start_time"))
            if not game:
                stats["unresolved"] += 1
                reasons["game_matching_failure"] += 1
                continue
            game_pk = game.get("gamePk")
            try:
                feed = client.fetch_game_feed(game_pk)
            except Exception:
                logger.exception("MLB StatsAPI game feed failed game_pk=%s", game_pk)
                stats["errors"] += 1
                reasons["game_feed_error"] += 1
                continue
            status = ((feed.get("gameData") or {}).get("status") or {}).get("abstractGameState")
            if status != "Final":
                stats["unresolved"] += 1
                reasons["game_not_final"] += 1
                continue
            feeds[event_id] = feed
            stats["games_final"] += 1
            teams = ((feed.get("liveData") or {}).get("boxscore") or {}).get("teams", {})
            away_score = (((teams.get("away") or {}).get("teamStats") or {}).get("batting") or {}).get("runs")
            home_score = (((teams.get("home") or {}).get("teamStats") or {}).get("batting") or {}).get("runs")
            save_event_result(conn, event_id, final_status="FINAL", away_score=away_score, home_score=home_score, result_source=RESULT_SOURCE)

        for rec in date_recs:
            feed = feeds.get(rec.get("event_id"))
            fact = extract_stat_fact(feed, rec) if feed else None
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
