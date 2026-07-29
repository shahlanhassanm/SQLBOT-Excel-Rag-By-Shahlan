"""Dynamic NL2SQL eval harness.

For every datasource in the DB it:
  1. reads the live schema (no hardcoded tables/columns),
  2. asks the default LLM to invent N natural-language questions FROM THAT SCHEMA
     (so the questions are document-specific yet generated dynamically), and
  3. runs each question through the real SQL-generation pipeline and records
     whether it produced valid, executable SQL.

Nothing here is domain-specific — it works for whatever files are loaded.

Run inside the container, e.g.:
    docker cp backend/tests/eval_harness.py sqlbot:/tmp/eval_harness.py
    # smoke (fast): 2 datasources x 2 questions, APEX off
    docker exec -e SMOKE_DS=2 -e SMOKE_Q=2 -e EVAL_APEX=off sqlbot \
        sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/eval_harness.py"
    # full sweep: every datasource x 5 questions (hours on the local 14B)
    docker exec -e SMOKE_DS=0 -e SMOKE_Q=5 -e EVAL_APEX=on sqlbot \
        sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/eval_harness.py"
"""
import os
import sys
import json
import random
import asyncio
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/opt/sqlbot/app")

from sqlmodel import Session, select

from common.core.db import engine
from common.core.config import settings
from apps.datasource.models.datasource import CoreDatasource
from apps.datasource.crud.table import build_ds_schema_text
from apps.ai_model.model_factory import invoke_default_llm
from apps.chat.task.llm import LLMService
from apps.chat.models.chat_model import CreateChat, ChatQuestion, ChatFinishStep
from apps.chat.curd.chat import create_chat
from apps.system.schemas.system_schema import UserInfoDTO
from apps.db.db import exec_sql

MAX_DS = int(os.environ.get("SMOKE_DS", "2"))      # 0 = all datasources
N_Q = int(os.environ.get("SMOKE_Q", "2"))          # questions per datasource
ONLY_IDS = [int(x) for x in os.environ.get("EVAL_DS_IDS", "").split(",") if x.strip()]
# Force specific questions instead of generating them (e.g. to re-test a known case).
FIXED_Q = [q.strip() for q in os.environ.get("EVAL_QUESTIONS", "").split("||") if q.strip()]
# Statistical sampling: randomly pick N datasources (0 = no sampling / use MAX_DS).
SAMPLE = int(os.environ.get("EVAL_SAMPLE", "0"))
SEED = int(os.environ.get("EVAL_SEED", "1234"))
# Concurrency: run this many question-tasks in parallel (each gets its own DB
# session). Real speedup needs a serving stack that answers concurrently
# (vLLM continuous batching, or Ollama with OLLAMA_NUM_PARALLEL>1).
CONCURRENCY = max(1, int(os.environ.get("EVAL_CONCURRENCY", "1")))
_print_lock = threading.Lock()

# APEX is the biggest latency cost; default OFF for the smoke so it finishes fast.
if os.environ.get("EVAL_APEX", "off").lower() in ("off", "false", "0"):
    settings.APEX_ENABLED = False
print(f"[cfg] APEX_ENABLED={settings.APEX_ENABLED} "
      f"PARALLEL_COLUMNS_HINT_ENABLED={settings.PARALLEL_COLUMNS_HINT_ENABLED} "
      f"MAX_DS={MAX_DS or 'all'} N_Q={N_Q}")


def _admin_user(oid: int) -> UserInfoDTO:
    return UserInfoDTO(id=1, account="admin", name="admin", email="admin@sqlbot.local",
                       oid=oid or 1, isAdmin=True, language="en")


def _gen_questions(schema_text: str, ds_name: str, n: int) -> list[str]:
    prompt = (
        f"You are testing a text-to-SQL system. Given the database schema below, "
        f"write exactly {n} DISTINCT natural-language questions a real user might ask "
        f"about this data. Vary them (a lookup/filter, a list, an aggregation). "
        f"Return ONLY a JSON array of {n} strings, no prose.\n\n"
        f"Datasource: {ds_name}\n{schema_text}"
    )
    raw = invoke_default_llm(prompt) or ""
    start, end = raw.find("["), raw.rfind("]")
    if start >= 0 and end > start:
        try:
            arr = json.loads(raw[start:end + 1])
            qs = [str(q).strip() for q in arr if str(q).strip()]
            if qs:
                return qs[:n]
        except Exception:
            pass
    # Fallback: any non-empty lines.
    lines = [l.strip(" -*0123456789.\t") for l in raw.splitlines() if l.strip()]
    return [l for l in lines if len(l) > 8][:n]


def _run_question(session: Session, user: UserInfoDTO, chat_id: int, q: str) -> dict:
    rq = ChatQuestion(chat_id=chat_id, question=q)
    loop = asyncio.new_event_loop()
    try:
        svc = loop.run_until_complete(
            LLMService.create(session, user, rq, None, embedding=False))
    finally:
        loop.close()
    svc.init_record(session=session)
    svc.run_task_async(in_chat=False, stream=False,
                       finish_step=ChatFinishStep.GENERATE_SQL, return_img=False)
    last = {}
    for chunk in svc.await_result():
        if chunk:
            last = chunk
    res = last if isinstance(last, dict) else {"raw": last}
    # GENERATE_SQL stops before execution; run the generated SQL here to get a
    # real row count (read-only, guarded by check_sql_read inside exec_sql).
    sql = res.get("sql") or res.get("sqlContent") or res.get("sql-content") or ""
    if sql and getattr(svc, "ds", None) is not None:
        try:
            res["_exec_rows"] = len(exec_sql(svc.ds, sql) or [])
            res["_exec_ok"] = True
        except Exception as e:
            res["_exec_ok"] = False
            res["_exec_err"] = str(e)[:120]
    return res


def _classify(res: dict) -> tuple[str, str]:
    sql = res.get("sql") or res.get("sqlContent") or res.get("sql-content") or ""
    is_union = isinstance(sql, str) and " UNION" in sql.upper()
    if res.get("success") is False or res.get("message") or res.get("type") == "error":
        return "FAIL", res.get("message") or res.get("content") or "error"
    if sql:
        tag = " [UNION]" if is_union else ""
        if res.get("_exec_ok") is False:
            return "SQL-ERR", f"generated but exec failed: {res.get('_exec_err')}{tag}"
        rows = res.get("_exec_rows", 0)
        return ("PASS", f"executed ok, {rows} rows{tag}") if rows else ("PASS-EMPTY", f"executed ok, 0 rows{tag}")
    return "UNKNOWN", f"keys={list(res.keys())} sample={json.dumps(res, default=str)[:160]}"


def _worker(task):
    """Run ONE (datasource, question) task in its own DB session (thread-safe)."""
    ds_id, ds_name, oid, q = task
    try:
        with Session(engine) as session:
            user = _admin_user(oid)
            chat = create_chat(session, user, CreateChat(datasource=ds_id, question=q),
                               require_datasource=True)
            res = _run_question(session, user, chat.id, q)
            verdict, detail = _classify(res)
    except Exception as e:
        verdict, detail = "FAIL", f"exception: {e}"
    return (ds_id, ds_name, q, verdict, detail)


def _select_datasources(session):
    ds_all = list(session.exec(select(CoreDatasource)))
    if ONLY_IDS:
        return [d for d in ds_all if d.id in ONLY_IDS]
    if SAMPLE and SAMPLE < len(ds_all):
        random.Random(SEED).shuffle(ds_all)
        return ds_all[:SAMPLE]
    if MAX_DS:
        return ds_all[:MAX_DS]
    return ds_all


def main():
    totals = {"PASS": 0, "PASS-EMPTY": 0, "SQL-ERR": 0, "FAIL": 0, "UNKNOWN": 0}

    # 1) pick datasources and generate questions per datasource (cheap; one read session).
    tasks = []
    with Session(engine) as session:
        ds_list = _select_datasources(session)
        print(f"[run] {len(ds_list)} datasource(s), {N_Q} q each, "
              f"concurrency={CONCURRENCY}\n")
        for ds in ds_list:
            try:
                schema = build_ds_schema_text(session, ds)
            except Exception as e:
                print(f"  ! DS {ds.id} {ds.name}: schema build failed: {e}")
                continue
            questions = FIXED_Q if FIXED_Q else _gen_questions(schema, ds.name, N_Q)
            for q in questions:
                tasks.append((ds.id, ds.name, ds.oid, q))
    print(f"[run] {len(tasks)} question-tasks queued\n")

    # 2) run the question-tasks (concurrently if CONCURRENCY>1); each in its own session.
    done = 0
    def _tally(r):
        nonlocal done
        ds_id, ds_name, q, verdict, detail = r
        totals[verdict] = totals.get(verdict, 0) + 1
        done += 1
        with _print_lock:
            print(f"  [{verdict:10}] ({done}/{len(tasks)}) DS{ds_id} {ds_name}: {q[:60]}")
            print(f"               -> {detail[:100]}")

    if CONCURRENCY == 1:
        for t in tasks:
            _tally(_worker(t))
    else:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            for fut in as_completed([ex.submit(_worker, t) for t in tasks]):
                _tally(fut.result())

    print("\n==== SUMMARY ====")
    for k, v in totals.items():
        print(f"  {k:10}: {v}")
    ran = sum(totals.values())
    ok = totals["PASS"] + totals["PASS-EMPTY"]
    print(f"  {'valid-sql':10}: {ok}/{ran}" + (f" ({100*ok//ran}%)" if ran else ""))


if __name__ == "__main__":
    main()
