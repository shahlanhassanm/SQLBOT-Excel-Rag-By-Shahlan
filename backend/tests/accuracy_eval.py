"""Accuracy eval against a fixed question bank with known-good answers.

Unlike eval_harness.py (which only scores "did the SQL execute"), this runs each
question, executes the generated SQL, and checks whether the EXPECTED VALUES
actually appear in the returned rows. That is the difference between
"valid SQL" and "correct answer".

Scoring per question:
  CORRECT  - every expected number and string found in the result rows
  PARTIAL  - some found
  WRONG    - none found (SQL ran, but the answer is not in it)
  SQL-ERR  - SQL generated but failed to execute
  FAIL     - pipeline error / no SQL

Questions with "ds": null run with NO datasource pinned, so the LLM auto-selects
and cross-datasource decomposition can fire (needed for the cross-file questions).

Run inside the container:
    docker cp backend/tests/accuracy_eval.py sqlbot:/tmp/accuracy_eval.py
    docker cp backend/tests/question_bank.json sqlbot:/tmp/question_bank.json
    docker exec -e EVAL_APEX=off sqlbot \
        sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/accuracy_eval.py"

Env:
    EVAL_APEX=on|off      override APEX for this run (default: inherit settings)
    EVAL_ONLY=S1Q1,S4Q2   run only these question ids
    EVAL_LIMIT=5          run only the first N questions
    EVAL_OUT=/tmp/x.json  where to write the detailed results
    EVAL_CONSENSUS=on|off override execution-based self-consistency for this run
    EVAL_FINISH=query_data|generate_sql  where to stop the pipeline (see below)

Note on finish step: by default the pipeline is stopped at GENERATE_SQL and this
harness executes the SQL itself. That is cheap, but it returns BEFORE the
pipeline's own execution stage, so anything that depends on execution results —
the empty-result grader, and execution-based self-consistency — never runs and
cannot be measured. Setting EVAL_CONSENSUS=on switches the run to QUERY_DATA so
those stages execute and the SQL reported back is the one consensus selected.

That same early return is why eval-created chats look EMPTY in the web UI: the
harness's own exec_sql() result is scored and thrown away, so chat_record.data,
sql_exec_result and chart stay NULL and the frontend has no rows to draw. Set
EVAL_FINISH=query_data to run the pipeline's execution stage and persist the
results, making every chat this harness creates browsable at /#/chat/index.
Costs no extra LLM calls (execution is pure SQL), but the SQL runs twice.
Already-finished runs can be repaired in place with backfill_record_data.py.
"""
import os
import re
import sys
import json
import asyncio
import traceback

sys.path.insert(0, "/opt/sqlbot/app")

from sqlmodel import Session

from common.core.db import engine
from common.core.config import settings
from apps.datasource.models.datasource import CoreDatasource
from apps.chat.task.llm import LLMService
from apps.chat.models.chat_model import CreateChat, ChatQuestion, ChatFinishStep
from apps.chat.curd.chat import create_chat
from apps.system.schemas.system_schema import UserInfoDTO
from apps.db.db import exec_sql

BANK = os.environ.get("EVAL_BANK", "/tmp/question_bank.json")
OUT = os.environ.get("EVAL_OUT", "/tmp/accuracy_results.json")
ONLY = [x.strip() for x in os.environ.get("EVAL_ONLY", "").split(",") if x.strip()]
LIMIT = int(os.environ.get("EVAL_LIMIT", "0"))

_apex = os.environ.get("EVAL_APEX", "").lower()
if _apex in ("off", "false", "0"):
    settings.APEX_ENABLED = False
elif _apex in ("on", "true", "1"):
    settings.APEX_ENABLED = True

# Execution-based self-consistency only happens on the pipeline's own execution
# path, so measuring it requires running past GENERATE_SQL.
_consensus = os.environ.get("EVAL_CONSENSUS", "").lower()
if _consensus in ("off", "false", "0"):
    settings.AGENTIC_SELF_CONSISTENCY_ENABLED = False
elif _consensus in ("on", "true", "1"):
    settings.AGENTIC_SELF_CONSISTENCY_ENABLED = True

# Stopping at GENERATE_SQL returns before the pipeline executes the SQL, so
# chat_record.data is never written and the chats this harness creates render
# empty in the web UI (no result table, no chart). QUERY_DATA runs the
# pipeline's own execution stage, which persists the rows. Self-consistency
# also needs that stage, so it forces QUERY_DATA on; EVAL_FINISH lets a run
# ask for browsable chats WITHOUT paying for self-consistency.
_finish = os.environ.get("EVAL_FINISH", "").lower()
if _finish in ("query_data", "querydata", "data", "2"):
    FINISH_STEP = ChatFinishStep.QUERY_DATA
elif _finish in ("generate_sql", "sql", "1"):
    FINISH_STEP = ChatFinishStep.GENERATE_SQL
else:
    FINISH_STEP = (ChatFinishStep.QUERY_DATA if settings.AGENTIC_SELF_CONSISTENCY_ENABLED
                   else ChatFinishStep.GENERATE_SQL)
print(f"[cfg] APEX_ENABLED={settings.APEX_ENABLED} "
      f"MULTI_CANDIDATE={settings.AGENTIC_MULTI_CANDIDATE_ENABLED} "
      f"DECOMPOSE={settings.AGENTIC_DECOMPOSE_ENABLED}", flush=True)
print(f"[cfg] IDENTIFIER_CHECK={settings.AGENTIC_IDENTIFIER_CHECK_ENABLED} "
      f"VALUE_LINKING={settings.VALUE_LINKING_ENABLED} "
      f"SKELETON_FEWSHOT={settings.SKELETON_FEWSHOT_ENABLED} "
      f"SELF_CONSISTENCY={settings.AGENTIC_SELF_CONSISTENCY_ENABLED} "
      f"finish_step={FINISH_STEP.name}", flush=True)

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _admin_user(oid: int = 1) -> UserInfoDTO:
    return UserInfoDTO(id=1, account="admin", name="admin", email="admin@sqlbot.local",
                       oid=oid or 1, isAdmin=True, language="en")


def _nums_in(blob: str) -> list[float]:
    out = []
    for m in _NUM_RE.findall(blob):
        try:
            out.append(abs(float(m.replace(",", ""))))
        except ValueError:
            pass
    return out


def _num_hit(want: float, have: list[float]) -> bool:
    """Match with tolerance: exact-ish, or rounded to whole units."""
    w = abs(float(want))
    for h in have:
        if abs(h - w) <= max(0.02, w * 0.005):
            return True
        if round(h) == round(w) and w >= 1:
            return True
    return False


def _score(entry: dict, rows) -> tuple[str, str, float]:
    blob = json.dumps(rows, default=str, ensure_ascii=False)
    low = blob.lower()
    have = _nums_in(blob)
    want_n, want_s = entry.get("nums") or [], entry.get("strs") or []
    total = len(want_n) + len(want_s)
    if total == 0:
        return "UNSCORED", "no expected values defined", 0.0
    miss_n = [n for n in want_n if not _num_hit(n, have)]
    miss_s = [s for s in want_s if s.lower() not in low]
    hits = total - len(miss_n) - len(miss_s)
    frac = hits / total
    missing = ([str(n) for n in miss_n] + miss_s)[:6]
    if hits == total:
        return "CORRECT", f"all {total} expected value(s) found", frac
    if hits > 0:
        return "PARTIAL", f"{hits}/{total} found; missing: {', '.join(missing)}", frac
    return "WRONG", f"0/{total} found; missing: {', '.join(missing)}", frac


def _run_one(entry: dict) -> dict:
    qid, q, ds_id = entry["id"], entry["q"], entry.get("ds")
    rec = {"id": qid, "q": q, "ds": ds_id, "verdict": "FAIL", "detail": "",
           "sql": "", "rows": None, "row_count": 0, "score": 0.0}
    try:
        with Session(engine) as session:
            user = _admin_user(1)
            chat = create_chat(session, user,
                               CreateChat(datasource=ds_id, question=q),
                               require_datasource=False)
            rq = ChatQuestion(chat_id=chat.id, question=q)
            loop = asyncio.new_event_loop()
            try:
                svc = loop.run_until_complete(
                    LLMService.create(session, user, rq, None, embedding=False))
            finally:
                loop.close()
            svc.init_record(session=session)
            svc.run_task_async(in_chat=False, stream=False,
                               finish_step=FINISH_STEP, return_img=False)
            last = {}
            for chunk in svc.await_result():
                if chunk:
                    last = chunk
            res = last if isinstance(last, dict) else {"raw": last}

            sql = res.get("sql") or res.get("sqlContent") or res.get("sql-content") or ""
            rec["sql"] = (sql or "")[:1500]
            rec["ds_used"] = getattr(getattr(svc, "ds", None), "name", None)

            if not sql:
                rec["detail"] = (res.get("message") or "no SQL generated")[:200]
                return rec
            try:
                out = exec_sql(svc.ds, sql)
            except Exception as e:
                rec["verdict"] = "SQL-ERR"
                rec["detail"] = f"exec failed: {str(e)[:160]}"
                return rec

            # exec_sql returns {"fields": [...], "data": [{col: val}, ...], "sql": b64}
            # - NOT a bare row list. The real rows live under "data"; iterating the
            # dict itself just yields the key names.
            rows = (out or {}).get("data", []) if isinstance(out, dict) else list(out or [])
            rows = [dict(r) if hasattr(r, "keys") else r for r in rows]
            rec["row_count"] = len(rows)
            rec["rows"] = rows[:40]
            verdict, detail, frac = _score(entry, rows[:400])
            rec["verdict"], rec["detail"], rec["score"] = verdict, detail, frac
    except Exception as e:
        rec["detail"] = f"exception: {str(e)[:200]}"
        traceback.print_exc()
    return rec


def main():
    bank = json.load(open(BANK))
    if ONLY:
        bank = [b for b in bank if b["id"] in ONLY]
    if LIMIT:
        bank = bank[:LIMIT]
    print(f"[run] {len(bank)} question(s)\n", flush=True)

    results = []
    tally = {}
    for i, entry in enumerate(bank, 1):
        import time
        t0 = time.time()
        rec = _run_one(entry)
        rec["seconds"] = round(time.time() - t0, 1)
        results.append(rec)
        tally[rec["verdict"]] = tally.get(rec["verdict"], 0) + 1
        print(f"  [{rec['verdict']:8}] ({i}/{len(bank)}) {rec['id']}: {entry['q'][:58]}")
        print(f"             -> {rec['detail'][:110]} ({rec['seconds']}s, {rec['row_count']} rows)",
              flush=True)
        json.dump(results, open(OUT, "w"), indent=1, default=str)

    print("\n==== SUMMARY ====")
    for k, v in sorted(tally.items()):
        print(f"  {k:8}: {v}")
    ran = len(results)
    correct = tally.get("CORRECT", 0)
    partial = tally.get("PARTIAL", 0)
    if ran:
        print(f"  strict accuracy : {correct}/{ran} ({100*correct//ran}%)")
        print(f"  incl. partial   : {correct + partial}/{ran} "
              f"({100*(correct+partial)//ran}%)")
    print(f"\n[out] {OUT}")


if __name__ == "__main__":
    main()
