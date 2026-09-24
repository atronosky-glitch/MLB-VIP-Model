"""PostgreSQL schema execution safety.

There is no PostgreSQL server in the test environment, so SQLite passing
does NOT prove production correctness. Two independent layers are checked:

1. split_sql_statements(): comment/string/dollar-quote aware statement
   splitting (regression for the 2026-09-22 crash-loop, where a semicolon
   inside an SQL comment produced an empty statement).
2. The REAL DDL that init_db() emits through the PostgreSQL code path (real
   DB wrapper + _convert_sql, fake driver connection) is parsed with
   libpg_query (pglast) -- PostgreSQL's own grammar -- so a statement
   PostgreSQL would reject at parse time fails here. (Runtime/semantic
   errors such as a missing table still need a real server; see the
   production-readiness report.)
"""

from __future__ import annotations

import re
from unittest import mock

import pytest

from database.connection import DB, _convert_sql, split_sql_statements


class TestSplitSqlStatements:
    def test_semicolon_inside_a_line_comment_is_not_a_split_point(self):
        script = "CREATE TABLE a (x INT); -- note; with a semicolon\nCREATE TABLE b (y INT);"
        assert split_sql_statements(script) == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]

    def test_the_exact_production_failure_shape(self):
        # a comment containing ';' leaves a trailing comment fragment that
        # the old naive split turned into an empty/comment-only statement
        script = "CREATE TABLE t (a INT);\n-- see notes; more text after the semicolon\n"
        out = split_sql_statements(script)
        assert out == ["CREATE TABLE t (a INT)"]
        assert all(s and not s.startswith("--") for s in out)

    def test_comment_only_and_whitespace_scripts_yield_nothing(self):
        assert split_sql_statements("") == []
        assert split_sql_statements("  \n\t ") == []
        assert split_sql_statements("-- just a comment; nothing else") == []
        assert split_sql_statements("/* block; comment */") == []
        assert split_sql_statements(";;;") == []

    def test_semicolons_inside_string_literals_and_identifiers(self):
        script = "INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('it''s; fine'); SELECT \"we;ird\" FROM t;"
        assert split_sql_statements(script) == [
            "INSERT INTO t VALUES ('a;b')", "INSERT INTO t VALUES ('it''s; fine')", 'SELECT "we;ird" FROM t',
        ]

    def test_block_comments_including_nested_are_dropped_with_their_semicolons(self):
        script = "SELECT 1 /* a; /* nested; */ still; comment */; SELECT 2;"
        out = split_sql_statements(script)
        assert [s.replace("  ", " ") for s in out] == ["SELECT 1", "SELECT 2"]

    def test_dollar_quoted_bodies(self):
        script = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql; SELECT 1;"
        out = split_sql_statements(script)
        assert len(out) == 2 and "RETURN 1; END;" in out[0]
        tagged = "SELECT $tag$a;b$tag$; SELECT 2;"
        assert split_sql_statements(tagged) == ["SELECT $tag$a;b$tag$", "SELECT 2"]

    def test_unterminated_input_never_raises_or_loops(self):
        for bad in ("SELECT 'unterminated", "SELECT /* unterminated", "SELECT $$ unterminated", "SELECT \"x"):
            assert isinstance(split_sql_statements(bad), list)

    def test_no_trailing_semicolon_still_returns_the_last_statement(self):
        assert split_sql_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]

    def test_idempotent_on_its_own_output(self):
        script = "CREATE TABLE a (x INT); -- c;\nCREATE INDEX i ON a(x);"
        once = split_sql_statements(script)
        assert split_sql_statements(";".join(once)) == once


class _Cursor:
    rowcount = 0
    description = None

    def __init__(self, sink):
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append(sql)

    def executemany(self, sql, params):
        self._sink.append(sql)

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _FakePg:
    def __init__(self):
        self.executed: list[str] = []

    def cursor(self, *a, **k):
        return _Cursor(self.executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _emit_init_db_sql() -> list[str]:
    import database.db_manager as dbm
    pg = _FakePg()
    ok_state = {"present": True, "to_regclass": "x", "information_schema_present": True,
                "transaction_status": "IDLE"}
    with mock.patch.object(dbm, "get_connection", return_value=DB(pg, dialect="postgresql")),          mock.patch.object(dbm, "lifecycle_table_diagnostic", return_value=ok_state),          mock.patch.object(dbm, "verify_required_schema", return_value={"dialect": "postgresql", "database_name": "x", "schema_name": "public", "required_tables": []}):     # runtime catalog checks need a real server
        dbm.init_db()
    return pg.executed


class TestRealSchemaThroughThePostgresPath:
    def test_init_db_emits_only_nonempty_non_comment_statements(self):
        statements = _emit_init_db_sql()
        assert len(statements) > 100
        for sql in statements:
            body = re.sub(r"--[^\n]*", "", sql).strip()
            assert body, f"empty/comment-only statement reached PostgreSQL: {sql[:80]!r}"

    def test_every_emitted_statement_parses_with_postgresqls_own_grammar(self):
        pglast = pytest.importorskip("pglast")
        failures = []
        for sql in _emit_init_db_sql():
            # bind placeholders -> $n so the grammar check sees valid SQL
            parsable = re.sub(r"%\(\w+\)s|%s", "$1", sql)
            try:
                pglast.parse_sql(parsable)
            except Exception as exc:                       # noqa: BLE001
                failures.append((sql[:100], str(exc)[:120]))
        assert not failures, failures[:5]

    def test_startup_is_idempotent_every_create_uses_if_not_exists(self):
        creates = [s for s in _emit_init_db_sql() if re.match(r"\s*CREATE\s+(TABLE|INDEX|UNIQUE)", s, re.I)]
        assert creates
        bad = [s[:80] for s in creates if not re.search(r"IF\s+NOT\s+EXISTS", s, re.I)]
        assert not bad, bad

    def test_new_launch_audit_tables_and_columns_are_in_the_postgres_ddl(self):
        blob = "\n".join(_emit_init_db_sql())
        for needle in ("auth_rate_events", "password_reset_token_hash", "customer_accounts"):
            assert needle in blob
