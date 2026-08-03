"""Is the difference between two BIRD runs real, or is it sampling noise?

The pipeline generates SQL at temperature 0.6, so two runs of IDENTICAL code
disagree on individual questions. Measured churn on one 21-question overlap was
~29% (4 regressions + 2 fixes) with no code change responsible. That makes the
naive comparison -- "run B scored 3 more than run A, so the change helped" --
unsound, because run A is one sample rather than a fixed reference.

This tool reports the paired comparison properly:

  * McNemar's test on the discordant pairs (fixed vs regressed). The concordant
    questions carry no information about which system is better, so only the
    b/c cells enter the statistic. Uses the exact binomial test, which is valid
    for the small discordant counts this benchmark produces (chi-square needs
    b+c >= 25 and quietly misleads below that).
  * A Wilson confidence interval on each run's EX, for reporting a single run.
  * The same breakdown by difficulty and by database, so a headline that rests
    entirely on one easy database is visible as such.

No LLM calls, no GPU, no database: pure analysis of two stored result files.

Usage:
    python bird_significance.py --a baseline.json --b candidate.json
    python bird_significance.py --a baseline.json --b candidate.json --by difficulty
"""
import argparse
import collections
import json
import math


def wilson(successes: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval. Preferred over the normal approximation because EX
    sits far enough from 0.5 and n is small enough that the naive interval can
    run outside [0, 1]."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def binom_sf(k: int, n: int, p: float = 0.5) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact, stdlib only."""
    if k <= 0:
        return 1.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return min(1.0, total)


def mcnemar_exact(fixed: int, regressed: int) -> float:
    """Two-sided exact McNemar p-value.

    Under the null the discordant pairs split 50/50, so this is a two-sided
    binomial sign test on (fixed, regressed). Doubling the one-tailed tail is
    the standard construction and is capped at 1.0.
    """
    n = fixed + regressed
    if n == 0:
        return 1.0
    k = max(fixed, regressed)
    return min(1.0, 2 * binom_sf(k, n))


def load(path: str) -> dict:
    with open(path) as fh:
        return {r["question_id"]: r for r in json.load(fh)}


def compare(a: dict, b: dict, qids) -> dict:
    fixed = [q for q in qids if not a[q]["ex"] and b[q]["ex"]]
    regressed = [q for q in qids if a[q]["ex"] and not b[q]["ex"]]
    ex_a = sum(a[q]["ex"] for q in qids)
    ex_b = sum(b[q]["ex"] for q in qids)
    n = len(qids)
    return {
        "n": n,
        "ex_a": ex_a, "ex_b": ex_b,
        "pct_a": 100 * ex_a / n if n else 0.0,
        "pct_b": 100 * ex_b / n if n else 0.0,
        "ci_a": wilson(ex_a, n),
        "ci_b": wilson(ex_b, n),
        "fixed": fixed, "regressed": regressed,
        "p": mcnemar_exact(len(fixed), len(regressed)),
        "churn": 100 * (len(fixed) + len(regressed)) / n if n else 0.0,
    }


def emit(label: str, r: dict, show_ids: bool = False) -> None:
    print(f"\n=== {label}  (n={r['n']}) ===")
    print(f"  A  : {r['ex_a']:3}/{r['n']} = {r['pct_a']:5.1f}%  "
          f"[95% CI {100*r['ci_a'][0]:.1f}-{100*r['ci_a'][1]:.1f}]")
    print(f"  B  : {r['ex_b']:3}/{r['n']} = {r['pct_b']:5.1f}%  "
          f"[95% CI {100*r['ci_b'][0]:.1f}-{100*r['ci_b'][1]:.1f}]")
    delta = r["pct_b"] - r["pct_a"]
    print(f"  delta          : {delta:+.1f}pp")
    print(f"  fixed by B     : {len(r['fixed'])}")
    print(f"  regressed by B : {len(r['regressed'])}")
    print(f"  churn          : {r['churn']:.1f}% of questions changed verdict")
    print(f"  McNemar exact p: {r['p']:.4f}"
          + ("  <- significant at 0.05" if r["p"] < 0.05
             else "  <- NOT distinguishable from noise"))
    if show_ids:
        if r["fixed"]:
            print(f"    fixed     : {sorted(r['fixed'])}")
        if r["regressed"]:
            print(f"    regressed : {sorted(r['regressed'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="baseline results json")
    ap.add_argument("--b", required=True, help="candidate results json")
    ap.add_argument("--by", default="", choices=["", "difficulty", "db_id"],
                    help="also break the comparison down by this field")
    ap.add_argument("--ids", action="store_true", help="list the changed question ids")
    args = ap.parse_args()

    a, b = load(args.a), load(args.b)
    qids = sorted(set(a) & set(b))
    if not qids:
        print("no overlapping question_ids between the two files")
        return
    if len(qids) < len(a) or len(qids) < len(b):
        print(f"[note] comparing the {len(qids)} shared questions "
              f"(A has {len(a)}, B has {len(b)})")

    emit(f"OVERALL  A={args.a.split('/')[-1]}  B={args.b.split('/')[-1]}",
         compare(a, b, qids), show_ids=args.ids)

    if args.by:
        groups = collections.defaultdict(list)
        for q in qids:
            groups[b[q].get(args.by) or a[q].get(args.by)].append(q)
        order = (["simple", "moderate", "challenging"] if args.by == "difficulty"
                 else sorted(groups))
        for key in order:
            if groups.get(key):
                emit(f"{args.by}={key}", compare(a, b, groups[key]))

    # Status-mix shift matters independently of EX: dialect/prompt fixes are
    # meant to convert hard failures (NO-SQL, SQL-ERR) into attempts, which can
    # show up as a better status mix before it shows up as EX.
    print("\n=== status mix ===")
    sa = collections.Counter(a[q]["status"] for q in qids)
    sb = collections.Counter(b[q]["status"] for q in qids)
    for st in sorted(set(sa) | set(sb)):
        print(f"  {st:9} A={sa.get(st,0):3}  B={sb.get(st,0):3}  "
              f"{sb.get(st,0)-sa.get(st,0):+d}")


if __name__ == "__main__":
    main()
