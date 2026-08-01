"""Unit tests for the BIRD harness's static SQL linter.

`backend/tests/bird_eval.py` produces every accuracy number quoted for this
project and had no tests. These cover `lint_sql`, whose false positives were
measured on 2026-07-28 to account for 48% of its "column is not in any
referenced table" reports across the qwen2.5-coder:32b and XiYanSQL-14B runs --
each one burning a repair LLM call on SQL that was already valid.

The catalog is primed directly so these run without PostgreSQL.
"""
import os
import importlib.util

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve the harness without assuming the repo layout. The rest of this suite
# hardcodes `dirname(dirname(__file__))/...`, which is why 25 of its tests fail
# the moment they run anywhere but a checkout root -- including inside the
# container the harness actually runs in. Look in the deployed location too.
def _repo_harness():
    """Nearest ancestor holding backend/tests/bird_eval.py.

    PROJECT_ROOT is dirname(dirname(__file__)), which under the documented
    in-container workflow is /tmp -- so the repo candidate never matched and
    resolution fell through to /tmp/bird_eval.py. That path is ALSO where the
    benchmark supervisor stages its own copy, so this file tested whichever
    harness last landed in /tmp rather than the repo's, and went green or red
    depending on run order (same failure family as AUDIT D-19).
    """
    d = os.path.dirname(os.path.abspath(__file__))
    while True:
        cand = os.path.join(d, "backend", "tests", "bird_eval.py")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent


_CANDIDATES = [
    os.environ.get("BIRD_EVAL_PATH", ""),
    _repo_harness(),
    os.path.join(PROJECT_ROOT, "backend", "tests", "bird_eval.py"),
    "/opt/sqlbot/app/tests/bird_eval.py",   # deployed copy
    "/tmp/bird_eval.py",                    # last resort: staged by the supervisor
]
BIRD_EVAL = next((p for p in _CANDIDATES if p and os.path.exists(p)), "")

pytest.importorskip("sqlglot")
if not BIRD_EVAL:
    raise AssertionError(
        "bird_eval.py not found in any of: "
        + ", ".join(p for p in _CANDIDATES if p)
        + " -- set BIRD_EVAL_PATH. Refusing to skip silently: these tests "
          "guard the linter that scores every benchmark run.")

_spec = importlib.util.spec_from_file_location("bird_eval", BIRD_EVAL)
be = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(be)

# A cut-down `financial` schema: the real BIRD db, enough of it to reproduce the
# hallucinations seen in the runs. account owns `frequency`, district owns `a2`
# and `a11` -- neither lives on client or disp, which is what the models assumed.
FAKE_CATALOG = {
    "client": {"client_id", "gender", "birth_date", "district_id"},
    "account": {"account_id", "district_id", "frequency", "date"},
    "disp": {"disp_id", "client_id", "account_id", "type"},
    "district": {"district_id", "a2", "a3", "a11"},
    "trans": {"trans_id", "account_id", "date", "amount", "balance"},
}
DB = "financial"


@pytest.fixture(autouse=True)
def _prime_catalog():
    be._catalog_cache[DB] = {t: set(c) for t, c in FAKE_CATALOG.items()}
    yield
    be._catalog_cache.pop(DB, None)


def lint(sql):
    return be.lint_sql(sql, DB)


# ---------------------------------------------------------------- valid SQL
# Every query here is legal PostgreSQL. The linter must stay silent, or it
# spends a repair call corrupting a correct answer.

def test_select_alias_in_order_by_is_not_a_missing_column():
    """The single most common false positive: 10 of 20 in the XiYan run."""
    sql = ("SELECT d.a2 AS district_name, COUNT(c.client_id) AS n "
           "FROM client c JOIN district d ON c.district_id = d.district_id "
           "GROUP BY d.a2 ORDER BY n DESC LIMIT 9")
    assert lint(sql) == []


def test_select_alias_in_having():
    sql = ("SELECT a.frequency, COUNT(*) AS cnt FROM account a "
           "GROUP BY a.frequency HAVING COUNT(*) > 5 ORDER BY cnt DESC")
    assert lint(sql) == []


def test_cte_name_is_not_a_missing_table():
    sql = ("WITH t AS (SELECT account_id, COUNT(*) AS cnt FROM trans "
           "GROUP BY account_id) SELECT account_id FROM t WHERE cnt > 5")
    assert lint(sql) == []


def test_subquery_alias_in_from():
    sql = "SELECT x.aid FROM (SELECT account_id AS aid FROM account) x LIMIT 1"
    assert lint(sql) == []


def test_bare_aggregate_used_as_output_name():
    """`ORDER BY count` with no explicit alias -- Postgres exposes the fn name."""
    sql = ("SELECT gender, COUNT(*) FROM client GROUP BY gender "
           "ORDER BY count DESC")
    assert lint(sql) == []


def test_plain_correct_join_is_clean():
    sql = ("SELECT a.account_id FROM account a "
           "JOIN district d ON a.district_id = d.district_id LIMIT 1")
    assert lint(sql) == []


def test_select_star_stays_clean():
    assert lint("SELECT * FROM client WHERE gender = 'F'") == []


# ------------------------------------------------------------- broken SQL
# The linter must still catch what it was built for. The alias fix widens
# `known_cols`, so these guard against it widening far enough to go blind.

def test_column_attributed_to_the_wrong_table_is_caught():
    """q95: the model put account_id on client. It is on account and disp."""
    sql = ("SELECT T1.account_id FROM client AS T1 "
           "JOIN district AS T2 ON T1.district_id = T2.district_id")
    problems = lint(sql)
    assert any("account_id" in p and "client" in p for p in problems), problems


def test_wrong_table_message_names_the_real_owner():
    """The repair loop's failure mode was silence -- it must now say where."""
    sql = "SELECT d.frequency FROM disp d"
    problems = lint(sql)
    assert problems, "frequency is not on disp"
    assert "`account`" in problems[0], problems


def test_unqualified_unknown_column_names_the_real_owner():
    sql = "SELECT client_id, frequency FROM client"
    problems = lint(sql)
    assert problems
    assert "`account`" in problems[0], problems


def test_missing_table_is_caught():
    problems = lint("SELECT * FROM custmoers")
    assert any("does not exist" in p for p in problems), problems


def test_sqlite_only_function_is_caught():
    problems = lint("SELECT strftime('%Y', date) FROM trans")
    assert any("SQLite-only" in p for p in problems), problems


def test_unparseable_sql_is_reported():
    problems = lint("SELECT FROM WHERE ORDER")
    assert problems


def test_empty_query():
    assert lint("   ") == ["empty query"]


# ------------------------------------------------------------- owners hint

def test_owners_hint_excludes_the_table_already_blamed():
    hint = be._owners_hint("account_id", FAKE_CATALOG, exclude="account")
    assert "`account`" not in hint
    assert "`disp`" in hint


def test_owners_hint_is_empty_for_a_truly_invented_column():
    assert be._owners_hint("not_a_real_column", FAKE_CATALOG) == ""


def test_owners_hint_truncates_long_lists():
    hint = be._owners_hint("account_id", FAKE_CATALOG)
    assert hint.count("`") <= 8  # at most 3 table names, then "(and N more)"
