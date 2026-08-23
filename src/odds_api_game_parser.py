"""Shared game-odds parser for any sport served via The Odds API (v4).

Extracted from what was originally WNBA-only parsing logic: the row-
building mechanics (team-name side resolution, spread sign handling,
decimal-odds conversion, group-key construction) have nothing WNBA-
specific about them — they operate purely on the generic
``h2h``/``spreads``/``totals`` shape The Odds API uses for every sport.
The only thing that varies per sport is which internal ``market_type``
string each market key maps to (e.g. WNBA's ``spreads`` -> ``game_spread_ou``
vs. MLB's baseball-convention ``game_runline_ou``) and the human-readable
display name — both passed in by the caller rather than hardcoded here.

Produces rows in the platform's generic odds-row schema (see
``src/wnba_odds_parser.py``'s module docstring for the full field list),
so callers flow through the existing generic scanner/analysis pipeline
unchanged regardless of sport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .player_prop_parser import _build_game_group_key, _SIDE_MAP


@dataclass
class ParsedGameOddsResult:
    odds_rows: list[dict] = field(default_factory=list)
    audit_rows: list[dict] = field(default_factory=list)


def parse_game_odds(
    games: list[dict],
    *,
    market_type_map: dict[str, str],
    display_name_map: dict[str, str],
) -> ParsedGameOddsResult:
    """Flatten The Odds API's game-odds response (h2h/spreads/totals) into
    generic odds rows, for any sport.

    *market_type_map* maps The Odds API's market key (``h2h``/``spreads``/
    ``totals``) to this platform's internal ``market_type`` string for the
    calling sport — a market key absent from the map is skipped, not
    guessed. *display_name_map* is the matching human-readable label per
    market key, used only for the row's ``player_name`` placeholder field
    (game markets have no real player).
    """
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
                market_type = market_type_map.get(market_key)
                if market_type is None:
                    continue  # not a game market this sport registers

                for outcome in market.get("outcomes") or []:
                    row = _build_row(
                        event_id=event_id, home_team=home_team, away_team=away_team,
                        book_name=book_name, book_last_update=book_last_update,
                        market_key=market_key, market_type=market_type, outcome=outcome,
                        display_name_map=display_name_map,
                    )
                    audit_row = dict(row)
                    audit_row["excluded"] = 1 if row["validation_status"] != "VALID" else 0
                    audit_row["exclusion_reasons"] = row["validation_reason"] if audit_row["excluded"] else ""
                    audit_rows.append(audit_row)
                    if not audit_row["excluded"]:
                        odds_rows.append(row)

    return ParsedGameOddsResult(odds_rows=odds_rows, audit_rows=audit_rows)


def _build_row(
    *, event_id: str, home_team: str, away_team: str,
    book_name: str, book_last_update: str | None,
    market_key: str, market_type: str, outcome: dict,
    display_name_map: dict[str, str],
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

    group_key_line = line
    if market_key == "spreads" and raw_line is not None:
        # abs(line) alone lets two books that disagree on WHICH TEAM IS
        # FAVORED collide into the same group even though they represent
        # opposite real bets (e.g. one book has away -1.5 favored, another
        # has away +1.5 as the underdog at the same magnitude) — live-caught
        # 2026-08-23 producing a nonsensical ~45% blended "EV". Canonicalize
        # to the away team's own signed line so books that agree on
        # direction still group together, but a book that disagrees gets
        # its own separate group instead of being silently merged.
        group_key_line = raw_line if side_raw == "away" else -raw_line if side_raw == "home" else line
    group_key = _build_game_group_key(event_id, market_type, group_key_line, is_alt_line=0)
    if not group_key:
        issues.append("Could not build market group key")

    status = "VALID" if not issues else "NONE"

    return {
        "event_id": event_id,
        "odd_id": f"{market_key}-{event_id}",
        "sportsbook": book_name,
        "player_id": "GAME",
        "player_name": display_name_map.get(market_key, market_key),
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
