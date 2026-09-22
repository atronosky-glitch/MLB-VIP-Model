"""Live arbitrage/middle scan — reads the odds a normal scan already
ingested (no extra API cost) and syncs any opportunities found into
arbitrage_opportunities / middle_opportunities.

Deliberately excludes spread/run-line markets (game_spread_ou,
game_runline_ou) — see database/db_manager.py's module docstring for
the arb/middle section for why (grading a spread needs the SIGNED line,
which isn't part of player_prop_odds's row shape).
"""

from __future__ import annotations

from database.db_manager import (
    sync_arbitrage_opportunities, sync_middle_opportunities,
    grade_arbitrage_opportunities, grade_middle_opportunities,
)
from src.arbitrage import find_arbitrage_opportunities
from src.middling import find_middle_opportunities

# How fresh a raw odds row must be to trust it as "the live market right
# now" -- matches src/prop_config.py's own freshness convention (an hour
# is generous; scans run far more often than that, so this only ever
# excludes odds from a source that's stopped updating).
DEFAULT_FRESHNESS_SECONDS = 3600

_EXCLUDED_MARKET_TYPES = frozenset({"game_spread_ou", "game_runline_ou"})


def _fetch_recent_odds_rows(conn, league: str, freshness_seconds: int) -> list[dict]:
    # The interval has to be a literal in the SQL text (not a bound
    # parameter) for database.connection._convert_sql's regex to
    # translate datetime('now', '-N seconds') into Postgres's
    # (NOW() + interval '-N seconds')::text — a bound '?' there would
    # pass through unconverted and fail against production. Safe to
    # interpolate: always an int this module computes itself, never
    # caller-supplied text.
    seconds = int(freshness_seconds)
    rows = conn.execute(
        f"""SELECT event_id, player_id, player_name, team_name, market_type,
                   market_group_key, side, line, sportsbook, price, decimal_odds
            FROM player_prop_odds
            WHERE league = ? AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
              AND captured_at >= (datetime('now', '-{seconds} seconds'))""",
        (league,),
    ).fetchall()
    return [
        dict(r) for r in rows
        if (dict(r).get("market_type") or "") not in _EXCLUDED_MARKET_TYPES
    ]


# A fallback display value only -- _league_prefix() in
# src/message_formatter.py always prefers the real "league" code (set
# on every opp below) when present, so this "sport" value is now just a
# secondary label. NCAAF was missing here entirely, which meant
# .get(league, "baseball") silently mislabeled every college-football
# opportunity as baseball -- kept correct now regardless, since this
# dict is cheap to keep complete.
_SPORT_BY_LEAGUE = {"MLB": "baseball", "NFL": "football", "WNBA": "basketball", "NCAAF": "football"}


def _event_context(conn) -> dict[str, dict]:
    """matchup/event_start_time/sport/league per event_id, for display.

    Deliberately NOT scoped by league (2026-09-22 fix): event_id is a
    globally unique key on its own, so filtering this lookup by
    "whichever league's scan pass is asking" only ever loses
    information. Confirmed live: a WNBA game's player_prop_odds rows
    were mistakenly tagged league='MLB' at ingestion (a data bug, now
    fixed at the source -- see save_player_prop_batch), so the MLB scan
    pass picked them up and asked THIS function for event_id's matchup
    scoped to league='MLB' -- which could never find the real
    (league='WNBA') games/historical_recommendations row for that same
    event_id, producing a permanent "Matchup unavailable, MLB" card for
    an actual WNBA game. Looking up by event_id alone, across every
    league, means a mistagged odds row still resolves to the TRUE
    matchup/league whenever games/historical_recommendations has it --
    defense in depth against this exact class of bug, not just a fix
    for this one instance of it.

    ``games`` (keyed by event_id, populated by the odds/schedule
    pipeline independent of whether the model ever produced a
    recommendation for that game) is the PRIMARY source -- confirmed
    live 2026-09-15: a "Game Total" middle (built directly from raw
    odds, not from any model recommendation, since the model doesn't
    generate picks for plain game-total markets) showed matchup=None
    and no league badge even though a real ``games`` row existed for
    that event_id the whole time (Denver Broncos @ Kansas City Chiefs,
    NFL) -- ``historical_recommendations`` simply has no row for an
    event the model never touched. ``historical_recommendations`` is
    kept as a fallback for anything ``games`` doesn't have (never the
    reverse), since it's a second real source and there's no reason to
    throw it away."""
    context: dict[str, dict] = {}

    game_rows = conn.execute(
        """SELECT event_id, league, away_team, home_team, start_time
           FROM games
           WHERE event_id IS NOT NULL"""
    ).fetchall()
    for r in game_rows:
        d = dict(r)
        away, home = d.get("away_team"), d.get("home_team")
        row_league = d.get("league")
        context[d["event_id"]] = {
            "matchup": f"{away} @ {home}" if away and home else None,
            "event_start_time": d.get("start_time"),
            "sport": _SPORT_BY_LEAGUE.get(row_league, "unknown"),
            "league": row_league,
        }

    rec_rows = conn.execute(
        """SELECT event_id, matchup, event_start_time, sport, league
           FROM historical_recommendations
           WHERE event_id IS NOT NULL
           ORDER BY created_at DESC"""
    ).fetchall()
    for r in rec_rows:
        d = dict(r)
        context.setdefault(d["event_id"], d)
        # A games row might itself have a null matchup/league (missing
        # team names, or no games row at all) -- backfill from
        # historical_recommendations rather than leaving it unknown
        # when a better answer exists.
        entry = context[d["event_id"]]
        if not entry.get("matchup") and d.get("matchup"):
            entry["matchup"] = d["matchup"]
        if not entry.get("league") and d.get("league"):
            entry["league"] = d["league"]

    return context


def run_scan(conn, league: str = "MLB", freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS) -> dict:
    """Detect and sync arbitrage/middle opportunities for *league* from
    already-ingested odds, then grade whatever's since settled."""
    rows = _fetch_recent_odds_rows(conn, league, freshness_seconds)
    context = _event_context(conn)

    arb_opps = find_arbitrage_opportunities(rows)
    mid_opps = find_middle_opportunities(rows)

    # Fallback sport label when _event_context found no games/
    # historical_recommendations row for an event at all (context is
    # then {} for it) -- was hardcoded "baseball" regardless of *league*,
    # which is the exact bug that mislabeled a shown-with-no-matchup
    # non-MLB opportunity. Now moot for the Discord header itself
    # (opp["league"] below is always correct and _league_prefix()
    # prefers it), but kept correct anyway since it's cheap to.
    default_sport = _SPORT_BY_LEAGUE.get(league, "unknown")

    for opp in arb_opps:
        ctx = context.get(opp.get("event_id"), {})
        opp["matchup"] = ctx.get("matchup")
        opp["event_start_time"] = ctx.get("event_start_time")
        opp["sport"] = ctx.get("sport") or default_sport
        # 2026-09-22: prefer the TRUE league from games/
        # historical_recommendations (via _event_context, now looked up
        # by event_id alone -- see its docstring) over *league*, the
        # scan-pass parameter -- *league* is only "which league's
        # player_prop_odds rows did we scan," which can be WRONG if an
        # odds row was mistagged at ingestion (a real, confirmed-live
        # bug: a WNBA game's odds were tagged league='MLB'). Falls back
        # to *league* only when no real source knows better, so display
        # is never worse than before.
        opp["league"] = ctx.get("league") or league
    for opp in mid_opps:
        ctx = context.get(opp.get("event_id"), {})
        opp["matchup"] = ctx.get("matchup")
        opp["event_start_time"] = ctx.get("event_start_time")
        opp["sport"] = ctx.get("sport") or default_sport
        opp["league"] = ctx.get("league") or league

    arb_sync = sync_arbitrage_opportunities(conn, league, arb_opps)
    mid_sync = sync_middle_opportunities(conn, league, mid_opps)
    arb_grade = grade_arbitrage_opportunities(conn)
    mid_grade = grade_middle_opportunities(conn)

    # sync_*_opportunities stamps opportunity_id onto each opp dict and
    # reports which ones are new this pass -- filter down to those so a
    # caller (src/worker.py) can alert only what just appeared, not
    # everything still active from a prior scan.
    new_arb_ids = set(arb_sync.get("new_ids", []))
    new_mid_ids = set(mid_sync.get("new_ids", []))
    new_arbitrage = [o for o in arb_opps if o.get("opportunity_id") in new_arb_ids]
    new_middles = [o for o in mid_opps if o.get("opportunity_id") in new_mid_ids]

    return {
        "league": league,
        "rows_examined": len(rows),
        "arbitrage": {"detected": len(arb_opps), **arb_sync, "grading": arb_grade},
        "middles": {"detected": len(mid_opps), **mid_sync, "grading": mid_grade},
        "new_arbitrage": new_arbitrage,
        "new_middles": new_middles,
    }
