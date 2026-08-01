"""Security tests for the read-only SQL gate and constraint-discovery SQL.

GROUP E — AUDIT D-25 and D-26.

D-25: `check_sql_read` type-checked each parsed statement but only keyword-
checked the FIRST, so `SELECT 1; SELECT pg_sleep(30)` passed and psycopg2
executed both. Combined with D-08 (spreadsheet headers reaching SQL) that
widened the injected-header surface from "extra subquery" to "extra statement".

D-26: `relations._constraint_sql` interpolated the schema name into
information_schema queries escaping only single quotes.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_sql_read_gate.py -q"
"""

import pytest

pytest.importorskip("sqlglot")

# apps.db.db cannot be imported standalone: the cycle runs through closed-source
# sqlbot_xpack/__init__.py (AUDIT D-39, WONTFIX). Importing main first warms the
# graph, the same workaround backend/tests/test_row_rag_e2e.py already uses.
import main  # noqa: F401,E402  (production import order)

from apps.db.db import check_sql_read  # noqa: E402


class _DS:
    def __init__(self, ds_type="pg"):
        self.type = ds_type
        self.id = 1


# --- D-25: stacked statements ----------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT pg_sleep(30)",
    "SELECT a FROM t; SELECT b FROM u",
    "SELECT 1;SELECT 2",
    "SELECT 1; DROP TABLE x",
    "SELECT 1; INSERT INTO t VALUES (1)",
    "SELECT 1; UPDATE t SET a = 1",
])
def test_stacked_statements_are_rejected(sql):
    assert check_sql_read(sql, _DS()) is False, f"stacked payload accepted: {sql}"


@pytest.mark.parametrize("sql", [
    "SELECT a FROM t",
    "SELECT a FROM t;",
    "SELECT a FROM t;   ",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT 1 AS a UNION ALL SELECT 2",
    "SELECT (SELECT max(b) FROM u) FROM t",
    "SELECT a FROM t WHERE b IN (SELECT c FROM u)",
])
def test_legitimate_single_statements_still_pass(sql):
    """Backward compatibility. A trailing semicolon, a CTE, a UNION and a
    subquery are all ONE statement and must keep working — the relations
    module's SQL Server constraint query is a UNION ALL."""
    assert check_sql_read(sql, _DS()) is True, f"legitimate query rejected: {sql}"


def test_single_write_statement_still_rejected():
    for sql in ("DROP TABLE x", "INSERT INTO t VALUES (1)", "UPDATE t SET a=1",
                "DELETE FROM t", "CREATE TABLE x (a int)", "ALTER TABLE t ADD c int"):
        assert check_sql_read(sql, _DS()) is False


@pytest.mark.parametrize("ds_type", ["pg", "mysql", "sqlServer", "hive"])
def test_stacked_rejection_applies_to_every_dialect(ds_type):
    assert check_sql_read("SELECT 1; SELECT 2", _DS(ds_type)) is False


# --- D-26: constraint-discovery SQL ----------------------------------------

def test_schema_name_quotes_are_escaped_not_rejected():
    """A quote in a schema name is LEGITIMATE — PostgreSQL allows it in a quoted
    identifier — so it must be escaped, not refused. Doubling `'` is the complete
    escape for a single-quoted literal, which is why the original code was
    substantially correct and this audit item was overstated."""
    from apps.datasource.relations import _constraint_sql

    class _D:
        type = "pg"
        id = 1

    sql = _constraint_sql(_D(), "we'ird")
    assert sql is not None
    assert "we''ird" in sql, "quote must be doubled, not dropped or refused"


def test_control_characters_are_refused():
    """A NUL or control byte is NOT escapable inside a literal — it corrupts the
    statement rather than being quoted, so discovery declines and the caller
    falls back to inference."""
    from apps.datasource.relations import _constraint_sql

    class _D:
        type = "pg"
        id = 1

    for hostile in ("pub\x00lic", "pub\nlic", "pub\rlic", "pub\x1blic"):
        assert _constraint_sql(_D(), hostile) is None, (
            f"control character accepted: {hostile!r}")


def test_ordinary_schema_names_still_produce_sql():
    from apps.datasource.relations import _constraint_sql

    class _D:
        type = "pg"
        id = 1

    for ok in ("public", "bird_dev", "Schema1", "_x", "a1_b2", "financial"):
        assert _constraint_sql(_D(), ok) is not None, f"rejected valid schema: {ok}"
