"""Phase-3 reproduction: re-execute every committed BIRD prediction and re-score it.

Reads a bird_eval results file, re-runs the stored predicted SQL and the gold SQL
against bird_dev, recomputes EX / EX-tolerant / Soft-F1 with the harness's own
scoring functions, and reports any disagreement with the stored per-question
values. Read-only; no repo or app code is touched.
"""
import json, os, sys, collections, decimal, datetime as _dt
import psycopg2

PG = dict(host="localhost", port=5432, user="root",
          password="Password123@pg", dbname="bird_dev")
BANK = "/tmp/bird_mini_dev_150.json"
TIMEOUT_MS = 30000


def run_sql(sql, db_id):
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {TIMEOUT_MS};")
            cur.execute(f'SET search_path TO "{db_id}";')
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


def calculate_ex(p, g):
    return 1 if set(p) == set(g) else 0


def _norm_cell(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return float(v)
    if isinstance(v, (float, decimal.Decimal)):
        return round(float(v), 6)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip()
    return v


def calculate_ex_tolerant(p, g):
    a = {tuple(_norm_cell(c) for c in r) for r in p}
    b = {tuple(_norm_cell(c) for c in r) for r in g}
    return 1 if a == b else 0


def calculate_row_match(pr, gr):
    total = len(gr)
    m = sum(1 for v in pr if v in gr)
    po = sum(1 for v in pr if v not in gr)
    to = sum(1 for v in gr if v not in pr)
    return m / total, po / total, to / total


def calculate_f1_score(pred, gt):
    if not pred and not gt:
        return 1.0
    pred = list(dict.fromkeys(pred)); gt = list(dict.fromkeys(gt))
    ms, ps, ts = [], [], []
    for i, row in enumerate(gt):
        if i >= len(pred):
            ms.append(0); ts.append(1); continue
        a, b, c = calculate_row_match(pred[i], row)
        ms.append(a); ps.append(b); ts.append(c)
    for _ in range(len(pred) - len(gt)):
        ms.append(0); ps.append(1); ts.append(0)
    tp, fp, fn = sum(ms), sum(ps), sum(ts)
    pr = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    return 2 * pr * rc / (pr + rc) if pr + rc else 0


gold = {q["question_id"]: q for q in json.load(open(BANK))}
gold_cache = {}


def gold_rows(qid, db_id):
    if qid not in gold_cache:
        gold_cache[qid] = run_sql(gold[qid]["SQL"], db_id)
    return gold_cache[qid]


for path in sys.argv[1:]:
    if not os.path.exists(path):
        print(f"### {path}: MISSING"); continue
    res = json.load(open(path))
    n = len(res)
    stored_ex = sum(r.get("ex", 0) for r in res)
    stored_tol = sum(r.get("ex_tol", 0) for r in res)
    stored_f1 = sum(r.get("f1", 0.0) for r in res)

    re_ex = re_tol = 0
    re_f1 = 0.0
    disagree = []
    status_re = collections.Counter()
    for r in res:
        qid = r["question_id"]; db = r["db_id"]; sql = r.get("sql") or ""
        if not sql.strip():
            status_re["NO-SQL"] += 1
            if r.get("ex"): disagree.append((qid, "stored ex=1 but no sql"))
            continue
        try:
            pred = run_sql(sql, db)
        except Exception as e:
            status_re["SQL-ERR"] += 1
            if r.get("ex"):
                disagree.append((qid, f"stored ex=1 but re-exec failed: {str(e)[:70]}"))
            continue
        try:
            g = gold_rows(qid, db)
        except Exception as e:
            disagree.append((qid, f"GOLD FAILED: {str(e)[:70]}")); continue
        e1 = calculate_ex(pred, g); e2 = calculate_ex_tolerant(pred, g)
        f1 = calculate_f1_score(pred, g)
        re_ex += e1; re_tol += e2; re_f1 += f1
        status_re["CORRECT" if e1 else "NEAR" if e2 else "WRONG"] += 1
        if e1 != r.get("ex", 0):
            disagree.append((qid, f"ex stored={r.get('ex')} recomputed={e1}"))

    print(f"### {os.path.basename(path)}   n={n}")
    print(f"    stored     EX={stored_ex}/{n} ({100*stored_ex/n:.1f}%)  "
          f"tol={stored_tol} ({100*stored_tol/n:.1f}%)  f1={100*stored_f1/n:.1f}")
    print(f"    recomputed EX={re_ex}/{n} ({100*re_ex/n:.1f}%)  "
          f"tol={re_tol} ({100*re_tol/n:.1f}%)  f1={100*re_f1/n:.1f}")
    print(f"    status(recomputed): {dict(status_re)}")
    print(f"    stored status:      {dict(collections.Counter(r.get('status') for r in res))}")
    if disagree:
        print(f"    DISAGREEMENTS ({len(disagree)}):")
        for qid, why in disagree[:15]:
            print(f"      q{qid}: {why}")
        if len(disagree) > 15:
            print(f"      ... {len(disagree)-15} more")
    else:
        print("    no per-question disagreement")
    secs = [r.get("seconds", 0) for r in res]
    if secs:
        print(f"    median {sorted(secs)[len(secs)//2]:.1f}s   total {sum(secs)/60:.0f}m")
    print()
