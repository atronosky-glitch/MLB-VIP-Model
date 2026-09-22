"""Regression test for a real production incident (2026-09-22): a SQL
comment inside init_db()'s schema script contained a literal English-
sentence semicolon ("One row per (event_id, period);") that database/
connection.py::DB.executescript's PostgreSQL path -- which naively
splits the whole script on every "; " character, comments included --
turned into its own "statement": a chunk of pure comment text with no
actual SQL. psycopg2 raises "can't execute an empty query" for that,
which crashed the worker at startup (init_db() runs before anything
else) in a tight restart loop against the real production Postgres
database. SQLite's native executescript has no such problem (it
understands comments), so every existing test using the sqlite-backed
db_conn fixture passed regardless -- this bug was invisible to the
whole local test suite. This test replicates the exact naive-split
logic against the real schema text, without needing a live Postgres
connection, so a comment semicolon anywhere in the schema fails loudly
here instead of at Render startup.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _extract_init_db_schema_sql() -> str:
    """Statically extract the exact string literal init_db() passes to
    conn.executescript(...), via the AST -- not by importing/running
    init_db, so this works without any DB connection at all."""
    source = (PROJECT_ROOT / "database" / "db_manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "init_db":
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "executescript"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    return call.args[0].value
    raise AssertionError("Could not find init_db()'s conn.executescript(...) string literal")


def _strip_sql_comments(statement: str) -> str:
    """Remove '--' line comments, mirroring what postgres itself treats
    as non-executable content -- used only to detect a comment-only
    chunk, not to run anything."""
    lines = []
    for line in statement.splitlines():
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return "\n".join(lines).strip()


def test_no_schema_statement_becomes_comment_only_after_the_postgres_split():
    """Mirrors database/connection.py::DB.executescript's PostgreSQL
    branch EXACTLY: sql_script.split(";"), each chunk stripped. A
    semicolon inside a comment (English punctuation, not SQL) splits
    the script in a place the author never intended, and if that
    happens to land between two comment lines with no real SQL between
    them, the resulting chunk is empty after removing comments --
    which is exactly the production crash this test guards against."""
    schema_sql = _extract_init_db_schema_sql()
    offenders = []
    for statement_number, statement in enumerate(schema_sql.split(";"), 1):
        statement = statement.strip()
        if not statement:
            continue
        if not _strip_sql_comments(statement):
            offenders.append((statement_number, statement))
    assert offenders == [], (
        f"{len(offenders)} schema statement(s) are comment-only after PostgreSQL's naive "
        f"';' split -- almost always a literal semicolon inside a SQL comment (English "
        f"punctuation, not a real statement terminator). Offending statement(s): {offenders}"
    )


def test_schema_sql_is_non_trivial_and_was_actually_extracted():
    """Sanity check that the AST extraction above is finding the real,
    full schema and not silently matching nothing / an unrelated call."""
    schema_sql = _extract_init_db_schema_sql()
    assert "CREATE TABLE" in schema_sql
    assert schema_sql.count("CREATE TABLE") > 10
