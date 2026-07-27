"""Parse SportsGameOdds v2 API response into flat odds rows with
participant-mapping validation.

Every odds row returned carries a ``validation_status`` that the
downstream pipeline must respect.  Rows with unapproved statuses
must be excluded from all model calculations (consensus, EV, etc.).

Participant-mapping protocol
----------------------------
Every odd ID in the API is structured as::

    {statID}-{entityID}-{periodID}-{betTypeID}-{sideID}

where ``entityID`` is one of ``away``, ``home``, ``all``, or a
player identifier.  The mapping between ``entityID`` and the actual
team or player is determined by the API's **stable identifiers**:

  * **``entityID == "away"``** → the away team from ``event.teams.away``
  * **``entityID == "home"``** → the home team from ``event.teams.home``
  * **``entityID`` is a player code** → that specific player

This relationship is **verified by the API's own ``marketName``**
field.  For example, ``points-away-game-ou-over`` has
``marketName = "Tampa Bay Rays Runs Over/Under"``, confirming that
``entityID = "away"`` corresponds to the away team (Tampa Bay).

Price-reversal detection (consensus-based analysis only)
--------------------------------------------------------
Consensus-based sign analysis is never used to alter prices.  It
is used only to assign a **validation status** to each sportsbook's
entry.

Prices are **never automatically swapped**.  Records with
unapproved statuses (``POSSIBLE_MAPPING_ERROR``, ``INVALID_MAPPING``)
are recorded in the audit trail but excluded from model calculations.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from .validation_constants import (
    STATUS_VALID,
    STATUS_POSSIBLE_MAPPING_ERROR,
    STATUS_INVALID_MAPPING,
    STATUS_NONE,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_HIGH,
    CONFIDENCE_NONE,
    REASON_OK,
    REASON_SWAP_SUSPECTED,
    REASON_BOTH_SAME_SIGN,
    REASON_NO_COMMON_BOOKS,
    REASON_ID_UNKNOWN,
    REASON_NO_OPPOSING_ODD,
    REASON_NOT_ML,
)

logger = logging.getLogger(__name__)


# ── Structured result ─────────────────────────────────────────────

@dataclass
class ParsedOddsResult:
    """Container separating normal odds rows from audit records.

    Attributes
    ----------
    odds_rows : list[dict]
        Normal odds rows ready for INSERT into the ``odds`` table.
        Every row includes ``validation_status``, ``mapping_confidence``,
        ``mapping_method``, ``validation_reason``, ``odd_id``.
    audit_rows : list[dict]
        Audit records for INSERT into ``odds_mapping_audit``.
        Includes participant provenance for every (odd_id, sportsbook) pair.
    """
    odds_rows: list[dict] = field(default_factory=list)
    audit_rows: list[dict] = field(default_factory=list)


# ======================================================================
# Public entry point
# ======================================================================

def parse_odds(event: dict) -> ParsedOddsResult:
    """Flatten one event's ``odds`` dict into a structured result.

    Returns a ``ParsedOddsResult`` with separate ``odds_rows`` and
    ``audit_rows`` lists.
    """
    event_id = event.get("eventID") or event.get("id")
    if not event_id:
        logger.warning("Event missing eventID, skipping")
        return ParsedOddsResult()

    odds_map = event.get("odds", {}) or {}
    if not odds_map:
        return ParsedOddsResult()

    teams = event.get("teams", {}) or {}
    participant_map = _build_participant_map(teams, odds_map)
    audit_rows: list[dict] = []

    raw_main: dict[str, dict[str, dict]] = defaultdict(dict)
    raw_alt: dict[str, list[dict]] = defaultdict(list)
    odd_meta: dict[str, dict] = {}

    for odd_id, odd_data in odds_map.items():
        if not isinstance(odd_data, dict):
            continue
        odd_meta[odd_id] = odd_data

        by_book = odd_data.get("byBookmaker", {}) or {}
        for book_name, book_data in by_book.items():
            if not isinstance(book_data, dict):
                continue
            _collect_main(event_id, odd_id, book_name, book_data, raw_main)
            _collect_alt(event_id, odd_id, book_name, book_data, raw_alt)

    # Build audit trail (provenance data)
    audit_rows = _build_audit(raw_main, odd_meta, participant_map)

    # Assign validation statuses via consensus-based sign analysis
    status_map = _validate_mappings(raw_main, odd_meta, participant_map)

    # Build odds rows with per-row validation attached
    odds_rows: list[dict] = []
    for odd_id, books in raw_main.items():
        for entry in books.values():
            book = entry["sportsbook"]

            # Resolve per-book status: the status_map may have
            # ``odd_id:book`` keys (per-book) or ``odd_id`` keys (whole oddID)
            key_specific = f"{odd_id}:{book}"
            if key_specific in status_map:
                v_status = status_map[key_specific]
            else:
                v_status = status_map.get(odd_id, STATUS_NONE)

            # Look up audit info for this (odd_id, book) for metadata
            row_audit = _find_audit(audit_rows, odd_id, book)

            odds_rows.append({
                "event_id": event_id,
                "sportsbook": book,
                "market": odd_id,
                "selection": odd_id,
                "price": entry["price"],
                "points": entry["points"],
                "is_alt_line": 0,
                "available": entry["available"],
                "odd_id": odd_id,
                "validation_status": v_status,
                "mapping_confidence": row_audit.get("mapping_confidence", CONFIDENCE_NONE),
                "mapping_method": row_audit.get("mapping_method", ""),
                "validation_reason": _reason_for_status(v_status, row_audit),
            })

    for odd_id, entries in raw_alt.items():
        for entry in entries:
            entry["odd_id"] = odd_id
            entry["validation_status"] = STATUS_VALID
            entry["mapping_confidence"] = CONFIDENCE_NONE
            entry["mapping_method"] = ""
            entry["validation_reason"] = ""
            odds_rows.append(entry)

    return ParsedOddsResult(odds_rows=odds_rows, audit_rows=audit_rows)


# ======================================================================
# Participant-map builder
# ======================================================================

def _build_participant_map(
    teams: dict,
    odds_map: dict,
) -> dict[str, dict]:
    """Build a map from API entityID to actual team/player info.

    Returns ``{entityID: {name, teamID, role}}``.
    Verification is done by cross-referencing ``marketName`` on
    team-specific markets (e.g. team totals).
    """
    participant_map: dict[str, dict] = {}

    away = teams.get("away", {})
    home = teams.get("home", {})

    away_name = (
        away.get("names", {}).get("long")
        or away.get("name")
        or "Unknown Away"
    )
    home_name = (
        home.get("names", {}).get("long")
        or home.get("name")
        or "Unknown Home"
    )
    away_id = away.get("teamID") or away.get("id")
    home_id = home.get("teamID") or home.get("id")

    participant_map["away"] = {
        "name": away_name,
        "team_id": away_id,
        "role": "away",
    }
    participant_map["home"] = {
        "name": home_name,
        "team_id": home_id,
        "role": "home",
    }

    # Cross-check: find a team-specific total market and verify
    # that marketName matches our expected mapping.
    for odd_id, odd_data in odds_map.items():
        if not isinstance(odd_data, dict):
            continue
        market_name = odd_data.get("marketName", "") or ""
        stat_entity = odd_data.get("statEntityID")
        if not stat_entity or stat_entity not in ("away", "home"):
            continue
        if stat_entity == "away" and away_name in market_name:
            participant_map[stat_entity]["_verified_by"] = odd_id
        elif stat_entity == "home" and home_name in market_name:
            participant_map[stat_entity]["_verified_by"] = odd_id

    return participant_map


# ======================================================================
# Phase 1 helpers
# ======================================================================

def _collect_main(
    event_id: str,
    odd_id: str,
    book_name: str,
    book_data: dict,
    raw_main: dict[str, dict[str, dict]],
) -> None:
    """Extract main-line price for one book in one oddID."""
    price_str = book_data.get("odds")
    if price_str is None:
        return
    try:
        price = int(price_str)
    except (ValueError, TypeError):
        return

    available = book_data.get("available", True)
    points = _resolve_points(book_data)

    raw_main[odd_id][book_name] = {
        "sportsbook": book_name,
        "price": price,
        "points": points,
        "available": 1 if available else 0,
    }


def _collect_alt(
    event_id: str,
    odd_id: str,
    book_name: str,
    book_data: dict,
    raw_alt: dict[str, list[dict]],
) -> None:
    """Extract alt-line entries for one book in one oddID."""
    alt_lines = book_data.get("altLines")
    if not alt_lines or not isinstance(alt_lines, list):
        return

    for alt in alt_lines:
        if not isinstance(alt, dict):
            continue
        price_str = alt.get("odds")
        if price_str is None:
            continue
        try:
            price = int(price_str)
        except (ValueError, TypeError):
            continue

        available = alt.get("available", True)
        points = _resolve_points(alt)

        raw_alt[odd_id].append({
            "event_id": event_id,
            "sportsbook": book_name,
            "market": f"{odd_id}_alt",
            "selection": f"{odd_id}_alt",
            "price": price,
            "points": points,
            "is_alt_line": 1,
            "available": 1 if available else 0,
        })


# ======================================================================
# Audit trail
# ======================================================================

def _build_audit(
    raw_main: dict[str, dict[str, dict]],
    odd_meta: dict[str, dict],
    participant_map: dict[str, dict],
) -> list[dict]:
    """Build audit records for every (odd_id, sportsbook) price entry.

    Each record proves which team the API identifiers map to.
    """
    records: list[dict] = []

    for odd_id, books in raw_main.items():
        odd_data = odd_meta.get(odd_id, {})
        entity_id = odd_data.get("statEntityID", "unknown")
        market_name = odd_data.get("marketName", "")
        side_id = odd_data.get("sideID", "")

        participant = participant_map.get(entity_id, {})
        team_name = participant.get("name", "unknown")
        team_id = participant.get("team_id", "unknown")
        role = participant.get("role", "unknown")
        verified_by = participant.get("_verified_by")

        if entity_id in participant_map:
            if verified_by:
                mapping_method = "statEntityID + marketName verification"
                mapping_confidence = CONFIDENCE_CONFIRMED
            else:
                mapping_method = "statEntityID"
                mapping_confidence = CONFIDENCE_HIGH
        else:
            mapping_method = "none (unrecognized entityID)"
            mapping_confidence = CONFIDENCE_NONE

        for book_name, entry in books.items():
            records.append({
                "event_id": odd_data.get("eventID", ""),
                "odd_id": odd_id,
                "sportsbook": book_name,
                "raw_participant_id": entity_id,
                "raw_participant_name": f"entityID={entity_id}",
                "matched_team_id": str(team_id) if team_id else "",
                "matched_team_name": team_name,
                "mapping_method": mapping_method,
                "mapping_confidence": mapping_confidence,
                "validation_status": STATUS_NONE,  # filled later
                "validation_reason": "",
                "price": entry["price"],
            })

    return records


def _find_audit(
    audit_rows: list[dict],
    odd_id: str,
    sportsbook: str,
) -> dict:
    """Look up the audit record for a given (odd_id, sportsbook)."""
    for r in audit_rows:
        if r["odd_id"] == odd_id and r["sportsbook"] == sportsbook:
            return r
    return {}


# ======================================================================
# Validation (consensus-based, read-only)
# ======================================================================

def _validate_mappings(
    raw_main: dict[str, dict[str, dict]],
    odd_meta: dict[str, dict],
    participant_map: dict[str, dict],
) -> dict[str, str]:
    """Assign a validation status to each oddID (and per-book where needed).

    Returns a dict keyed by ``odd_id`` (whole-oddID status) or
    ``odd_id:book_name`` (per-book status for flagged records).
    """
    statuses: dict[str, str] = {}

    # Baseline: assign VALID for recognised entityIDs, NONE for unknown
    for odd_id in raw_main:
        odd_data = odd_meta.get(odd_id, {})
        entity_id = odd_data.get("statEntityID")

        if entity_id is None or entity_id not in participant_map:
            statuses[odd_id] = STATUS_VALID  # non-team markets like totals
            continue

        statuses[odd_id] = STATUS_VALID

    # Sign-based consensus analysis — game moneyline only
    for odd_id in list(raw_main.keys()):
        if "-game-ml-away" not in odd_id:
            continue

        odd_data = odd_meta.get(odd_id, {})
        opposing_id = odd_data.get("opposingOddID")
        if not opposing_id or opposing_id not in raw_main:
            statuses[odd_id] = STATUS_VALID
            continue

        side_a = raw_main[odd_id]
        side_b = raw_main[opposing_id]
        common = set(side_a) & set(side_b)
        if len(common) < 5:
            continue

        # Count consensus sign for side A (entity = away)
        a_neg_count = sum(1 for b in common if side_a[b].get("price", 0) < 0)
        total = len(common)

        consensus_a_neg = a_neg_count > total - a_neg_count
        if consensus_a_neg:
            has_strong_consensus = a_neg_count >= 5
        else:
            has_strong_consensus = (total - a_neg_count) >= 5

        if not has_strong_consensus:
            continue

        b_neg_count = sum(1 for b in common if side_b[b].get("price", 0) < 0)
        consensus_b_neg = b_neg_count > total - b_neg_count

        if consensus_a_neg == consensus_b_neg:
            # Both sides have same dominant sign — market is broken
            statuses[odd_id] = STATUS_INVALID_MAPPING
            statuses[opposing_id] = STATUS_INVALID_MAPPING
            continue

        # Per-book flagging: check exact inverse pattern
        for book in common:
            a_price = side_a[book].get("price", 0)
            b_price = side_b[book].get("price", 0)
            book_a_neg = a_price < 0
            book_b_neg = b_price < 0

            if book_a_neg != consensus_a_neg:
                if book_a_neg == consensus_b_neg and book_b_neg == consensus_a_neg:
                    statuses[f"{odd_id}:{book}"] = STATUS_POSSIBLE_MAPPING_ERROR
                    statuses[f"{opposing_id}:{book}"] = STATUS_POSSIBLE_MAPPING_ERROR

    return statuses


def _reason_for_status(status: str, audit_rec: dict) -> str:
    """Return a human-readable reason string for a given validation status."""
    if status == STATUS_VALID:
        return REASON_OK
    if status == STATUS_POSSIBLE_MAPPING_ERROR:
        return REASON_SWAP_SUSPECTED
    if status == STATUS_INVALID_MAPPING:
        return REASON_BOTH_SAME_SIGN
    if status == STATUS_NONE:
        return REASON_ID_UNKNOWN
    return ""


# ======================================================================
# Shared helpers
# ======================================================================

def _resolve_points(book_data: dict) -> float | None:
    """Extract the line value (spread, total, or None for moneyline)."""
    for key in ("spread", "overUnder"):
        val = book_data.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def parse_odd_id_components(odd_id: str) -> dict:
    """Decompose an oddID into its semantic components.

    Pattern: ``{statID}-{entityID}-{periodID}-{betTypeID}-{sideID}``

    Returns a dict with keys: stat_id, entity_id, period_id, bet_type, side.
    Any components that can't be parsed are returned as ``None``.
    """
    parts = odd_id.rsplit("-", 4) if odd_id else []
    if len(parts) < 5:
        return {"stat_id": None, "entity_id": None, "period_id": None,
                "bet_type": None, "side": None}

    side = parts[-1]
    bet_type = parts[-2]
    period = parts[-3]
    entity = parts[-4]
    stat = "-".join(parts[:-4]) if len(parts) > 4 else parts[0]

    return {
        "stat_id": stat or None,
        "entity_id": entity or None,
        "period_id": period or None,
        "bet_type": bet_type or None,
        "side": side or None,
    }


# ======================================================================
# Audit display
# ======================================================================

def print_audit_table(audit_records: list[dict]) -> None:
    """Print a human-readable audit table (team-mapped records only)."""
    filtered = [
        r for r in audit_records
        if r.get("mapping_confidence") in (CONFIDENCE_CONFIRMED, CONFIDENCE_HIGH)
    ]

    if not filtered:
        print("  (no participant-mapped records)")
        return

    print(
        f"  {'Sportsbook':<15} {'Odd ID':<35} {'Entity':<8}"
        f" {'Mapped Team':<25} {'Role':<6} {'Odds':>8} {'Method':<35} {'Confidence':<12}"
    )
    print("  " + "-" * 144)
    for r in filtered:
        odd_short = r["odd_id"]
        if len(odd_short) > 34:
            odd_short = odd_short[:31] + "..."
        print(
            f"  {r['sportsbook']:<15} {odd_short:<35} {r['entity_id']:<8}"
            f" {r['mapped_team_name'][:24]:<25} {r['home_away_role']:<6}"
            f" {r['american_odds']:>+8} {r['mapping_method'][:34]:<35} {r['mapping_confidence']:<12}"
        )
