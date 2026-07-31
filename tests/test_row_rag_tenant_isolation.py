"""Tenant-isolation tests for the row-RAG fallback (AUDIT D-01).

``row_embeddings`` is a SINGLE global table shared by every datasource and every
workspace, and its ``datasource_id`` column is nullable and never populated by
the importer (verified on the live instance: 545 of 545 rows NULL). Before this
fix ``store.query_topk`` ran::

    SELECT ... FROM row_embeddings ORDER BY embedding <=> %s LIMIT %s

with no WHERE clause at all, so a question asked in workspace A could surface —
and persist into A's chat record — rows from workspace B's spreadsheets.

These tests drive ``query_topk`` against a stub connection, so they assert the
SQL that is actually emitted without needing pgvector, an embedding model or a
live database.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_row_rag_tenant_isolation.py -q"
"""

import pytest

from apps.datasource.row_rag import store

EMBEDDING = [0.1, 0.2, 0.3]


class _Cursor:
    """Records every statement executed; answers the to_regclass probe."""

    def __init__(self, recorder):
        self.recorder = recorder
        self._last = None

    def execute(self, sql, params=None):
        self.recorder.append((sql, params))
        self._last = sql

    def fetchone(self):
        # the to_regclass('public.row_embeddings') existence probe
        return ("row_embeddings",)

    def fetchall(self):
        return [("t_allowed", "Title: A | Plot: B", 0.91)]

    def close(self):
        pass


class _Conn:
    def __init__(self, recorder):
        self.recorder = recorder

    def cursor(self):
        return _Cursor(self.recorder)

    def close(self):
        pass


class _Engine:
    def __init__(self):
        self.statements = []

    def raw_connection(self):
        return _Conn(self.statements)


def _search_statement(engine):
    """The one statement that actually queries row_embeddings for candidates."""
    for sql, params in engine.statements:
        if "ORDER BY embedding" in sql:
            return sql, params
    return None, None


# --- the filter must be present and must carry the allow-list --------------

def test_search_is_restricted_to_the_allowed_tables():
    engine = _Engine()
    store.query_topk(engine, EMBEDDING, 10, ["t_allowed", "t_also_allowed"])

    sql, params = _search_statement(engine)
    assert sql is not None, "no candidate search was issued"
    assert "WHERE table_name = ANY(" in sql, (
        "row_embeddings must be filtered by the caller's readable tables (D-01); "
        f"got: {sql}")
    assert ["t_allowed", "t_also_allowed"] in params, (
        f"the allow-list must be bound as a parameter; got params={params}")


def test_allow_list_is_bound_not_interpolated():
    """The allow-list contains user-influenced table names, so it must be a bound
    parameter, never string-formatted into the SQL."""
    engine = _Engine()
    store.query_topk(engine, EMBEDDING, 10, ["t'; DROP TABLE row_embeddings; --"])
    sql, params = _search_statement(engine)
    assert "DROP TABLE" not in sql
    assert any("DROP TABLE" in str(p) for p in params)


# --- fail closed -----------------------------------------------------------

@pytest.mark.parametrize("empty", [None, [], ()])
def test_empty_allow_list_returns_nothing_and_queries_nothing(empty):
    """Fail CLOSED. A caller that cannot determine the permitted set must leak
    nothing — the pre-fix behaviour of returning every tenant's rows is exactly
    what this guards."""
    engine = _Engine()
    assert store.query_topk(engine, EMBEDDING, 10, empty) == []
    sql, _ = _search_statement(engine)
    assert sql is None, "no query should be issued for an empty allow-list"


def test_empty_embedding_still_returns_nothing():
    engine = _Engine()
    assert store.query_topk(engine, [], 10, ["t_allowed"]) == []


# --- the fallback threads the allow-list through ---------------------------

def test_fallback_passes_allow_list_to_query_topk(monkeypatch):
    from apps.datasource.row_rag import fallback
    from common.core.config import settings

    monkeypatch.setattr(settings, "ROW_RAG_ENABLED", True)

    class _Model:
        def embed_query(self, _q):
            return EMBEDDING

    monkeypatch.setattr(fallback.EmbeddingModelCache, "get_model",
                        staticmethod(lambda *a, **k: _Model()))

    seen = {}

    def _fake_topk(engine, emb, top_k, table_names):
        seen["table_names"] = table_names
        return []

    monkeypatch.setattr(fallback.store, "query_topk", _fake_topk)

    fallback.row_rag_fallback(object(), "any question", ["t_allowed"])
    assert seen["table_names"] == ["t_allowed"], (
        "row_rag_fallback must forward the caller's allow-list to query_topk")


def test_fallback_defaults_to_no_allow_list_and_therefore_no_rows(monkeypatch):
    """The default argument is None, which query_topk treats as fail-closed. A
    caller that forgets to pass the allow-list gets nothing, not everything."""
    from apps.datasource.row_rag import fallback
    from common.core.config import settings

    monkeypatch.setattr(settings, "ROW_RAG_ENABLED", True)

    class _Model:
        def embed_query(self, _q):
            return EMBEDDING

    monkeypatch.setattr(fallback.EmbeddingModelCache, "get_model",
                        staticmethod(lambda *a, **k: _Model()))

    engine = _Engine()
    assert fallback.row_rag_fallback(engine, "any question") is None
    assert _search_statement(engine) == (None, None)
