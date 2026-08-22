"""Shared player-props fetch loop for The Odds API — extracted from
``src/sports/wnba.py::fetch_and_parse_props()`` 2026-08-22 when MLB and
NFL got their own Odds-API-sourced props: the discover/dedup/per-event
credit-checked fetch loop was never actually WNBA-specific, only the
sport key, market keys, and parser were.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _recently_captured_prop_event_ids(conn, event_ids: list[str], within_hours: float = 0.5) -> set[str]:
    """Event IDs that already have a player_prop_odds row captured within
    the last *within_hours* — used to avoid re-spending props credits on
    games a scheduler already covered this cycle."""
    if not event_ids:
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    placeholders = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT event_id FROM player_prop_odds
            WHERE event_id IN ({placeholders}) AND captured_at >= ?""",
        (*event_ids, cutoff),
    ).fetchall()
    return {r["event_id"] for r in rows}


def fetch_player_props(
    conn, *, sport_key: str, prop_market_keys: str, parse_fn, league: str,
    event_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Discover, dedup, and fetch live player-prop odds for one Odds-API
    sport, then hand the raw per-event responses to *parse_fn* (a
    league-specific thin wrapper around
    ``src.odds_api_props_parser.parse_player_props``).

    Requires a DB connection (*conn*) for identity-resolution caching and
    the per-event recently-captured dedup check — unlike a pure fetch,
    which wouldn't need one.

    Props are billed per event (8 credits/event for an 8-market request,
    confirmed live 2026-08-19 for WNBA — scales with however many markets
    *prop_market_keys* actually lists), so every event fetched here is
    gated by a real, live ``credit_budget_check()`` against the shared
    Odds-API budget (now used by WNBA and, as of 2026-08-22, MLB/NFL too)
    — not just a scheduling-level estimate.
    """
    from src.odds_api_client import OddsAPIClient, EVENTS_CACHE_TTL_SECONDS
    from src.player_identity import ESPNRosterClient
    from src.odds_api_credits import record_client_quota, credit_budget_check, PROPS_COST_PER_EVENT

    # get_events() takes no time-varying params -- without a bounded
    # max_cache_age this would serve the same frozen event list forever
    # after the first real call (see EVENTS_CACHE_TTL_SECONDS's docstring
    # in src/odds_api_client.py).
    client = OddsAPIClient(max_cache_age=EVENTS_CACHE_TTL_SECONDS)
    events, events_from_cache = client.get_events(sport_key=sport_key)
    record_client_quota(conn, client, endpoint="events", job_type=f"{league.lower()}_props_discovery",
                         cache_hit=events_from_cache)
    if event_id:
        events = [e for e in events if e.get("id") == event_id]
    else:
        # Intelligent prioritization: skip events whose props were already
        # captured recently. Without this, a scheduler that legitimately
        # re-checks every throttle interval inside the pregame window
        # (see src/league_schedule.py) would re-spend credits on the SAME
        # games every time it fires — an explicit event_id request (e.g.
        # a manual re-check) always bypasses this and fetches fresh.
        recent = _recently_captured_prop_event_ids(conn, [e["id"] for e in events])
        skipped = [e for e in events if e["id"] in recent]
        events = [e for e in events if e["id"] not in recent]
        if skipped:
            logger.info(
                "%s props: skipping %d event(s) already captured within the last 30 min",
                league, len(skipped),
            )

    roster_client = ESPNRosterClient()
    event_odds_responses = []
    for event in events:
        allowed, reason = credit_budget_check(conn, PROPS_COST_PER_EVENT)
        if not allowed:
            logger.warning(
                "%s props fetch stopped at %d/%d events — credit budget: %s",
                league, len(event_odds_responses), len(events), reason,
            )
            break
        try:
            event_odds, from_cache = client.get_event_odds(
                event["id"], sport_key=sport_key, markets=prop_market_keys,
            )
        except Exception:
            continue
        record_client_quota(conn, client, endpoint="event_odds", job_type=f"{league.lower()}_props_scan",
                             cache_hit=from_cache)
        event_odds_responses.append(event_odds)

    parsed = parse_fn(event_odds_responses, conn=conn, roster_client=roster_client)
    return parsed.odds_rows, parsed.audit_rows
