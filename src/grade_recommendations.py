"""CLI for grading historical recommendations.

Usage:
    python -m src.grade_recommendations --grade-all
    python -m src.grade_recommendations --grade-event EVENT_ID
    python -m src.grade_recommendations --grade-recommendation REC_ID
    python -m src.grade_recommendations --show-unsettled
    python -m src.grade_recommendations --show-settled
    python -m src.grade_recommendations --summary
    python -m src.grade_recommendations --summary --breakdown market_type
    python -m src.grade_recommendations --dry-run
    python -m src.grade_recommendations --json
    python -m src.grade_recommendations --csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from database.db_manager import (
    init_db,
    get_connection,
    get_unsettled_recommendations,
    get_settled_recommendations,
    get_recommendation_by_id,
    save_player_stat_result,
    settle_recommendation,
    save_bet_units,
    record_grading_completed,
)
from src.grading import (
    GRADER_VERSION,
    grade_ou,
    grade_yn,
    compute_units,
    performance_summary,
    breakdown_by_field,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grade historical recommendations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grade-all", action="store_true",
                      help="Grade all unresolved recommendations")
    mode.add_argument("--grade-event", type=str, default=None,
                      help="Grade all unresolved for an event")
    mode.add_argument("--grade-recommendation", type=str, default=None,
                      help="Grade a specific recommendation by ID")
    mode.add_argument("--show-unsettled", action="store_true",
                      help="Show all unsettled recommendations")
    mode.add_argument("--show-settled", action="store_true",
                      help="Show all settled recommendations")
    mode.add_argument("--summary", action="store_true",
                      help="Show performance summary")
    mode.add_argument("--ingest-result", type=str, nargs=4,
                      metavar=("EVENT_ID", "PLAYER_ID", "MARKET_TYPE", "VALUE"),
                      help="Ingest a player stat result: EVENT_ID PLAYER_ID MARKET_TYPE VALUE")
    mode.add_argument("--override", type=str, nargs=3,
                      metavar=("REC_ID", "STATUS", "REASON"),
                      help="Manual override: REC_ID STATUS REASON")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be graded without writing")
    parser.add_argument("--breakdown", type=str, default=None,
                        help="Breakdown field (market_type, sportsbook, side, etc.)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--csv", action="store_true",
                        help="Output as CSV")
    return parser


def _print_table(rows: list[dict], columns: list[str], widths: list[int]) -> None:
    """Print a formatted table."""
    header = "  ".join(f"{c:<{w}}" for c, w in zip(columns, widths))
    print(header)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        vals = []
        for c, w in zip(columns, widths):
            v = row.get(c, "")
            if isinstance(v, float):
                vals.append(f"{v:<{w}.4f}" if w > 6 else f"{v:<{w}.2f}")
            elif isinstance(v, int):
                vals.append(f"{v:<{w}}")
            else:
                vals.append(f"{str(v)[:w]:<{w}}")
        print("  ".join(vals))


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    init_db()
    conn = get_connection()

    try:
        if args.grade_all:
            _grade_all(conn, args.dry_run)
        elif args.grade_event:
            _grade_event(conn, args.grade_event, args.dry_run)
        elif args.grade_recommendation:
            _grade_single(conn, args.grade_recommendation, args.dry_run)
        elif args.show_unsettled:
            _show_unsettled(conn, args.json, args.csv)
        elif args.show_settled:
            _show_settled(conn, args.json, args.csv)
        elif args.summary:
            _show_summary(conn, args.breakdown, args.json, args.csv)
        elif args.ingest_result:
            _ingest_result(conn, args.ingest_result)
        elif args.override:
            _apply_override(conn, args.override)
        else:
            build_parser().print_help()
    finally:
        conn.close()


def _grade_all(conn, dry_run: bool) -> None:
    recs = get_unsettled_recommendations(conn)
    if not recs:
        print("No unsettled recommendations found.")
        return

    graded = 0
    for rec in recs:
        result = _grade_single_rec(rec, dry_run=dry_run)
        if result:
            graded += 1

    print(f"{'Would grade' if dry_run else 'Graded'} {graded}/{len(recs)} recommendation(s).")


def _grade_event(conn, event_id: str, dry_run: bool) -> None:
    recs = get_unsettled_recommendations(conn)
    recs = [r for r in recs if r.get("event_id") == event_id]
    if not recs:
        print(f"No unsettled recommendations found for event {event_id}.")
        return

    graded = 0
    for rec in recs:
        result = _grade_single_rec(rec, dry_run=dry_run)
        if result:
            graded += 1

    print(f"{'Would grade' if dry_run else 'Graded'} {graded}/{len(recs)} for event {event_id}.")


def _grade_single(conn, rec_id: str, dry_run: bool) -> None:
    rec = get_recommendation_by_id(conn, rec_id)
    if not rec:
        print(f"Recommendation {rec_id} not found.", file=sys.stderr)
        sys.exit(1)

    result = _grade_single_rec(rec, dry_run=dry_run)
    if result:
        print(f"{'Would grade' if dry_run else 'Graded'}: {result}")


def _grade_single_rec(rec: dict, dry_run: bool = False) -> str | None:
    """Grade a single recommendation. Returns status string or None."""
    market_type = rec.get("market_type", "")
    side = rec.get("side", "")
    line = rec.get("line")

    # Determine if this is O/U or YN
    is_yn = market_type.endswith("_yn")

    if is_yn:
        status = grade_yn()
        reason = "YN grading unsupported — requires external settlement feed"
    else:
        # O/U — need final stat value from player_stat_results
        from database.db_manager import get_player_stat_result
        conn = get_connection()
        try:
            stat_result = get_player_stat_result(
                conn, rec["event_id"], rec["player_id"], market_type,
            )
        finally:
            conn.close()

        if not stat_result or stat_result.get("final_stat_value") is None:
            status = "UNRESOLVED"
            reason = "No final stat value available"
        else:
            final_stat = stat_result["final_stat_value"]
            status = grade_ou(final_stat, line, side)
            reason = f"final={final_stat}, line={line}, side={side}"

    if status == "UNRESOLVED":
        return None

    if dry_run:
        return f"[DRY RUN] {status}: {reason}"

    # Settle
    conn = get_connection()
    try:
        ok = settle_recommendation(
            conn, rec["recommendation_id"], status,
            final_stat_value=stat_result.get("final_stat_value") if not is_yn and stat_result else None,
            settlement_reason=reason,
            grader_version=GRADER_VERSION,
        )
        if ok and status in ("WIN", "LOSS", "PUSH", "VOID", "CANCELLED"):
            save_bet_units(conn, rec["recommendation_id"], status, rec["offered_american_odds"])
            record_grading_completed(
                conn,
                rec,
                status,
                final_stat_value=stat_result.get("final_stat_value") if not is_yn and stat_result else None,
                grader_version=GRADER_VERSION,
            )
    finally:
        conn.close()

    return f"{status}: {reason}"


def _show_unsettled(conn, as_json: bool, as_csv: bool) -> None:
    recs = get_unsettled_recommendations(conn)
    if not recs:
        print("No unsettled recommendations.")
        return

    if as_json:
        print(json.dumps(recs, indent=2, default=str))
    elif as_csv:
        _print_csv(recs)
    else:
        cols = ["recommendation_id", "event_id", "player_name", "market_type",
                "side", "sportsbook", "offered_american_odds", "rec_status", "scan_timestamp"]
        widths = [36, 20, 18, 24, 6, 14, 8, 16, 20]
        _print_table(recs, cols, widths)


def _show_settled(conn, as_json: bool, as_csv: bool) -> None:
    recs = get_settled_recommendations(conn)
    if not recs:
        print("No settled recommendations.")
        return

    if as_json:
        print(json.dumps(recs, indent=2, default=str))
    elif as_csv:
        _print_csv(recs)
    else:
        cols = ["recommendation_id", "player_name", "market_type",
                "side", "offered_american_odds", "settlement_status",
                "profit_units", "settled_at"]
        widths = [36, 18, 24, 6, 8, 16, 12, 20]
        _print_table(recs, cols, widths)


def _show_summary(conn, breakdown_field: str | None, as_json: bool, as_csv: bool) -> None:
    recs = get_settled_recommendations(conn)
    if not recs:
        print("No settled recommendations for summary.")
        return

    summary = performance_summary(recs)

    if breakdown_field:
        bd = breakdown_by_field(recs, breakdown_field)
        summary["breakdown"] = bd

    if as_json:
        print(json.dumps(summary, indent=2, default=str))
    elif as_csv:
        if "breakdown" in summary:
            for key, vals in summary["breakdown"].items():
                print(f"\n--- {breakdown_field}={key} ---")
                for k, v in vals.items():
                    print(f"  {k}: {v}")
        else:
            for k, v in summary.items():
                print(f"  {k}: {v}")
    else:
        print("\n=== PERFORMANCE SUMMARY ===")
        for k, v in summary.items():
            if k != "breakdown":
                print(f"  {k:<30} {v}")
        if "breakdown" in summary:
            print(f"\n  Breakdown by {breakdown_field}:")
            for key, vals in summary["breakdown"].items():
                print(f"\n    {key}:")
                for k, v in vals.items():
                    print(f"      {k:<28} {v}")


def _ingest_result(conn, args: list[str]) -> None:
    event_id, player_id, market_type, value_str = args
    try:
        value = float(value_str)
    except ValueError:
        print(f"Invalid stat value: {value_str}", file=sys.stderr)
        sys.exit(1)

    save_player_stat_result(
        conn, event_id, player_id, market_type,
        final_stat_value=value,
        result_source="manual",
        result_status="AVAILABLE",
    )
    print(f"Ingested result: {event_id} / {player_id} / {market_type} = {value}")


def _apply_override(conn, args: list[str]) -> None:
    from database.db_manager import apply_manual_override
    rec_id, status, reason = args
    if status not in ("WIN", "LOSS", "PUSH", "VOID", "CANCELLED"):
        print(f"Invalid status: {status}. Must be one of: WIN, LOSS, PUSH, VOID, CANCELLED",
              file=sys.stderr)
        sys.exit(1)
    ok = apply_manual_override(conn, rec_id, status, reason)
    if ok:
        print(f"Override applied: {rec_id} → {status}")
    else:
        print("Override failed.", file=sys.stderr)
        sys.exit(1)


def _print_csv(rows: list[dict]) -> None:
    if not rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)


if __name__ == "__main__":
    main()
