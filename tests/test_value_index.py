"""Unit tests for apps/datasource/value_index.py — the DB-facing half of value
linking. ``exec_fn`` is injected, so these run without a real database, but the
module imports settings, so this file is container-only.

Run inside the container:
    docker cp tests/test_value_index.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_value_index.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from apps.datasource.value_index import (
    build_distinct_sql,
    build_table_field_specs,
    clear_cache,
    collect_column_values,
    fetch_distinct_values,
)


class _DS:
    def __init__(self, ds_type='excel', ds_id=1):
        self.type = ds_type
        self.id = ds_id


# --- dialect-correct row limiting -------------------------------------------
def test_limit_clause_default_is_limit():
    sql = build_distinct_sql('excel', '"t"', '"c"', 200)
    assert sql.endswith('LIMIT 200')


def test_oracle_uses_fetch_first():
    sql = build_distinct_sql('oracle', '"t"', '"c"', 50)
    assert 'FETCH FIRST 50 ROWS ONLY' in sql
    assert 'LIMIT' not in sql


def test_sqlserver_uses_top():
    sql = build_distinct_sql('mssql', '"t"', '"c"', 25)
    assert 'SELECT DISTINCT TOP 25' in sql
    assert 'LIMIT' not in sql


def test_distinct_sql_always_filters_nulls():
    for ds_type in ('excel', 'oracle', 'mssql', 'mysql'):
        assert 'IS NOT NULL' in build_distinct_sql(ds_type, '"t"', '"c"', 10)


# --- value fetching ---------------------------------------------------------
def test_fetch_distinct_values_unwraps_rows():
    def fake_exec(ds, sql):
        return {'fields': ['status'], 'data': [{'status': 'Paid'}, {'status': 'Overdue'}]}

    assert fetch_distinct_values(_DS(), 't', 'status', 200, fake_exec) == ['Paid', 'Overdue']


def test_fetch_distinct_values_skips_blanks_and_nulls():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': 'x'}, {'c': None}, {'c': '   '}, {'c': 'y'}]}

    assert fetch_distinct_values(_DS(), 't', 'c', 200, fake_exec) == ['x', 'y']


def test_fetch_distinct_values_handles_empty_result():
    assert fetch_distinct_values(_DS(), 't', 'c', 200, lambda ds, sql: {'fields': [], 'data': []}) == []


# --- collection, caching, failure isolation ---------------------------------
def test_collect_skips_non_text_columns():
    clear_cache()
    seen = []

    def fake_exec(ds, sql):
        seen.append(sql)
        return {'fields': ['c'], 'data': [{'c': 'v'}]}

    specs = [{'table_name': 't', 'fields': [
        {'name': 'amount', 'type': 'numeric'},
        {'name': 'status', 'type': 'varchar'},
    ]}]
    out = collect_column_values(_DS(), specs, fake_exec)
    assert list(out.keys()) == [('t', 'status')]
    assert len(seen) == 1


def test_collect_isolates_a_failing_column():
    """One unprobeable column must not lose the others."""
    clear_cache()

    def fake_exec(ds, sql):
        if 'bad' in sql:
            raise RuntimeError('column exploded')
        return {'fields': ['c'], 'data': [{'c': 'v'}]}

    specs = [{'table_name': 't', 'fields': [
        {'name': 'bad', 'type': 'text'},
        {'name': 'good', 'type': 'text'},
    ]}]
    out = collect_column_values(_DS(), specs, fake_exec)
    assert ('t', 'good') in out
    assert ('t', 'bad') not in out


def test_collect_caches_between_calls():
    clear_cache()
    calls = []

    def fake_exec(ds, sql):
        calls.append(sql)
        return {'fields': ['c'], 'data': [{'c': 'v'}]}

    specs = [{'table_name': 't', 'fields': [{'name': 'c', 'type': 'text'}]}]
    collect_column_values(_DS(), specs, fake_exec)
    collect_column_values(_DS(), specs, fake_exec)
    assert len(calls) == 1  # second call served from cache


def test_collect_returns_empty_for_no_text_columns():
    clear_cache()
    specs = [{'table_name': 't', 'fields': [{'name': 'n', 'type': 'int'}]}]
    assert collect_column_values(_DS(), specs, lambda ds, sql: {}) == {}


# --- ORM adaptation ---------------------------------------------------------
class _Field:
    def __init__(self, name, ftype):
        self.field_name = name
        self.field_type = ftype


class _Table:
    def __init__(self, name):
        self.table_name = name


class _TableObj:
    def __init__(self, name, fields):
        self.table = _Table(name)
        self.fields = fields


def test_build_table_field_specs_maps_orm_objects():
    objs = [_TableObj('t1', [_Field('a', 'varchar'), _Field('b', 'numeric')])]
    specs = build_table_field_specs(objs)
    assert specs == [{'table_name': 't1', 'fields': [
        {'name': 'a', 'type': 'varchar'}, {'name': 'b', 'type': 'numeric'}]}]


def test_build_table_field_specs_honours_pruned_table_list():
    objs = [_TableObj('keep', [_Field('a', 'varchar')]),
            _TableObj('drop', [_Field('b', 'varchar')])]
    specs = build_table_field_specs(objs, table_names=['keep'])
    assert [s['table_name'] for s in specs] == ['keep']


def test_build_table_field_specs_skips_fieldless_tables():
    assert build_table_field_specs([_TableObj('t', [])]) == []
