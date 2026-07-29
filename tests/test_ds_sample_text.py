"""Run in-container:
    docker cp tests/test_ds_sample_text.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_sample_text.py -q"
"""
from apps.datasource.crud.table import build_ds_sample_text


class _DS:
    type = 'pg'


def test_distinct_values_and_dedup():
    def fake_exec(ds, sql):
        return {'fields': ['title', 'genre'],
                'data': [{'title': 'A', 'genre': 'X'},
                         {'title': 'B', 'genre': 'X'},
                         {'title': 'A', 'genre': 'Y'}]}
    out = build_ds_sample_text(_DS(), ['movies'], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert 'movies' in out
    assert '"A"' in out and '"B"' in out
    assert out.count('"X"') == 1
    assert '"Y"' in out


def test_truncates_long_values_and_caps_per_column():
    long = "z" * 500
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': long}] + [{'c': f'v{i}'} for i in range(20)]}
    out = build_ds_sample_text(_DS(), ['t'], fake_exec,
                               n_rows=50, values_per_col=3, value_maxlen=10,
                               total_budget=4000)
    assert ('z' * 10) in out and ('z' * 11) not in out
    assert out.count('"v') <= 2


def test_total_budget_cap():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': f'value{i}'} for i in range(50)]}
    out = build_ds_sample_text(_DS(), [f't{i}' for i in range(10)], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=80)
    assert 0 < len(out) <= 80   # produced output, but capped


def test_none_and_whitespace_values_skipped():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': None}, {'c': '   '}, {'c': 'real'}]}
    out = build_ds_sample_text(_DS(), ['t'], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert '"real"' in out
    assert out.count('"') == 2   # only the one real value is quoted


def test_skips_table_on_exec_error():
    def boom(ds, sql):
        raise RuntimeError('no such table')
    out = build_ds_sample_text(_DS(), ['t'], boom,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert out == ''


def test_empty_table_yields_nothing():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': []}
    out = build_ds_sample_text(_DS(), ['t'], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert out == ''
