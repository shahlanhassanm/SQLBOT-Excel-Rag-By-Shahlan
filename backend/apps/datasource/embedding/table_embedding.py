# Author: Junjun
# Date: 2025/9/23
import json
import time
import traceback

from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.embedding.utils import cosine_similarity
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def get_table_embedding(tables: list[dict], question: str):
    _list = []
    for table in tables:
        _list.append({"id": table.get('id'), "schema_table": table.get('schema_table'), "cosine_similarity": 0.0})

    if _list:
        try:
            text = [s.get('schema_table') for s in _list]

            model = EmbeddingModelCache.get_model()
            start_time = time.time()
            results = model.embed_documents(text)
            end_time = time.time()
            SQLBotLogUtil.info(str(end_time - start_time))

            q_embedding = model.embed_query(question)
            for index in range(len(results)):
                item = results[index]
                _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, item)

            _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
            _list = _list[:settings.TABLE_EMBEDDING_COUNT]
            # print(len(_list))
            SQLBotLogUtil.info(json.dumps(_list))
            return _list
        except Exception:
            traceback.print_exc()
    return _list


def calc_table_embedding(tables: list[dict], question: str):
    _list = []
    for table in tables:
        _list.append(
            {"id": table.get('id'), "schema_table": table.get('schema_table'), "embedding": table.get('embedding'),
             "cosine_similarity": 0.0, "table_name": table.get('table_name')})

    if _list:
        try:
            # text = [s.get('schema_table') for s in _list]
            #
            model = EmbeddingModelCache.get_model()
            start_time = time.time()
            # results = model.embed_documents(text)
            # end_time = time.time()
            # SQLBotLogUtil.info(str(end_time - start_time))
            results = [item.get('embedding') for item in _list]

            # A table whose embedding was never computed keeps similarity 0.0,
            # so it sorts last and silently disappears from the schema the model
            # sees. That is indistinguishable from "irrelevant" -- say so.
            missing = [item.get('table_name') for item, emb in zip(_list, results) if not emb]
            if missing:
                SQLBotLogUtil.warning(
                    f'table embedding: {len(missing)} table(s) have no stored embedding and '
                    f'cannot be ranked for relevance: {missing[:10]}')

            q_embedding = model.embed_query(question)
            for index in range(len(results)):
                item = results[index]
                if item:
                    _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, json.loads(item))

            _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
            _list = _list[:settings.TABLE_EMBEDDING_COUNT]

            # Similarity floor. The count alone is a fixed cutoff: it ships ten
            # tables whether two are relevant or twenty are, padding the prompt
            # with distractors on narrow questions. Mirrors the floor the fanout
            # ranking already applies (AGENTIC_FANOUT_COSINE_FLOOR). The
            # top-ranked table is always kept so a floor set too high can never
            # empty the schema.
            floor = settings.TABLE_EMBEDDING_COSINE_FLOOR
            if floor > 0 and _list:
                kept = [ele for ele in _list if ele.get('cosine_similarity', 0.0) >= floor]
                if not kept:
                    kept = _list[:1]
                if len(kept) < len(_list):
                    SQLBotLogUtil.info(
                        f'table embedding: floor {floor} dropped '
                        f'{len(_list) - len(kept)} of {len(_list)} table(s)')
                _list = kept

            # print(len(_list))
            end_time = time.time()
            SQLBotLogUtil.info(str(end_time - start_time))
            SQLBotLogUtil.info(json.dumps([{"id": ele.get('id'), "schema_table": ele.get('schema_table'),
                                            "cosine_similarity": ele.get('cosine_similarity'), "table_name": ele.get('table_name')}
                                           for ele in _list]))
            return _list
        except Exception:
            # Falling through used to return the FULL, unranked, untruncated
            # list: an embedding outage quietly turned "the 10 most relevant
            # tables" into "every table in the datasource", blowing the context
            # window with no error surfaced. Truncate to the same budget and say
            # loudly that the ranking is arbitrary.
            traceback.print_exc()
            SQLBotLogUtil.error(
                f'table embedding failed for {len(_list)} table(s); falling back to the '
                f'first {settings.TABLE_EMBEDDING_COUNT} in schema order -- table '
                f'selection for this question is NOT relevance-ranked')
            return _list[:settings.TABLE_EMBEDDING_COUNT]
    return _list
