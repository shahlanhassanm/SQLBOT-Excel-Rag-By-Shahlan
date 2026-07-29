"""Run in-container:
    docker cp tests/test_fts_addendum.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_fts_addendum.py -q"
"""
from apps.chat.task.apex_helpers import fts_prompt_addendum


def test_pg_family_enabled_returns_guidance():
    for t in ('excel', 'pg', 'kingbase', 'PG', 'Excel', '  pg  '):  # incl. whitespace-padded
        out = fts_prompt_addendum(t, True)
        assert 'to_tsvector' in out and 'plainto_tsquery' in out


def test_non_pg_returns_empty():
    for t in ('mysql', 'oracle', 'sqlserver', 'doris', '', None):
        assert fts_prompt_addendum(t, True) == ''


def test_disabled_returns_empty():
    for t in ('pg', 'excel', 'kingbase', 'mysql'):  # enabled-gate fires before family check
        assert fts_prompt_addendum(t, False) == ''
