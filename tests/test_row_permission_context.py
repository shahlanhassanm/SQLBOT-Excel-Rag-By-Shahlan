"""Row-level permissions must apply to prompt context, not just generated SQL.

AUDIT D-02. Row rules were enforced only as an LLM rewrite of the SQL the model
produced (LLMService.generate_filter). Two other paths query the datasource
directly and put raw cell values into the prompt AND the UI execution log:

  get_table_sample_data()   up to TABLE_SAMPLE_PROBE_ROWS raw rows per table
  value_index probes        up to VALUE_LINKING_DISTINCT_LIMIT distinct values

Neither applied the row filter, so a user restricted to their own region saw
every region's rows in the sample block. Column permissions WERE applied, which
is what made the omission easy to miss.

These tests assert the emitted SQL. They are pure: no database, no model.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_row_permission_context.py -q"
"""

import pytest

from apps.datasource.value_index import _filter_key, build_distinct_sql

FILTER = "region = 'EMEA'"


# --- value linking probes (value_index.build_distinct_sql) -----------------

@pytest.mark.parametrize("ds_type", ["pg", "excel", "mysql", "sqlserver", "oracle", "ck"])
def test_distinct_probe_applies_the_row_filter(ds_type):
    sql = build_distinct_sql(ds_type, '"t"', '"c"', 200, FILTER)
    assert FILTER in sql, f"row filter missing for {ds_type}: {sql}"
    assert "IS NOT NULL AND (" in sql, (
        f"filter must be ANDed onto the existing predicate, not replace it: {sql}")


@pytest.mark.parametrize("ds_type", ["pg", "sqlserver", "oracle"])
def test_distinct_probe_without_filter_is_unchanged(ds_type):
    """Backward compatibility: callers with no row rules emit the original SQL."""
    before = build_distinct_sql(ds_type, '"t"', '"c"', 200)
    assert "AND (" not in before
    assert before == build_distinct_sql(ds_type, '"t"', '"c"', 200, None)
    assert before == build_distinct_sql(ds_type, '"t"', '"c"', 200, "   ")


def test_distinct_probe_keeps_dialect_row_limit_with_a_filter():
    assert "TOP 200" in build_distinct_sql("sqlserver", '"t"', '"c"', 200, FILTER)
    assert "FETCH FIRST 200 ROWS ONLY" in build_distinct_sql("oracle", '"t"', '"c"', 200, FILTER)
    assert "LIMIT 200" in build_distinct_sql("pg", '"t"', '"c"', 200, FILTER)


# --- the value cache must not serve one user's values to another ----------

def test_cache_key_separates_different_filters():
    """AUDIT D-13: without the filter in the key, the first (privileged) caller
    populates the cache and restricted callers are served their values."""
    assert _filter_key("orders", None) == "orders"
    assert _filter_key("orders", FILTER) != _filter_key("orders", None)
    assert _filter_key("orders", FILTER) != _filter_key("orders", "region = 'APAC'")
    # stable for the same filter, so caching still works
    assert _filter_key("orders", FILTER) == _filter_key("orders", FILTER)


# --- table sampling (crud/datasource.get_table_sample_data) ---------------

class _DS:
    def __init__(self, ds_type):
        self.type = ds_type
        self.id = 1


class _Field:
    def __init__(self, name):
        self.field_name = name


def _capture_sql(monkeypatch, ds_type, row_filter):
    """Run get_table_sample_data with exec_sql stubbed, return the SQL issued."""
    from apps.datasource.crud import datasource as ds_mod

    seen = {}

    def _fake_exec(ds, sql, origin_column=False):
        seen["sql"] = sql
        return {"fields": ["a"], "data": [{"a": "v"}]}

    monkeypatch.setattr(ds_mod, "exec_sql", _fake_exec)
    ds_mod.get_table_sample_data(_DS(ds_type), "orders", [_Field("a")], row_filter)
    return seen.get("sql", "")


@pytest.mark.parametrize("ds_type", ["pg", "excel", "mysql", "sqlServer", "ck", "hive"])
def test_sample_query_applies_the_row_filter(monkeypatch, ds_type):
    sql = _capture_sql(monkeypatch, ds_type, FILTER)
    assert FILTER in sql, f"row filter missing for {ds_type}: {sql}"
    assert " WHERE (" in sql


@pytest.mark.parametrize("ds_type", ["oracle", "dm"])
def test_sample_query_ands_the_filter_onto_rownum(monkeypatch, ds_type):
    """Oracle/DM already carry `WHERE ROWNUM <= n`; a second WHERE would be a
    syntax error, so the filter must be ANDed."""
    sql = _capture_sql(monkeypatch, ds_type, FILTER)
    assert "WHERE ROWNUM" in sql
    assert f"AND ({FILTER})" in sql
    assert sql.count("WHERE") == 1


@pytest.mark.parametrize("ds_type", ["pg", "sqlServer", "oracle", "ck"])
def test_sample_query_without_filter_is_unchanged(monkeypatch, ds_type):
    """Backward compatibility: an unrestricted caller gets the original SQL."""
    assert _capture_sql(monkeypatch, ds_type, None) == _capture_sql(monkeypatch, ds_type, "")


def test_sample_query_keeps_the_row_limit_with_a_filter(monkeypatch):
    assert "TOP 50" in _capture_sql(monkeypatch, "sqlServer", FILTER)
    assert "LIMIT 50" in _capture_sql(monkeypatch, "pg", FILTER)
