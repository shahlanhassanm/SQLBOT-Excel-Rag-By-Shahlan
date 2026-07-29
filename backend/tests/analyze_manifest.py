"""Summarize a bulk_ingest manifest — the deterministic parsing check across an
entire folder of files, no LLM involved.

Reads the JSONL written by bulk_ingest.py and reports: ingest success rate, parse
anomaly rate and breakdown, table/column distribution (multi-table "island"
files, single-column tables, blank auto-headers), how many files have parallel/
repeated columns, and the worst offenders to eyeball. Pure Python — runs anywhere.

Usage:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/analyze_manifest.py /tmp/ingest_manifest.jsonl"
  or locally:
    python backend/tests/analyze_manifest.py path/to/ingest_manifest.jsonl
"""
import os
import re
import sys
import json
from collections import Counter

_SUFFIX_RE = re.compile(r"[\s._\-]*\d+$")
# Review threshold for header confidence (default higher than the production
# 0.62 LLM-trigger, since a header can misfire while scoring moderately).
HEADER_CONF_THRESHOLD = float(os.environ.get("HEADER_REVIEW_CONF", "0.70"))


def _base(name: str) -> str:
    b = _SUFFIX_RE.sub("", str(name).strip())
    return (b or str(name).strip()).lower()


def _has_parallel_columns(columns) -> bool:
    seen = Counter(_base(c) for c in columns if c)
    return any(v >= 2 for v in seen.values())


def main(path):
    files = failed = flagged = 0
    total_tables = 0
    multi_table_files = 0            # >1 table (island splitting fired)
    single_col_tables = 0
    blank_header_tables = 0
    parallel_col_files = 0           # files with repeated/parallel columns
    col_counts = []                  # columns per table
    anomaly_types = Counter()
    worst = []                       # failed or flagged files, for eyeballing
    fail_examples = []
    header_confs = []                # per-file min header confidence
    low_conf = []                    # (file, conf) below threshold

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            files += 1
            if not rec.get("ok"):
                failed += 1
                fail_examples.append((rec.get("file"), rec.get("error", "")[:80]))
                continue
            tables = rec.get("tables", [])
            total_tables += len(tables)
            if len(tables) > 1:
                multi_table_files += 1
            file_has_parallel = False
            for t in tables:
                cols = t.get("columns", [])
                col_counts.append(len(cols))
                if len(cols) <= 1:
                    single_col_tables += 1
                if any(str(c).startswith(("col_", "Unnamed")) for c in cols):
                    blank_header_tables += 1
                if _has_parallel_columns(cols):
                    file_has_parallel = True
            if file_has_parallel:
                parallel_col_files += 1
            hc = rec.get("header_conf_min")
            if hc is not None:
                header_confs.append(hc)
                if hc < HEADER_CONF_THRESHOLD:
                    low_conf.append((rec.get("file"), hc))
            anoms = rec.get("anomalies") or []
            if anoms:
                flagged += 1
                for a in anoms:
                    # bucket by the text after the last ':' (the anomaly kind)
                    anomaly_types[a.split(":")[-1].strip()] += 1
                worst.append((rec.get("file"), anoms))

    def pct(n):
        return f"{100*n/files:.1f}%" if files else "-"

    avg_cols = sum(col_counts) / len(col_counts) if col_counts else 0
    print("==== MANIFEST ANALYSIS ====")
    print(f"  files              : {files}")
    print(f"  ingested ok        : {files - failed}  ({pct(files - failed)})")
    print(f"  failed             : {failed}  ({pct(failed)})")
    print(f"  parse-flagged      : {flagged}  ({pct(flagged)})")
    print(f"  clean (ok, no flag): {files - failed - flagged}  ({pct(files - failed - flagged)})")
    print("  --- structure ---")
    print(f"  total tables       : {total_tables}")
    print(f"  multi-table files  : {multi_table_files}  (island splitting fired)")
    print(f"  cols/table         : avg {avg_cols:.1f}, "
          f"min {min(col_counts) if col_counts else 0}, max {max(col_counts) if col_counts else 0}")
    print(f"  single-col tables  : {single_col_tables}")
    print(f"  blank/auto headers : {blank_header_tables}")
    print(f"  parallel-col files : {parallel_col_files}  (repeated columns -> UNION path)")
    if header_confs:
        avg_conf = sum(header_confs) / len(header_confs)
        print("  --- header detection ---")
        print(f"  header conf        : avg {avg_conf:.2f}, min {min(header_confs):.2f} "
              f"(over {len(header_confs)} files)")
        print(f"  low-confidence     : {len(low_conf)}  ({pct(len(low_conf))})  "
              f"(< {HEADER_CONF_THRESHOLD} -> likely header MISFIRE, review these)")
        for fn, c in sorted(low_conf, key=lambda x: x[1])[:10]:
            print(f"      {c:.2f}  {fn}")
    if anomaly_types:
        print("  --- anomaly types ---")
        for kind, n in anomaly_types.most_common():
            print(f"    {n:6d}  {kind}")
    if fail_examples:
        print("  --- first failures ---")
        for fn, err in fail_examples[:10]:
            print(f"    {fn}: {err}")
    if worst:
        print("  --- first flagged files ---")
        for fn, anoms in worst[:10]:
            print(f"    {fn}: {', '.join(anoms)[:100]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: analyze_manifest.py <manifest.jsonl>")
        sys.exit(2)
    main(sys.argv[1])
