"""Minimal read-only-after-commit diagnostic for Phase 19A lifecycle DDL.

This intentionally runs only the lifecycle table helper. It never drops or
overwrites data and never prints DATABASE_URL or other credentials.
"""

from __future__ import annotations

import argparse
import os
import sys

from database.db_manager import (
    create_recommendation_lifecycle_table,
    get_connection,
    lifecycle_table_diagnostic,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debug Phase 19A lifecycle table creation")
    parser.add_argument("--db-path", default=os.environ.get("MLB_DB_PATH", "database/mlb_model.db"))
    args = parser.parse_args(argv)
    conn = None
    try:
        print("LIFECYCLE DDL START table=recommendation_lifecycle_events")
        conn = get_connection(args.db_path)
        create_recommendation_lifecycle_table(conn)
        before = lifecycle_table_diagnostic(conn)
        print(
            "LIFECYCLE DDL BEFORE COMMIT "
            f"to_regclass={before['to_regclass']} "
            f"information_schema={before['information_schema_present']} "
            f"transaction_status={before['transaction_status']}"
        )
        if not before["present"]:
            raise RuntimeError("Lifecycle table absent before commit")
        conn.commit()
        after = lifecycle_table_diagnostic(conn)
        print(
            "LIFECYCLE DDL AFTER COMMIT "
            f"to_regclass={after['to_regclass']} "
            f"information_schema={after['information_schema_present']} "
            f"transaction_status={after['transaction_status']}"
        )
        if not after["present"]:
            raise RuntimeError("Lifecycle table absent after commit")
        return 0
    except Exception as exc:
        print(f"LIFECYCLE DDL FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
