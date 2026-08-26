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


def _parse_commence_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


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


def _reload_recent_prop_odds_rows(conn, event_ids: list[str], within_hours: float = 0.5) -> list[dict]:
    """Rebuild generic odds rows (the exact shape src.odds_api_props_parser
    produces) from already-captured player_prop_odds rows for *event_ids*.

    Real bug, found 2026-08-26 investigating "zero WNBA recommendations
    saved for 6 straight days" despite real games, real odds, and real
    +EV opportunities existing: _recently_captured_prop_event_ids()
    correctly skips re-FETCHING (saving credits), but the events it skips
    were then dropped from fetch_player_props()'s return value entirely
    — not re-fetched AND not reused. Since a scheduler's props-scan
    cadence (~20-40min in production) is close to or shorter than the
    30-minute dedup window, and a WNBA game stays in the pregame/live
    window for hours, this meant almost every real scheduled run after
    the first one that day saw ZERO player props at all (confirmed live:
    game markets alone never produced a single actionable WNBA
    opportunity, while a genuinely fresh props fetch found 17 real
    actionable opportunities at 2-6% EV the same slate). This reloads the
    rows already sitting in player_prop_odds instead of silently losing
    them — no new API call, no new credits spent, but the scan still
    sees them.
    """
    if not event_ids:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    placeholders = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"""SELECT event_id, odd_id, sportsbook, player_id, player_name,
                   team_id, team_name, market_type, market_group_key,
                   side, line, price, decimal_odds, is_alt_line, available,
                   validation_status, mapping_confidence, mapping_method,
                   validation_reason, captured_at
            FROM player_prop_odds
            WHERE event_id IN ({placeholders}) AND captured_at >= ?
              AND validation_status = 'VALID' AND available = 1""",
        (*event_ids, cutoff),
    ).fetchall()
    reloaded = []
    for r in rows:
        row = dict(r)
        # player_prop_odds has no observation_time column (only
        # captured_at) — the parser's observation_time is a slightly
        # more precise book_last_update when available, but captured_at
        # is the same fallback _build_prop_row itself uses when that
        # parse fails, so it's a correct, honest substitute here.
        row["observation_time"] = row.get("captured_at") or ""
        row["raw_line"] = row.get("line")  # no favorite/underdog sign concept for player O/U props
        reloaded.append(row)
    return reloaded


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

    # Real bug, found live 2026-08-22 testing NFL props end-to-end:
    # get_events() returns EVERY event currently listed for the sport --
    # for a full-season sport like NFL that's the entire season (272
    # games), not "the near-term slate". Without this filter, the loop
    # below would spend credit-budget-check cycles and real API calls
    # working through games months away, none of which are pregame-
    # relevant right now -- the exact same class of bug already found
    # and fixed for the game-odds fetch (fetch_game_odds_via_odds_api's
    # -6h/+42h window). WNBA never hit this in practice (its unbounded
    # response happens to already be near-term-only), but the filter is
    # harmless and defensive there too.
    if not event_id:
        now = datetime.now(timezone.utc)
        window_start, window_end = now - timedelta(hours=6), now + timedelta(hours=42)
        before_filter = len(events)
        events = [
            e for e in events
            if (dt := _parse_commence_time(e.get("commence_time"))) is not None
            and window_start <= dt <= window_end
        ]
        if before_filter != len(events):
            logger.info(
                "%s props: %d/%d discovered events are outside the near-term "
                "pregame window, skipped", league, before_filter - len(events), before_filter,
            )

    reloaded_rows: list[dict] = []
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
            # Real bug (see _reload_recent_prop_odds_rows's docstring):
            # "skip" must mean skip the re-FETCH, not lose the data —
            # reload what's already in player_prop_odds for these events
            # so this scan still sees them.
            reloaded_rows = _reload_recent_prop_odds_rows(conn, [e["id"] for e in skipped])
            if reloaded_rows:
                logger.info(
                    "%s props: reloaded %d already-captured row(s) for %d skipped event(s)",
                    league, len(reloaded_rows), len(skipped),
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
    return parsed.odds_rows + reloaded_rows, parsed.audit_rows
