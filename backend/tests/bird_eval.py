"""BIRD Mini-Dev benchmark harness (PostgreSQL edition) for SQLBot.

Scores text-to-SQL on a 150-question stratified subset of BIRD Mini-Dev
(see bird_make_subset.py) using BIRD's own metrics:

  EX       Execution Accuracy -- run predicted and gold SQL, compare the result
           sets. set(pred) == set(gold), exactly as BIRD's evaluation_ex.py.
  Soft-F1  Partial credit for nearly-right result sets (BIRD evaluation_f1.py,
           ported verbatim below).

Exact Match is deliberately NOT reported: the pipeline emits valid SQL that is
worded differently from the gold query, which EM penalises even when the answer
is right.

Two modes, same questions, same scoring, so the difference is attributable:

  --mode model      Prompt the LLM directly with the schema + evidence. This is
                    the raw model baseline, no SQLBot in the loop.
  --mode pipeline   Run through LLMService exactly like the product does, with
                    the question's database pinned as the datasource. Measures
                    value linking, identifier check, skeleton few-shot, etc.

Prereqs:
  1. bird_dev database loaded and split into per-db schemas (bird_setup_schemas.py)
  2. datasources registered                                 (bird_register_datasources.py)

Run inside the container:
    docker cp backend/tests/bird_eval.py sqlbot:/tmp/
    docker cp backend/tests/bird_mini_dev_150.json sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python /tmp/bird_eval.py --mode model"

Env / flags:
    --mode model|pipeline     which system under test (default: model)
    --limit N                 only the first N questions
    --out PATH                results json (default: /tmp/bird_<mode>.json)
    --resume                  skip question_ids already present in --out
    --timeout SEC             per-question wall clock (default 300)
    BIRD_MODEL                ollama model for --mode model (default gpt-oss:20b)
    BIRD_OLLAMA               ollama base url (default http://host.docker.internal:11434)
"""
import os
import re
import sys
import json
import time
import argparse
import collections

sys.path.insert(0, "/opt/sqlbot/app")

import psycopg2

BANK = os.environ.get("BIRD_BANK", "/tmp/bird_mini_dev_150.json")
DS_MAP = os.environ.get("BIRD_DS_MAP", "/tmp/bird_ds_map.json")

PG = dict(host=os.environ.get("BIRD_PG_HOST", "localhost"),
          port=int(os.environ.get("BIRD_PG_PORT", "5432")),
          user=os.environ.get("BIRD_PG_USER", "root"),
          password=os.environ.get("BIRD_PG_PASSWORD", "Password123@pg"),
          dbname=os.environ.get("BIRD_PG_DB", "bird_dev"))

OLLAMA = os.environ.get("BIRD_OLLAMA", "http://host.docker.internal:11434")
MODEL = os.environ.get("BIRD_MODEL", "gpt-oss:20b")
DESC_DIR = os.environ.get("BIRD_DESC_DIR", "/tmp/bird_desc")

EXEC_TIMEOUT_MS = 60_000


# --------------------------------------------------------------------------
# scoring -- ported from BIRD mini_dev/evaluation/{evaluation_ex,evaluation_f1}.py
# --------------------------------------------------------------------------
def calculate_ex(predicted_res, ground_truth_res) -> int:
    return 1 if set(predicted_res) == set(ground_truth_res) else 0


def _norm_cell(v: object) -> object:
    """Collapse the differences BIRD's strict EX punishes but nobody means.

    The gold queries cast with `AS REAL` (float4, ~6 significant digits) while a
    model writing the same maths with `::numeric` gets full Decimal precision:
    0.904908 vs 0.9049079754601227 is the SAME ANSWER but fails set equality.
    Rounding both to 6dp makes them compare equal. Dates/Decimals are likewise
    normalised so type alone never decides correctness.
    """
    import decimal
    import datetime as _dt
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int,)):
        return float(v)
    if isinstance(v, (float, decimal.Decimal)):
        return round(float(v), 6)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip()
    return v


def _is_timeout(exc: BaseException) -> bool:
    """True when a question failed on the clock rather than on its answer.

    Covers the stdlib TimeoutError, requests/urllib3 timeouts and psycopg2's
    statement_timeout, without importing any of them: the harness runs in both
    the container and on the host, where the set of installed clients differs.
    """
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, TimeoutError):
            return True
        name = type(e).__name__.lower()
        if "timeout" in name or "timedout" in name:
            return True
        if "timeout" in str(e).lower() or "canceling statement" in str(e).lower():
            return True
        stack.extend([e.__cause__, e.__context__])
    return False


def calculate_ex_tolerant(predicted_res, ground_truth_res) -> int:
    p = {tuple(_norm_cell(c) for c in row) for row in predicted_res}
    g = {tuple(_norm_cell(c) for c in row) for row in ground_truth_res}
    return 1 if p == g else 0


def calculate_row_match(predicted_row, ground_truth_row):
    total_columns = len(ground_truth_row)
    matches = 0
    element_in_pred_only = 0
    element_in_truth_only = 0
    for pred_val in predicted_row:
        if pred_val in ground_truth_row:
            matches += 1
        else:
            element_in_pred_only += 1
    for truth_val in ground_truth_row:
        if truth_val not in predicted_row:
            element_in_truth_only += 1
    return (matches / total_columns,
            element_in_pred_only / total_columns,
            element_in_truth_only / total_columns)


def _row_sort_key(row: tuple[object, ...]) -> tuple[tuple[bool, str, str], ...]:
    """Total-order key for a result row, safe across BIRD's cell types.

    A bare ``sorted()`` raises TypeError the moment a column mixes None with a
    string, or an int with a date — which BIRD rows routinely do. Sorting on
    ``(is_none, type_name, str(value))`` per cell is total, stable and needs no
    type coercion, so it cannot change any value the metric then compares.
    """
    return tuple((v is None, type(v).__name__, str(v)) for v in row)


def calculate_f1_tolerant(predicted: list[tuple[object, ...]],
                          ground_truth: list[tuple[object, ...]]) -> float:
    """Soft-F1 over _norm_cell-normalised rows.

    The official metric uses exact `in` membership, so a record can score
    ex_tol=1 (EX-tolerant rounds floats to 6dp) and f1=0 simultaneously — the
    two metrics disagreeing about the same rows (AUDIT E-09). This pairs the
    tolerant EX with a tolerant F1 so (ex, f1) and (ex_tol, f1_tol) are each
    internally consistent.

    The official calculate_f1_score is deliberately left unchanged so published
    Soft-F1 stays comparable.
    """
    p = [tuple(_norm_cell(c) for c in row) for row in predicted]
    g = [tuple(_norm_cell(c) for c in row) for row in ground_truth]
    return calculate_f1_score(p, g)


def calculate_f1_score(predicted, ground_truth) -> float:
    if not predicted and not ground_truth:
        return 1.0
    predicted = list(dict.fromkeys(predicted))
    ground_truth = list(dict.fromkeys(ground_truth))
    # Rows are paired POSITIONALLY below. Most gold queries have no ORDER BY, so
    # without a canonical order the same prediction scores differently between
    # runs — measured at 51.8 / 52.0 / 52.5 on one unchanged results file.
    # Sorting both sides makes the metric a function of the row multiset, which
    # is what it was always meant to measure (AUDIT D-10).
    predicted.sort(key=_row_sort_key)
    ground_truth.sort(key=_row_sort_key)

    match_scores, pred_only_scores, truth_only_scores = [], [], []
    for i, gt_row in enumerate(ground_truth):
        if i >= len(predicted):
            match_scores.append(0)
            truth_only_scores.append(1)
            continue
        m, p, t = calculate_row_match(predicted[i], gt_row)
        match_scores.append(m)
        pred_only_scores.append(p)
        truth_only_scores.append(t)
    for _ in range(len(predicted) - len(ground_truth)):
        match_scores.append(0)
        pred_only_scores.append(1)
        truth_only_scores.append(0)

    tp, fp, fn = sum(match_scores), sum(pred_only_scores), sum(truth_only_scores)
    precision = tp / (tp + fp) if tp + fp > 0 else 0
    recall = tp / (tp + fn) if tp + fn > 0 else 0
    return (2 * precision * recall / (precision + recall)
            if precision + recall > 0 else 0)


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
def run_sql(sql: str, db_id: str):
    """Execute against the question's schema, return rows as hashable tuples."""
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {EXEC_TIMEOUT_MS};")
            cur.execute(f'SET search_path TO "{db_id}";')
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load_descriptions(db_id: str) -> dict:
    """BIRD ships per-column descriptions that the first run never loaded.

    Without them the model sees `A2 text` and has no way to learn it means
    "district name" -- which is why `financial` (columns A2..A16) scored 10%.
    Every published BIRD baseline feeds these in, so omitting them was scoring
    this deployment handicapped.

    Returns {table_lower: {column_lower: "description"}}.
    """
    import csv
    import glob
    import io

    out: dict = {}
    for path in glob.glob(os.path.join(DESC_DIR, db_id, "*.csv")):
        table = os.path.splitext(os.path.basename(path))[0].lower()
        try:
            # these files are latin-1/utf-8-sig mixed and some have stray bytes
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
            # drop the description when it just restates the column name
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


def value_samples(db_id: str, max_card: int = 25) -> dict:
    """Real cell values for low-cardinality text columns.

    F5: 8 questions returned zero rows because the model invented a literal that
    does not occur in the data ('Paid' vs 'PAID', 'M' vs 'Male'). Showing it the
    actual domain of each small text column removes the guess. Only columns with
    <= max_card distinct values are sampled, so this never dumps a name column.

    Returns {table_lower: {column_lower: [values]}}.
    """
    conn = psycopg2.connect(**PG, connect_timeout=10)
    out: dict = {}
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 20000;")
            cur.execute(f'SET search_path TO "{db_id}";')
            cur.execute("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = %s
                  AND data_type IN ('text','character varying','character')
                ORDER BY table_name, ordinal_position;""", (db_id,))
            cols = cur.fetchall()

            for t, c in cols:
                try:
                    # bounded probe: stop counting once past the threshold
                    cur.execute(
                        f'SELECT DISTINCT "{c}" FROM "{db_id}"."{t}" '
                        f'WHERE "{c}" IS NOT NULL LIMIT {max_card + 1};')
                    vals = [r[0] for r in cur.fetchall()]
                except Exception:
                    continue
                if not vals or len(vals) > max_card:
                    continue
                vals = sorted(str(v) for v in vals if str(v).strip() != "")
                if not vals or sum(len(v) for v in vals) > 400:
                    continue
                out.setdefault(t.lower(), {})[c.lower()] = vals
    finally:
        conn.close()
    return out


def schema_ddl(db_id: str, with_descriptions: bool = False,
               with_values: bool = False) -> str:
    """CREATE TABLE text for one BIRD database, for the model-only prompt."""
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position;""", (db_id,))
            cols = collections.defaultdict(list)
            for t, c, d in cur.fetchall():
                cols[t].append((c, d))

            cur.execute("""
                SELECT tc.table_name, kcu.column_name, tc.constraint_type,
                       ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                LEFT JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                 AND tc.table_schema = ccu.table_schema
                WHERE tc.table_schema = %s
                  AND tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY');""",
                        (db_id,))
            keys = collections.defaultdict(list)
            for t, c, kind, ft, fc in cur.fetchall():
                keys[t].append(f"  PRIMARY KEY ({c})" if kind == "PRIMARY KEY"
                               else f"  FOREIGN KEY ({c}) REFERENCES {ft}({fc})")
    finally:
        conn.close()

    desc = load_descriptions(db_id) if with_descriptions else {}
    vals = value_samples(db_id) if with_values else {}

    out = []
    for t in sorted(cols):
        tdesc = desc.get(t, {})
        tvals = vals.get(t, {})
        # Comments must sit AFTER the separating comma -- putting them before it
        # swallows the comma into the `--` comment and leaves the model reading a
        # column list with no separators.
        body = []
        for c, d in cols[t]:
            note = tdesc.get(c.lower())
            vv = tvals.get(c.lower())
            if vv:
                shown = ", ".join(repr(v) for v in vv[:12])
                more = "" if len(vv) <= 12 else f", … ({len(vv)} distinct)"
                note = (f"{note}; " if note else "") + f"values: {shown}{more}"
            body.append((f"  {c} {d}", note))
        body += [(k, None) for k in sorted(set(keys.get(t, [])))]
        lines = []
        for i, (text, blurb) in enumerate(body):
            sep = "," if i < len(body) - 1 else ""
            lines.append(f"{text}{sep}" + (f"  -- {blurb}" if blurb else ""))
        out.append(f"CREATE TABLE {t} (\n" + "\n".join(lines) + "\n);")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# SQL extraction
# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:sql|postgresql)?\s*(.+?)```", re.S | re.I)


# --------------------------------------------------------------------------
# F4 -- catch bad SQL before it reaches the database, and repair it once
# --------------------------------------------------------------------------
# The first run lost 8 questions to SQL that never executed: SQLite functions
# (strftime, group_concat) surviving an explicit PostgreSQL instruction,
# hallucinated columns, a text-vs-integer comparison, a malformed UNION and an
# invalid GROUP BY. All eight are detectable without running the query.

SQLITE_ONLY = {
    "strftime": "TO_CHAR(... , 'YYYY')",
    "group_concat": "STRING_AGG(expr, ',')",
    "iif": "CASE WHEN ... THEN ... ELSE ... END",
    "instr": "POSITION(sub IN str)",
    "ifnull": "COALESCE(...)",
    "datetime": "CAST(... AS TIMESTAMP)",
    "julianday": "EXTRACT(EPOCH FROM ...)/86400",
    "substr": "SUBSTRING(...)",
    "printf": "FORMAT(...)",
    "random": "RANDOM()",
    "typeof": "pg_typeof(...)",
}

_catalog_cache: dict[str, dict] = {}


def catalog(db_id: str) -> dict:
    """{table_lower: {col_lower}} for identifier checking."""
    if db_id in _catalog_cache:
        return _catalog_cache[db_id]
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name FROM information_schema.columns
                WHERE table_schema = %s;""", (db_id,))
            out: dict = {}
            for t, c in cur.fetchall():
                out.setdefault(t.lower(), set()).add(c.lower())
    finally:
        conn.close()
    _catalog_cache[db_id] = out
    return out


def _schema_index_from_ddl(schema_str: str, sv) -> dict:
    """Adapt this harness's CREATE TABLE text to the product's schema index.

    `sql_validate.build_schema_index` parses the M-Schema format the chat
    pipeline builds (`# Table: schema.name` + `(col:type)`), while this harness
    emits plain DDL. Feeding it DDL yields an empty -- and therefore silently
    disabled -- index, which is how the two systems drifted apart in the first
    place (AUDIT E-03, and the same class of mismatch as the XiYan M-Schema
    finding).

    Only the INDEXING is adapted here; the identifier extraction, diffing and
    feedback formatting are the shipping functions.

    Column names in this bank contain spaces ("Academic Year text"), so the
    type is taken as the last whitespace-separated token and the name is
    everything before it.
    """
    index = {'tables': {}, 'columns': {}, 'all_columns': {}}
    table = None
    for raw in (schema_str or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith('create table'):
            name = line[len('CREATE TABLE'):].strip().rstrip('(').strip().strip('"')
            table = sv.normalize_identifier(name)
            index['tables'][table] = name
            index['columns'].setdefault(table, {})
            continue
        if line.startswith(')') or line == ';':
            table = None
            continue
        if table is None:
            continue
        col_def = line.rstrip(',').strip()
        if not col_def or ' ' not in col_def:
            continue
        col_name = col_def.rsplit(' ', 1)[0].strip().strip('"')
        if not col_name:
            continue
        norm = sv.normalize_identifier(col_name)
        index['columns'][table][norm] = col_name
        index['all_columns'].setdefault(norm, col_name)
    return index


def _production_identifier_findings(sql: str, db_id: str) -> "list[str] | None":
    """Run the SHIPPING identifier check, not the harness's copy.

    AUDIT E-03: this harness re-implemented four capabilities the product
    already has -- identifier checking, join-graph discovery, value sampling and
    the repair loop -- and called none of the product code. `--mode model
    --all-fixes` therefore measured a third system that does not ship, so a
    benchmark gain could not be assumed to reach users.

    This routes the identifier check through `apps.chat.task.sql_validate`, the
    same module the chat pipeline uses, so the two can be COMPARED. It is not
    wired into `lint_sql`, and deliberately so: measured against this bank the
    harness's local check is strictly better than the shipping one. It knows
    that PostgreSQL exposes a bare aggregate as an output name (`ORDER BY
    count` is legal), and its `_owners_hint` names the table that actually owns
    a missing column. Substituting the product's check made two existing lint
    tests fail with false positives.

    So the E-03 divergence is real but points the OTHER way: the fix that helps
    users is porting the harness's sophistication INTO the product (lever L-C),
    not degrading the harness to match. Keep this function as the differ, and
    see `test_eval_harness_integrity.py`.

    Returns None when the app is not importable (the harness also runs on the
    host, outside the container).
    """
    try:
        from apps.chat.task import sql_validate as sv
    except Exception:
        return None
    try:
        index = _schema_index_from_ddl(
            get_schema(db_id, descriptions=False, values=False), sv)
        if not sv.schema_index_is_usable(index):
            return None
        extracted = sv.extract_identifiers(sql, sv.sqlglot_dialect("pg"))
        if not extracted:
            return None
        findings = sv.diff_identifiers(extracted, index, "pg")
        # Keep ONLY existence errors. `case-mismatch` findings are correct for
        # the product -- it wants identifiers copied verbatim so quoting is
        # safe -- but they are FALSE POSITIVES against this bank: PostgreSQL
        # folds unquoted identifiers, so `s.School` and `school` are the same
        # column. Feeding that back would teach the repair loop to "fix"
        # working SQL, the same class of harm as the lint_sql alias/CTE false
        # positives already recorded in the audit.
        findings = [f for f in findings
                    if str(f.get("kind", "")).startswith("unknown-")]
        if not findings:
            return []
        text = sv.format_identifier_feedback(findings)
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    except Exception:
        return None


def lint_sql(sql: str, db_id: str) -> list:
    """Static problems, worst first. Empty list means 'looks sane'."""
    problems = []
    if not sql.strip():
        return ["empty query"]


    low = sql.lower()
    for fn, repl in SQLITE_ONLY.items():
        # word-boundary + open paren so column names like `random_id` don't trip
        if re.search(r"\b" + re.escape(fn) + r"\s*\(", low):
            if fn == "substr" and "substring" in low:
                continue
            problems.append(
                f"`{fn}(` is SQLite-only and does not exist in PostgreSQL; use {repl}")

    try:
        import sqlglot
        from sqlglot import exp
        try:
            tree = sqlglot.parse_one(sql, dialect="postgres")
        except Exception as e:
            return problems + [f"does not parse as PostgreSQL: {str(e)[:160]}"]

        cat = catalog(db_id)

        # Names the query defines for itself. A CTE is a table for the rest of
        # the statement, so `WITH t AS (...) SELECT ... FROM t` must not be
        # reported as "table `t` does not exist".
        derived_tables = {
            (cte.alias or "").lower()
            for cte in tree.find_all(exp.CTE) if cte.alias
        }
        # ... and a subquery in FROM is a table too: `FROM (SELECT ...) x`.
        for sub in tree.find_all(exp.Subquery):
            if sub.alias:
                derived_tables.add(sub.alias.lower())

        # tables actually referenced
        used_tables, alias_map = set(), {}
        for tbl in tree.find_all(exp.Table):
            name = (tbl.name or "").lower()
            if name:
                used_tables.add(name)
                if tbl.alias:
                    alias_map[tbl.alias.lower()] = name
        for t in sorted(used_tables):
            if t not in cat and t not in derived_tables:
                near = _closest(t, cat.keys())
                problems.append(f"table `{t}` does not exist"
                                + (f"; did you mean `{near}`?" if near else ""))

        # columns, only when we can attribute them to a known table
        known_cols = set()
        for t in used_tables:
            known_cols |= cat.get(t, set())

        # Output names the query invents. PostgreSQL lets a SELECT alias be used
        # in GROUP BY / ORDER BY / HAVING, and the projection of a CTE or
        # subquery is visible to whatever selects from it -- none of those live
        # in information_schema, so without this the linter reports valid SQL as
        # broken. Measured: 48% of "not in any referenced table" hits were this.
        for alias in tree.find_all(exp.Alias):
            name = (alias.alias or "").lower()
            if name:
                known_cols.add(name)
        # `SELECT x AS a` gives an exp.Alias, but an unaliased `SELECT COUNT(*)`
        # does not -- PostgreSQL still names that output column `count`, and
        # `ORDER BY count` is legal. sqlglot exposes it as the node key. Bare
        # column projections are skipped on purpose: `SELECT t1.account_id` must
        # stay checkable, and adding its own name here would silence exactly the
        # hallucination we are hunting.
        for sel in tree.find_all(exp.Select):
            for proj in sel.expressions:
                if isinstance(proj, exp.Column):
                    continue
                if isinstance(proj, exp.Func):
                    known_cols.add(proj.key.lower())
                name = (proj.alias_or_name or "").lower()
                if name and name != "*":
                    known_cols.add(name)
        # NB: do not skip on `tree.find(exp.Star)` -- COUNT(*) contains a Star and
        # that would disable identifier checking for most aggregate queries. A
        # bare `SELECT *` simply contributes no Column nodes of its own, so the
        # remaining explicit column references stay checkable either way.
        if known_cols:
            for col in tree.find_all(exp.Column):
                cname = (col.name or "").lower()
                if not cname or cname in known_cols:
                    continue
                tbl_ref = (col.table or "").lower()
                target = alias_map.get(tbl_ref, tbl_ref)
                if target and target in cat and cname not in cat[target]:
                    near = _closest(cname, cat[target])
                    problems.append(
                        f"column `{cname}` is not in table `{target}`"
                        + _owners_hint(cname, cat, exclude=target)
                        + (f"; the schema has `{near}`" if near else ""))
                elif not target:
                    near = _closest(cname, known_cols)
                    problems.append(
                        f"column `{cname}` is not in any referenced table"
                        + _owners_hint(cname, cat)
                        + (f"; did you mean `{near}`?" if near else ""))
    except ImportError:
        pass
    # de-duplicate, keep order
    return list(dict.fromkeys(problems))[:6]


def _closest(word: str, options) -> str:
    import difflib
    m = difflib.get_close_matches(word, list(options), n=1, cutoff=0.75)
    return m[0] if m else ""


def _owners_hint(cname: str, cat: dict, exclude: str = "") -> str:
    """Name the tables that really do own `cname`, for the repair prompt.

    The measured failure of the repair loop was silence: telling the model
    "column `frequency` is not in table `disp`" gave it nothing it did not
    already know, so it returned byte-identical SQL and the attempt was wasted
    (28 of 47 repairs in the XiYan run ended "no change, giving up"). The column
    is almost always real and sitting on a table the query failed to join, so
    say which one.
    """
    owners = sorted(t for t, cols in cat.items()
                    if cname in cols and t != exclude)
    if not owners:
        return ""
    shown = ", ".join(f"`{t}`" for t in owners[:3])
    more = f" (and {len(owners) - 3} more)" if len(owners) > 3 else ""
    return f"; it belongs to {shown}{more} -- join that table or qualify it there"


def explain_sql(sql: str, db_id: str) -> str:
    """Ask Postgres to plan the query without running it. '' means it planned."""
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 15000;")
            cur.execute(f'SET search_path TO "{db_id}";')
            cur.execute("EXPLAIN " + sql)
        return ""
    except Exception as e:
        return str(e).strip().splitlines()[0][:200]
    finally:
        conn.close()


REPAIR_PROMPT = """The PostgreSQL query below is wrong. Fix it.

Query:
{sql}

Problem:
{problem}

Schema:
{schema}

Question the query must answer: {question}

Return ONLY the corrected PostgreSQL query. No explanation, no markdown.
"""


def generate_with_repair(entry, args, model, trace):
    """One generation, then at most `args.repairs` execution-guided repairs.

    Repair fires on: static lint failure (F4), EXPLAIN failure (F4), or a query
    that plans and runs but returns zero rows where that is likely wrong (F5).
    Each repair costs one extra LLM call and only on questions that need it.
    """
    db_id = entry["db_id"]
    schema = get_schema(db_id, args.descriptions, args.values)
    ev = entry.get("evidence") or ""
    if args.promptfix:
        prompt = PROMPT_V2.format(
            schema=schema, joins=join_graph(db_id) if args.joins else "\n",
            evidence=f"External knowledge: {ev}\n\n" if ev else "",
            question=entry["question"])
    else:
        prompt = PROMPT.format(
            schema=schema,
            evidence=f"External knowledge: {ev}\n\n" if ev else "",
            question=entry["question"])

    sql = extract_sql(chat(prompt, args.timeout, model))

    for attempt in range(args.repairs):
        problem = ""

        issues = lint_sql(sql, db_id) if args.validate else []
        if issues:
            problem = "; ".join(issues)
        elif args.validate:
            err = explain_sql(sql, db_id)
            if err:
                problem = f"PostgreSQL rejected it: {err}"

        if not problem and args.zero_row_retry and sql:
            try:
                if len(run_sql(sql, db_id)) == 0:
                    problem = ("The query runs but returns ZERO rows, which is almost "
                               "certainly wrong for this question. A filter is too "
                               "strict or compares against a literal that does not "
                               "occur in the data — check the listed column values "
                               "and the join conditions.")
            except Exception:
                pass  # execution errors are the linter's job, not this branch's

        if not problem:
            break

        trace.append(f"repair{attempt+1}: {problem[:150]}")
        try:
            fixed = extract_sql(chat(REPAIR_PROMPT.format(
                sql=sql, problem=problem, schema=schema,
                question=entry["question"]), args.timeout, model))
        except Exception as e:
            trace.append(f"repair{attempt+1} call failed: {type(e).__name__}")
            break
        if not fixed or fixed.strip() == sql.strip():
            trace.append(f"repair{attempt+1}: no change, giving up")
            break
        sql = fixed

    return sql


def extract_sql(text: str) -> str:
    if not text:
        return ""
    # gpt-oss is a reasoning model; drop any exposed chain of thought
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)
    m = _FENCE.findall(text)
    sql = m[-1].strip() if m else text.strip()
    # keep from the first SELECT/WITH onwards; models like to preface prose
    m2 = re.search(r"\b(WITH|SELECT)\b", sql, re.I)
    if m2:
        sql = sql[m2.start():]
    return sql.split(";")[0].strip()


# --------------------------------------------------------------------------
# mode: model-only baseline
# --------------------------------------------------------------------------
PROMPT = """You are a PostgreSQL expert. Answer the question with ONE PostgreSQL query.

Database schema:
{schema}

{evidence}Question: {question}

Rules:
- Return ONLY the SQL query, no explanation.
- Use plain table names (the correct schema is already on the search_path).
- Return exactly the columns the question asks for, nothing extra.
"""

# v2 targets the three non-reasoning failure modes measured in the first run:
#   7 questions numerically correct but rejected on float precision
#   4 questions right but carrying an extra label column
#   2 questions using SQLite functions despite a PostgreSQL instruction
# It adds no LLM calls -- it is purely a better-specified contract.
PROMPT_V2 = """You are a PostgreSQL expert. Answer the question with ONE PostgreSQL query.

Database schema:
{schema}
{joins}
Worked examples of the required OUTPUT SHAPE:

  Q: What is the highest eligible free rate among schools in Alameda?
  A: SELECT MAX(CAST("Free Meal Count" AS REAL) / NULLIF("Enrollment", 0))
     FROM frpm WHERE "County Name" = 'Alameda'
     -- one value asked for, one column returned; no school name column

  Q: List the name and phone of schools opened after 1990, with their district.
  A: SELECT school, phone, district FROM schools WHERE opendate > '1990-12-31'
     -- three things asked for, three columns, in the order asked

{evidence}Question: {question}

Rules:
- Return ONLY the SQL query. No explanation, no markdown, no trailing semicolon.
- Use plain table names; the correct schema is already on the search_path.
- OUTPUT SHAPE: return exactly the column(s) the question asks for and nothing
  else. If the question asks "what is the highest X", return only X -- do NOT
  add a name, id or label column for context. Extra columns are wrong answers.
- DIVISION: when computing a ratio, percentage or average, cast the numerator
  with CAST(... AS REAL) and guard the denominator with NULLIF(..., 0).
  Example: CAST(SUM(a) AS REAL) / NULLIF(SUM(b), 0)
- This is PostgreSQL, NOT SQLite. Do not use strftime, group_concat, IIF or
  LIMIT without ORDER BY. Use TO_CHAR, STRING_AGG, CASE WHEN instead.
- Prefer LEFT JOIN when the question says "if there is any" or asks to list
  entities that may lack a match.
- ORDER BY ... DESC with LIMIT: always add NULLS LAST. PostgreSQL sorts NULLs
  first on DESC, so `ORDER BY score DESC LIMIT 1` returns a NULL row instead of
  the maximum. Write `ORDER BY score DESC NULLS LAST LIMIT 1`.
- PERCENTAGE: if the question asks for a percentage or says "percent"/"%",
  multiply by 100. `CAST(SUM(x) AS REAL) * 100 / NULLIF(COUNT(*), 0)`. A bare
  ratio is the wrong unit and scores as wrong.
- Never use LIKE on a date or timestamp column -- PostgreSQL has no date~~text
  operator. Use a range (`d >= DATE '1991-11-01' AND d < DATE '1991-12-01'`) or
  EXTRACT(YEAR FROM d).
- Quote identifiers that contain spaces or capitals with "double quotes".
"""

_ddl_cache: dict[str, str] = {}


_join_cache: dict[str, str] = {}


def join_graph(db_id: str) -> str:
    """Explicit FK list (F7).

    The FKs are already inside the CREATE TABLE text, but buried one per table.
    10 questions failed on a wrong join path, so the join graph is also stated
    once, up front, as its own block where it is hard to miss.
    """
    if db_id in _join_cache:
        return _join_cache[db_id]
    conn = psycopg2.connect(**PG, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tc.table_name, kcu.column_name,
                       ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                 AND tc.table_schema = ccu.table_schema
                WHERE tc.table_schema = %s AND tc.constraint_type = 'FOREIGN KEY'
                ORDER BY 1, 2;""", (db_id,))
            edges = [f"  {a}.{b} = {c}.{d}" for a, b, c, d in cur.fetchall()]
    finally:
        conn.close()
    edges = list(dict.fromkeys(edges))
    _join_cache[db_id] = ("\nJoin these tables ONLY on these foreign keys:\n"
                          + "\n".join(edges) + "\n") if edges else "\n"
    return _join_cache[db_id]


def get_schema(db_id: str, descriptions: bool, values: bool) -> str:
    key = f"{db_id}|{descriptions}|{values}"
    if key not in _ddl_cache:
        _ddl_cache[key] = schema_ddl(db_id, with_descriptions=descriptions,
                                     with_values=values)
    return _ddl_cache[key]


def chat(prompt: str, timeout: int, model: str) -> str:
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "temperature": 0,
            # A 32B at Q4 is ~20GB of weights on a 24GB card, so the KV cache
            # decides whether the model fits. At the default 32k context it does
            # not: 9GB spilled to CPU and inference crawled. The largest prompt
            # here is ~4.3k tokens, so 8k leaves ample headroom and keeps the
            # whole model resident on the GPU.
            "num_ctx": int(os.environ.get("BIRD_NUM_CTX", "8192")),
        },
        "keep_alive": os.environ.get("BIRD_KEEP_ALIVE", "30m"),
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def ask_model(entry: dict, timeout: int, descriptions: bool = False,
              promptfix: bool = False, model: str = None) -> str:
    import urllib.request

    db_id = entry["db_id"]
    key = f"{db_id}|{descriptions}"
    if key not in _ddl_cache:
        _ddl_cache[key] = schema_ddl(db_id, with_descriptions=descriptions)
    ev = entry.get("evidence") or ""
    template = PROMPT_V2 if promptfix else PROMPT
    prompt = template.format(schema=_ddl_cache[key],
                             evidence=f"External knowledge: {ev}\n\n" if ev else "",
                             question=entry["question"])
    body = json.dumps({
        "model": model or MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0},
        # The host runs OLLAMA_KEEP_ALIVE=0, so the model unloads after every
        # answer and the next question pays ~15s to reload it -- ~40min across
        # 150 questions. keep_alive is per-request, so ask for it here instead
        # of touching the host's systemd config. Native /api/chat is used rather
        # than the OpenAI-compatible route because the latter drops this field.
        "keep_alive": os.environ.get("BIRD_KEEP_ALIVE", "30m"),
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return payload["message"]["content"]


# --------------------------------------------------------------------------
# mode: full SQLBot pipeline
# --------------------------------------------------------------------------
def ask_pipeline(entry: dict, ds_id: int, finish: str = "sql") -> tuple[str, str]:
    """finish='sql'  -> stop after SQL generation (identifier-check retries only;
                        self-consistency candidates are CANCELLED and execution
                        retries/grader never run — measures less than the UI has).
       finish='data' -> run through execution: execution-error retries, the
                        empty-result grader and the self-consistency vote all
                        fire, and the returned SQL is the post-vote/post-repair
                        statement. This is what a UI user actually gets."""
    import asyncio
    from sqlmodel import Session
    from common.core.db import engine
    from apps.chat.task.llm import LLMService
    from apps.chat.models.chat_model import CreateChat, ChatQuestion, ChatFinishStep
    from apps.chat.curd.chat import create_chat
    from apps.system.schemas.system_schema import UserInfoDTO

    ev = entry.get("evidence") or ""
    # BIRD gives 'evidence' as external knowledge the model is meant to have;
    # every published baseline feeds it in alongside the question.
    question = f"{entry['question']}\n\n(Hint: {ev})" if ev else entry["question"]

    user = UserInfoDTO(id=1, account="admin", name="admin",
                       email="admin@sqlbot.local", oid=1, isAdmin=True, language="en")
    with Session(engine) as session:
        chat = create_chat(session, user,
                           CreateChat(datasource=ds_id, question=question),
                           require_datasource=False)
        rq = ChatQuestion(chat_id=chat.id, question=question)
        loop = asyncio.new_event_loop()
        try:
            svc = loop.run_until_complete(
                LLMService.create(session, user, rq, None, embedding=False))
        finally:
            loop.close()
        svc.init_record(session=session)
        step = (ChatFinishStep.QUERY_DATA if finish == "data"
                else ChatFinishStep.GENERATE_SQL)
        svc.run_task_async(in_chat=False, stream=False,
                           finish_step=step, return_img=False)
        last = {}
        for chunk in svc.await_result():
            if chunk:
                last = chunk
        res = last if isinstance(last, dict) else {"raw": last}
        sql = res.get("sql") or res.get("sqlContent") or res.get("sql-content") or ""
        return sql, (res.get("message") or "")[:200]


def pipeline_model_in_use() -> str:
    """The model `--mode pipeline` will ACTUALLY use.

    Pipeline mode builds its SQL through LLMService, which reads SQLBot's own
    configured model (the `ai_model` row with default_model = true). `--model`
    only feeds the `chat()` helper used by `--mode model`, so a pipeline run
    launched `--model gpt-oss:20b` silently ran whatever the database said --
    while run_provenance recorded "gpt-oss:20b". A results file asserting a
    model it did not use is worse than one asserting nothing, which is the whole
    point of E-06 (AUDIT D-40).

    Returns "" when the model cannot be determined, so provenance records
    "unknown" rather than a guess.
    """
    # PG points at the BIRD data database (bird_dev); `ai_model` lives in
    # SQLBot's own database, so override just the dbname.
    params = dict(PG)
    params["dbname"] = os.environ.get("SQLBOT_PG_DB", "sqlbot")
    try:
        import psycopg2
        conn = psycopg2.connect(**params, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT base_model FROM ai_model WHERE default_model IS TRUE "
                    "ORDER BY id LIMIT 1")
                row = cur.fetchone()
                return (row[0] or "").strip() if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def run_provenance(args: argparse.Namespace, model: str) -> dict[str, object]:
    """Everything needed to reproduce a run.

    A results file used to be a bare list of per-question records with no record
    of which model, which flags or which code produced it — which is how two
    byte-identical result files ended up under different names (AUDIT E-06/D-22).
    """
    import datetime as _dt
    import subprocess as _sp

    def _git(*a: str) -> str:
        try:
            return _sp.run(("git",) + a, cwd=os.path.dirname(os.path.abspath(__file__)),
                           capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    flags = {k: v for k, v in sorted(vars(args).items()) if k not in ("out",)}

    # In pipeline mode the model comes from the DB, not from --model (D-40).
    # Record what will actually run, and keep the requested value alongside so a
    # mismatch is visible in the file rather than silently resolved.
    effective = model
    requested = model
    mismatch = None
    if args.mode == "pipeline":
        configured = pipeline_model_in_use()
        effective = configured or "unknown"
        # getattr: run_provenance is also called with synthetic Namespaces in
        # tests, and must not require every argparse field to exist.
        requested_flag = getattr(args, "model", "")
        if configured and requested_flag and configured != requested_flag:
            mismatch = (f"--model={requested_flag!r} was IGNORED: pipeline mode "
                        f"uses the ai_model default {configured!r}")
        elif not configured:
            mismatch = "pipeline model could not be read from ai_model"

    return {
        "model": effective,
        "model_requested": requested,
        "model_source": ("ai_model.default_model" if args.mode == "pipeline"
                         else "--model/BIRD_MODEL"),
        "model_mismatch": mismatch,
        "mode": args.mode,
        "flags": flags,
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "bank": BANK,
        "utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    }


def run_tag(prov: dict[str, object]) -> str:
    """Short stable id for a (model, mode, flags) combination.

    Stamped on every record so a resumed run that changed a flag is detectable
    rather than silently blended into one file (AUDIT E-08).
    """
    import hashlib as _h
    payload = json.dumps({k: prov[k] for k in ("model", "mode", "flags")},
                         sort_keys=True, default=str)
    return _h.sha256(payload.encode()).hexdigest()[:12]


def stratified_sample(bank: list, n: int) -> list:
    """N questions spread evenly over (db_id, difficulty), deterministic.

    Round-robins across strata so every database appears before any database gets
    a second question, then keeps question_id order for a stable, resumable run.
    No RNG: the same N always yields the same subset, so two configurations can be
    compared on identical questions.
    """
    if n <= 0 or n >= len(bank):
        return bank
    strata = collections.OrderedDict()
    for q in bank:
        strata.setdefault((q["db_id"], q["difficulty"]), []).append(q)
    picked, keys = [], list(strata)
    while len(picked) < n:
        progressed = False
        for k in keys:
            if strata[k]:
                picked.append(strata[k].pop(0))
                progressed = True
                if len(picked) == n:
                    break
        if not progressed:
            break
    return sorted(picked, key=lambda q: q["question_id"])


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["model", "pipeline"], default="model")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--allow-mixed", action="store_true",
                    help="permit --resume into a file written under a different "
                         "configuration (E-08); off by default because the mix "
                         "is invisible in the results")
    # 300s was too tight while the model was still cold: q12/q27/q366 timed out
    # in the first run and were scored as failures.
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--descriptions", action="store_true",
                    help="include BIRD column descriptions in the schema")
    ap.add_argument("--promptfix", action="store_true",
                    help="use the v2 prompt (output shape, casting, PG dialect)")
    ap.add_argument("--only-db", default="",
                    help="comma-separated db_ids to restrict the run to")
    ap.add_argument("--sample", type=int, default=0,
                    help="stratified subset of N questions, spread evenly across "
                         "databases and difficulties. Use this INSTEAD of --limit for "
                         "a slice: --limit takes the first N by question_id, which "
                         "clusters into a few databases (a 40-question --limit slice "
                         "covered 4 of 11 dbs and missed formula_1 entirely, the one "
                         "database where context-overflow failures occur).")
    ap.add_argument("--finish", choices=["sql", "data"], default="sql",
                    help="pipeline mode depth: 'sql' stops after generation "
                         "(no execution retries/vote); 'data' runs the full "
                         "execute+retry+self-consistency path the UI ships")
    ap.add_argument("--model", default="", help="override BIRD_MODEL")
    ap.add_argument("--values", action="store_true",
                    help="F5: sample real values of low-cardinality text columns")
    ap.add_argument("--validate", action="store_true",
                    help="F4: sqlglot dialect lint + identifier check + EXPLAIN")
    ap.add_argument("--zero-row-retry", action="store_true",
                    help="F5: retry once when the query returns no rows")
    ap.add_argument("--repairs", type=int, default=0,
                    help="max execution-guided repair attempts per question")
    ap.add_argument("--joins", action="store_true",
                    help="F7: state the FK join graph as its own prompt block")
    ap.add_argument("--all-fixes", action="store_true",
                    help="root causes 1-7: descriptions + v2 prompt + few-shot + "
                         "join graph + value linking + validate/repair + zero-row retry")
    args = ap.parse_args()

    if args.all_fixes:
        args.descriptions = True     # 1  cryptic columns
        args.promptfix = True        # 2,3 casting + output shape + dialect
        args.joins = True            # 7  wrong join path
        args.values = True           # 6  filters that match no cell value
        args.validate = True         # 5  dialect/identifier errors before execution
        args.zero_row_retry = True   # 6  empty result set
        # 2, not 1: the first attempt is now informative (it names the table that
        # owns the missing column), so a second pass has something to work with.
        # Repairs only fire on questions that fail validation, so the cost is
        # paid on ~20% of the bank, not all of it.
        args.repairs = max(args.repairs, 2)

    model = args.model or MODEL

    out_path = args.out or f"/tmp/bird_{args.mode}.json"
    bank = json.load(open(BANK))
    if args.only_db:
        keep = {d.strip() for d in args.only_db.split(",") if d.strip()}
        bank = [q for q in bank if q["db_id"] in keep]
    if args.sample:
        bank = stratified_sample(bank, args.sample)
        print(f"[sample] stratified {len(bank)} questions across "
              f"{len({q['db_id'] for q in bank})} databases", flush=True)
    elif args.limit:
        bank = bank[:args.limit]

    results = []
    done = set()
    _prov = run_provenance(args, model)
    _tag = run_tag(_prov)
    # Say it out loud at launch, not only in the results file: a run that
    # silently uses a different model than the operator asked for wastes hours
    # of GPU and produces a mislabelled file (AUDIT D-40).
    print(f"[model] mode={args.mode} using={_prov['model']!r} "
          f"(source: {_prov['model_source']})")
    if _prov.get("model_mismatch"):
        print(f"[model] WARNING: {_prov['model_mismatch']}")
        print("[model] To change the pipeline model, update the ai_model row "
              "whose default_model is true; --model cannot do it.")
    if args.resume and os.path.exists(out_path):
        results = json.load(open(out_path))
        done = {r["question_id"] for r in results}
        print(f"[resume] {len(done)} already scored in {out_path}")
        # E-08: refuse to blend records produced under a different configuration
        prior = {r.get("run_tag") for r in results if r.get("run_tag")}
        mismatched = prior - {_tag}
        if mismatched:
            print(f"[resume] REFUSING: {out_path} holds records from a different "
                  f"configuration (tags {sorted(mismatched)}, this run {_tag}).\n"
                  f"          Resuming would silently mix configurations in one "
                  f"file. Use a new --out, or --allow-mixed to override.",
                  flush=True)
            if not args.allow_mixed:
                sys.exit(2)

    ds_map = {}
    if args.mode == "pipeline":
        ds_map = json.load(open(DS_MAP))

    print(f"[cfg] mode={args.mode} model={model} questions={len(bank)} "
          f"timeout={args.timeout}s out={out_path}", flush=True)
    print(f"[fix] F6-descriptions={args.descriptions} F2/F3-promptV2={args.promptfix} "
          f"F5-values={args.values} F4-validate={args.validate} "
          f"F5-zeroRowRetry={args.zero_row_retry} repairs={args.repairs}", flush=True)

    if args.mode == "model":
        # F1: load the model before the clock starts. The first run's three
        # timeouts were all cold-start -- the host unloads after every answer, so
        # question 1 paid for a full model load on top of its own inference.
        try:
            t_warm = time.time()
            chat("Reply with the word ok.", args.timeout, model)
            print(f"[warm] {model} resident after {time.time()-t_warm:.0f}s", flush=True)
        except Exception as e:
            print(f"[warm] pre-warm failed ({type(e).__name__}), continuing", flush=True)

        if args.values:
            t_v = time.time()
            for db in sorted({q["db_id"] for q in bank}):
                get_schema(db, args.descriptions, args.values)
            print(f"[schema] value-linked schemas built in {time.time()-t_v:.0f}s",
                  flush=True)

    t_start = time.time()
    for i, entry in enumerate(bank, 1):
        qid = entry["question_id"]
        if qid in done:
            continue
        rec = {"question_id": qid, "db_id": entry["db_id"],
               "difficulty": entry["difficulty"], "question": entry["question"],
               "ex": 0, "ex_tol": 0, "f1": 0.0, "status": "FAIL",
               "sql": "", "detail": "", "trace": []}
        t0 = time.time()
        try:
            if args.mode == "model":
                trace = rec["trace"]
                if args.repairs or args.validate or args.zero_row_retry:
                    sql, msg = generate_with_repair(entry, args, model, trace), ""
                else:
                    raw = ask_model(entry, args.timeout, args.descriptions,
                                    args.promptfix, model)
                    sql, msg = extract_sql(raw), ""
            else:
                sql, msg = ask_pipeline(entry, ds_map[entry["db_id"]],
                                        finish=args.finish)
                sql = extract_sql(sql) if sql else ""
            rec["sql"] = sql[:2000]

            if not sql:
                rec["status"], rec["detail"] = "NO-SQL", msg or "no SQL produced"
            else:
                try:
                    pred = run_sql(sql, entry["db_id"])
                except Exception as e:
                    rec["status"] = "SQL-ERR"
                    rec["detail"] = str(e).strip().splitlines()[0][:200]
                else:
                    gold = run_sql(entry["SQL"], entry["db_id"])
                    rec["ex"] = calculate_ex(pred, gold)
                    rec["ex_tol"] = calculate_ex_tolerant(pred, gold)
                    rec["f1"] = round(calculate_f1_score(pred, gold), 4)
                    rec["f1_tol"] = round(calculate_f1_tolerant(pred, gold), 4)
                    rec["status"] = ("CORRECT" if rec["ex"]
                                     else "NEAR" if rec["ex_tol"] else "WRONG")
                    rec["detail"] = f"pred_rows={len(pred)} gold_rows={len(gold)}"
        except Exception as e:
            rec["detail"] = f"{type(e).__name__}: {str(e)[:200]}"
            # A timeout says the GPU was slow, not that the model was wrong.
            # Scored as wrong for official-EX compatibility, but tagged so a
            # slow host is visible instead of silently depressing the score
            # (AUDIT E-12).
            if _is_timeout(e):
                rec["status"] = "TIMEOUT"

        rec["seconds"] = round(time.time() - t0, 1)
        rec["run_tag"] = _tag
        results.append(rec)
        # The results file stays a bare LIST for backward compatibility with
        # bird_analyze.py / bird_diff.py / bird_significance.py / rescore.py;
        # full provenance goes in a sidecar (AUDIT E-06).
        json.dump(results, open(out_path, "w"), indent=1, default=str)
        json.dump({**_prov, "run_tag": _tag, "questions_scored": len(results)},
                  open(out_path + ".meta.json", "w"), indent=1, default=str)

        n = len(results)
        ex_so_far = 100 * sum(r["ex"] for r in results) / n
        eta = (time.time() - t_start) / max(1, n - len(done)) * (len(bank) - n) / 60
        print(f"  [{rec['status']:8}] ({i}/{len(bank)}) q{qid} {entry['difficulty'][:4]} "
              f"{entry['db_id'][:20]:20} f1={rec['f1']:.2f} {rec['seconds']:5.1f}s "
              f"| EX so far {ex_so_far:5.1f}% | ETA {eta:.0f}m", flush=True)
        print(f"             {rec['detail'][:110]}", flush=True)

    tag = args.mode + ("+desc" if args.descriptions else "") + ("+v2" if args.promptfix else "")
    report(results, out_path, f"{tag} [{model}]")


def report(results, out_path, mode):
    n = len(results)
    if not n:
        print("no results")
        return
    ex = sum(r["ex"] for r in results)
    ex_tol = sum(r.get("ex_tol", 0) for r in results)
    f1 = sum(r["f1"] for r in results)
    f1_tol = sum(r.get("f1_tol", r["f1"]) for r in results)

    print(f"\n==== BIRD Mini-Dev (150-subset) -- mode={mode} ====")
    print(f"  questions      : {n}")
    print(f"  EX (official)  : {ex}/{n} = {100*ex/n:.1f}%")
    print(f"  EX (tolerant)  : {ex_tol}/{n} = {100*ex_tol/n:.1f}%   "
          f"(+{ex_tol-ex} float/type-only misses)")
    print(f"  Soft-F1        : {100*f1/n:.1f}%")
    print(f"  Soft-F1 (tol)  : {100*f1_tol/n:.1f}%   "
          f"(paired with EX-tolerant; see AUDIT E-09)")

    # AUDIT E-12: timeouts are a property of the host, not of the model. They
    # are counted as wrong above (official-metric compatibility), so surface
    # them explicitly -- otherwise a slow GPU quietly depresses the score in a
    # way a faster one would not, and the run looks like a worse model.
    timed_out = [r for r in results if r.get("status") == "TIMEOUT"]
    if timed_out:
        ex_ex = 100 * ex / (n - len(timed_out)) if n > len(timed_out) else 0.0
        print(f"  TIMEOUTS       : {len(timed_out)}/{n} "
              f"({100*len(timed_out)/n:.1f}%) -- counted as WRONG above")
        print(f"    EX excl. them: {ex}/{n-len(timed_out)} = {ex_ex:.1f}%   "
              f"<- compare runs on different hardware with THIS number")
        print(f"    qids         : {[r['question_id'] for r in timed_out][:20]}")

    print("\n  by difficulty:")
    for d in ("simple", "moderate", "challenging"):
        sub = [r for r in results if r["difficulty"] == d]
        if sub:
            e = sum(r["ex"] for r in sub)
            t = sum(r.get("ex_tol", 0) for r in sub)
            print(f"    {d:12} {e:3}/{len(sub):3} = {100*e/len(sub):5.1f}%   "
                  f"(tol {100*t/len(sub):5.1f}%)   "
                  f"soft-f1 {100*sum(r['f1'] for r in sub)/len(sub):5.1f}%")

    print("\n  by status:")
    for k, v in sorted(collections.Counter(r["status"] for r in results).items()):
        print(f"    {k:9} {v:3}")

    print("\n  by database:")
    for db in sorted({r["db_id"] for r in results}):
        sub = [r for r in results if r["db_id"] == db]
        e = sum(r["ex"] for r in sub)
        print(f"    {db:26} {e:3}/{len(sub):3} = {100*e/len(sub):5.1f}%")

    secs = [r.get("seconds", 0) for r in results]
    print(f"\n  time           : total {sum(secs)/60:.0f}m, "
          f"median {sorted(secs)[len(secs)//2]:.1f}s/question")
    print(f"\n[out] {out_path}")


if __name__ == "__main__":
    main()
