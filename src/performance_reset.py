"""Start a fresh customer-facing record (non-destructive).

    python -m src.performance_reset --reason "Pinnacle-only picks"            # dry run: shows what would change
    python -m src.performance_reset --reason "Pinnacle-only picks" --apply    # sets the new epoch

Nothing is deleted. The customer site counts picks scanned after the epoch and shows a notice plus an
all-time record, so the earlier history stays visible. Uses the configured database (DATABASE_URL).
"""

from __future__ import annotations

import argparse

from database.db_manager import (
    get_all_time_record, get_connection, get_performance_baseline_info, init_db, reset_performance_baseline,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reason", required=True, help="Shown to customers, e.g. 'Pinnacle-only picks'")
    ap.add_argument("--apply", action="store_true", help="Actually write the new epoch (default is a dry run)")
    args = ap.parse_args(argv)
    init_db()
    conn = get_connection()
    try:
        print("current baseline:", get_performance_baseline_info(conn))
        print("all-time record (kept either way):", get_all_time_record(conn))
        if not args.apply:
            print("DRY RUN: nothing changed. Re-run with --apply to start a fresh record.")
            return 0
        when = reset_performance_baseline(conn, args.reason)
        print("new baseline:", when, "|", args.reason)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
