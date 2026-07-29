"""Root-cause analysis of a bird_eval.py result file.

bird_eval reports WHETHER each question was right. This re-executes the
predicted and gold SQL side by side to say WHY the wrong ones were wrong,
which is the difference between "the model can't reason" and "the model
answered fine but returned an extra column".

Pure Postgres work -- no LLM calls, so it costs no GPU.

Run inside the container:
    docker cp backend/tests/bird_analyze.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python /tmp/bird_analyze.py --results /tmp/bird_model.json \
        --out /tmp/bird_model_analysis.json"
"""
import os
import sys
import json
import argparse
import collections

sys.path.insert(0, "/opt/sqlbot/app")
import psycopg2

BANK = os.environ.get("BIRD_BANK", "/tmp/bird_mini_dev_150.json")
PG = dict(host="localhost", port=5432, user="root",
          password="Password123@pg", dbname="bird_dev")


def run(sql, db_id, limit=5):
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 60000;")
            cur.execute(f'SET search_path TO "{db_id}";')
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [c.name for c in cur.description] if cur.description else []
            return {"ok": True, "ncols": len(cols), "cols": cols,
                    "nrows": len(rows), "sample": [list(map(str, r)) for r in rows[:limit]]}
    except Exception as e:
        return {"ok": False, "error": str(e).strip().splitlines()[0][:200]}
    finally:
        conn.close()


def classify(rec, pred, gold):
    """Why is this one wrong? Ordered most-specific first."""
    if rec["status"] == "CORRECT":
        return "correct"
    if rec["status"] == "NEAR":
        return "float precision only (numerically correct)"
    if rec["status"] == "FAIL":
        return "harness timeout (not a model error)"
    if not pred.get("ok"):
        e = pred.get("error", "")
        if "timeout" in e:
            return "query too slow (>60s)"
        if "function" in e and "does not exist" in e:
            return "SQLite function used on Postgres"
        if "column" in e and "does not exist" in e:
            return "referenced a column that does not exist"
        if "operator does not exist" in e:
            return "type mismatch (text vs number)"
        if "syntax error" in e:
            return "malformed SQL"
        if "GROUP BY" in e:
            return "invalid GROUP BY"
        return "other SQL error"

    # Row problems are checked BEFORE column problems on purpose. A query that
    # returns 0 rows AND an extra column is a broken filter, not a shaping nit --
    # classifying it as "wrong column count" would flatter the model by making a
    # real reasoning failure look cosmetic.
    if pred["nrows"] == 0 and gold["nrows"] > 0:
        return "returned no rows (filter too strict)"
    if gold["nrows"] == 0 and pred["nrows"] > 0:
        return "gold is empty, prediction is not"
    if pred["nrows"] != gold["nrows"]:
        direction = "more" if pred["nrows"] > gold["nrows"] else "fewer"
        return (f"{direction} rows than gold ({pred['nrows']} vs {gold['nrows']}) "
                f"-- wrong join or filter")
    if pred["ncols"] != gold["ncols"]:
        # right number of rows, different width: check whether gold's values are
        # actually present in the prediction before calling it merely cosmetic
        gvals = {tuple(sorted(r)) for r in gold["sample"]}
        pvals = [set(r) for r in pred["sample"]]
        contained = all(any(set(g).issubset(p) for p in pvals) for g in
                        [tuple(r) for r in gold["sample"]]) if pred["sample"] else False
        shape = f"wrong column count ({pred['ncols']} vs {gold['ncols']})"
        return (f"{shape} -- gold's values ARE present, extra/missing columns only"
                if contained else
                f"{shape} -- and the values differ too")

    # same shape, different content: is it the same values in a different column
    # order, or genuinely different numbers?
    p = {tuple(sorted(r)) for r in pred["sample"]}
    g = {tuple(sorted(r)) for r in gold["sample"]}
    if p == g:
        return "same values, different column order"
    return "same shape, different values -- wrong computation or filter"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    results = json.load(open(args.results))
    bank = {q["question_id"]: q for q in json.load(open(BANK))}

    out = []
    for rec in results:
        q = bank[rec["question_id"]]
        pred = run(rec["sql"], rec["db_id"]) if rec.get("sql") else {"ok": False, "error": "no SQL"}
        gold = run(q["SQL"], rec["db_id"])
        reason = classify(rec, pred, gold)
        out.append({**rec, "reason": reason, "gold_sql": q["SQL"],
                    "evidence": q.get("evidence", ""),
                    "pred_exec": pred, "gold_exec": gold})
        print(f"  q{rec['question_id']:5} {rec['status']:8} {reason}", flush=True)

    json.dump(out, open(args.out, "w"), indent=1, default=str)

    print("\n==== why the wrong ones were wrong ====")
    wrong = [r for r in out if r["status"] not in ("CORRECT",)]
    for reason, n in collections.Counter(r["reason"] for r in wrong).most_common():
        print(f"  {n:4}  {reason}")
    print(f"\n[out] {args.out}")


if __name__ == "__main__":
    main()
