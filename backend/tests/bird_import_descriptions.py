"""Load BIRD's per-column descriptions into SQLBot's core_field.custom_comment.

The pipeline schema showed bare `(a2:text)` tuples while model-only mode fed
the model BIRD's own column descriptions — a measured part of the 0/10-vs-5/10
schema gap. The product already renders `core_field.custom_comment` into the
schema block; the BIRD description CSVs were simply never imported. This does
that import, with the same CSV parsing and 220-char cap the model-only harness
uses (bird_eval.load_descriptions), so both modes see the same text.

Fill-empty-only by default so a hand-written comment is never clobbered.

Run inside the container:
    docker cp backend/tests/bird_import_descriptions.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python /tmp/bird_import_descriptions.py"
Options: --force (overwrite non-empty), --clear (null out what this wrote),
         --dry-run (report only).
"""
import argparse
import csv
import glob
import io
import json
import os

import psycopg2

DESC_DIR = os.environ.get("BIRD_DESC_DIR", "/tmp/bird_desc")
DS_MAP = os.environ.get("BIRD_DS_MAP", "/tmp/bird_ds_map.json")
PG = {
    "host": os.environ.get("SQLBOT_PG_HOST", "localhost"),
    "port": int(os.environ.get("SQLBOT_PG_PORT", "5432")),
    "dbname": os.environ.get("SQLBOT_PG_DB", "sqlbot"),
    "user": os.environ.get("SQLBOT_PG_USER", "root"),
    "password": os.environ.get("SQLBOT_PG_PASSWORD", "Password123@pg"),
}


def load_descriptions(db_id: str) -> dict:
    """Same parsing as bird_eval.load_descriptions: friendly name + description
    (+ value domain), deduped, whitespace-collapsed, capped at 220 chars."""
    out: dict = {}
    for path in glob.glob(os.path.join(DESC_DIR, db_id, "*.csv")):
        table = os.path.splitext(os.path.basename(path))[0].lower()
        try:
            text = open(path, encoding="utf-8-sig", errors="replace").read()
        except OSError:
            continue
        cols = {}
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("original_column_name") or "").strip()
            if not name:
                continue
            friendly = (row.get("column_name") or "").strip()
            desc = (row.get("column_description") or "").strip()
            vals = (row.get("value_description") or "").strip()
            parts = [p for p in (friendly, desc) if p]
            uniq = []
            for p in parts:
                if p.lower() != name.lower() and p.lower() not in [u.lower() for u in uniq]:
                    uniq.append(p)
            blurb = "; ".join(uniq)
            if vals and vals.lower() not in blurb.lower():
                blurb = f"{blurb}; values: {vals}" if blurb else f"values: {vals}"
            if blurb:
                cols[name.strip().lower()] = " ".join(blurb.split())[:220]
        if cols:
            out[table] = cols
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite non-empty custom_comment too")
    ap.add_argument("--clear", action="store_true",
                    help="set custom_comment back to NULL for BIRD datasources")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ds_map = json.load(open(DS_MAP))
    conn = psycopg2.connect(**PG)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            total_set = total_skip = 0
            for db_id, ds_id in sorted(ds_map.items()):
                if args.clear:
                    cur.execute(
                        "UPDATE core_field SET custom_comment = NULL WHERE ds_id = %s",
                        (ds_id,))
                    print(f"{db_id:28} ds={ds_id}: cleared {cur.rowcount} comment(s)")
                    continue
                descs = load_descriptions(db_id)
                if not descs:
                    print(f"{db_id:28} ds={ds_id}: NO description csvs under "
                          f"{DESC_DIR}/{db_id}")
                    continue
                cur.execute(
                    "SELECT id, table_name FROM core_table WHERE ds_id = %s", (ds_id,))
                n_set = n_skip = 0
                for table_id, table_name in cur.fetchall():
                    cols = descs.get((table_name or "").lower())
                    if not cols:
                        continue
                    cur.execute(
                        "SELECT id, field_name, custom_comment FROM core_field "
                        "WHERE table_id = %s", (table_id,))
                    for field_id, field_name, existing in cur.fetchall():
                        blurb = cols.get((field_name or "").lower())
                        if not blurb:
                            continue
                        if existing and existing.strip() and not args.force \
                                and existing.strip() != blurb:
                            n_skip += 1
                            continue
                        if not args.dry_run:
                            cur.execute(
                                "UPDATE core_field SET custom_comment = %s WHERE id = %s",
                                (blurb, field_id))
                        n_set += 1
                total_set += n_set
                total_skip += n_skip
                print(f"{db_id:28} ds={ds_id}: set {n_set}, "
                      f"kept existing {n_skip}")
            if args.dry_run:
                conn.rollback()
                print(f"DRY RUN: would set {total_set} (skip {total_skip})")
            else:
                conn.commit()
                print(f"committed: set {total_set}, kept existing {total_skip}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
