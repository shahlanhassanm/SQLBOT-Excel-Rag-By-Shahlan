"""Register the 11 BIRD Mini-Dev schemas as SQLBot datasources.

Each BIRD database becomes one `pg` datasource pointing at database `bird_dev`
with dbSchema = <db_id>, so the pipeline sees exactly that database's tables
(3-13 of them) and not all 75 at once.

Datasources are named `bird__<db_id>` so they are easy to spot and delete, and
so they cannot collide with the user's real Excel datasources.

Prereqs: bird_dev loaded and split (see bird_setup_schemas.py).

Run inside the container:
    docker cp backend/tests/bird_register_datasources.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_register_datasources.py"

Env:
    BIRD_PG_HOST/PORT/USER/PASSWORD/DB   connection to the BIRD data (defaults
                                         match the container's bundled Postgres)
    BIRD_DROP=1                          delete the bird__* datasources and exit
"""
import os
import sys
import json
import datetime

sys.path.insert(0, "/opt/sqlbot/app")

from sqlmodel import Session, select

# apps.datasource.crud.datasource and sqlbot_xpack import each other; importing
# the xpack package first lets that cycle resolve (same reason accuracy_eval.py
# reaches this module via apps.chat.task.llm rather than directly).
import sqlbot_xpack  # noqa: F401  (import order matters)

from common.core.db import engine
from apps.datasource.models.datasource import CoreDatasource, CoreTable
from apps.datasource.utils.utils import aes_encrypt
from apps.datasource.crud.datasource import sync_table
from apps.datasource.crud.table import delete_table_by_ds_id
from apps.datasource.crud.field import delete_field_by_ds_id
from apps.db.db import get_tables, check_connection

PREFIX = "bird__"
DB_IDS = [
    "california_schools", "card_games", "codebase_community",
    "debit_card_specializing", "european_football_2", "financial",
    "formula_1", "student_club", "superhero", "thrombosis_prediction",
    "toxicology",
]

CONF = {
    "host": os.environ.get("BIRD_PG_HOST", "localhost"),
    "port": int(os.environ.get("BIRD_PG_PORT", "5432")),
    "username": os.environ.get("BIRD_PG_USER", "root"),
    "password": os.environ.get("BIRD_PG_PASSWORD", "Password123@pg"),
    "database": os.environ.get("BIRD_PG_DB", "bird_dev"),
    "timeout": 60,
}


def drop_all(session: Session) -> None:
    rows = session.exec(select(CoreDatasource).where(
        CoreDatasource.name.like(f"{PREFIX}%"))).all()
    for ds in rows:
        delete_table_by_ds_id(session, ds.id)
        delete_field_by_ds_id(session, ds.id)
        session.delete(ds)
    session.commit()
    print(f"[drop] removed {len(rows)} bird__ datasource(s)")


def main():
    with Session(engine) as session:
        if os.environ.get("BIRD_DROP"):
            drop_all(session)
            return

        drop_all(session)  # idempotent: rebuild from scratch every run

        mapping = {}
        for db_id in DB_IDS:
            conf = dict(CONF, dbSchema=db_id)
            ds = CoreDatasource(
                name=f"{PREFIX}{db_id}",
                description=f"BIRD Mini-Dev database '{db_id}' (benchmark only)",
                type="pg",
                type_name="PostgreSQL",
                # aes_encrypt returns bytes; storing them straight into the Text
                # column persists the b"..." repr, which aes_decrypt then can't
                # base64-decode. Decode here.
                configuration=aes_encrypt(json.dumps(conf)).decode("utf-8"),
                create_time=datetime.datetime.now(),
                create_by=1,
                status="Success",
                oid=1,
                num="0",
                recommended_config=0,
            )
            session.add(ds)
            session.flush()
            session.refresh(ds)
            session.commit()

            conn_ok = check_connection(None, ds, is_raise=False)
            tables = get_tables(ds)
            core_tables = [CoreTable(ds_id=ds.id, checked=True,
                                     table_name=t.tableName,
                                     table_comment=t.tableComment or "",
                                     custom_comment=t.tableComment or "")
                           for t in tables]
            sync_table(session, ds, core_tables)

            ds.num = str(len(core_tables))
            session.add(ds)
            session.commit()

            mapping[db_id] = ds.id
            print(f"  [ok] ds_id={ds.id:4} {ds.name:34} {len(core_tables):2} tables "
                  f"(conn={'ok' if conn_ok else 'FAILED'})")

        out = "/tmp/bird_ds_map.json"
        json.dump(mapping, open(out, "w"), indent=1)
        print(f"\n[out] db_id -> ds_id map written to {out}")
        print(json.dumps(mapping, indent=1))


if __name__ == "__main__":
    main()
