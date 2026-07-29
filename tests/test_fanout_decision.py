"""Does the real model detect fanout for 'list all thriller movies'?

Builds the candidate list exactly like the finder, calls the decompose prompt
with the configured chat model, and prints the routing verdict.

    docker cp tests/test_fanout_decision.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_fanout_decision.py"
"""
import asyncio
import json
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from sqlalchemy import create_engine, text as sqltext
from common.core.config import settings
from apps.chat.task.agentic import parse_decomposition
from apps.datasource.crud.table import get_ds_table_names
from common.utils.embedding_threads import session_maker
from apps.chat.models.chat_model import AiModelQuestion, SystemPromptMessage
from langchain_core.messages import HumanMessage
from apps.ai_model.model_factory import LLMFactory, get_default_config


def main():
    eng = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))
    with eng.connect() as c:
        rows = c.execute(sqltext("select id, name, description from core_datasource order by id")).fetchall()
    session = session_maker()
    cands = []
    for r in rows:
        cands.append({"id": r[0], "name": r[1], "description": r[2],
                      "tables": get_ds_table_names(session, r[0])})
    session_maker.remove()
    names = {r[0]: r[1] for r in rows}
    valid = {r[0] for r in rows}

    cfg = asyncio.new_event_loop().run_until_complete(get_default_config())
    llm = LLMFactory.create_llm(cfg).llm

    for question, primary in [("list all the thriller movies", 11),
                              ("total revenue and expenses", 5)]:
        q = AiModelQuestion(question=question, lang="English")
        msgs = [SystemPromptMessage(q.decompose_sys_question(primary_ds_id=primary)),
                HumanMessage(q.decompose_user_question(json.dumps(cands)))]
        out = ''
        for ch in llm.stream(msgs):
            out += ch.content or ''
        verdict = parse_decomposition(out, valid)
        print(f"\nQ: {question}")
        print(f"   raw: {out.strip()[:200]}")
        print(f"   mode: {verdict['mode']}")
        for s in verdict['subs']:
            print(f"     -> {names.get(s['ds_id'])}: {s['question']}")


if __name__ == '__main__':
    main()
