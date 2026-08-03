"""Why does the pipeline get a question wrong when the raw model gets it right?

bird_eval says WHETHER each question was right; bird_analyze says why a single
run's SQL missed the gold. Neither answers the question that actually matters
when the full SQLBot stack scores below the bare model on identical questions:
*what did the pipeline do differently*.

That gap is real and measured -- on one 10-question overlap the pipeline scored
0/10 while model-only qwen2.5-coder:32b scored 5/10 -- and "the model is weak"
does not explain it, because a same-size specialist scored 4/10 too.

This tool joins two result files on question_id and classifies every divergence
structurally (sqlglot AST, not string compare), so the output is a ranked list
of *causes* rather than a list of failures. The headline section is REGRESSIONS:
questions model-only answered and the pipeline did not.

No LLM calls, no GPU: pure analysis of stored SQL.

Run inside the container:
    docker cp backend/tests/bird_diff.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python /tmp/bird_diff.py \\
            --pipeline /tmp/bird_pipeline_enriched_14b.json \\
            --model    /tmp/bird_final_32b.json \\
            --out      /tmp/bird_diff.json"
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, "/opt/sqlbot/app")

try:
    import sqlglot
    from sqlglot import exp
except Exception:                                    # pragma: no cover
    sqlglot = None

BANK = os.environ.get("BIRD_BANK", "/tmp/bird_mini_dev_150.json")
DIALECT = "postgres"

# Error-text signatures. Ordered: the first match wins, so put the specific
# ones before the generic ones.
_ERR_PATTERNS = [
    ("DIALECT-FUNCTION", r"function .* does not exist"),
    ("DIALECT-SYNTAX", r"syntax error at or near"),
    ("UNKNOWN-COLUMN", r"column .* does not exist"),
    ("UNKNOWN-TABLE", r"relation .* does not exist"),
    ("MISSING-FROM", r"missing FROM-clause entry"),
    ("AMBIGUOUS", r"ambiguous"),
    ("GROUP-BY", r"must appear in the GROUP BY"),
    ("TYPE-MISMATCH", r"cannot be matched|operator does not exist"),
]


def classify_error(detail: str) -> str:
    d = (detail or "").lower()
    for name, pat in _ERR_PATTERNS:
        if re.search(pat, d, re.I):
            return name
    return "OTHER-SQL-ERROR"


# --------------------------------------------------------------------------
# structural features
# --------------------------------------------------------------------------

def features(sql: str) -> dict:
    """Structural fingerprint of a query, for comparing two SQL strings by what
    they *do* rather than how they are written."""
    out = {"parsed": False, "tables": set(), "joins": 0, "filters": set(),
           "aggs": set(), "projections": 0, "distinct": False,
           "order": False, "limit": None, "subqueries": 0}
    if not sql or not sqlglot:
        return out
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return out
    if tree is None:
        return out
    out["parsed"] = True

    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if name:
            out["tables"].add(name)
    out["joins"] = len(list(tree.find_all(exp.Join)))
    out["subqueries"] = len(list(tree.find_all(exp.Subquery)))
    out["distinct"] = bool(list(tree.find_all(exp.Distinct)))
    out["order"] = bool(list(tree.find_all(exp.Order)))

    lim = tree.find(exp.Limit)
    if lim is not None:
        try:
            out["limit"] = int(lim.expression.name)
        except Exception:
            out["limit"] = -1

    for fn in tree.find_all(exp.AggFunc):
        out["aggs"].add(fn.sql_name().upper())

    # Filter predicates, normalised to `column op` so that differing literals
    # do not read as different logic while a missing condition still does.
    where = tree.find(exp.Where)
    if where is not None:
        for cmp_ in where.find_all(exp.Binary):
            left = cmp_.left
            if isinstance(left, exp.Column):
                out["filters"].add(f"{(left.name or '').lower()}:"
                                   f"{cmp_.key.upper()}")

    sel = tree.find(exp.Select)
    if sel is not None:
        out["projections"] = len(sel.expressions)
    return out


def diff_reasons(a: dict, b: dict) -> list:
    """How does query A differ structurally from query B (the reference)?"""
    if not a["parsed"]:
        return ["UNPARSEABLE"]
    r = []
    if a["tables"] != b["tables"]:
        extra, missing = a["tables"] - b["tables"], b["tables"] - a["tables"]
        if extra:
            r.append(f"EXTRA-TABLE({','.join(sorted(extra))})")
        if missing:
            r.append(f"MISSING-TABLE({','.join(sorted(missing))})")
    elif a["joins"] != b["joins"]:
        # same tables, different join count -- a join-path difference
        r.append(f"JOIN-SHAPE({a['joins']}v{b['joins']})")
    if a["aggs"] != b["aggs"]:
        r.append(f"AGG({','.join(sorted(a['aggs'])) or 'none'}"
                 f"|{','.join(sorted(b['aggs'])) or 'none'})")
    miss_f = b["filters"] - a["filters"]
    extra_f = a["filters"] - b["filters"]
    if miss_f:
        r.append(f"MISSING-FILTER({','.join(sorted(miss_f))})")
    if extra_f:
        r.append(f"EXTRA-FILTER({','.join(sorted(extra_f))})")
    if a["distinct"] != b["distinct"]:
        r.append("DISTINCT" if b["distinct"] else "EXTRA-DISTINCT")
    if a["limit"] != b["limit"]:
        r.append(f"LIMIT({a['limit']}v{b['limit']})")
    if a["projections"] != b["projections"]:
        r.append(f"PROJECTION({a['projections']}v{b['projections']})")
    if not r:
        r.append("EQUIVALENT-SHAPE")
    return r


# --------------------------------------------------------------------------

def load(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"missing results file: {path}")
    return {r["question_id"]: r for r in json.load(open(path))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline", required=True, help="--mode pipeline results")
    ap.add_argument("--model", required=True, help="--mode model results")
    ap.add_argument("--out", default="", help="write full JSON report here")
    ap.add_argument("--show", type=int, default=12,
                    help="regressions to print in full")
    args = ap.parse_args()

    pipe, model = load(args.pipeline), load(args.model)
    gold = {}
    if os.path.exists(BANK):
        gold = {q["question_id"]: q for q in json.load(open(BANK))}
    else:
        # Without gold SQL, wrong-row rows where model-only ALSO missed diff
        # against empty features and everything mislabels as EXTRA-*.
        print(f"[warn] question bank not found at {BANK} (set BIRD_BANK); "
              f"'both wrong' causes will be unreliable", file=sys.stderr)

    shared = sorted(set(pipe) & set(model))
    if not shared:
        raise SystemExit("no overlapping question_ids between the two files")

    rows, buckets = [], collections.Counter()
    regressions, both_wrong, wins = [], [], []

    for qid in shared:
        p, m = pipe[qid], model[qid]
        pex, mex = int(p.get("ex", 0)), int(m.get("ex", 0))

        if p.get("status") == "NO-SQL":
            cause = "REFUSED-NO-SQL"
        elif p.get("status") == "SQL-ERR":
            cause = classify_error(p.get("detail", ""))
        elif pex:
            cause = "CORRECT"
        else:
            # Wrong rows: compare against whichever reference is trustworthy --
            # the gold SQL if the model-only run also missed, otherwise the
            # model-only SQL, which is the thing that actually worked here.
            ref_sql = (m.get("sql") if mex else
                       (gold.get(qid, {}) or {}).get("SQL", ""))
            cause = "|".join(diff_reasons(features(p.get("sql", "")),
                                          features(ref_sql)))

        buckets[cause] += 1
        row = {"question_id": qid, "db_id": p.get("db_id"),
               "difficulty": p.get("difficulty"),
               "pipeline_status": p["status"], "pipeline_ex": pex,
               "model_status": m["status"], "model_ex": mex,
               "cause": cause,
               "pipeline_sql": (p.get("sql") or "")[:600],
               "model_sql": (m.get("sql") or "")[:600],
               "pipeline_detail": (p.get("detail") or "")[:200]}
        rows.append(row)
        if mex and not pex:
            regressions.append(row)
        elif pex and not mex:
            wins.append(row)
        elif not pex and not mex:
            both_wrong.append(row)

    n = len(shared)
    pex_tot = sum(r["pipeline_ex"] for r in rows)
    mex_tot = sum(r["model_ex"] for r in rows)

    print("=" * 78)
    print(f"PIPELINE vs MODEL-ONLY   ({n} shared questions)")
    print("=" * 78)
    print(f"  pipeline EX   : {pex_tot}/{n} = {100*pex_tot/n:5.1f}%")
    print(f"  model-only EX : {mex_tot}/{n} = {100*mex_tot/n:5.1f}%")
    print(f"  delta         : {pex_tot - mex_tot:+d} questions")
    print()
    print(f"  regressions (model right, pipeline wrong) : {len(regressions)}")
    print(f"  wins        (pipeline right, model wrong) : {len(wins)}")
    print(f"  both wrong                                : {len(both_wrong)}")

    print("\n" + "=" * 78)
    print("PIPELINE FAILURE CAUSES (ranked)")
    print("=" * 78)
    for cause, c in buckets.most_common():
        bar = "#" * min(40, c * 2)
        print(f"  {c:>3}  {cause[:52]:52} {bar}")

    if regressions:
        print("\n" + "=" * 78)
        print(f"REGRESSIONS -- the pipeline's own losses (showing "
              f"{min(args.show, len(regressions))} of {len(regressions)})")
        print("=" * 78)
        for r in regressions[:args.show]:
            print(f"\n  q{r['question_id']} [{r['db_id']}/{r['difficulty']}] "
                  f"pipeline={r['pipeline_status']}  cause={r['cause']}")
            if r["pipeline_detail"]:
                print(f"     detail   : {r['pipeline_detail'][:110]}")
            print(f"     pipeline : {r['pipeline_sql'][:170]}")
            print(f"     model-ok : {r['model_sql'][:170]}")

        print("\n" + "-" * 78)
        print("  regression causes only:")
        for cause, c in collections.Counter(
                r["cause"] for r in regressions).most_common():
            print(f"    {c:>3}  {cause[:60]}")

    if args.out:
        json.dump({"summary": {"shared": n, "pipeline_ex": pex_tot,
                               "model_ex": mex_tot,
                               "regressions": len(regressions),
                               "wins": len(wins)},
                   "causes": dict(buckets), "rows": rows},
                  open(args.out, "w"), indent=1, default=str)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
