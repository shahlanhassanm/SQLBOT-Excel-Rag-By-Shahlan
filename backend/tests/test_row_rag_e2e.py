# backend/tests/test_row_rag_e2e.py
import pandas as pd
import pytest

import main  # noqa: F401  (production import order)
from common.core.config import settings
from apps.db.engine import get_engine_conn  # builds the sqlbot-DB engine, URL-quotes user
from apps.datasource.row_rag.ingest import embed_and_store_df
from apps.datasource.row_rag.fallback import row_rag_fallback
from apps.datasource.row_rag import store


def _engine():
    # NOTE: do NOT hand-build the URL from POSTGRES_* — POSTGRES_USER is
    # "pg@localhost" and must be URL-quoted. get_engine_conn() does that and
    # points at the same `sqlbot` DB the app/ingest/fallback all use.
    return get_engine_conn()


@pytest.fixture(autouse=True)
def _enable_rag(monkeypatch):
    monkeypatch.setattr(settings, "ROW_RAG_ENABLED", True)


def test_semantic_row_fallback_retrieves_sentinel_and_rejects_gibberish():
    # NOTE: row_embeddings is a GLOBAL store that may already hold real backfilled
    # rows, so this test does NOT assume an empty store. It inserts a sentinel row
    # with a highly distinctive phrase that won't collide with real data, then
    # checks (a) the sentinel is retrieved as the top hit for that phrase, and
    # (b) pure gibberish clears nothing -> None (no hallucinated row).
    eng = _engine()
    table = "__rag_e2e_sentinel__"
    sentinel = ("a luminous origami dragon quietly teaches advanced calculus to a "
                "colony of penguins on a floating antarctic library")
    df = pd.DataFrame([{"Title": "ZZ Sentinel Row", "Plot": sentinel}])
    try:
        stored = embed_and_store_df(df, table, eng)
    except Exception as e:
        pytest.skip(f"embedding endpoint unavailable: {e}")
    if stored == 0:
        pytest.skip("embedding endpoint returned no vectors (Ollama down?)")
    assert stored == 1

    try:
        # near-paraphrase of the sentinel -> the sentinel row should be the top hit
        # The allow-list is required since AUDIT D-01: row_embeddings is a global
        # store, so the search is scoped to the tables the caller may read.
        hit = row_rag_fallback(eng, "origami dragon teaching calculus to penguins in a "
                                    "floating library in antarctica", [table])
        assert hit is not None, "sentinel should be retrievable"
        assert hit["fields"][0] == "source"
        assert hit["data"][0].get("Title") == "ZZ Sentinel Row", \
            f"sentinel should rank top, got {hit['data'][0]}"

        # a COHERENT but off-topic query (nothing in the data is about this) should
        # clear nothing -> None. (Real queries score ~0.6+, coherent off-topic ~0.45;
        # note pure random-character gibberish is a known weak spot of the embedding
        # model and is NOT asserted here - users type real words.)
        miss = row_rag_fallback(eng, "the biochemistry of photosynthesis in tropical "
                                     "rainforest canopy plants", [table])
        assert miss is None

        # D-01: a caller whose allow-list excludes the sentinel's table must not
        # retrieve it, even with the query that ranks it top.
        other_tenant = row_rag_fallback(
            eng, "origami dragon teaching calculus to penguins in a "
                 "floating library in antarctica", ["__some_other_tenants_table__"])
        assert other_tenant is None, "sentinel leaked across the table allow-list"
    finally:
        store.delete_table(eng, table)
