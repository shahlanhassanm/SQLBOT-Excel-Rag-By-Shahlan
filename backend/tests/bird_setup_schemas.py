"""Split BIRD Mini-Dev's flat PostgreSQL dump into one schema per database.

BIRD ships MINIDEV_postgresql/BIRD_dev.sql as a single dump that drops all 75
tables from all 11 logical databases into one flat `public` schema. Evaluating
against that as-is would be unfair to the model: every question would see the
schema of all 11 databases at once (75 tables instead of the 3-10 the question's
own db_id has), which is a much harder schema-linking problem than the published
mini-dev numbers were measured under.

The 11 databases share no table names (verified: 75 distinct names, 0 collisions),
so the flat dump can be split unambiguously. This moves each table into a schema
named after its db_id via ALTER TABLE ... SET SCHEMA -- a catalog-only operation,
so no data is copied and indexes/constraints follow their table.

Run AFTER loading the dump:
    docker exec sqlbot psql -U root -d postgres -c "CREATE DATABASE bird_dev OWNER root;"
    docker exec -i sqlbot psql -U root -d bird_dev < MINIDEV_postgresql/BIRD_dev.sql
    python3 backend/tests/bird_setup_schemas.py --tables /path/to/MINIDEV/dev_tables.json

Idempotent: re-running finds the tables already in their schema and reports OK.
"""
import json
import argparse
import subprocess

CONTAINER = "sqlbot"
DB = "bird_dev"


def psql(sql: str, db: str = DB) -> str:
    out = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "root", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()[:400]}")
    return out.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True, help="BIRD dev_tables.json")
    args = ap.parse_args()

    meta = json.load(open(args.tables))

    # actual (lowercased) table names as loaded, mapped to where they live now
    rows = [r.split("|") for r in psql(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname NOT IN ('pg_catalog','information_schema');").splitlines() if r]
    location = {t: s for s, t in rows}
    print(f"[db] {len(location)} tables present")

    moved = already = missing = 0
    for entry in meta:
        db_id = entry["db_id"]
        psql(f'CREATE SCHEMA IF NOT EXISTS "{db_id}";')
        for raw in entry["table_names_original"]:
            t = raw.lower()          # the PG dump lowercased every identifier
            cur = location.get(t)
            if cur is None:
                print(f"  !! {db_id}.{raw}: not found in dump")
                missing += 1
                continue
            if cur == db_id:
                already += 1
                continue
            if cur != "public":
                print(f"  !! {t}: unexpected schema {cur}, leaving alone")
                continue
            psql(f'ALTER TABLE public."{t}" SET SCHEMA "{db_id}";')
            moved += 1
    print(f"[split] moved={moved} already_placed={already} missing={missing}")

    leftover = psql("SELECT count(*) FROM pg_tables WHERE schemaname='public';")
    print(f"[check] tables left in public: {leftover}")
    print("\n[schemas]")
    for line in psql(
        "SELECT table_schema, count(*) FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
        "GROUP BY 1 ORDER BY 1;").splitlines():
        if line:
            s, n = line.split("|")
            print(f"  {s:26} {n:>3} tables")


if __name__ == "__main__":
    main()
