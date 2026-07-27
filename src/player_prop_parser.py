"""Parse player-prop Over/Under and Yes/No markets from the SportsGameOdds v2 API.

Market patterns (from registry)::

  O/U odd_id: {stat_prefix}-{PLAYER_ID}-game-ou-{side}
  YN odd_id:  {stat_prefix}-{PLAYER_ID}-game-yn-{side}

Player identity comes from the ``playerID`` field in odd_data (not
statEntityID, which is absent for player props).

O/U alternate lines are kept isolated from main lines via the
``market_group_key`` field.  YN markets have no alternate lines.

The parser uses the market registry from ``prop_config`` to determine
which oddID patterns to accept and what market_type string to assign.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .validation_constants import (
    STATUS_VALID,
    STATUS_NONE,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_HIGH,
    CONFIDENCE_NONE,
    REASON_OK,
    REASON_ID_UNKNOWN,
)

logger = logging.getLogger(__name__)


# ── Backward-compatible constants ──────────────────────────────────
# These remain importable for existing tests and external code.

STAT_ID = "pitching_strikeouts"
PERIOD = "game"
BET_TYPE_OU = "ou"
BET_TYPE_YN = "yn"

SIDE_OVER = "OVER"
SIDE_UNDER = "UNDER"
SIDE_YES = "YES"
SIDE_NO = "NO"

_SIDE_MAP = {
    "over": SIDE_OVER,
    "under": SIDE_UNDER,
    "yes": SIDE_YES,
    "no": SIDE_NO,
}


# ── Validation outcome reasons ────────────────────────────────────

REASON_MISSING_EVENT_ID = "Missing event ID"
REASON_MISSING_PLAYER_ID = "Missing player ID"
REASON_MISSING_PLAYER_NAME = "Missing player name"
REASON_INVALID_SIDE = "Invalid side for over/under market"
REASON_INVALID_YN_SIDE = "Invalid side for yes/no market"
REASON_MISSING_LINE = "Missing or non-numeric line"
REASON_INVALID_ODDS = "Invalid or missing American odds"
REASON_MISSING_SPORTSBOOK = "Missing sportsbook name"
REASON_UNAVAILABLE = "Price not available"
REASON_NOT_PITCHING_STRIKEOUTS = "Not a pitcher strikeout over/under market"
REASON_INVALID_GROUP_KEY = "Could not build valid market group key"
REASON_EMPTY_YN_NO_SIDE = "Yes/No No-side has empty byBookmaker (expected)"


# ── Structured result ─────────────────────────────────────────────

@dataclass
class ParsedPlayerPropResult:
    """Container separating approved odds rows from excluded audit rows."""
    odds_rows: list[dict] = field(default_factory=list)
    audit_rows: list[dict] = field(default_factory=list)


# ==================================================================
# Public entry point
# ==================================================================

def parse_player_props(event: dict) -> ParsedPlayerPropResult:
    """Flatten one event's player-prop O/U and YN markets.

    Uses the market registry from ``prop_config`` to detect which oddIDs
    belong to supported markets.

    Returns a ``ParsedPlayerPropResult`` with separate ``odds_rows``
    (approved) and ``audit_rows`` (all attempted rows with reasons).
    """
    from . import prop_config as cfg

    event_id = event.get("eventID") or event.get("id")
    if not event_id:
        return ParsedPlayerPropResult()

    teams = _resolve_teams(event)
    odds_map = event.get("odds", {}) or {}
    if not odds_map:
        return ParsedPlayerPropResult()

    audit_rows: list[dict] = []
    odds_rows: list[dict] = []

    for odd_id, odd_data in odds_map.items():
        if not isinstance(odd_data, dict):
            continue

        # ── Try O/U markets from registry ──
        ou_match = cfg.match_ou_market(odd_id)
        if ou_match is not None:
            _process_ou_market(
                event_id=event_id,
                odd_id=odd_id,
                odd_data=odd_data,
                teams=teams,
                market_type=ou_match.market_type_ou,
                odds_rows=odds_rows,
                audit_rows=audit_rows,
            )
            continue

        # ── Try YN markets from registry ──
        yn_match = cfg.match_yn_market(odd_id)
        if yn_match is not None:
            _process_yn_market(
                event_id=event_id,
                odd_id=odd_id,
                odd_data=odd_data,
                teams=teams,
                market_type=yn_match.market_type_yn,
                odds_rows=odds_rows,
                audit_rows=audit_rows,
            )
            continue

    return ParsedPlayerPropResult(odds_rows=odds_rows, audit_rows=audit_rows)


# ==================================================================
# Entry processor
# ==================================================================

def _process_entry(
    event_id: str,
    odd_id: str,
    odd_data: dict,
    book_name: str,
    book_data: dict,
    player_id: str,
    player_name: str,
    side: str | None,
    side_raw: str | None,
    teams: dict,
    is_alt_line: int,
    market_type: str,
    odds_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    """Validate and append one price entry (main or alt line)."""
    captured_at = datetime.now(timezone.utc).isoformat()

    # Extract lastUpdatedAt from the API response per book entry
    last_updated_raw = book_data.get("lastUpdatedAt")
    observation_time = ""
    if last_updated_raw:
        try:
            observation_time = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00")).isoformat()
        except (ValueError, TypeError):
            observation_time = ""

    # -- Pre-validation --
    issues: list[str] = []

    if not event_id:
        issues.append(REASON_MISSING_EVENT_ID)
    if not player_id:
        issues.append(REASON_MISSING_PLAYER_ID)
    if not player_name:
        issues.append(REASON_MISSING_PLAYER_NAME)
    if side is None:
        issues.append(REASON_INVALID_SIDE)
    if not book_name:
        issues.append(REASON_MISSING_SPORTSBOOK)

    line = _resolve_line(book_data)
    if line is None:
        issues.append(REASON_MISSING_LINE)

    price_raw = book_data.get("odds")
    if price_raw is None:
        issues.append(REASON_INVALID_ODDS)
    price = _parse_price(price_raw)
    if price is None:
        issues.append(REASON_INVALID_ODDS)

    available = book_data.get("available", True)
    if not available:
        issues.append(REASON_UNAVAILABLE)

    # Build market group key
    group_key = _build_group_key(event_id, player_id, line, is_alt_line, side, market_type) if line is not None else None
    if not group_key:
        issues.append(REASON_INVALID_GROUP_KEY)

    decimal_odds = None
    if price is not None:
        try:
            decimal_odds = round(1.0 + price / 100.0 if price > 0 else 1.0 + 100.0 / abs(price), 4)
        except (ZeroDivisionError, ValueError):
            issues.append(REASON_INVALID_ODDS)
            decimal_odds = None

    # Determine validation status
    if issues:
        status = STATUS_NONE
        confidence = CONFIDENCE_NONE
        method = "playerID mapping failed validation"
        reason = "; ".join(issues)
    else:
        status = STATUS_VALID
        confidence = CONFIDENCE_HIGH
        method = "playerID direct mapping"
        reason = REASON_OK

    row = {
        "event_id": event_id,
        "odd_id": odd_id,
        "sportsbook": book_name,
        "player_id": player_id,
        "player_name": player_name,
        "team_id": teams.get("team_id", ""),
        "team_name": teams.get("team_name", ""),
        "market_type": market_type,
        "market_group_key": group_key or "",
        "side": side or side_raw or "",
        "line": line,
        "price": price,
        "decimal_odds": decimal_odds,
        "is_alt_line": is_alt_line,
        "available": 1 if available else 0,
        "validation_status": status,
        "mapping_confidence": confidence,
        "mapping_method": method,
        "validation_reason": reason,
        "captured_at": captured_at,
        "observation_time": observation_time,
    }

    audit_row = dict(row)
    audit_row["excluded"] = 1 if issues else 0
    audit_row["exclusion_reasons"] = "; ".join(issues) if issues else ""

    audit_rows.append(audit_row)

    if not issues:
        odds_rows.append(row)


# ==================================================================
# Market-type processors
# ==================================================================

def _process_ou_market(
    event_id: str,
    odd_id: str,
    odd_data: dict,
    teams: dict,
    market_type: str,
    odds_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    """Process one O/U odd_id (both main and alt lines)."""
    player_id = odd_data.get("playerID", "") or ""
    player_names = (odd_data.get("playerNames", {}) or {})
    player_name = (player_names.get("full", "") or player_names.get("short", "")
                   or _extract_player_name_from_market(odd_data) or "")

    side_raw = _extract_side(odd_id)
    side = _SIDE_MAP.get(side_raw, None)

    by_book = odd_data.get("byBookmaker", {}) or {}

    # Main lines
    for book_name, book_data in by_book.items():
        if not isinstance(book_data, dict):
            continue
        _process_entry(
            event_id=event_id,
            odd_id=odd_id,
            odd_data=odd_data,
            book_name=book_name,
            book_data=book_data,
            player_id=player_id,
            player_name=player_name,
            side=side,
            side_raw=side_raw,
            teams=teams,
            is_alt_line=0,
            market_type=market_type,
            odds_rows=odds_rows,
            audit_rows=audit_rows,
        )

    # Alternate lines
    for book_name, book_data in by_book.items():
        if not isinstance(book_data, dict):
            continue
        alt_lines = book_data.get("altLines")
        if not alt_lines or not isinstance(alt_lines, list):
            continue
        for alt in alt_lines:
            if not isinstance(alt, dict):
                continue
            _process_entry(
                event_id=event_id,
                odd_id=odd_id,
                odd_data=odd_data,
                book_name=book_name,
                book_data=alt,
                player_id=player_id,
                player_name=player_name,
                side=side,
                side_raw=side_raw,
                teams=teams,
                is_alt_line=1,
                market_type=market_type,
                odds_rows=odds_rows,
                audit_rows=audit_rows,
            )


def _process_yn_market(
    event_id: str,
    odd_id: str,
    odd_data: dict,
    teams: dict,
    market_type: str,
    odds_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    """Process one YN odd_id (no alt lines, line is always None)."""
    player_id = odd_data.get("playerID", "") or ""
    player_names = (odd_data.get("playerNames", {}) or {})
    player_name = (player_names.get("full", "") or player_names.get("short", "")
                   or _extract_player_name_from_market(odd_data) or "")

    side_raw = _extract_side(odd_id)
    side = _SIDE_MAP.get(side_raw, None)

    by_book = odd_data.get("byBookmaker", {}) or {}

    # No-side with empty byBookmaker is expected — audit only, no odds rows
    if side == SIDE_NO and not by_book:
        audit_rows.append({
            "event_id": event_id,
            "odd_id": odd_id,
            "sportsbook": "",
            "player_id": player_id,
            "player_name": player_name,
            "team_id": teams.get("team_id", ""),
            "team_name": teams.get("team_name", ""),
            "market_type": market_type,
            "market_group_key": _build_yn_group_key(event_id, player_id, market_type),
            "side": SIDE_NO,
            "line": None,
            "price": None,
            "decimal_odds": None,
            "is_alt_line": 0,
            "available": 0,
            "validation_status": STATUS_NONE,
            "mapping_confidence": CONFIDENCE_NONE,
            "mapping_method": "YN No-side empty (expected)",
            "validation_reason": REASON_EMPTY_YN_NO_SIDE,
            "captured_at": "",
            "observation_time": "",
            "excluded": 1,
            "exclusion_reasons": REASON_EMPTY_YN_NO_SIDE,
        })
        return

    for book_name, book_data in by_book.items():
        if not isinstance(book_data, dict):
            continue
        _process_yn_entry(
            event_id=event_id,
            odd_id=odd_id,
            odd_data=odd_data,
            book_name=book_name,
            book_data=book_data,
            player_id=player_id,
            player_name=player_name,
            side=side,
            side_raw=side_raw,
            teams=teams,
            market_type=market_type,
            odds_rows=odds_rows,
            audit_rows=audit_rows,
        )


def _process_yn_entry(
    event_id: str,
    odd_id: str,
    odd_data: dict,
    book_name: str,
    book_data: dict,
    player_id: str,
    player_name: str,
    side: str | None,
    side_raw: str | None,
    teams: dict,
    market_type: str,
    odds_rows: list[dict],
    audit_rows: list[dict],
) -> None:
    """Validate and append one YN price entry."""
    captured_at = datetime.now(timezone.utc).isoformat()

    last_updated_raw = book_data.get("lastUpdatedAt")
    observation_time = ""
    if last_updated_raw:
        try:
            observation_time = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00")).isoformat()
        except (ValueError, TypeError):
            observation_time = ""

    issues: list[str] = []

    if not event_id:
        issues.append(REASON_MISSING_EVENT_ID)
    if not player_id:
        issues.append(REASON_MISSING_PLAYER_ID)
    if not player_name:
        issues.append(REASON_MISSING_PLAYER_NAME)
    if side not in (SIDE_YES, SIDE_NO):
        issues.append(REASON_INVALID_YN_SIDE)
    if not book_name:
        issues.append(REASON_MISSING_SPORTSBOOK)

    price_raw = book_data.get("odds")
    if price_raw is None:
        issues.append(REASON_INVALID_ODDS)
    price = _parse_price(price_raw)
    if price is None:
        issues.append(REASON_INVALID_ODDS)

    available = book_data.get("available", True)
    if not available:
        issues.append(REASON_UNAVAILABLE)

    group_key = _build_yn_group_key(event_id, player_id, market_type)

    decimal_odds = None
    if price is not None:
        try:
            decimal_odds = round(1.0 + price / 100.0 if price > 0 else 1.0 + 100.0 / abs(price), 4)
        except (ZeroDivisionError, ValueError):
            issues.append(REASON_INVALID_ODDS)
            decimal_odds = None

    if issues:
        status = STATUS_NONE
        confidence = CONFIDENCE_NONE
        method = "playerID mapping failed validation"
        reason = "; ".join(issues)
    else:
        status = STATUS_VALID
        confidence = CONFIDENCE_HIGH
        method = "playerID direct mapping"
        reason = REASON_OK

    row = {
        "event_id": event_id,
        "odd_id": odd_id,
        "sportsbook": book_name,
        "player_id": player_id,
        "player_name": player_name,
        "team_id": teams.get("team_id", ""),
        "team_name": teams.get("team_name", ""),
        "market_type": market_type,
        "market_group_key": group_key,
        "side": side or side_raw or "",
        "line": None,
        "price": price,
        "decimal_odds": decimal_odds,
        "is_alt_line": 0,
        "available": 1 if available else 0,
        "validation_status": status,
        "mapping_confidence": confidence,
        "mapping_method": method,
        "validation_reason": reason,
        "captured_at": captured_at,
        "observation_time": observation_time,
    }

    audit_row = dict(row)
    audit_row["excluded"] = 1 if issues else 0
    audit_row["exclusion_reasons"] = "; ".join(issues) if issues else ""

    audit_rows.append(audit_row)

    if not issues:
        odds_rows.append(row)


# ==================================================================
# Helpers
# ==================================================================

def _is_pitching_k_ou(odd_id: str) -> bool:
    """Check if odd_id is a pitcher strikeout over/under market."""
    parts = odd_id.rsplit("-", 4)
    if len(parts) < 5:
        return False
    stat, entity, period, bet_type, side = parts
    stat_full = "-".join(odd_id.rsplit("-", 4)[:-4]) if len(parts) > 4 else parts[0]
    if len(parts) > 5:
        stat_full = "-".join(parts[:-4])
    else:
        stat_full = parts[0]
    if stat_full != STAT_ID:
        return False
    if period != PERIOD:
        return False
    if bet_type != BET_TYPE_OU:
        return False
    if side not in ("over", "under"):
        return False
    return True


def _is_pitching_k_yn(odd_id: str) -> bool:
    """Check if odd_id is a pitcher strikeout yes/no market."""
    parts = odd_id.rsplit("-", 4)
    if len(parts) < 5:
        return False
    stat_full = "-".join(parts[:-4]) if len(parts) > 4 else parts[0]
    if stat_full != STAT_ID:
        return False
    period = parts[-3]
    bet_type = parts[-2]
    side = parts[-1]
    if period != PERIOD:
        return False
    if bet_type != BET_TYPE_YN:
        return False
    if side not in ("yes", "no"):
        return False
    return True


def _extract_side(odd_id: str) -> str | None:
    """Extract the side from a pitcher strikeout odd_id."""
    parts = odd_id.rsplit("-", 1)
    if len(parts) < 2:
        return None
    return parts[-1]


def _resolve_line(book_data: dict) -> float | None:
    """Extract the over/under line."""
    val = book_data.get("overUnder")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_price(price_raw) -> int | None:
    """Parse American odds to int."""
    try:
        return int(price_raw)
    except (ValueError, TypeError, OverflowError):
        return None


def _resolve_teams(event: dict) -> dict:
    """Resolve team info from the event."""
    teams = event.get("teams", {}) or {}
    # We don't know which team the pitcher is on from the API response
    # structure alone for player props. Return empty placeholders.
    # The playerID is the stable identifier; team info can be enriched
    # later from a roster lookup.
    return {"team_id": "", "team_name": ""}


def _build_group_key(
    event_id: str,
    player_id: str,
    line: float | None,
    is_alt_line: int,
    side: str | None,
    market_type: str = "pitching_strikeouts_ou",
) -> str:
    """Build a stable group key for pairing over/under at the same line.

    The key strips the side so that Over and Under at the same
    exact line share the same group key.
    """
    if not event_id or not player_id or line is None:
        return ""
    alt_tag = "_alt" if is_alt_line else ""
    return f"{event_id}|{player_id}|{market_type}|game|{line}{alt_tag}"


def _build_yn_group_key(event_id: str, player_id: str, market_type: str = "pitching_strikeouts_yn") -> str:
    """Build a stable group key for a Yes/No market.

    YN markets have no line, so the key is just event+player+type.
    """
    if not event_id or not player_id:
        return ""
    return f"{event_id}|{player_id}|{market_type}|game"


def _extract_player_name_from_market(odd_data: dict) -> str:
    """Extract player name from the marketName field.

    marketName is like "Jack Flaherty Strikeouts Over/Under".
    The player name is everything before "Strikeouts" (or similar suffix).
    """
    market_name = (odd_data.get("marketName", "") or "").strip()
    if not market_name:
        return ""
    # Remove the suffix like " Strikeouts Over/Under"
    for suffix in [
        # Composite batter markets (longest first)
        " Hits + Runs + RBIs Over/Under", " Hits + Runs + RBIs O/U",
        " Hits + Runs + RBIs Yes/No",
        " Runs + RBIs Over/Under", " Runs + RBIs O/U",
        " Runs + RBIs Yes/No",
        # Pitcher-specific
        " Outs Recorded Over/Under", " Outs Recorded O/U",
        " Strikeouts Over/Under", " Strikeouts O/U",
        " Hits Allowed Over/Under", " Hits Allowed O/U",
        " Earned Runs Over/Under", " Earned Runs O/U",
        " Walks Over/Under", " Walks O/U",
        # Batter O/U
        " Total Bases Over/Under", " Total Bases O/U",
        " Home Runs Over/Under", " Home Runs O/U",
        " Runs Batted In Over/Under", " Runs Batted In O/U",
        " Doubles Over/Under", " Doubles O/U",
        " Triples Over/Under", " Triples O/U",
        " Stolen Bases Over/Under", " Stolen Bases O/U",
        " Hits Over/Under", " Hits O/U",
        " Singles Over/Under", " Singles O/U",
        # YN markets
        " Any Strikeouts Yes/No",
        " Any Hits Yes/No",
        " Any Home Runs Yes/No",
        " Any Runs Batted In Yes/No",
        " Any Doubles Yes/No",
        " Any Triples Yes/No",
        " Any Walks Yes/No",
        " Any Stolen Bases Yes/No",
        " Any Singles Yes/No",
        " Any Total Bases Yes/No",
        " To Record First Home Run Yes/No",
        # Fallback suffixes (shorter)
        " Strikeouts", " Strikeout",
        " Outs Recorded", " Outs",
        " Hits Allowed", " Hits",
        " Earned Runs", " Earned Run",
        " Walks", " Walk",
        " Total Bases", " Total Base",
        " Home Runs", " Home Run",
        " Runs Batted In", " Runs Batted",
        " Doubles", " Double",
        " Triples", " Triple",
        " Stolen Bases", " Stolen Base",
        " Singles", " Single",
    ]:
        idx = market_name.find(suffix)
        if idx > 0:
            return market_name[:idx].strip()
    # Fallback: remove last 3 words
    words = market_name.split()
    if len(words) >= 4:
        # "Over/Under" at the end
        if words[-1] in ("Over/Under", "O/U"):
            return " ".join(words[:-3])
    return market_name


def parse_odd_id_components(odd_id: str) -> dict:
    """Decompose a player-prop oddID into semantic components."""
    parts = odd_id.rsplit("-", 4) if odd_id else []
    if len(parts) < 5:
        return {"stat_id": None, "player_id": None, "period_id": None,
                "bet_type": None, "side": None}

    side = parts[-1]
    bet_type = parts[-2]
    period = parts[-3]
    player = parts[-4]
    stat = "-".join(parts[:-4]) if len(parts) > 4 else parts[0]

    return {
        "stat_id": stat or None,
        "player_id": player or None,
        "period_id": period or None,
        "bet_type": bet_type or None,
        "side": side or None,
    }
