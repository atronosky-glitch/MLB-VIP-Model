"""Migrate SQLite database to PostgreSQL.

Usage:
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --dry-run
    python scripts/migrate_sqlite_to_postgres.py --drop-existing
    python scripts/migrate_sqlite_to_postgres.py --sqlite-path /path/to/mlb_model.db
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BATCH_SIZE = 500

SKIP_TABLES = {"sqlite_sequence"}


def get_sqlite_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_connection(database_url: str):
    """Open a psycopg2 connection."""
    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 is required. Install with: pip install psycopg2-binary")
        sys.exit(1)

    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    return conn


def get_table_names(sqlite_conn: sqlite3.Connection) -> list[str]:
    """Return all user table names from SQLite, ordered by dependency."""
    cursor = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [row["name"] for row in cursor.fetchall() if row["name"] not in SKIP_TABLES]


def get_table_row_count(sqlite_conn: sqlite3.Connection, table_name: str) -> int:
    """Return the row count for a table."""
    return sqlite_conn.execute(f"SELECT COUNT(*) AS cnt FROM [{table_name}]").fetchone()["cnt"]


def get_table_columns(sqlite_conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Return ordered column names for a table."""
    cursor = sqlite_conn.execute(f"PRAGMA table_info([{table_name}])")
    return [row["name"] for row in cursor.fetchall()]


def get_autoincrement_columns(sqlite_conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Return the set of column names that are INTEGER PRIMARY KEY AUTOINCREMENT."""
    cursor = sqlite_conn.execute(f"PRAGMA table_info([{table_name}])")
    result = set()
    for row in cursor.fetchall():
        # row: cid, name, type, notnull, dflt_value, pk
        if row["type"].upper() == "INTEGER" and row["pk"] == 1:
            result.add(row["name"])
    return result


def build_pg_create_table(sqlite_conn: sqlite3.Connection, table_name: str) -> str:
    """Build a PostgreSQL CREATE TABLE statement from the SQLite schema."""
    columns = get_table_columns(sqlite_conn, table_name)
    ai_cols = get_autoincrement_columns(sqlite_conn, table_name)

    cursor = sqlite_conn.execute(f"PRAGMA table_info([{table_name}])")
    col_infos = cursor.fetchall()

    pg_cols = []
    for col in col_infos:
        col_name = col["name"]
        col_type = col["type"].upper()
        notnull = col["notnull"]
        pk = col["pk"]
        default = col["dflt_value"]

        if col_name in ai_cols:
            pg_type = "SERIAL"
            pg_cols.append(f'    {col_name} {pg_type} PRIMARY KEY')
            continue

        if col_type == "INTEGER":
            pg_type = "INTEGER"
        elif col_type == "REAL":
            pg_type = "DOUBLE PRECISION"
        elif col_type in ("TEXT", ""):
            pg_type = "TEXT"
        elif col_type == "BLOB":
            pg_type = "BYTEA"
        else:
            pg_type = "TEXT"

        parts = [f"    {col_name} {pg_type}"]

        if pk:
            parts.append("PRIMARY KEY")
        if notnull and not pk:
            parts.append("NOT NULL")

        if default is not None and default != "":
            if "datetime('now')" in default:
                parts.append("DEFAULT NOW()")
            elif default.startswith("'") and default.endswith("'"):
                parts.append(f"DEFAULT {default}")
            else:
                parts.append(f"DEFAULT {default}")

        pg_cols.append(" ".join(parts))

    cols_sql = ",\n".join(pg_cols)
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n{cols_sql}\n);"


def drop_table_sql(table_name: str) -> str:
    return f"DROP TABLE IF EXISTS {table_name} CASCADE;"


def get_table_rows(
    sqlite_conn: sqlite3.Connection, table_name: str
) -> tuple[list[str], list[tuple]]:
    """Get column names and all rows from a table."""
    columns = get_table_columns(sqlite_conn, table_name)
    cursor = sqlite_conn.execute(f"SELECT * FROM [{table_name}]")
    rows = [tuple(row) for row in cursor.fetchall()]
    return columns, rows


def migrate_table(
    pg_conn,
    sqlite_conn: sqlite3.Connection,
    table_name: str,
    *,
    drop_existing: bool = False,
    dry_run: bool = False,
) -> int:
    """Migrate a single table. Returns row count inserted."""
    row_count = get_table_row_count(sqlite_conn, table_name)
    columns = get_table_columns(sqlite_conn, table_name)

    if dry_run:
        print(f"  [dry-run] {table_name}: {len(columns)} columns, {row_count} rows")
        return row_count

    pg_cursor = pg_conn.cursor()

    if drop_existing:
        pg_cursor.execute(drop_table_sql(table_name))

    create_sql = build_pg_create_table(sqlite_conn, table_name)
    pg_cursor.execute(create_sql)

    if row_count == 0:
        pg_conn.commit()
        print(f"  {table_name}: 0 rows (schema created)")
        return 0

    all_columns, rows = get_table_rows(sqlite_conn, table_name)

    placeholders = ", ".join(["%s"] * len(all_columns))
    col_list = ", ".join([f'"{c}"' for c in all_columns])
    insert_sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"

    total_inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        pg_cursor.executemany(insert_sql, batch)
        total_inserted += len(batch)

    pg_conn.commit()
    print(f"  {table_name}: {total_inserted} rows inserted")
    return total_inserted


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite to PostgreSQL")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without writing",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop tables in PostgreSQL before migrating",
    )
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help="Path to SQLite database (default: MLB_DB_PATH env var or database/mlb_model.db)",
    )
    args = parser.parse_args()

    # Resolve SQLite path
    if args.sqlite_path:
        sqlite_path = args.sqlite_path
    else:
        env_path = os.environ.get("MLB_DB_PATH", "")
        if env_path:
            sqlite_path = env_path
        else:
            sqlite_path = str(Path(__file__).resolve().parent.parent / "database" / "mlb_model.db")

    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    # Resolve PostgreSQL connection
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url and not args.dry_run:
        print("ERROR: DATABASE_URL environment variable is not set")
        sys.exit(1)

    print(f"SQLite source : {sqlite_path}")
    if not args.dry_run:
        print(f"PostgreSQL dest: {database_url.split('@')[-1] if '@' in database_url else database_url}")
    else:
        print("PostgreSQL dest: (dry-run, skipped)")
    print()

    # Open connections
    sqlite_conn = get_sqlite_connection(sqlite_path)
    pg_conn = None
    if not args.dry_run:
        pg_conn = get_postgres_connection(database_url)

    tables = get_table_names(sqlite_conn)
    print(f"Found {len(tables)} tables to migrate\n")

    total_tables = 0
    total_rows = 0

    try:
        for table_name in tables:
            try:
                rows = migrate_table(
                    pg_conn,
                    sqlite_conn,
                    table_name,
                    drop_existing=args.drop_existing,
                    dry_run=args.dry_run,
                )
                total_tables += 1
                total_rows += rows
            except Exception as e:
                print(f"  ERROR on {table_name}: {e}")
                if pg_conn and not args.dry_run:
                    pg_conn.rollback()
                # Continue with remaining tables
    finally:
        sqlite_conn.close()
        if pg_conn:
            pg_conn.close()

    print()
    print("=" * 50)
    print(f"Migration {'preview' if args.dry_run else 'complete'}")
    print(f"  Tables: {total_tables}")
    print(f"  Rows  : {total_rows}")
    print("=" * 50)


if __name__ == "__main__":
    main()
