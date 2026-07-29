"""One-shot SYNCHRONOUS, PER-DATASOURCE re-summarize + re-embed of all existing
datasources. Processes each datasource in its own save_ds_embedding call so a
single failure can't abort the whole batch, and prints progress. Slow (~90s/ds).

    docker cp tests/_resummarize_all.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/_resummarize_all.py"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text
from sqlalchemy.orm import scoped_session, sessionmaker
from common.core.db import engine
from apps.datasource.crud.table import save_ds_embedding

session_maker = scoped_session(sessionmaker(bind=engine))
with engine.connect() as c:
    ids = [r[0] for r in c.execute(text("select id from core_datasource order by id")).fetchall()]

print(f"re-summarizing {len(ids)} datasources, one at a time (slow)...", flush=True)
for _id in ids:
    save_ds_embedding(session_maker, [_id])   # per-ds isolation
    with engine.connect() as c:
        r = c.execute(text(
            "select name, substr(coalesce(description, ''), 1, 100) "
            "from core_datasource where id = :i"), {"i": _id}).fetchone()
    print(_id, r[0], "->", r[1], flush=True)
print("DONE")
