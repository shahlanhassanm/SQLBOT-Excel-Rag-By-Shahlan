# backend/apps/datasource/row_rag/store.py
"""pgvector-backed per-row embedding store.

Single table ``row_embeddings`` in the bundled Postgres. Keyed on table_name
(globally unique via import hash suffix); datasource_id is nullable because the
CoreDatasource row does not exist yet when tables are imported.
"""
from collections.abc import Sequence
from typing import Any, Dict, List, Optional

from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def _vec_literal(vec: List[float]) -> str:
    # pgvector accepts '[1,2,3]' text; cast with ::vector at the call site.
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def check_embedding_dim(existing_dim: Optional[int], configured_dim: int) -> None:
    """Refuse to use a table whose vector width is not the configured one.

    ROW_RAG_EMBED_DIM is configurable but was never checked against the column
    that already exists. Changing the embedding model without dropping
    row_embeddings makes EVERY insert into vector(N) fail, and the failure was
    swallowed -- row RAG then silently returned nothing forever (AUDIT H-14).
    """
    if existing_dim is None:            # table not created yet: nothing to clash with
        return
    if int(existing_dim) != int(configured_dim):
        raise ValueError(
            f"row_embeddings.embedding is vector({existing_dim}) but "
            f"ROW_RAG_EMBED_DIM is {configured_dim}. Every insert would fail. "
            f"Either set ROW_RAG_EMBED_DIM={existing_dim} or drop the "
            f"row_embeddings table to re-embed at the new width.")


def _existing_embedding_dim(cur: Any) -> Optional[int]:
    cur.execute(
        "SELECT a.atttypmod FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relname = 'row_embeddings' AND a.attname = 'embedding' "
        "AND a.attnum > 0 AND NOT a.attisdropped")
    row = cur.fetchone()
    if not row or row[0] is None or int(row[0]) < 0:
        return None
    return int(row[0])


def ensure_schema(engine) -> None:
    """Idempotently create the extension, table and indexes."""
    dim = int(settings.ROW_RAG_EMBED_DIM)
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        check_embedding_dim(_existing_embedding_dim(cur), dim)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS row_embeddings (
                id            bigserial PRIMARY KEY,
                table_name    text    NOT NULL,
                datasource_id integer,
                row_ordinal   integer NOT NULL,
                content_text  text    NOT NULL,
                embedding     vector({dim}) NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS row_embeddings_tn_idx ON row_embeddings (table_name)")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS row_embeddings_vec_idx
            ON row_embeddings USING ivfflat (embedding vector_cosine_ops)
        """)
        conn.commit()
    except Exception:
        # roll back so a failed DDL doesn't leave the pooled raw_connection in an
        # aborted-transaction state that poisons the next user of the connection.
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def delete_table(engine, table_name: str) -> None:
    """Remove any existing rows for a table (used before re-inserting on re-import)."""
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM row_embeddings WHERE table_name = %s", (table_name,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def insert_rows(engine, table_name: str, contents: List[str],
                embeddings: List[List[float]], datasource_id: Optional[int] = None,
                ordinals: Optional[List[int]] = None) -> int:
    """Insert one row per (content, embedding).

    ``row_ordinal`` is taken from ``ordinals`` (the source-DataFrame row index)
    when provided; otherwise it falls back to the list position. Callers that
    filter out empty rows before embedding should pass the original ordinals so
    the stored handle still points at the right source row.
    """
    if not contents:
        return 0
    conn = engine.raw_connection()
    cur = conn.cursor()
    inserted = 0
    try:
        for i, (content, emb) in enumerate(zip(contents, embeddings)):
            if not emb:
                continue
            row_ordinal = ordinals[i] if ordinals is not None else i
            cur.execute(
                "INSERT INTO row_embeddings "
                "(table_name, datasource_id, row_ordinal, content_text, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                (table_name, datasource_id, row_ordinal, content, _vec_literal(emb)),
            )
            inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    SQLBotLogUtil.info(f"row_rag: stored {inserted} row embeddings for {table_name}")
    return inserted


def query_topk(engine, q_embedding: List[float], top_k: int,
               table_names: Sequence[str] | None) -> List[Dict[str, Any]]:
    """Return up to top_k rows ordered by cosine similarity (descending).

    Each item: {table_name, content_text, cosine}. Returns [] if the table is
    absent.

    ``table_names`` is the set of tables the caller is allowed to read and is
    REQUIRED. ``row_embeddings`` is a single global store shared by every
    datasource and every workspace, and its ``datasource_id`` column is nullable
    and never populated by the importer, so without this filter the search
    returns rows from other tenants' spreadsheets (AUDIT D-01).

    Fails CLOSED: an empty or None allow-list yields no results rather than all
    results, so a caller that cannot determine the permitted set leaks nothing.
    """
    if not q_embedding:
        return []
    if not table_names:
        SQLBotLogUtil.info(
            "row_rag: empty table allow-list, returning no candidates (fail-closed)")
        return []
    allowed = [str(t) for t in table_names]
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.row_embeddings')")
        if cur.fetchone()[0] is None:
            return []
        # The ivfflat index does APPROXIMATE search and with the default
        # ivfflat.probes=1 it silently under-returns (e.g. only 1 of N rows even
        # under LIMIT). This fallback must have reliable recall, so probe every
        # list — Postgres clamps this to the actual list count, giving exact KNN
        # recall at the data scale this feature targets (Excel-sized tables).
        try:
            cur.execute("SET ivfflat.probes = 1000")
        except Exception:
            pass
        qv = _vec_literal(q_embedding)
        cur.execute(
            "SELECT table_name, content_text, 1 - (embedding <=> %s::vector) AS cosine "
            "FROM row_embeddings WHERE table_name = ANY(%s) "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, allowed, qv, int(top_k)),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return [{"table_name": r[0], "content_text": r[1], "cosine": float(r[2])} for r in rows]
