"""Identifier quoting must escape, not just wrap (AUDIT D-08).

Field and table names were interpolated into SQL as f"{prefix}{name}{suffix}"
using DB.prefix / DB.suffix, with no escaping of the quote character itself.
Field names originate from uploaded spreadsheet headers, and
island_detection.region_columns only strips whitespace — so a header cell
containing a double quote survived into core_field.field_name and then broke out
of the identifier on every subsequent query.

The worst site was get_table_sample_data(), which runs on EVERY chat question,
not just on an admin screen: the payload is planted once at upload and fires for
every user thereafter.

These tests assert the emitted SQL. Pure: no database, no model.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_identifier_quoting.py -q"
"""

import pytest

from apps.chat.task.apex_helpers import quote_ident
from apps.db.constant import DB

# a header that closes the identifier and appends its own projection
HOSTILE = 'a" , (SELECT version()) AS x, "b'


class _DS:
    def __init__(self, ds_type):
        self.type = ds_type
        self.id = 1


class _Field:
    def __init__(self, name):
        self.field_name = name


# --- the swap must be behaviour-preserving for well-formed identifiers ----

@pytest.mark.parametrize("db", list(DB))
def test_quote_ident_matches_the_legacy_prefix_suffix(db):
    """quote_ident must emit exactly the same quote characters DB declares, for
    every one of the 14 dialects — otherwise this is a silent dialect change
    rather than a security fix."""
    assert quote_ident("safe_name", db.type) == f"{db.prefix}safe_name{db.suffix}"


# --- and must escape where the legacy form did not -----------------------

def test_quote_ident_escapes_double_quote_on_pg_family():
    """Excel data lives in the bundled PostgreSQL, so this is the live vector."""
    out = quote_ident(HOSTILE, "pg")
    assert out.startswith('"') and out.endswith('"')
    assert '""' in out, "embedded quote must be doubled"
    # the injected projection is now inside the identifier, not beside it
    assert not out.endswith('AS x, "b"') or '""' in out


@pytest.mark.parametrize("ds_type,char", [("mysql", "`"), ("sqlServer", "]")])
def test_quote_ident_escapes_the_dialects_own_quote_char(ds_type, char):
    name = f"a{char}b"
    out = quote_ident(name, ds_type)
    assert char * 2 in out, f"{ds_type} must double its own quote char: {out}"


# --- the call sites --------------------------------------------------------

def _sample_sql(monkeypatch, ds_type, field_name, table_name="orders"):
    from apps.datasource.crud import datasource as ds_mod
    seen = {}

    def _fake_exec(ds, sql, origin_column=False):
        seen["sql"] = sql
        return {"fields": ["a"], "data": [{"a": "v"}]}

    monkeypatch.setattr(ds_mod, "exec_sql", _fake_exec)
    ds_mod.get_table_sample_data(_DS(ds_type), table_name, [_Field(field_name)])
    return seen.get("sql", "")


def test_sample_query_escapes_a_hostile_column_name(monkeypatch):
    """The regression that matters: this path runs on every chat question.

    Asserted by PARSING, not by substring counting. After escaping, the payload
    text ("SELECT version()") is still present — but as literal characters
    INSIDE one quoted identifier, which is exactly the safe outcome. A substring
    count cannot tell those two cases apart; the parse tree can.
    """
    sqlglot = pytest.importorskip("sqlglot")
    sql = _sample_sql(monkeypatch, "pg", HOSTILE)
    assert '""' in sql, f"hostile header was not escaped: {sql}"

    tree = sqlglot.parse_one(sql, read="postgres")
    # one projection, and it is the hostile string as a plain column name
    assert len(tree.expressions) == 1, f"payload created extra projections: {sql}"
    assert tree.expressions[0].name == HOSTILE, (
        f"the header must round-trip as ONE identifier, got "
        f"{tree.expressions[0].name!r}")
    # and no subquery was smuggled in
    assert not list(tree.find_all(sqlglot.exp.Subquery)), f"subquery injected: {sql}"


def test_sample_query_escapes_a_hostile_table_name(monkeypatch):
    sqlglot = pytest.importorskip("sqlglot")
    hostile_table = 't" ; DROP TABLE x; --'
    sql = _sample_sql(monkeypatch, "pg", "amount", table_name=hostile_table)
    assert '""' in sql

    tree = sqlglot.parse_one(sql, read="postgres")
    tables = list(tree.find_all(sqlglot.exp.Table))
    assert len(tables) == 1, f"payload created extra table refs: {sql}"
    assert tables[0].name == hostile_table
    assert not list(tree.find_all(sqlglot.exp.Drop)), f"DROP smuggled in: {sql}"


def test_legacy_unescaped_form_would_have_been_injectable():
    """Pins WHY the fix is needed: the old f"{prefix}{name}{suffix}" form parses
    the same header as multiple projections including a subquery."""
    sqlglot = pytest.importorskip("sqlglot")
    legacy = f'SELECT "{HOSTILE}" FROM "orders" LIMIT 50'
    tree = sqlglot.parse_one(legacy, read="postgres")
    assert len(tree.expressions) > 1, "expected the legacy form to be injectable"
    assert list(tree.find_all(sqlglot.exp.Subquery)), "expected a smuggled subquery"


@pytest.mark.parametrize("ds_type", ["pg", "excel", "mysql", "sqlServer", "oracle", "dm", "ck", "hive"])
def test_sample_query_still_quotes_normal_names_per_dialect(monkeypatch, ds_type):
    """Backward compatibility: a well-formed name is quoted exactly as before."""
    sql = _sample_sql(monkeypatch, ds_type, "amount")
    expected = quote_ident("amount", ds_type)
    assert expected in sql, f"{ds_type}: expected {expected} in {sql}"


def test_sample_query_shape_is_unchanged_for_normal_names(monkeypatch):
    """The dialect-specific row-limit clauses must survive the quoting swap."""
    assert "LIMIT" in _sample_sql(monkeypatch, "pg", "amount")
    assert "TOP" in _sample_sql(monkeypatch, "sqlServer", "amount")
    assert "ROWNUM" in _sample_sql(monkeypatch, "oracle", "amount")
