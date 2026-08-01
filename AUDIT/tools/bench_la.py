"""L-A benchmark validation: apply the SHIPPED apply_nulls_last to every stored
BIRD prediction, re-execute, and score with the harness's own metrics.

Isolates the lever: same model output, same questions, same database. The only
variable is the rewrite. Writes a new results file so bird_significance.py can
do the paired McNemar test.
"""
import json, sys, time, decimal, collections
import datetime as _dt
import psycopg2

sys.path.insert(0, "/opt/sqlbot/app")
from apps.chat.task.agentic import apply_nulls_last            # the shipped code
from apps.chat.task.sql_validate import sqlglot_dialect

PG = dict(host="localhost", port=5432, user="root",
          password="Password123@pg", dbname="bird_dev")
BANK = "/tmp/bird_mini_dev_150.json"
IN_PATH = sys.argv[1]
OUT_PATH = sys.argv[2]
DS_TYPE = "pg"   # BIRD datasources are PostgreSQL


def run_sql(sql, db):
    c = psycopg2.connect(**PG, connect_timeout=10)
    try:
        c.set_session(readonly=True, autocommit=True)
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = 30000;")
            cur.execute(f'SET search_path TO "{db}";')
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
    finally:
        c.close()


def ex(p, g):
    return 1 if set(p) == set(g) else 0


def _norm(v):
    if isinstance(v, bool) or v is None: return v
    if isinstance(v, int): return float(v)
    if isinstance(v, (float, decimal.Decimal)): return round(float(v), 6)
    if isinstance(v, (_dt.datetime, _dt.date)): return v.isoformat()
    if isinstance(v, str): return v.strip()
    return v


def ex_tol(p, g):
    return 1 if {tuple(_norm(c) for c in r) for r in p} == {tuple(_norm(c) for c in r) for r in g} else 0


def em(pred_sql, gold_sql):
    """Exact Match, normalised on whitespace/case. Reported for completeness."""
    n = lambda s: " ".join((s or "").split()).rstrip(";").upper()
    return 1 if n(pred_sql) == n(gold_sql) else 0


gold_bank = {q["question_id"]: q for q in json.load(open(BANK))}
rows = json.load(open(IN_PATH))
gold_cache = {}

out_records = []
rewrite_ns = 0
n_rewritten = 0
changed, regressed, fixed = [], [], []

for r in rows:
    qid, db = r["question_id"], r["db_id"]
    sql = r.get("sql") or ""
    rec = dict(r)

    t0 = time.perf_counter_ns()
    new_sql = apply_nulls_last(sql, sqlglot_dialect(DS_TYPE)) if sql else sql
    rewrite_ns += time.perf_counter_ns() - t0
    if new_sql != sql:
        n_rewritten += 1

    rec["sql"] = new_sql
    if not new_sql.strip():
        rec.update(ex=0, ex_tol=0, f1=0.0, status="NO-SQL", em=0)
        out_records.append(rec); continue

    try:
        pred = run_sql(new_sql, db)
    except Exception as e:
        rec.update(ex=0, ex_tol=0, f1=0.0, status="SQL-ERR",
                   detail=str(e).splitlines()[0][:150], em=0)
        if r.get("ex"): regressed.append((qid, "rewrite broke execution"))
        out_records.append(rec); continue

    if qid not in gold_cache:
        gold_cache[qid] = run_sql(gold_bank[qid]["SQL"], db)
    g = gold_cache[qid]

    e1, e2 = ex(pred, g), ex_tol(pred, g)
    rec.update(ex=e1, ex_tol=e2, em=em(new_sql, gold_bank[qid]["SQL"]),
               status="CORRECT" if e1 else "NEAR" if e2 else "WRONG")
    out_records.append(rec)

    if e1 != r.get("ex", 0):
        (fixed if e1 else regressed).append((qid, r["difficulty"], db))
    if new_sql != sql:
        changed.append(qid)

json.dump(out_records, open(OUT_PATH, "w"), indent=1, default=str)

n = len(rows)
b_ex = sum(r.get("ex", 0) for r in rows)
b_tol = sum(r.get("ex_tol", 0) for r in rows)
a_ex = sum(r["ex"] for r in out_records)
a_tol = sum(r["ex_tol"] for r in out_records)
a_em = sum(r.get("em", 0) for r in out_records)

print(f"=== L-A benchmark validation  ({IN_PATH.split('/')[-1]}, n={n}) ===")
print(f"  EX (official)   {b_ex}/{n} = {100*b_ex/n:5.1f}%   ->   {a_ex}/{n} = {100*a_ex/n:5.1f}%   ({100*(a_ex-b_ex)/n:+.1f}pp)")
print(f"  EX (tolerant)   {b_tol}/{n} = {100*b_tol/n:5.1f}%   ->   {a_tol}/{n} = {100*a_tol/n:5.1f}%   ({100*(a_tol-b_tol)/n:+.1f}pp)")
print(f"  Exact Match                       ->   {a_em}/{n} = {100*a_em/n:5.1f}%   (harness does not report EM; see note)")
print()
print(f"  SQL rewritten by the lever : {n_rewritten}/{n}")
print(f"  verdict changed            : {len(fixed)+len(regressed)}")
print(f"    fixed                    : {len(fixed)}   {[f[0] for f in fixed]}")
print(f"    REGRESSED                : {len(regressed)} {regressed if regressed else ''}")
print()
print("  by difficulty (EX before -> after):")
for d in ("simple", "moderate", "challenging"):
    bs = [r for r in rows if r["difficulty"] == d]
    as_ = [r for r in out_records if r["difficulty"] == d]
    if bs:
        be, ae = sum(r.get("ex", 0) for r in bs), sum(r["ex"] for r in as_)
        print(f"    {d:12} {be:3}/{len(bs):3} = {100*be/len(bs):5.1f}%  ->  {ae:3}/{len(as_):3} = {100*ae/len(as_):5.1f}%  ({100*(ae-be)/len(bs):+.1f}pp)")
print()
print("  by status:")
bc = collections.Counter(r.get("status") for r in rows)
ac = collections.Counter(r["status"] for r in out_records)
for k in sorted(set(bc) | set(ac)):
    print(f"    {k:9} {bc.get(k,0):3} -> {ac.get(k,0):3}")
print()
print(f"  latency: rewrite total {rewrite_ns/1e6:.1f} ms over {n} queries "
      f"= {rewrite_ns/1e6/n:.3f} ms/query (CPU only, no model call)")
print(f"  tokens : 0 extra input, 0 extra output (deterministic post-processing)")
print(f"  [out] {OUT_PATH}")
