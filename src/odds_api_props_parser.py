"""Shared player-props row-building for The Odds API — extracted from
``src/wnba_odds_parser.py`` 2026-08-22 when MLB and NFL got their own
Odds-API-sourced player props: the identity-resolution/row-building logic
was never actually WNBA-specific, only the market-key-to-market_type
mapping was (same pattern as ``src/odds_api_game_parser.py`` for game
markets, extracted the same day for the same reason).

Every prop's raw name is routed through ``src.player_identity`` before it
can become a row — a name that can't be resolved with HIGH/MEDIUM
confidence against the game's actual two rosters is excluded, never
guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .player_prop_parser import _build_group_key, _SIDE_MAP
from .player_identity import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    ESPNRosterClient,
    resolve_player_identity,
)

logger = logging.getLogger(__name__)

_TRUSTED_CONFIDENCE = {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}


@dataclass
class ParsedPropsResult:
    odds_rows: list[dict] = field(default_factory=list)
    audit_rows: list[dict] = field(default_factory=list)


def parse_player_props(
    event_odds_responses: list[dict],
    *,
    conn,
    league: str,
    prop_market_type_map: dict[str, str],
    roster_client: ESPNRosterClient | None = None,
) -> ParsedPropsResult:
    """Flatten per-event player-prop responses from
    ``OddsAPIClient.get_event_odds()`` into generic odds rows.

    *event_odds_responses* is a list of raw per-event odds payloads (each
    shaped like ``GET /v4/sports/{sport}/events/{id}/odds``), one per
    event already fetched by the caller — this function does no network
    I/O of its own for odds, only for identity resolution (roster
    lookups, cached in *conn* after the first resolution).

    *prop_market_type_map* maps this sport's real Odds-API market keys
    (e.g. ``"batter_hits"``) to this platform's canonical ``market_type``
    string (e.g. ``"batting_hits_ou"``) — deliberately reusing each
    league's existing primary-provider market_type naming wherever that
    market already exists there, so the existing settlement contract
    (``src/mlb_results.py``/``src/nfl_results.py``/``src/wnba_results.py``)
    applies automatically with no new settlement code needed.
    """
    from database.db_manager import (
        get_cached_player_identity_mapping,
        save_player_identity_mapping,
    )

    roster_client = roster_client or ESPNRosterClient()
    odds_rows: list[dict] = []
    audit_rows: list[dict] = []
    # Resolve each unique raw name once per call, even though it may
    # appear across several bookmakers/markets in the same event.
    resolution_cache: dict[tuple[str, str], tuple[str | None, str, str, str]] = {}

    for event_odds in event_odds_responses or []:
        event_id = event_odds.get("id", "")
        home_team = event_odds.get("home_team", "")
        away_team = event_odds.get("away_team", "")
        if not event_id or not home_team or not away_team:
            continue

        for bookmaker in event_odds.get("bookmakers") or []:
            book_name = bookmaker.get("key", "")
            for market in bookmaker.get("markets") or []:
                market_key = market.get("key", "")
                market_type = prop_market_type_map.get(market_key)
                if market_type is None:
                    continue
                book_last_update = market.get("last_update") or bookmaker.get("last_update")

                for outcome in market.get("outcomes") or []:
                    raw_name = (outcome.get("description") or "").strip()
                    cache_key = (event_id, raw_name)
                    if cache_key not in resolution_cache:
                        resolution_cache[cache_key] = _resolve_and_cache(
                            conn, raw_name, league=league, home_team=home_team,
                            away_team=away_team, client=roster_client,
                            get_cached=get_cached_player_identity_mapping,
                            save=save_player_identity_mapping,
                        )
                    canonical_id, display_name, confidence, method = resolution_cache[cache_key]

                    row = _build_prop_row(
                        event_id=event_id, book_name=book_name, book_last_update=book_last_update,
                        market_type=market_type, outcome=outcome,
                        canonical_id=canonical_id, display_name=display_name or raw_name,
                        confidence=confidence, method=method,
                    )
                    audit_row = dict(row)
                    audit_row["excluded"] = 1 if row["validation_status"] != "VALID" else 0
                    audit_row["exclusion_reasons"] = row["validation_reason"] if audit_row["excluded"] else ""
                    audit_rows.append(audit_row)
                    if not audit_row["excluded"]:
                        odds_rows.append(row)

    return ParsedPropsResult(odds_rows=odds_rows, audit_rows=audit_rows)


def _resolve_and_cache(
    conn, raw_name: str, *, league: str, home_team: str, away_team: str,
    client: ESPNRosterClient, get_cached, save,
) -> tuple[str | None, str, str, str]:
    """Resolve one raw name, using the DB-cached mapping when available."""
    if not raw_name:
        return None, "", "UNRESOLVED", "empty_name"

    cached = None
    try:
        cached = get_cached(conn, league, "the_odds_api", raw_name)
    except Exception:
        logger.exception("Identity mapping cache lookup failed for %r", raw_name)

    if cached:
        return (
            cached.get("canonical_player_id"),
            raw_name,
            cached.get("mapping_confidence", "UNRESOLVED"),
            cached.get("mapping_method", "cached"),
        )

    resolution = resolve_player_identity(
        raw_name, league=league, home_team=home_team, away_team=away_team, client=client,
    )
    try:
        save(
            conn, league=league, provider="the_odds_api", provider_name_raw=raw_name,
            canonical_player_id=resolution.canonical_player_id,
            display_name=resolution.display_name, mapping_confidence=resolution.confidence,
            mapping_method=resolution.method, team_id=resolution.team_id,
            team_name=resolution.team_name,
        )
    except Exception:
        logger.exception("Identity mapping save failed for %r", raw_name)

    return resolution.canonical_player_id, resolution.display_name, resolution.confidence, resolution.method


def _build_prop_row(
    *, event_id: str, book_name: str, book_last_update: str | None,
    market_type: str, outcome: dict,
    canonical_id: str | None, display_name: str, confidence: str, method: str,
) -> dict:
    captured_at = datetime.now(timezone.utc).isoformat()
    observation_time = ""
    if book_last_update:
        try:
            observation_time = datetime.fromisoformat(
                book_last_update.replace("Z", "+00:00")
            ).isoformat()
        except (ValueError, TypeError):
            observation_time = ""

    issues: list[str] = []
    if confidence not in _TRUSTED_CONFIDENCE or not canonical_id:
        issues.append(f"Player identity not trusted (confidence={confidence}, method={method})")

    price = outcome.get("price")
    if not isinstance(price, (int, float)):
        issues.append("Missing or invalid price")
        price = None
    else:
        price = int(price)

    side_raw = "over" if (outcome.get("name") or "").lower() == "over" else \
               "under" if (outcome.get("name") or "").lower() == "under" else None
    if side_raw is None:
        issues.append("Could not resolve side")
    side = _SIDE_MAP.get(side_raw, side_raw or "")

    point = outcome.get("point")
    line = float(point) if point is not None else None
    if line is None:
        issues.append("Missing line")

    decimal_odds = None
    if price is not None:
        try:
            decimal_odds = round(1.0 + price / 100.0 if price > 0 else 1.0 + 100.0 / abs(price), 4)
        except (ZeroDivisionError, ValueError):
            issues.append("Invalid odds — could not compute decimal odds")

    player_id = canonical_id or ""
    group_key = (
        _build_group_key(event_id, player_id, line, is_alt_line=0, side=side, market_type=market_type)
        if player_id and line is not None else ""
    )
    if not group_key and not issues:
        issues.append("Could not build market group key")

    status = "VALID" if not issues else "NONE"

    return {
        "event_id": event_id,
        "odd_id": f"{market_type}-{event_id}-{display_name}",
        "sportsbook": book_name,
        "player_id": player_id,
        "player_name": display_name,
        "team_id": "",
        "team_name": "",
        "market_type": market_type,
        "market_group_key": group_key,
        "side": side,
        "line": line,
        "raw_line": line,  # no favorite/underdog sign concept for player O/U props
        "price": price,
        "decimal_odds": decimal_odds,
        "is_alt_line": 0,
        "available": 1,
        "validation_status": status,
        "mapping_confidence": confidence,
        "mapping_method": method,
        "validation_reason": "; ".join(issues) if issues else "OK",
        "captured_at": captured_at,
        "observation_time": observation_time,
    }
