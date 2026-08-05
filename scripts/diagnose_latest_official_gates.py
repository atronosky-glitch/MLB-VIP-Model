"""Diagnose official-pick gates for today's latest production PostgreSQL scan.

This script intentionally refuses SQLite. It is read-only and uses only the
shared database abstraction selected by DATABASE_URL.

Render Shell:
    python scripts/diagnose_latest_official_gates.py
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from database.connection import get_database_url, get_connection_dialect_name
from database.db_manager import get_connection


GATE_PATTERNS = {
    "missing_pinnacle": ("missing_pinnacle", "pinnacle missing", "missing pinnacle"),
    "one_sided_pinnacle": ("one_side", "one-sided", "one sided", "opposite side"),
    "insufficient_books": ("insufficient", "minimum books", "min books", "book requirement"),
    "ev_threshold": ("ev threshold", "ev_threshold", "ev below", "minimum ev"),
    "confidence_threshold": ("confidence threshold", "confidence_threshold", "confidence below"),
    "market_quality": ("market quality", "market_quality", "quality threshold"),
    "line_fragmentation": ("line fragmentation", "line_fragmentation", "line mismatch"),
    "pinnacle_approval": ("pinnacle approval", "pinnacle_approved", "pinnacle threshold"),
    "official_gate_logic": ("official gate", "official_gate", "official status"),
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema()"
    ).fetchall()
    return {row["table_name"] for row in rows}


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_reasons(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return [f"{key}: {val}" for key, val in value.items()]
    text = str(value).strip()
    if not text:
        return []
    try:
        return _json_reasons(json.loads(text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [part.strip() for part in re.split(r"[;|\n]", text) if part.strip()]


def _gate_matches(reasons: list[str]) -> set[str]:
    normalized = " | ".join(reasons).lower()
    return {
        gate for gate, markers in GATE_PATTERNS.items()
        if any(marker in normalized for marker in markers)
    }


def _tier(row: dict, official_ids: set[str]) -> str:
    tier = str(row.get("recommendation_tier") or "").upper()
    if tier:
        return tier
    if row.get("recommendation_id") in official_ids:
        return "OFFICIAL_TRACKED"
    return "UNKNOWN"


def diagnose() -> int:
    if not get_database_url():
        print("ERROR: DATABASE_URL is not configured; refusing to query SQLite.")
        return 2

    conn = None
    try:
        conn = get_connection()
        if get_connection_dialect_name(conn) != "postgresql":
            print("ERROR: shared connection did not select PostgreSQL.")
            return 2

        tables = _tables(conn)
        required = {"scan_runs", "historical_recommendations"}
        missing = sorted(required - tables)
        if missing:
            print(f"ERROR: production schema missing required tables: {', '.join(missing)}")
            return 1

        today = datetime.now(timezone.utc).date()
        run_rows = conn.execute(
            "SELECT run_id, run_type, started_at, finished_at, data_source, metadata_json "
            "FROM scan_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC"
        ).fetchall()
        candidates = []
        for row in run_rows:
            started = _parse_time(row["started_at"])
            finished = _parse_time(row["finished_at"])
            if finished and (finished.date() == today or started and started.date() == today):
                candidates.append(dict(row))
        candidates = [r for r in candidates if (r.get("run_type") or "scan") == "scan"]
        if not candidates:
            print(f"NO COMPLETED PRODUCTION SCAN FOUND FOR UTC DATE {today.isoformat()}")
            return 0
        selected = candidates[0]
        run_id = selected["run_id"]
        print(f"selected_run_id={run_id}")
        print(f"started_at={selected.get('started_at')}")
        print(f"finished_at={selected.get('finished_at')}")
        print(f"data_source={selected.get('data_source') or 'UNKNOWN'}")

        rec_columns = _columns(conn, "historical_recommendations")
        if "scan_run_id" not in rec_columns:
            print("ERROR: historical_recommendations.scan_run_id is unavailable")
            return 1
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM historical_recommendations WHERE scan_run_id = ?",
            (run_id,),
        ).fetchall()]

        official_ids: set[str] = set()
        if "official_picks" in tables:
            official_columns = _columns(conn, "official_picks")
            if "recommendation_id" in official_columns:
                official_ids = {
                    row["recommendation_id"] for row in conn.execute(
                        "SELECT recommendation_id FROM official_picks "
                        "WHERE recommendation_id IN (SELECT recommendation_id "
                        "FROM historical_recommendations WHERE scan_run_id = ?)",
                        (run_id,),
                    ).fetchall()
                }

        tiers = Counter(_tier(row, official_ids) for row in rows)
        print(f"total_recommendations={len(rows)}")
        print(f"official_recommendations={tiers['OFFICIAL_TRACKED']}")
        print(f"research_recommendations={tiers['RESEARCH_ONLY']}")
        print(f"discovery_recommendations={tiers['DISCOVERY_TRACKED']}")
        print(f"unknown_tier_recommendations={tiers['UNKNOWN']}")

        reason_columns = [
            column for column in ("qualification_reasons", "disqualification_reasons")
            if column in rec_columns
        ]
        evidence_available = bool(reason_columns)
        print(f"gate_reason_evidence_available={evidence_available}")
        if not evidence_available:
            print("gate_evidence_unavailable=qualification/disqualification reason columns are not stored")

        failure_counts = Counter()
        pinnacle_only = 0
        market_counts: dict[str, Counter] = defaultdict(Counter)
        for row in rows:
            tier = _tier(row, official_ids)
            reasons = []
            for column in reason_columns:
                reasons.extend(_json_reasons(row.get(column)))
            gates = _gate_matches(reasons)
            if not gates and tier in ("RESEARCH_ONLY", "UNKNOWN") and evidence_available:
                failure_counts["other_disqualification_reasons"] += 1
            for gate in gates:
                failure_counts[gate] += 1
            pinnacle_gates = {"missing_pinnacle", "one_sided_pinnacle", "pinnacle_approval"}
            if tier in ("RESEARCH_ONLY", "UNKNOWN") and gates and gates <= pinnacle_gates:
                pinnacle_only += 1
            market = row.get("market_type") or "UNKNOWN"
            market_counts[market]["total"] += 1
            market_counts[market][tier.lower()] += 1
            for gate in gates:
                market_counts[market][gate] += 1

        for gate in list(GATE_PATTERNS) + ["other_disqualification_reasons"]:
            print(f"gate_{gate}={failure_counts[gate] if evidence_available else 'UNAVAILABLE'}")
        print(f"blocked_only_by_pinnacle={pinnacle_only if evidence_available else 'UNAVAILABLE'}")

        print("market_type_breakdown=")
        for market, counts in sorted(market_counts.items()):
            print(f"  {market}: {dict(counts)}")

        if "pinnacle_approved" not in rec_columns:
            print("pinnacle_approval_column=UNAVAILABLE")
        if "qualification_reasons" not in rec_columns and "disqualification_reasons" not in rec_columns:
            print("official_gate_logic=UNAVAILABLE")
        return 0
    except Exception as exc:
        print(f"ERROR: production diagnostic failed: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(diagnose())
