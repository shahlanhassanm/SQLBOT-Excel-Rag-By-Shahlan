"""Tests for question->table selection (apps/datasource/embedding/table_embedding.py).

This is the function that decides which tables the model is allowed to see, so
its edge cases matter more than most. Added 2026-07-28 alongside two fixes:

  * a similarity floor, because TABLE_EMBEDDING_COUNT alone is a fixed cutoff
    that ships ten tables whether two are relevant or twenty are;
  * a bounded failure path, because the old bare `except` returned the FULL
    unranked list -- an embedding outage silently turned "the 10 most relevant
    tables" into "every table in the datasource".

No database and no embedding model: the model cache and the similarity function
are both replaced.
"""
import json
import sys

import pytest

sys.path.insert(0, '/opt/sqlbot/app')

from common.core.config import settings  # noqa: E402
import apps.datasource.embedding.table_embedding as te  # noqa: E402


def _tables(n):
    return [{'id': i, 'schema_table': f'# Table: t{i}\n[\n(c:text)\n]\n',
             'embedding': json.dumps([1.0, 0.0]), 'table_name': f't{i}'}
            for i in range(n)]


class _Model:
    def __init__(self, fail=False):
        self.fail = fail

    def embed_query(self, _question):
        if self.fail:
            raise RuntimeError('embedding backend unavailable')
        return [1.0, 0.0]


@pytest.fixture
def rig(monkeypatch):
    """Similarity descends 1.0, 0.9, 0.8 ... by schema position."""
    state = {'n': 0}

    def _sim(_a, _b):
        score = 1.0 - 0.1 * state['n']
        state['n'] += 1
        return round(score, 4)

    monkeypatch.setattr(te, 'cosine_similarity', _sim)
    monkeypatch.setattr(te.EmbeddingModelCache, 'get_model',
                        staticmethod(lambda: _Model()), raising=False)
    monkeypatch.setattr(settings, 'TABLE_EMBEDDING_COUNT', 10)
    monkeypatch.setattr(settings, 'TABLE_EMBEDDING_COSINE_FLOOR', 0.0)
    return state


def test_floor_disabled_keeps_exactly_the_count(rig):
    """Default is unchanged from the pre-floor behaviour."""
    out = te.calc_table_embedding(_tables(12), 'q')
    assert len(out) == 10


def test_floor_drops_tables_below_it(rig, monkeypatch):
    monkeypatch.setattr(settings, 'TABLE_EMBEDDING_COSINE_FLOOR', 0.6)
    out = te.calc_table_embedding(_tables(12), 'q')
    assert len(out) == 5
    assert all(t['cosine_similarity'] >= 0.6 for t in out)


def test_floor_never_empties_the_schema(rig, monkeypatch):
    """A floor set above every score must still yield the best table."""
    monkeypatch.setattr(settings, 'TABLE_EMBEDDING_COSINE_FLOOR', 0.99)
    out = te.calc_table_embedding(_tables(12), 'q')
    assert len(out) == 1
    assert out[0]['cosine_similarity'] == 1.0


def test_results_are_ranked_best_first(rig):
    out = te.calc_table_embedding(_tables(12), 'q')
    scores = [t['cosine_similarity'] for t in out]
    assert scores == sorted(scores, reverse=True)


def test_embedding_failure_is_bounded_not_unbounded(rig, monkeypatch):
    """The regression this guards: returning all 12 would blow the context."""
    monkeypatch.setattr(te.EmbeddingModelCache, 'get_model',
                        staticmethod(lambda: _Model(fail=True)), raising=False)
    out = te.calc_table_embedding(_tables(12), 'q')
    assert len(out) == settings.TABLE_EMBEDDING_COUNT


def test_table_without_an_embedding_does_not_crash(rig):
    tables = _tables(3)
    tables[1]['embedding'] = None
    out = te.calc_table_embedding(tables, 'q')
    assert len(out) == 3
    # it keeps 0.0 and therefore sorts last
    assert out[-1]['table_name'] == 't1'


def test_empty_input(rig):
    assert te.calc_table_embedding([], 'q') == []
