"""MLB league adapter.

Thin wrapper around the existing, mature MLB-specific modules — no new
logic. MLB's market registry and results/settlement source predate the
sports/ package and are intentionally left in place rather than moved,
to avoid any behavior change to the production MLB pipeline.

MLB's primary provider remains SportsGameOdds (``ODDS_PROVIDER`` is
deliberately left unset here, defaulting to ``"sportsgameodds"`` — see
``player_prop_scanner.py``/``daily_pipeline.py``'s ``getattr(...,
"sportsgameodds")`` fallback). ``fetch_game_odds_via_odds_api()`` below is
NOT a provider switch — it's an explicit fallback the callers reach for
only when a SportsGameOdds call fails with a real quota/rate-limit error
(HTTP 429), added 2026-08-22 after that account's free-tier monthly
object quota was exhausted mid-session (verified live via the real
``/v2/account/usage`` endpoint: 2,501/2,500 entities used). Game markets
only (moneyline/run line/total) — The Odds API gives no stable player ID,
so player props would need the same identity-resolution work
``src/player_identity.py`` did for WNBA, deliberately out of scope for
this pass.
"""

from __future__ import annotations

LEAGUE_ID = "MLB"
SPORT = "baseball"
AVAILABLE = True
UNAVAILABLE_REASON = None

ODDS_API_SPORT_KEY = "baseball_mlb"


def get_market_registry():
    from src.prop_config import MARKET_REGISTRY
    return MARKET_REGISTRY


def get_settlement_module():
    """Return the module with ingest_results_for_recommendations() for MLB."""
    from src import mlb_results
    return mlb_results


def fetch_game_odds_via_odds_api(
    event_id: str | None = None, conn=None,
) -> tuple[list[dict], list[dict], list[dict], bool]:
    """Fetch live MLB game odds (moneyline/run line/total) via The Odds
    API — the fallback path, not the primary one (see module docstring).

    Same return shape as ``src.sports.wnba.fetch_and_parse()``:
    (odds_rows, audit_rows, normalized_events, from_cache), so callers can
    treat it identically to any other provider's fetch+parse — normalized
    events are shaped compatible with
    ``player_prop_scanner._build_event_map``.

    Live-verified 2026-08-22 against a real MLB slate (see
    ``src/mlb_odds_parser.py``'s docstring for the exact real event/books
    observed).
    """
    from src.odds_api_client import OddsAPIClient
    from src.mlb_odds_parser import parse_mlb_game_odds
    from src.odds_api_credits import credit_budget_check, GAME_ODDS_COST
    from datetime import datetime, timedelta, timezone

    if conn is not None:
        # Same defensive try/except as record_client_quota below — a
        # problem in the check itself (bad conn, missing table) must not
        # block a fallback that's often the only remaining way to get
        # data; only a genuine, successfully-read "budget exhausted"
        # result should stop the call.
        try:
            ok, reason = credit_budget_check(conn, GAME_ODDS_COST)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not run Odds API budget check for MLB fallback — proceeding", exc_info=True,
            )
            ok, reason = True, "budget check failed, proceeding"
        if not ok:
            raise RuntimeError(
                f"MLB SportsGameOdds fallback skipped — Odds API budget "
                f"exhausted: {reason}"
            )

    client = OddsAPIClient()
    # Same -6h/+42h near-term window the SportsGameOdds path uses, and for
    # the same reason: without it, this endpoint returns every game
    # currently listed for the sport — verified live 2026-08-22 that an
    # unbounded NFL call returns the *entire season*, months out, not "the
    # near-term slate" a daily pick-generation run needs.
    now = datetime.now(timezone.utc)
    games, from_cache = client.get_odds(
        sport_key=ODDS_API_SPORT_KEY, regions="us", markets="h2h,spreads,totals",
        commence_time_from=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        commence_time_to=(now + timedelta(hours=42)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if conn is not None:
        from src.odds_api_credits import record_client_quota
        try:
            record_client_quota(conn, client, endpoint="odds", job_type="mlb_fallback_game_odds",
                                 cache_hit=from_cache)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Could not record MLB fallback odds-API credit usage", exc_info=True,
            )
    if event_id:
        games = [g for g in games if g.get("id") == event_id]

    parsed = parse_mlb_game_odds(games)

    normalized_events = [
        {
            "id": g.get("id"),
            "eventID": g.get("id"),
            "teams": {
                "home": {"name": g.get("home_team", "")},
                "away": {"name": g.get("away_team", "")},
            },
            "status": {"startsAt": g.get("commence_time", "")},
        }
        for g in games
    ]

    return parsed.odds_rows, parsed.audit_rows, normalized_events, from_cache
