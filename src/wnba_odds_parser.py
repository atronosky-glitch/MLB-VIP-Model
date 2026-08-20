"""Parse The Odds API's WNBA response into the platform's generic odds-row schema.

The Odds API's wire format is fundamentally different from SportsGameOdds's
oddID grammar (no composed ID string to pattern-match — nested
``bookmakers[].markets[].outcomes[]`` objects instead), so this is a
separate parser rather than an extension of ``player_prop_parser.py``. It
produces rows in the exact same schema
(``event_id``/``sportsbook``/``player_id``/``player_name``/``team_id``/
``team_name``/``market_type``/``market_group_key``/``side``/``line``/
``price``/``decimal_odds``/``is_alt_line``/``available``/
``validation_status``/``mapping_confidence``/``mapping_method``/
``validation_reason``/``captured_at``/``observation_time``), so it flows
through the existing generic scanner/analysis pipeline (``player_prop_scanner.py``,
``player_prop_analysis.py``) completely unchanged.

Game markets (moneyline/spread/total via ``h2h``/``spreads``/``totals``)
and player props (points/rebounds/assists/threes and their PA/PR/RA/PRA
combos — the 8 markets verified live 2026-08-19 to be genuine two-sided
Over/Under props; see docs/MARKET_CAPABILITY.md for what else the
provider offers but isn't registered). Props route every name through
``src/player_identity.py`` before becoming a row — a player who cannot be
resolved with HIGH/MEDIUM confidence against the game's actual rosters is
excluded, never guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .player_prop_parser import _build_game_group_key, _build_group_key, _SIDE_MAP
from .player_identity import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    ESPNRosterClient,
    resolve_player_identity,
)

logger = logging.getLogger(__name__)

_TRUSTED_CONFIDENCE = {CONFIDENCE_HIGH, CONFIDENCE_MEDIUM}

# Verified live 2026-08-19 against a real WNBA event: all 8 are genuine
# two-sided (Over/Under, outcome.description = player name) props.
_PROP_MARKET_TYPE = {
    "player_points": "player_points_ou",
    "player_rebounds": "player_rebounds_ou",
    "player_assists": "player_assists_ou",
    "player_threes": "player_threes_ou",
    "player_points_assists": "player_points_assists_ou",
    "player_points_rebounds": "player_points_rebounds_ou",
    "player_rebounds_assists": "player_rebounds_assists_ou",
    "player_points_rebounds_assists": "player_points_rebounds_assists_ou",
}
PROP_MARKET_KEYS = ",".join(_PROP_MARKET_TYPE)

# statID-equivalent market_type naming, consistent with src/sports/nfl.py's
# convention (same DB market_type shape across every league/provider).
_MARKET_TYPE = {
    "h2h": "game_moneyline",
    "spreads": "game_spread_ou",
    "totals": "game_total_ou",
}
_DISPLAY_NAME = {
    "h2h": "Moneyline",
    "spreads": "Spread",
    "totals": "Game Total",
}


@dataclass
class ParsedWNBAOddsResult:
    odds_rows: list[dict] = field(default_factory=list)
    audit_rows: list[dict] = field(default_factory=list)


def parse_wnba_game_odds(games: list[dict]) -> ParsedWNBAOddsResult:
    """Flatten The Odds API's WNBA game-odds response (h2h/spreads/totals)."""
    odds_rows: list[dict] = []
    audit_rows: list[dict] = []

    for game in games or []:
        event_id = game.get("id", "")
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        if not event_id or not home_team or not away_team:
            continue

        for bookmaker in game.get("bookmakers") or []:
            book_name = bookmaker.get("key", "")
            book_last_update = bookmaker.get("last_update")

            for market in bookmaker.get("markets") or []:
                market_key = market.get("key", "")
                market_type = _MARKET_TYPE.get(market_key)
                if market_type is None:
                    continue  # not a game market this parser handles yet

                for outcome in market.get("outcomes") or []:
                    row = _build_row(
                        event_id=event_id, home_team=home_team, away_team=away_team,
                        book_name=book_name, book_last_update=book_last_update,
                        market_key=market_key, market_type=market_type, outcome=outcome,
                    )
                    audit_row = dict(row)
                    audit_row["excluded"] = 1 if row["validation_status"] != "VALID" else 0
                    audit_row["exclusion_reasons"] = row["validation_reason"] if audit_row["excluded"] else ""
                    audit_rows.append(audit_row)
                    if not audit_row["excluded"]:
                        odds_rows.append(row)

    return ParsedWNBAOddsResult(odds_rows=odds_rows, audit_rows=audit_rows)


def _build_row(
    *, event_id: str, home_team: str, away_team: str,
    book_name: str, book_last_update: str | None,
    market_key: str, market_type: str, outcome: dict,
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
    if not book_name:
        issues.append("Missing sportsbook")

    price = outcome.get("price")
    if not isinstance(price, (int, float)):
        issues.append("Missing or invalid price")
        price = None
    else:
        price = int(price)

    name = outcome.get("name", "")
    point = outcome.get("point")

    raw_line: float | None = None
    if market_key == "h2h":
        side_raw, team_name = _side_for_team_name(name, home_team, away_team)
        line = None
    elif market_key == "spreads":
        side_raw, team_name = _side_for_team_name(name, home_team, away_team)
        # point is signed (favorite negative, underdog positive) — line is
        # the abs-valued magnitude used for O/U-style pairing/EV analysis;
        # raw_line preserves the sign for settlement (grade_spread needs
        # the actual favorite/underdog direction, never guessed/reconstructed).
        raw_line = float(point) if point is not None else None
        line = abs(raw_line) if raw_line is not None else None
        if line is None:
            issues.append("Missing spread line")
    elif market_key == "totals":
        side_raw = "over" if name.lower() == "over" else "under" if name.lower() == "under" else None
        team_name = ""
        line = float(point) if point is not None else None
        raw_line = line
        if line is None:
            issues.append("Missing total line")
    else:
        side_raw, team_name, line = None, "", None

    if side_raw is None:
        issues.append("Could not resolve side")
    side = _SIDE_MAP.get(side_raw, side_raw or "")

    decimal_odds = None
    if price is not None:
        try:
            decimal_odds = round(1.0 + price / 100.0 if price > 0 else 1.0 + 100.0 / abs(price), 4)
        except (ZeroDivisionError, ValueError):
            issues.append("Invalid odds — could not compute decimal odds")

    group_key = _build_game_group_key(event_id, market_type, line, is_alt_line=0)
    if not group_key:
        issues.append("Could not build market group key")

    status = "VALID" if not issues else "NONE"

    return {
        "event_id": event_id,
        "odd_id": f"{market_key}-{event_id}",
        "sportsbook": book_name,
        "player_id": "GAME",
        "player_name": _DISPLAY_NAME.get(market_key, market_key),
        "team_id": "GAME",
        "team_name": team_name,
        "market_type": market_type,
        "market_group_key": group_key or "",
        "side": side,
        "line": line,
        "raw_line": raw_line,
        "price": price,
        "decimal_odds": decimal_odds,
        "is_alt_line": 0,
        "available": 1,
        "validation_status": status,
        "mapping_confidence": "HIGH" if status == "VALID" else "NONE",
        "mapping_method": "team name direct mapping",
        "validation_reason": "; ".join(issues) if issues else "OK",
        "captured_at": captured_at,
        "observation_time": observation_time,
    }


def _side_for_team_name(name: str, home_team: str, away_team: str) -> tuple[str | None, str]:
    """Map an outcome's team name to away/home, exact match only.

    Never guesses via substring/fuzzy matching — an unrecognized name is
    excluded (validation_status != VALID) rather than silently mapped.
    """
    if name == home_team:
        return "home", home_team
    if name == away_team:
        return "away", away_team
    return None, ""


# ==================================================================
# Player props — routed through canonical player identity resolution
# ==================================================================

def parse_wnba_player_props(
    event_odds_responses: list[dict],
    *,
    conn,
    roster_client: ESPNRosterClient | None = None,
) -> ParsedWNBAOddsResult:
    """Flatten per-event player-prop responses from
    ``OddsAPIClient.get_event_odds()`` into generic odds rows.

    *event_odds_responses* is a list of raw per-event odds payloads (each
    shaped like ``GET /v4/sports/basketball_wnba/events/{id}/odds``), one
    per event already fetched by the caller — this function does no
    network I/O of its own for odds, only for identity resolution
    (roster lookups, cached in *conn* after the first resolution).

    Every prop outcome's player name is resolved via
    ``src.player_identity.resolve_player_identity`` before it can become a
    row; LOW/UNRESOLVED confidence excludes the row (audited, not dropped
    silently) rather than guessing an identity.
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
                market_type = _PROP_MARKET_TYPE.get(market_key)
                if market_type is None:
                    continue
                book_last_update = market.get("last_update") or bookmaker.get("last_update")

                for outcome in market.get("outcomes") or []:
                    raw_name = (outcome.get("description") or "").strip()
                    cache_key = (event_id, raw_name)
                    if cache_key not in resolution_cache:
                        resolution_cache[cache_key] = _resolve_and_cache(
                            conn, raw_name, league="WNBA", home_team=home_team,
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

    return ParsedWNBAOddsResult(odds_rows=odds_rows, audit_rows=audit_rows)


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
