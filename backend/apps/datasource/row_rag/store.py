# backend/apps/datasource/row_rag/store.py
"""pgvector-backed per-row embedding store.

Single table ``row_embeddings`` in the bundled Postgres. Keyed on table_name
(globally unique via import hash suffix); datasource_id is nullable because the
CoreDatasource row does not exist yet when tables are imported.
"""
from typing import Any, Dict, List, Optional

from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def _vec_literal(vec: List[float]) -> str:
    # pgvector accepts '[1,2,3]' text; cast with ::vector at the call site.
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def ensure_schema(engine) -> None:
    """Idempotently create the extension, table and indexes."""
    dim = int(settings.ROW_RAG_EMBED_DIM)
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
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


def query_topk(engine, q_embedding: List[float], top_k: int) -> List[Dict[str, Any]]:
    """Return up to top_k rows ordered by cosine similarity (descending).
    Each item: {table_name, content_text, cosine}. Returns [] if table absent."""
    if not q_embedding:
        return []
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
            "FROM row_embeddings ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, qv, int(top_k)),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return [{"table_name": r[0], "content_text": r[1], "cosine": float(r[2])} for r in rows]
