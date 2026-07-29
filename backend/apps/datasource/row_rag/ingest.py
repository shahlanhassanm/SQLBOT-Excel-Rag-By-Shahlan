# backend/apps/datasource/row_rag/ingest.py
"""Embed every row of an imported DataFrame and persist to the pgvector store.

Gated by settings.ROW_RAG_ENABLED at the call site. Best-effort: any failure is
logged and swallowed so a RAG hiccup never breaks the core Excel import.
"""
import traceback
from typing import Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.row_rag import store
from apps.datasource.row_rag.text import build_rows_content_text
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def embed_and_store_df(df, table_name: str, engine, datasource_id: Optional[int] = None) -> int:
    """Build content_text for each row, embed in batches, store. Returns count."""
    if not settings.ROW_RAG_ENABLED:
        return 0
    try:
        rows = df.to_dict(orient="records")
        contents = build_rows_content_text(rows, max_chars=settings.ROW_RAG_CONTENT_MAX_CHARS)
        # keep only non-empty content rows, remembering their original ordinal
        pairs = [(i, c) for i, c in enumerate(contents) if c]
        if not pairs:
            return 0
        store.ensure_schema(engine)
        store.delete_table(engine, table_name)  # replace on re-import

        model = EmbeddingModelCache.get_model()
        batch = max(1, int(settings.ROW_RAG_EMBED_BATCH))
        ordinals = [i for i, _ in pairs]
        all_contents = [c for _, c in pairs]
        embeddings = []
        for start in range(0, len(all_contents), batch):
            chunk = all_contents[start:start + batch]
            embeddings.extend(model.embed_documents(chunk))
        # a truncating/buggy endpoint must not silently under-populate the store
        if len(embeddings) != len(all_contents):
            raise ValueError(
                f"embedding count {len(embeddings)} != row count {len(all_contents)}")
        return store.insert_rows(engine, table_name, all_contents, embeddings,
                                 datasource_id, ordinals=ordinals)
    except Exception:
        traceback.print_exc()
        SQLBotLogUtil.error(f"row_rag ingest failed for {table_name} (non-fatal)")
        return 0
