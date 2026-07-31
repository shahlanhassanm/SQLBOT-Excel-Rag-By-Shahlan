# backend/apps/datasource/row_rag/fallback.py
"""Last-resort semantic row retrieval, used only after the SQL pipeline fails.

Returns a {fields, data} table of grounded rows, or None when nothing clears the
confidence floor (caller then shows the normal 'no match' message). Never invents
content.
"""
import traceback
from collections.abc import Sequence
from typing import Any, Dict, List, Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.row_rag import store
from apps.datasource.row_rag.select import build_fallback_table, filter_by_threshold
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def _parse_content_text(text: str) -> Dict[str, Any]:
    """Inverse of build_row_content_text: 'a: 1 | b: x' -> {'a': '1', 'b': 'x'}."""
    row: Dict[str, Any] = {}
    for part in (text or "").split(" | "):
        if ": " in part:
            k, v = part.split(": ", 1)
            row[k] = v
    return row


def row_rag_fallback(engine, question: str,
                     table_names: Sequence[str] | None = None) -> Optional[Dict[str, Any]]:
    """Semantic row retrieval restricted to ``table_names``.

    ``table_names`` is the set of tables the asking user is allowed to read.
    It is threaded through to ``store.query_topk``, which fails closed on an
    empty set — ``row_embeddings`` is a single global store shared by every
    workspace, so an unrestricted search returns other tenants' rows
    (AUDIT D-01).
    """
    if not settings.ROW_RAG_ENABLED:
        return None
    try:
        model = EmbeddingModelCache.get_model()
        q_emb = model.embed_query(question)
        raw = store.query_topk(engine, q_emb, settings.ROW_RAG_TOP_K, table_names)
        candidates: List[Dict[str, Any]] = [
            {"table_name": r["table_name"], "content_text": r["content_text"],
             "cosine": r["cosine"], "row": _parse_content_text(r["content_text"])}
            for r in raw
        ]
        # Effective floor = the absolute confidence floor, raised to keep only the
        # cluster near the top hit. query_topk returns rows ordered by cosine desc,
        # so raw[0] is the best match. This cuts cross-file noise (a loosely related
        # row from another file that squeaks over the absolute floor).
        floor = settings.ROW_RAG_MIN_COSINE
        if raw:
            floor = max(floor, raw[0]['cosine'] - settings.ROW_RAG_REL_MARGIN)
        kept = filter_by_threshold(candidates, floor)
        SQLBotLogUtil.info(
            f"row_rag fallback: {len(raw)} candidates, "
            f"{len(kept)} >= {round(floor, 4)} (abs floor {settings.ROW_RAG_MIN_COSINE}, "
            f"rel margin {settings.ROW_RAG_REL_MARGIN}); "
            f"top_cosine={raw[0]['cosine'] if raw else 'n/a'}")
        if not kept:
            return None
        return build_fallback_table(kept)
    except Exception:
        traceback.print_exc()
        SQLBotLogUtil.error("row_rag fallback failed (non-fatal)")
        return None
