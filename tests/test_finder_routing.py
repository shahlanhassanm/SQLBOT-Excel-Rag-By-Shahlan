"""Show what the datasource finder actually picks for sample questions.

Replicates the EXACT ranking the deployed code uses in get_ds_embedding:
cosine(question, stored ds.embedding) + BM25(question, schema_text), fused by
Reciprocal Rank Fusion. Prints top-3 per question so we can judge correctness.

    docker cp tests/test_finder_routing.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_finder_routing.py"
"""
import json
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from sqlalchemy import create_engine, text as sqltext
from common.core.config import settings
from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.embedding.utils import cosine_similarity
from apps.chat.task.agentic import tokenize, bm25_scores, rrf_fuse
from apps.datasource.crud.table import build_ds_schema_text
from common.utils.embedding_threads import session_maker
from apps.datasource.models.datasource import CoreDatasource

QUESTIONS = [
    "list all the thriller movies",
    "total payment amount by gateway",
    "which games have the highest player count",
    "show me overdue transactions and finance totals",
    "DuitNow payment gateway details",
    "total revenue and expenses",
]


def main():
    eng = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))
    with eng.connect() as c:
        rows = c.execute(sqltext(
            "select id, name, embedding from core_datasource order by id")).fetchall()
    ds_rows = [(r[0], r[1], r[2]) for r in rows if r[2]]
    ids = [r[0] for r in ds_rows]
    names = {r[0]: r[1] for r in ds_rows}
    embs = {r[0]: json.loads(r[2]) for r in ds_rows}

    # lexical schema text per datasource (same source the finder uses for BM25)
    session = session_maker()
    schema_texts = {}
    for ds_id in ids:
        ds = session.query(CoreDatasource).filter(CoreDatasource.id == ds_id).first()
        schema_texts[ds_id] = build_ds_schema_text(session, ds)
    session_maker.remove()
    doc_tokens = [tokenize(schema_texts[i]) for i in ids]

    model = EmbeddingModelCache.get_model()
    print(f"Datasources: {[names[i] for i in ids]}\n")

    for q in QUESTIONS:
        qv = model.embed_query(q)
        cos = {i: cosine_similarity(qv, embs[i]) for i in ids}
        cos_order = sorted(ids, key=lambda i: cos[i], reverse=True)
        lex = bm25_scores(tokenize(q), doc_tokens)
        lex_by_id = {ids[k]: lex[k] for k in range(len(ids))}
        lex_order = sorted(ids, key=lambda i: lex_by_id[i], reverse=True)
        fused = rrf_fuse([cos_order, lex_order])
        final = sorted(ids, key=lambda i: fused.get(i, 0.0), reverse=True)

        top3 = [(names[i], round(cos[i], 3), round(lex_by_id[i], 2)) for i in final[:3]]
        print(f"Q: {q}")
        print(f"   PICK -> {top3[0][0]}")
        print(f"   top3 (name, cosine, bm25): {top3}")
        print(f"   cosine-only top: {names[cos_order[0]]} | bm25-only top: {names[lex_order[0]]}")
        print()


if __name__ == '__main__':
    main()
