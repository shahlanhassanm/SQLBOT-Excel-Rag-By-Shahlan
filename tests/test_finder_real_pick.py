"""Show the REAL routing decision end-to-end:
1. hybrid ranking (fixed fusion: lexical only counts real keyword matches)
2. the actual LLM picker (configured chat model) choosing among candidates

    docker cp tests/test_finder_real_pick.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_finder_real_pick.py"
"""
import json
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from sqlalchemy import create_engine, text as sqltext
from common.core.config import settings
from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.embedding.utils import cosine_similarity
from apps.chat.task.agentic import tokenize, bm25_scores, rrf_fuse
from apps.datasource.crud.table import build_ds_schema_text, get_ds_table_names
from common.utils.embedding_threads import session_maker
from apps.datasource.models.datasource import CoreDatasource

QUESTIONS = [
    ("list all the thriller movies", "thriller_2013_movies"),
    ("which games have the highest player count", "game"),
    ("DuitNow payment gateway details", "DuitNow_Payment_Gateways"),
    ("total revenue and expenses", "finance"),
]


def hybrid_rank(question, ids, embs, doc_tokens):
    qv = EmbeddingModelCache.get_model().embed_query(question)
    cos = {i: cosine_similarity(qv, embs[i]) for i in ids}
    cos_order = sorted(ids, key=lambda i: cos[i], reverse=True)
    lex = bm25_scores(tokenize(question), doc_tokens)
    lex_by_id = {ids[k]: lex[k] for k in range(len(ids))}
    scored = [(lex_by_id[i], i) for i in ids if lex_by_id[i] > 0]
    lex_order = [i for _, i in sorted(scored, key=lambda p: p[0], reverse=True)]
    rank_lists = [cos_order] + ([lex_order] if lex_order else [])
    fused = rrf_fuse(rank_lists)
    return sorted(ids, key=lambda i: fused.get(i, 0.0), reverse=True)


def llm_pick(question, candidates):
    """Call the configured chat model exactly like select_datasource does."""
    from apps.ai_model.model_factory import LLMFactory, get_default_config
    from apps.chat.models.chat_model import AiModelQuestion, SystemPromptMessage
    from langchain_core.messages import HumanMessage
    import asyncio
    cfg = asyncio.new_event_loop().run_until_complete(get_default_config())
    llm = LLMFactory.create_llm(cfg).llm
    q = AiModelQuestion(question=question, lang="English")
    msgs = [SystemPromptMessage(q.datasource_sys_question()),
            HumanMessage(q.datasource_user_question(json.dumps(candidates)))]
    out = ''
    for ch in llm.stream(msgs):
        out += ch.content or ''
    return out.strip()


def main():
    eng = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))
    with eng.connect() as c:
        rows = c.execute(sqltext("select id, name, description, embedding from core_datasource order by id")).fetchall()
    data = [(r[0], r[1], r[2], r[3]) for r in rows if r[3]]
    ids = [r[0] for r in data]
    names = {r[0]: r[1] for r in data}
    embs = {r[0]: json.loads(r[3]) for r in data}

    session = session_maker()
    schema_texts = {i: build_ds_schema_text(session, session.query(CoreDatasource).get(i)) for i in ids}
    tables = {i: get_ds_table_names(session, i) for i in ids}
    descs = {r[0]: r[2] for r in data}
    session_maker.remove()
    doc_tokens = [tokenize(schema_texts[i]) for i in ids]

    correct_rank = 0
    correct_llm = 0
    for q, expected in QUESTIONS:
        ranked = hybrid_rank(q, ids, embs, doc_tokens)
        rank_top = names[ranked[0]]
        # build candidate payload exactly like get_ds_embedding returns (top 10)
        cands = [{"id": i, "name": names[i], "description": descs[i], "tables": tables[i]}
                 for i in ranked[:10]]
        try:
            pick_raw = llm_pick(q, cands)
        except Exception as e:
            pick_raw = f"(llm error: {e})"
        # extract id from the model's JSON answer
        from common.utils.utils import extract_nested_json
        pid = None
        js = extract_nested_json(pick_raw)
        if js:
            try:
                pid = json.loads(js).get('id')
            except Exception:
                pass
        llm_name = names.get(pid, f"?({pick_raw[:40]})")
        rk = "OK" if rank_top == expected else "XX"
        lk = "OK" if llm_name == expected else "XX"
        correct_rank += rank_top == expected
        correct_llm += llm_name == expected
        print(f"Q: {q}")
        print(f"   expected:      {expected}")
        print(f"   ranking top1:  {rank_top}   [{rk}]")
        print(f"   LLM picker:    {llm_name}   [{lk}]")
        print()
    print(f"ranking correct: {correct_rank}/{len(QUESTIONS)} | LLM picker correct: {correct_llm}/{len(QUESTIONS)}")


if __name__ == '__main__':
    main()
