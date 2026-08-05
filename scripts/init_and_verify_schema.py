"""Initialize and verify the complete database schema, read-only after init.

Usage:
    python scripts/init_and_verify_schema.py
    python scripts/init_and_verify_schema.py --db-path path/to/local.db

DATABASE_URL selects PostgreSQL through the shared db-manager factory. The
script never drops tables or prints connection strings.
"""

from __future__ import annotations

import argparse
import os
import sys

from database.db_manager import get_connection, init_db, verify_required_schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize and verify the complete database schema")
    parser.add_argument("--db-path", default=os.environ.get("MLB_DB_PATH", "database/mlb_model.db"))
    args = parser.parse_args(argv)

    conn = None
    try:
        print("SCHEMA INIT START")
        init_diagnostic = init_db(args.db_path)
        if not init_diagnostic or not init_diagnostic.get("lifecycle_helper_ran"):
            raise RuntimeError("lifecycle creation helper did not report completion")
        print("SCHEMA INIT PHASE lifecycle_helper=completed")
        print("SCHEMA INIT COMMIT completed")
        print("SCHEMA VERIFY START")
        conn = get_connection(args.db_path)
        diagnostic = verify_required_schema(conn)
        print(
            "SCHEMA VERIFIED "
            f"dialect={diagnostic['dialect']} "
            f"database={diagnostic['database_name']} "
            f"schema={diagnostic['schema_name']} "
            f"required_tables={len(diagnostic['required_tables'])}"
        )
        return 0
    except Exception as exc:
        print(f"SCHEMA VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
