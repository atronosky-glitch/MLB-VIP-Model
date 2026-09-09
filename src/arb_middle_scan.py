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


def _event_context(conn, league: str) -> dict[str, dict]:
    """matchup/event_start_time per event_id, for display — best-effort
    from whatever recent historical_recommendations rows already know;
    never blocks detection if unavailable."""
    rows = conn.execute(
        """SELECT event_id, matchup, event_start_time, sport
           FROM historical_recommendations
           WHERE league = ? AND event_id IS NOT NULL
           ORDER BY created_at DESC"""
        , (league,),
    ).fetchall()
    context: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        context.setdefault(d["event_id"], d)
    return context


def run_scan(conn, league: str = "MLB", freshness_seconds: int = DEFAULT_FRESHNESS_SECONDS) -> dict:
    """Detect and sync arbitrage/middle opportunities for *league* from
    already-ingested odds, then grade whatever's since settled."""
    rows = _fetch_recent_odds_rows(conn, league, freshness_seconds)
    context = _event_context(conn, league)

    arb_opps = find_arbitrage_opportunities(rows)
    mid_opps = find_middle_opportunities(rows)

    for opp in arb_opps:
        ctx = context.get(opp.get("event_id"), {})
        opp["matchup"] = ctx.get("matchup")
        opp["event_start_time"] = ctx.get("event_start_time")
        opp["sport"] = ctx.get("sport", "baseball")
    for opp in mid_opps:
        ctx = context.get(opp.get("event_id"), {})
        opp["matchup"] = ctx.get("matchup")
        opp["event_start_time"] = ctx.get("event_start_time")
        opp["sport"] = ctx.get("sport", "baseball")

    arb_sync = sync_arbitrage_opportunities(conn, league, arb_opps)
    mid_sync = sync_middle_opportunities(conn, league, mid_opps)
    arb_grade = grade_arbitrage_opportunities(conn)
    mid_grade = grade_middle_opportunities(conn)

    return {
        "league": league,
        "rows_examined": len(rows),
        "arbitrage": {"detected": len(arb_opps), **arb_sync, "grading": arb_grade},
        "middles": {"detected": len(mid_opps), **mid_sync, "grading": mid_grade},
    }
