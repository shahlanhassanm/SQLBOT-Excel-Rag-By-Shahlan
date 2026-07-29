"""Build a deterministic 150-question subset of BIRD Mini-Dev (PostgreSQL edition).

Mini-Dev ships 500 questions over 11 databases with a 30/50/20 simple/moderate/
challenging split. Running all 500 through the pipeline on this vGPU is ~8-12h
per pass, so we sample 150 while preserving BOTH marginals:

  * difficulty  -> 45 simple / 75 moderate / 30 challenging (same 30/50/20 shape)
  * db_id       -> proportional to each database's share of the 500

Allocation per (db_id, difficulty) cell uses largest-remainder rounding, so the
subset's per-cell counts are the closest integer match to the full set's
proportions. Sampling inside a cell is seeded (SEED=0), so the subset is
reproducible -- rerunning this script yields the identical 150 questions.

Usage:
    python3 backend/tests/bird_make_subset.py \
        --src /path/to/mini_dev_postgresql.json \
        --out backend/tests/bird_mini_dev_150.json
"""
import json
import random
import argparse
import collections

SEED = 0
TARGET = 150
DIFFICULTIES = ["simple", "moderate", "challenging"]


def largest_remainder(counts: dict, total: int) -> dict:
    """Apportion `total` across keys proportionally to `counts`, exactly."""
    pool = sum(counts.values())
    if pool == 0:
        return {k: 0 for k in counts}
    exact = {k: v * total / pool for k, v in counts.items()}
    alloc = {k: int(v) for k, v in exact.items()}
    short = total - sum(alloc.values())
    # hand out the leftovers to the largest fractional parts, ties by key name
    order = sorted(counts, key=lambda k: (-(exact[k] - alloc[k]), k))
    for k in order[:short]:
        alloc[k] += 1
    return alloc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="mini_dev_postgresql.json (500 q)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=TARGET)
    args = ap.parse_args()

    data = json.load(open(args.src))
    print(f"[src] {len(data)} questions")

    # difficulty budget first, so the headline metric stays comparable to the
    # published 30/50/20 mini-dev numbers
    by_diff = collections.Counter(q["difficulty"] for q in data)
    diff_budget = largest_remainder(dict(by_diff), args.n)
    print(f"[budget] difficulty: {diff_budget}")

    rng = random.Random(SEED)
    picked = []
    for diff in DIFFICULTIES:
        stratum = [q for q in data if q["difficulty"] == diff]
        by_db = collections.Counter(q["db_id"] for q in stratum)
        db_budget = largest_remainder(dict(by_db), diff_budget[diff])
        for db_id, k in sorted(db_budget.items()):
            cell = sorted([q for q in stratum if q["db_id"] == db_id],
                          key=lambda q: q["question_id"])
            picked.extend(rng.sample(cell, k) if k < len(cell) else cell)

    picked.sort(key=lambda q: q["question_id"])
    assert len(picked) == args.n, f"got {len(picked)}, want {args.n}"
    assert len({q["question_id"] for q in picked}) == args.n, "duplicate question_id"

    json.dump(picked, open(args.out, "w"), indent=1, ensure_ascii=False)

    print(f"\n[out] {args.out}  ({len(picked)} questions)")
    print(f"  difficulty: {dict(collections.Counter(q['difficulty'] for q in picked))}")
    print("  per-db (subset vs full):")
    sub_db = collections.Counter(q["db_id"] for q in picked)
    full_db = collections.Counter(q["db_id"] for q in data)
    for db in sorted(full_db):
        print(f"    {db:26} {sub_db[db]:3}  ({100*sub_db[db]/len(picked):4.1f}% "
              f"vs {100*full_db[db]/len(data):4.1f}%)")


if __name__ == "__main__":
    main()
