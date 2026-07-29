# Author: Junjun
# Date: 2025/9/18
import json
import time
import traceback
from typing import Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.chat.task.agentic import tokenize, bm25_scores, rrf_fuse
from apps.datasource.crud.table import build_ds_schema_text, get_ds_table_names
from apps.datasource.embedding.utils import cosine_similarity
from apps.datasource.models.datasource import CoreDatasource
from apps.system.crud.assistant import AssistantOutDs
from common.core.config import settings
from common.core.deps import CurrentAssistant
from common.core.deps import SessionDep, CurrentUser
from common.utils.utils import SQLBotLogUtil


def _hybrid_rerank(question: str, _list: list, lexical_texts: list):
    """Fuse the cosine-similarity ordering with a BM25 lexical ordering via
    Reciprocal Rank Fusion. ``_list`` must already be sorted by
    cosine_similarity (descending) and aligned 1:1 with ``lexical_texts``.
    Falls back to the cosine order on any internal error."""
    if not settings.AGENTIC_HYBRID_RANKING_ENABLED or len(_list) <= 1:
        return _list
    try:
        ids = [item.get('id') for item in _list]
        q_tokens = tokenize(question)
        doc_tokens = [tokenize(t or '') for t in lexical_texts]
        lex_scores = bm25_scores(q_tokens, doc_tokens)
        # Only datasources with an ACTUAL keyword match contribute to the
        # lexical ranking. Otherwise (no overlap -> all scores 0) the sort would
        # fall back to arbitrary input order and pollute RRF, dragging weakly
        # related datasources above a strong embedding match.
        scored = [(s, i) for s, i in zip(lex_scores, ids) if s > 0]
        lexical_order = [i for _, i in sorted(scored, key=lambda p: p[0], reverse=True)]
        rank_lists = [ids]
        if lexical_order:
            rank_lists.append(lexical_order)
        fused = rrf_fuse(rank_lists)
        _list = sorted(_list, key=lambda x: fused.get(x.get('id'), 0.0), reverse=True)
        SQLBotLogUtil.info('hybrid ds ranking: ' + json.dumps(
            [{'id': i, 'rrf': round(fused.get(i, 0.0), 5)} for i in
             [item.get('id') for item in _list]]))
    except Exception:
        traceback.print_exc()
    return _list


def get_ds_embedding(session: SessionDep, current_user: CurrentUser, _ds_list, out_ds: AssistantOutDs,
                     question: str,
                     current_assistant: Optional[CurrentAssistant] = None):
    _list = []
    if current_assistant and current_assistant.type == 1:
        if out_ds.ds_list:
            for _ds in out_ds.ds_list:
                ds = out_ds.get_ds(_ds.id)
                table_schema, tables = out_ds.get_db_schema(_ds.id, question, embedding=False)
                ds_info = f"{ds.name}, {ds.description}\n"
                ds_schema = ds_info + table_schema
                _list.append({"id": ds.id, "ds_schema": ds_schema, "cosine_similarity": 0.0, "ds": ds})

        if _list:
            try:
                text = [s.get('ds_schema') for s in _list]

                model = EmbeddingModelCache.get_model()
                results = model.embed_documents(text)

                q_embedding = model.embed_query(question)
                for index in range(len(results)):
                    item = results[index]
                    _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, item)

                _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
                _list = _hybrid_rerank(question, _list, [s.get('ds_schema') for s in _list])
                # print(len(_list))
                _list = _list[:settings.DS_EMBEDDING_COUNT]
                SQLBotLogUtil.info(json.dumps(
                    [{"id": ele.get("id"), "name": ele.get("ds").name,
                      "cosine_similarity": ele.get("cosine_similarity")}
                     for ele in _list]))
                return [{"id": obj.get('ds').id, "name": obj.get('ds').name,
                         "description": obj.get('ds').description,
                         "cosine": round(float(obj.get('cosine_similarity') or 0.0), 4)}
                        for obj in _list]
            except Exception:
                traceback.print_exc()
    else:
        for _ds in _ds_list:
            if _ds.get('id'):
                ds = session.get(CoreDatasource, _ds.get('id'))
                # table_schema = get_table_schema(session, current_user, ds, question, embedding=False)
                # ds_info = f"{ds.name}, {ds.description}\n"
                # ds_schema = ds_info + table_schema
                _list.append({"id": ds.id, "cosine_similarity": 0.0, "ds": ds, "embedding": ds.embedding})

        if _list:
            try:
                # text = [s.get('ds_schema') for s in _list]

                model = EmbeddingModelCache.get_model()
                start_time = time.time()
                # results = model.embed_documents(text)
                results = [item.get('embedding') for item in _list]

                q_embedding = model.embed_query(question)
                for index in range(len(results)):
                    item = results[index]
                    if item:
                        _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, json.loads(item))

                _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
                if settings.AGENTIC_HYBRID_RANKING_ENABLED:
                    try:
                        lexical_texts = [build_ds_schema_text(session, item.get('ds')) for item in _list]
                        _list = _hybrid_rerank(question, _list, lexical_texts)
                    except Exception:
                        traceback.print_exc()
                # print(len(_list))
                end_time = time.time()
                SQLBotLogUtil.info(str(end_time - start_time))
                _list = _list[:settings.DS_EMBEDDING_COUNT]
                SQLBotLogUtil.info(json.dumps(
                    [{"id": ele.get("id"), "name": ele.get("ds").name,
                      "cosine_similarity": ele.get("cosine_similarity")}
                     for ele in _list]))
                _candidates = []
                for obj in _list:
                    _entry = {"id": obj.get('ds').id, "name": obj.get('ds').name,
                              "description": obj.get('ds').description,
                              "cosine": round(float(obj.get('cosine_similarity') or 0.0), 4)}
                    try:
                        _entry["tables"] = get_ds_table_names(session, obj.get('ds').id)
                    except Exception:
                        pass
                    _candidates.append(_entry)
                return _candidates
            except Exception:
                traceback.print_exc()
    return _list
