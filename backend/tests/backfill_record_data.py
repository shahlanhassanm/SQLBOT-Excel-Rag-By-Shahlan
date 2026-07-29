"""Backfill chat_record.data for records that have SQL but no stored result.

Why this exists
---------------
accuracy_eval.py runs the pipeline with finish_step=GENERATE_SQL. llm.py returns
at that gate BEFORE the execution stage, so save_sql_exec_data() never runs and
chat_record.data / sql_exec_result / chart stay NULL. The harness executes the
SQL itself only to score it, then discards the rows. Net effect: every chat the
harness created shows up in the web UI sidebar with a question and a SQL bubble
but NO result table and NO chart — the frontend asks
GET /chat/record/{id}/data, get_chat_chart_data() finds data IS NULL and
returns {}, and there is nothing to draw.

This script replays each record's already-stored SQL against its own datasource
and writes the rows back in the exact shape the pipeline would have written
(fields / data / sql / datasource), so those chats render normally.

Filling data alone is NOT enough. ChartBlock.vue gates the whole result block on
`v-if="message?.record?.chart"`, so a record with rows but no chart config
renders as an empty box — toolbar visible, table missing. The GENERATE_CHART
stage that normally writes that column is even further past the early return
than the execution stage is, so it never ran either. This script therefore also
synthesises the minimal table config the renderer needs,
{"type":"table","title":...,"columns":[{"name":c,"value":c}]}, matching the
shape real UI-created records store. An existing chart is never overwritten.

No LLM calls. Read-only against the user datasources; the only writes are to
chat_record.data, and only for rows where it is currently empty.

Run inside the container:
    docker cp backend/tests/backfill_record_data.py sqlbot:/tmp/
    docker exec sqlbot sh -c \
        "cd /opt/sqlbot/app && .venv/bin/python /tmp/backfill_record_data.py"

Env:
    BACKFILL_DRY=1         report what would change, write nothing
    BACKFILL_CHATS=201,203 only these chat ids (default: all eligible)
    BACKFILL_BEFORE=...    only records created before this ISO timestamp;
                           use it to skip a run that is still in flight
    BACKFILL_LIMIT=10      stop after N records
"""
import os
import sys
import orjson
from datetime import datetime

sys.path.insert(0, "/opt/sqlbot/app")

from sqlmodel import Session, select, and_

from common.core.db import engine
from common.utils.utils import prepare_for_orjson
from apps.datasource.models.datasource import CoreDatasource
# Import order matters: apps.chat.task.llm must be imported before apps.db.db,
# exactly as accuracy_eval.py does it. Importing apps.db.db first pulls
# sqlbot_xpack in through a half-initialised path and dies with
# "ImportError: cannot import name get_assistant_info".
from apps.chat.task.llm import LLMService  # noqa: F401  (import for side effect)
from apps.chat.models.chat_model import ChatRecord
from apps.db.db import exec_sql

DRY = os.environ.get("BACKFILL_DRY", "") in ("1", "true", "yes")
CHATS = [int(x) for x in os.environ.get("BACKFILL_CHATS", "").replace(",", " ").split() if x]
_before_raw = os.environ.get("BACKFILL_BEFORE", "").strip()
# create_time is a bare timestamp column; comparing it to a str makes psycopg
# bind a VARCHAR and postgres refuses the comparison, so parse it up front.
BEFORE = datetime.fromisoformat(_before_raw) if _before_raw else None
LIMIT = int(os.environ.get("BACKFILL_LIMIT", "0"))

# Mirrors the pipeline's own cap in llm.py so a backfilled record cannot hold
# more rows than a normally-answered one would.
ROW_LIMIT = 1000


def main() -> int:
    filled = skipped = failed = 0

    with Session(engine) as session:
        stmt = select(ChatRecord).where(
            and_(ChatRecord.sql.is_not(None), ChatRecord.sql != "",
                 ChatRecord.datasource.is_not(None)))
        if CHATS:
            stmt = stmt.where(ChatRecord.chat_id.in_(CHATS))
        if BEFORE is not None:
            stmt = stmt.where(ChatRecord.create_time < BEFORE)
        # A record needs repair if EITHER half is missing: data (no rows) or
        # chart (rows present but the renderer refuses to draw them).
        records = [r for r in session.execute(stmt).scalars().all()
                   if not r.data or not r.chart]
        records.sort(key=lambda r: r.id)
        if LIMIT:
            records = records[:LIMIT]

        print(f"[backfill] {len(records)} record(s) with SQL but no stored result"
              f"{' (DRY RUN)' if DRY else ''}\n", flush=True)

        ds_cache: dict[int, CoreDatasource] = {}
        for rec in records:
            ds = ds_cache.get(rec.datasource)
            if ds is None:
                ds = session.get(CoreDatasource, rec.datasource)
                if ds is None:
                    print(f"  [skip   ] record {rec.id}: datasource {rec.datasource} is gone")
                    skipped += 1
                    continue
                ds_cache[rec.datasource] = ds

            if rec.data and rec.chart:
                skipped += 1
                continue

            try:
                out = exec_sql(ds, rec.sql)
            except Exception as e:
                # A record whose SQL never ran (the SQL-ERR bucket in the eval)
                # has no result to show. Leaving data NULL is the honest state.
                print(f"  [sql-err] record {rec.id}: {str(e)[:110]}")
                failed += 1
                continue

            rows = prepare_for_orjson(out.get("data") or [])
            if len(rows) > ROW_LIMIT:
                out["data"], out["limit"] = rows[:ROW_LIMIT], ROW_LIMIT
            else:
                out["data"] = rows
            out["datasource"] = ds.id

            # Column keys come back already lower-cased from exec_sql, and the
            # renderer looks each row up by `value`, so name and value must both
            # be that exact key — no prettifying.
            fields = [str(f) for f in (out.get("fields") or [])]
            if not fields and rows:
                fields = [str(k) for k in rows[0].keys()]
            chart = {
                "type": "table",
                "title": (rec.question or "").strip()[:60] or "Result",
                "columns": [{"name": f, "value": f} for f in fields],
            }

            if not DRY:
                rec.data = orjson.dumps(out).decode()
                if not rec.chart:
                    rec.chart = orjson.dumps(chart).decode()
                session.add(rec)
                session.commit()
            filled += 1
            print(f"  [ok     ] record {rec.id} (chat {rec.chat_id}): "
                  f"{len(rows)} row(s), {len(fields)} col(s) "
                  f"<- {(rec.question or '')[:46]}", flush=True)

    print(f"\n==== SUMMARY ====\n  filled : {filled}\n  sql-err: {failed}\n  skipped: {skipped}")
    if DRY:
        print("  (dry run — nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
