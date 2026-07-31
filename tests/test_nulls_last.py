"""Deterministic NULLS LAST rewrite for descending sorts (AUDIT lever L-A).

PostgreSQL and Oracle treat NULL as larger than any value, so
`ORDER BY score DESC LIMIT 1` returns a NULL row instead of the maximum. The SQL
prompt already carries an explicit NULLS LAST rule and the model ignored it on
17 of the 72 BIRD-150 failures, so the rule is enforced deterministically after
generation instead.

Measured on the 150-question bank against bird_final_32b.json:
  17 candidate failures -> 5 flipped to CORRECT (q32, q50, q82, q879, q1122)
   9 currently-correct answers also touched -> 9 still correct, 0 regressions

Implemented with sqlglot rather than a regex, because a regex would also rewrite
DESC inside a string literal or a column named "desc". Those two cases are
pinned below.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_nulls_last.py -q"
"""

import pytest

from apps.chat.task.agentic import apply_nulls_last

pytest.importorskip("sqlglot")


def _norm(sql):
    return " ".join(sql.split()).upper()


# --- the core rewrite ------------------------------------------------------

def test_adds_nulls_last_to_a_descending_order_by():
    out = apply_nulls_last("SELECT a FROM t ORDER BY score DESC LIMIT 1", "postgres")
    assert "DESC NULLS LAST" in _norm(out)


def test_is_idempotent():
    sql = "SELECT a FROM t ORDER BY score DESC NULLS LAST LIMIT 1"
    assert _norm(apply_nulls_last(sql, "postgres")).count("NULLS LAST") == 1


def test_leaves_ascending_sorts_alone():
    out = apply_nulls_last("SELECT a FROM t ORDER BY score ASC", "postgres")
    assert "NULLS LAST" not in _norm(out)


def test_handles_mixed_directions():
    out = _norm(apply_nulls_last("SELECT a FROM t ORDER BY x ASC, y DESC", "postgres"))
    assert "Y DESC NULLS LAST" in out
    assert "X ASC NULLS LAST" not in out


def test_rewrites_inside_a_subquery():
    out = apply_nulls_last(
        "SELECT a FROM (SELECT b FROM t ORDER BY b DESC LIMIT 5) s", "postgres")
    assert "DESC NULLS LAST" in _norm(out)


def test_handles_ordinal_order_by():
    assert "DESC NULLS LAST" in _norm(
        apply_nulls_last("SELECT a FROM t ORDER BY 1 DESC", "postgres"))


# --- the cases a regex would corrupt --------------------------------------

def test_does_not_touch_desc_inside_a_string_literal():
    sql = "SELECT a FROM t WHERE note = 'sort DESC now' ORDER BY x DESC"
    out = apply_nulls_last(sql, "postgres")
    assert "'sort DESC now'" in out, f"string literal was rewritten: {out}"
    assert "X DESC NULLS LAST" in _norm(out)


def test_handles_a_column_literally_named_desc():
    out = apply_nulls_last('SELECT "desc" FROM t ORDER BY "desc" DESC', "postgres")
    assert '"desc"' in out
    assert "DESC NULLS LAST" in _norm(out)


# --- fails open ------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_input_returned_unchanged(bad):
    assert apply_nulls_last(bad, "postgres") == bad


def test_unparseable_sql_returned_unchanged():
    junk = "this is not sql at all ((("
    assert apply_nulls_last(junk, "postgres") == junk


def test_sql_without_order_by_returned_byte_identical():
    sql = "SELECT a, b FROM t WHERE x = 1"
    assert apply_nulls_last(sql, "postgres") == sql


# --- dialect gating --------------------------------------------------------

def test_dialect_gate_excludes_mysql_family():
    """MySQL/SQL Server/SQLite/ClickHouse/Hive already sort NULLs last on DESC
    and mostly reject the syntax, so they must not be in the configured list."""
    from common.core.config import settings
    dialects = {d.strip() for d in settings.AGENTIC_NULLS_LAST_DIALECTS.split(",")}
    for excluded in ("mysql", "sqlserver", "sqlite", "ck", "hive", "doris", "starrocks"):
        assert excluded not in dialects, f"{excluded} must not receive NULLS LAST"
    for included in ("pg", "excel", "oracle"):
        assert included in dialects


def test_feature_flag_exists_and_defaults_on():
    from common.core.config import settings
    assert settings.AGENTIC_NULLS_LAST_ENABLED is True
