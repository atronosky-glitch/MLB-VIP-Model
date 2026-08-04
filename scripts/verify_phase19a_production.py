"""Read-only production verification for Phase 19A lifecycle evidence.

Usage:
    python scripts/verify_phase19a_production.py
    python scripts/verify_phase19a_production.py --json

The shared db-manager factory selects PostgreSQL from DATABASE_URL and SQLite
from MLB_DB_PATH/defaults otherwise. This script never writes to the database
and never prints connection strings or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from database.connection import get_connection_dialect_name
from database.db_manager import get_connection


TABLE = "recommendation_lifecycle_events"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read a named DB row across SQLite and PostgreSQL wrappers."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _count_map(conn, query: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in conn.execute(query).fetchall():
        key = _row_value(row, "key")
        if key is None:
            key = "unknown"
        result[str(key)] = int(_row_value(row, "count", 0) or 0)
    return result


def _table_exists(conn) -> bool:
    dialect = get_connection_dialect_name(conn)
    if dialect == "postgresql":
        row = conn.execute(
            "SELECT 1 AS present FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ?",
            (TABLE,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 AS present FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (TABLE,),
        ).fetchone()
    return row is not None


def verify_lifecycle(conn, recent_limit: int = 10) -> dict[str, Any]:
    """Return read-only Phase 19A integrity details for an open connection."""
    result: dict[str, Any] = {
        "table": TABLE,
        "table_exists": False,
        "event_counts": {},
        "recent_events": [],
        "duplicate_event_keys": 0,
        "closing_available": {},
        "clv_available": {},
        "line_move_types": {},
        "orphaned_recommendations": 0,
        "canonical_clv": "clv_probability",
        "secondary_clv_diagnostic": "clv_price_diff",
        "integrity_failures": [],
    }

    if not _table_exists(conn):
        result["integrity_failures"].append(f"Missing table: {TABLE}")
        return result
    result["table_exists"] = True

    result["event_counts"] = _count_map(
        conn,
        "SELECT event_type AS key, COUNT(*) AS count "
        f"FROM {TABLE} GROUP BY event_type ORDER BY event_type",
    )
    result["closing_available"] = _count_map(
        conn,
        "SELECT CASE WHEN closing_available = 1 THEN 'true' "
        "WHEN closing_available = 0 THEN 'false' ELSE 'unknown' END AS key, "
        f"COUNT(*) AS count FROM {TABLE} GROUP BY key ORDER BY key",
    )
    result["clv_available"] = _count_map(
        conn,
        "SELECT CASE WHEN clv_available = 1 THEN 'true' "
        "WHEN clv_available = 0 THEN 'false' ELSE 'unknown' END AS key, "
        f"COUNT(*) AS count FROM {TABLE} GROUP BY key ORDER BY key",
    )
    result["line_move_types"] = _count_map(
        conn,
        f"SELECT COALESCE(line_move_type, 'unknown') AS key, COUNT(*) AS count "
        f"FROM {TABLE} GROUP BY line_move_type ORDER BY line_move_type",
    )

    duplicate_row = conn.execute(
        f"SELECT COUNT(*) AS count FROM (SELECT event_key FROM {TABLE} "
        "GROUP BY event_key HAVING COUNT(*) > 1) duplicates"
    ).fetchone()
    result["duplicate_event_keys"] = int(_row_value(duplicate_row, "count", 0) or 0)

    try:
        orphan_row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {TABLE} e "
            "LEFT JOIN historical_recommendations r "
            "ON r.recommendation_id = e.recommendation_id "
            "WHERE r.recommendation_id IS NULL"
        ).fetchone()
        result["orphaned_recommendations"] = int(_row_value(orphan_row, "count", 0) or 0)
    except Exception:
        result["integrity_failures"].append(
            "Cannot verify orphaned recommendation_id values: missing or unreadable historical_recommendations"
        )

    invalid_clv = conn.execute(
        f"SELECT COUNT(*) AS count FROM {TABLE} "
        "WHERE clv_available = 1 AND (closing_available != 1 OR clv_probability IS NULL)"
    ).fetchone()
    if int(_row_value(invalid_clv, "count", 0) or 0):
        result["integrity_failures"].append("CLV marked available without canonical probability CLV")

    invalid_line_clv = conn.execute(
        f"SELECT COUNT(*) AS count FROM {TABLE} "
        "WHERE line_move_type = 'line_changed' AND clv_available = 1"
    ).fetchone()
    if int(_row_value(invalid_line_clv, "count", 0) or 0):
        result["integrity_failures"].append("Line-changed events have clv_available=true")

    invalid_missing_close = conn.execute(
        f"SELECT COUNT(*) AS count FROM {TABLE} "
        "WHERE line_move_type = 'no_close' AND closing_available = 1"
    ).fetchone()
    if int(_row_value(invalid_missing_close, "count", 0) or 0):
        result["integrity_failures"].append("No-close events have closing_available=true")

    rows = conn.execute(
        f"SELECT lifecycle_event_id, recommendation_id, event_type, snapshot_kind, "
        f"event_timestamp, source_run_id, line_move_type, closing_available, "
        f"clv_available, clv_probability FROM {TABLE} "
        "ORDER BY event_timestamp DESC LIMIT ?",
        (max(0, int(recent_limit)),),
    ).fetchall()
    result["recent_events"] = [
        {
            "lifecycle_event_id": _row_value(row, "lifecycle_event_id"),
            "recommendation_id": _row_value(row, "recommendation_id"),
            "event_type": _row_value(row, "event_type"),
            "snapshot_kind": _row_value(row, "snapshot_kind"),
            "event_timestamp": _row_value(row, "event_timestamp"),
            "source_run_id": _row_value(row, "source_run_id"),
            "line_move_type": _row_value(row, "line_move_type"),
            "closing_available": _row_value(row, "closing_available"),
            "clv_available": _row_value(row, "clv_available"),
            "clv_probability": _row_value(row, "clv_probability"),
        }
        for row in rows
    ]

    if result["duplicate_event_keys"]:
        result["integrity_failures"].append("Duplicate lifecycle event_key values found")
    if result["orphaned_recommendations"]:
        result["integrity_failures"].append("Orphaned recommendation_id values found")
    return result


def _print_report(report: dict[str, Any]) -> None:
    """Print a credential-free human report."""
    print(f"table={report['table']} exists={report['table_exists']}")
    print(f"event_counts={report['event_counts']}")
    print(f"closing_available={report['closing_available']}")
    print(f"clv_available={report['clv_available']}")
    print(f"line_move_types={report['line_move_types']}")
    print(f"duplicate_event_keys={report['duplicate_event_keys']}")
    print(f"orphaned_recommendations={report['orphaned_recommendations']}")
    print(f"canonical_clv={report['canonical_clv']}")
    print(f"secondary_clv_diagnostic={report['secondary_clv_diagnostic']}")
    print("recent_events=")
    for event in report["recent_events"]:
        print(f"  {event}")
    if report["integrity_failures"]:
        print(f"integrity_failures={report['integrity_failures']}")
    else:
        print("integrity_failures=[]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Phase 19A lifecycle evidence (read-only)")
    parser.add_argument("--db-path", default=os.environ.get("MLB_DB_PATH", "database/mlb_model.db"))
    parser.add_argument("--recent-limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    conn = None
    try:
        conn = get_connection(args.db_path)
        report = verify_lifecycle(conn, recent_limit=args.recent_limit)
        if args.as_json:
            print(json.dumps(report, indent=2, default=str))
        else:
            _print_report(report)
        return 1 if report["integrity_failures"] else 0
    except Exception as exc:
        print(f"verification_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
