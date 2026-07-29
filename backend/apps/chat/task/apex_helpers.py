"""APEX-SQL helpers: schema string parser/rebuilder, probe-result compressor, and
common utilities used by the dual-pathway pruning and parallel data profiling stages.

The schema string produced by `get_table_schema()` has this shape:

    【DB_ID】 dbname
    【Schema】
    # Table: schema.tname, optional_comment
    [
    (col1:type1),
    (col2:type2, optional_comment),
    ...
    ]
    # Table: another_table
    [
    (col_a:typeA)
    ]

These helpers parse that into a structured representation, allow filtering by
table/column name, and rebuild a syntactically identical schema string.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


_HEADER_RE = re.compile(r"^【DB_ID】\s*(?P<db>.*?)\s*$", re.MULTILINE)
_TABLE_HEADER_RE = re.compile(r"^#\s*Table:\s*(?P<header>.+?)\s*$", re.MULTILINE)
# A column entry is `(name:type)` or `(name:type, comment)` — comment may itself
# contain commas, so we capture greedily up to the closing paren.
_COLUMN_RE = re.compile(r"\(\s*(?P<name>[^:)\s][^:]*?)\s*:\s*(?P<rest>[^)]*?)\s*\)")


def parse_schema(schema_str: str) -> Dict[str, Any]:
    """Parse a schema string into a structured dict.

    Returns:
        {
          "db_id": "name" or "",
          "header_prefix": "<everything before first # Table:>",
          "tables": [
            {
              "raw_header": "# Table: schema.tname, comment",
              "table_name": "tname" or "schema.tname",
              "columns": [
                {"name": "col", "type": "int", "comment": "..."},
                ...
              ]
            }
          ]
        }
    """
    result: Dict[str, Any] = {"db_id": "", "header_prefix": "", "tables": []}
    if not schema_str:
        return result

    m = _HEADER_RE.search(schema_str)
    if m:
        result["db_id"] = m.group("db").strip()

    first_table_match = _TABLE_HEADER_RE.search(schema_str)
    if not first_table_match:
        result["header_prefix"] = schema_str
        return result

    result["header_prefix"] = schema_str[: first_table_match.start()]

    table_matches = list(_TABLE_HEADER_RE.finditer(schema_str))
    for i, tm in enumerate(table_matches):
        start = tm.end()
        end = table_matches[i + 1].start() if i + 1 < len(table_matches) else len(schema_str)
        body = schema_str[start:end]

        header_text = tm.group("header").strip()
        # split off trailing comment if present (header may be "schema.t, comment")
        if "," in header_text:
            qualified, _comment = header_text.split(",", 1)
        else:
            qualified = header_text
        qualified = qualified.strip()
        table_name = qualified.split(".")[-1].strip() if "." in qualified else qualified

        columns: List[Dict[str, str]] = []
        for cm in _COLUMN_RE.finditer(body):
            name = cm.group("name").strip()
            rest = cm.group("rest").strip()
            if "," in rest:
                ctype, comment = rest.split(",", 1)
                columns.append({"name": name, "type": ctype.strip(), "comment": comment.strip()})
            else:
                columns.append({"name": name, "type": rest, "comment": ""})

        result["tables"].append({
            "raw_header": tm.group(0),
            "qualified": qualified,
            "table_name": table_name,
            "columns": columns,
        })

    return result


def rebuild_schema(parsed: Dict[str, Any]) -> str:
    """Rebuild a schema string from the structured form. Empty tables are dropped."""
    out = parsed.get("header_prefix", "") or ""
    if not out.endswith("\n"):
        out += "\n"
    for t in parsed.get("tables", []):
        cols = t.get("columns", [])
        if not cols:
            continue
        header = t.get("raw_header", f"# Table: {t.get('qualified', t.get('table_name'))}").rstrip()
        out += header + "\n[\n"
        parts: List[str] = []
        for c in cols:
            if c.get("comment"):
                parts.append(f"({c['name']}:{c['type']}, {c['comment']})")
            else:
                parts.append(f"({c['name']}:{c['type']})")
        out += ",\n".join(parts) + "\n]\n"
    return out


def apply_pruning(parsed: Dict[str, Any],
                  delete_tables: List[str],
                  delete_columns: Dict[str, List[str]],
                  keep_tables: List[str],
                  keep_columns: Dict[str, List[str]]) -> Dict[str, Any]:
    r"""Apply the union rule: pruned = (Batch \ C_del) ∪ C_keep.

    A column survives unless it is explicitly in delete_columns AND not in keep_columns.
    A table survives unless it is explicitly in delete_tables AND not in keep_tables.
    """
    del_tables = {t.strip() for t in (delete_tables or []) if t}
    keep_tables_set = {t.strip() for t in (keep_tables or []) if t}
    del_cols = {t.strip(): {c.strip() for c in cs} for t, cs in (delete_columns or {}).items() if t}
    keep_cols = {t.strip(): {c.strip() for c in cs} for t, cs in (keep_columns or {}).items() if t}

    out_tables: List[Dict[str, Any]] = []
    for t in parsed.get("tables", []):
        tname = t.get("table_name", "")
        if tname in del_tables and tname not in keep_tables_set:
            # table fully removed
            continue
        survived_cols: List[Dict[str, str]] = []
        for c in t.get("columns", []):
            cname = c.get("name", "")
            in_del = cname in del_cols.get(tname, set())
            in_keep = cname in keep_cols.get(tname, set())
            if in_del and not in_keep:
                continue
            survived_cols.append(c)
        if survived_cols:
            new_t = dict(t)
            new_t["columns"] = survived_cols
            out_tables.append(new_t)

    return {
        "db_id": parsed.get("db_id", ""),
        "header_prefix": parsed.get("header_prefix", ""),
        "tables": out_tables,
    }


def schema_to_table_blocks(parsed: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return [(table_name, single-table-schema-string), ...] for per-table prompts."""
    blocks: List[Tuple[str, str]] = []
    for t in parsed.get("tables", []):
        single = {"db_id": parsed.get("db_id", ""), "header_prefix": parsed.get("header_prefix", ""), "tables": [t]}
        blocks.append((t.get("table_name", ""), rebuild_schema(single)))
    return blocks


def batch_tables_by_token_budget(parsed: Dict[str, Any], approx_tokens_per_batch: int = 10_000) -> List[Dict[str, Any]]:
    """Split tables into batches that fit a rough token budget (4 chars ≈ 1 token).

    Tables with identical column signatures are merged into one entry whose
    header lists every matching table name — this is the paper's Schema Merging
    optimisation. A merged group is treated as a single batch unit.
    """
    char_budget = approx_tokens_per_batch * 4

    # 1) Group identical-signature tables together.
    signature_groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in parsed.get("tables", []):
        sig = json.dumps(
            [(c["name"], c["type"]) for c in t.get("columns", [])],
            sort_keys=False,
        )
        signature_groups.setdefault(sig, []).append(t)

    # 2) For groups with >1 table, build a synthetic representative.
    grouped: List[Dict[str, Any]] = []
    for sig, ts in signature_groups.items():
        if len(ts) == 1:
            grouped.append(ts[0])
            continue
        rep = dict(ts[0])
        names = [x.get("table_name") for x in ts]
        rep["raw_header"] = f"# Table: <SHARED_SCHEMA>, applies to: {', '.join(names)}"
        rep["table_name"] = ts[0].get("table_name")
        rep["_aliases"] = names
        grouped.append(rep)

    # 3) Pack into batches.
    batches: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {
        "db_id": parsed.get("db_id", ""),
        "header_prefix": parsed.get("header_prefix", ""),
        "tables": [],
    }
    current_chars = 0
    for t in grouped:
        single = {"db_id": parsed.get("db_id", ""), "header_prefix": parsed.get("header_prefix", ""), "tables": [t]}
        size = len(rebuild_schema(single))
        if current["tables"] and current_chars + size > char_budget:
            batches.append(current)
            current = {
                "db_id": parsed.get("db_id", ""),
                "header_prefix": parsed.get("header_prefix", ""),
                "tables": [],
            }
            current_chars = 0
        current["tables"].append(t)
        current_chars += size
    if current["tables"]:
        batches.append(current)
    return batches


def safe_json_loads(text: str) -> Optional[Any]:
    """Extract and parse a JSON object from a possibly noisy LLM response."""
    if not text:
        return None
    # First try direct parse.
    try:
        return json.loads(text)
    except Exception:
        pass
    # Then try to find the first '{' ... matching '}' block.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start : i + 1]
                try:
                    return json.loads(snippet)
                except Exception:
                    return None
    return None


def merge_keep_sets(parsed: Dict[str, Any],
                    delete_tables_per_batch: List[List[str]],
                    delete_columns_per_batch: List[Dict[str, List[str]]],
                    keep_tables_per_batch: List[List[str]],
                    keep_columns_per_batch: List[Dict[str, List[str]]]) -> Tuple[List[str], Dict[str, List[str]], List[str], Dict[str, List[str]]]:
    """Flatten per-batch decisions into global sets."""
    del_t: set[str] = set()
    keep_t: set[str] = set()
    del_c: Dict[str, set[str]] = {}
    keep_c: Dict[str, set[str]] = {}

    for batch in delete_tables_per_batch or []:
        for t in batch or []:
            if t:
                del_t.add(t.strip())
    for batch in keep_tables_per_batch or []:
        for t in batch or []:
            if t:
                keep_t.add(t.strip())
    for batch in delete_columns_per_batch or []:
        for t, cols in (batch or {}).items():
            del_c.setdefault(t.strip(), set()).update(c.strip() for c in cols if c)
    for batch in keep_columns_per_batch or []:
        for t, cols in (batch or {}).items():
            keep_c.setdefault(t.strip(), set()).update(c.strip() for c in cols if c)

    return (
        sorted(del_t),
        {t: sorted(cs) for t, cs in del_c.items()},
        sorted(keep_t),
        {t: sorted(cs) for t, cs in keep_c.items()},
    )


_BACKTICK_DIALECTS = {"mysql", "doris", "starrocks", "hive"}
_BRACKET_DIALECTS = {"sqlserver", "mssql"}
# Everything else (pg, snowflake, oracle, sqlite, dm, kingbase, redshift, ch, es) -> double quotes.


def quote_ident(name: str, ds_type: Optional[str]) -> str:
    """Return ``name`` quoted appropriately for the given dialect.

    Always quotes, even when the identifier looks safe, because the schema we
    receive may contain mixed-case or reserved-word identifiers (the Excel
    importer happily produces names like ``Dashboard_6b12db3a38``) and PostgreSQL
    silently lower-cases unquoted identifiers, which yields confusing
    "relation does not exist" errors at runtime.
    """
    if name is None:
        return ""
    s = str(name)
    t = (ds_type or "").lower()
    if t in _BACKTICK_DIALECTS:
        return "`" + s.replace("`", "``") + "`"
    if t in _BRACKET_DIALECTS:
        return "[" + s.replace("]", "]]") + "]"
    return '"' + s.replace('"', '""') + '"'


def quoted_table_ref(ds_type: Optional[str], db_id: str, table_name: str) -> str:
    """Build a fully-quoted ``"db"."table"`` (or dialect equivalent) reference.

    If ``db_id`` is empty, returns just the quoted table name (some dialects
    such as MySQL/SQLite don't carry a schema prefix in our stored schema).
    """
    if db_id:
        # Some dialects (mysql/sqlite/hive/doris/starrocks/es) don't take a schema prefix.
        t = (ds_type or "").lower()
        if t in {"sqlite", "es", "hive"}:
            return quote_ident(table_name, ds_type)
        return f"{quote_ident(db_id, ds_type)}.{quote_ident(table_name, ds_type)}"
    return quote_ident(table_name, ds_type)


def quoted_columns_list(ds_type: Optional[str], columns: List[Dict[str, str]]) -> List[str]:
    """Return each column name pre-quoted for the dialect."""
    return [quote_ident(c.get("name", ""), ds_type) for c in (columns or []) if c.get("name")]


def is_probe_failure_summary(summary: str) -> bool:
    """A probe-result string is a 'failure' if it begins with our sentinel or
    looks like a database error we should hide from downstream prompts."""
    if not summary:
        return True
    s = summary.lstrip().lower()
    return s.startswith("(probe failed") or s.startswith("(empty)")


_PG_FAMILY = {"excel", "pg", "kingbase"}


def fts_prompt_addendum(ds_type, enabled: bool) -> str:
    """Scoped PostgreSQL full-text-search guidance for the SQL system prompt.

    Returns guidance only for PostgreSQL-family engines (Excel data lives in our
    PG) and only when enabled; '' otherwise, so non-PG datasources never get
    PG-specific syntax. Generic — no column/table names baked in.
    """
    if not enabled:
        return ""
    if (ds_type or "").strip().lower() not in _PG_FAMILY:
        return ""
    return (
        "\n- For free-text search questions (find/about/mentions/containing specific "
        "words in a TEXT column), prefer PostgreSQL full-text search of the form "
        "to_tsvector('simple', the_text_column) @@ plainto_tsquery('simple', 'the search words') "
        "(substitute the real column name and the user's search words). "
        "Use normal comparisons (=, <, >, LIKE) for exact or structured filters."
    )


_PARALLEL_SUFFIX_RE = re.compile(r"[\s._\-]*\d+$")


def _parallel_base_name(name: str) -> str:
    """Normalise a column name to its 'role base' by stripping a trailing numeric
    suffix that dedup/repetition produces.

    Excel/CSV imports rename duplicate headers (two identically-named columns
    become ``col`` + ``col_1`` / ``col.1`` / ``col 1`` / ``col2``). Collapsing the
    trailing number groups those siblings back together. Purely lexical — no table
    or column names are baked in.
    """
    if not name:
        return ""
    base = _PARALLEL_SUFFIX_RE.sub("", str(name).strip())
    return (base or str(name).strip()).lower()


def detect_parallel_column_groups(schema_str: str) -> List[Tuple[str, List[str]]]:
    """Find columns that repeat within a table — the schema-only signal that the
    table stores the same kind of value across several parallel columns.

    A group is any set of >=2 columns in the same table whose normalised base
    name matches (e.g. ``col`` + ``col_1``). Original column names are kept in
    schema order so the caller can convey adjacency.

    Returns ``[(table_name, [col, col, ...]), ...]`` for every such group.
    """
    parsed = parse_schema(schema_str)
    groups: List[Tuple[str, List[str]]] = []
    for t in parsed.get("tables", []):
        buckets: Dict[str, List[str]] = {}
        order: List[str] = []
        for c in t.get("columns", []):
            cname = c.get("name", "")
            if not cname:
                continue
            base = _parallel_base_name(cname)
            if base not in buckets:
                buckets[base] = []
                order.append(base)
            buckets[base].append(cname)
        tname = t.get("table_name", "")
        for base in order:
            cols = buckets[base]
            if len(cols) >= 2:
                groups.append((tname, cols))
    return groups


def parallel_columns_addendum(schema_str: str, enabled: bool = True) -> str:
    """Generic SQL-prompt guidance for tables that store one concept across
    several parallel columns (repeated role columns, each paired with its own
    entity column).

    Returns '' when disabled or when no repeated columns exist, so ordinary
    single-column tables are never affected. When repeated columns are found it
    names the actual columns of the loaded schema (fully dynamic, per document)
    and instructs the model to UNION across the parallel columns instead of
    answering from just one — the fix for "the rest of the answer is in another
    column" cases.
    """
    if not enabled:
        return ""
    groups = detect_parallel_column_groups(schema_str)
    if not groups:
        return ""
    lines = "\n".join(
        f'    - table "{tname}": repeated columns [{", ".join(cols)}]'
        for tname, cols in groups
    )
    return (
        "\n- IMPORTANT (parallel / repeated columns): some tables record the SAME concept "
        "in several columns that each describe a DIFFERENT participant of the same row. "
        "In this schema:\n"
        f"{lines}\n"
        "  Each repeated column belongs to a DIFFERENT role in the row and is paired with "
        "its OWN entity column: repeated attribute column A pairs with entity column A, "
        "repeated attribute column B pairs with entity column B, and so on. When the user "
        "asks to list an entity filtered by a value that could appear in any of these "
        "repeated columns, you MUST return matches from EVERY pairing using UNION: write "
        "ONE SELECT per repeated column, and in each SELECT return THAT column's own "
        "paired entity column (not always the same one). General shape:\n"
        "      SELECT <entity_A> FROM <table> WHERE <attr_A> = '<value>'\n"
        "      UNION\n"
        "      SELECT <entity_B> FROM <table> WHERE <attr_B> = '<value>'\n"
        "  Do NOT use a single SELECT with OR across the repeated columns, and do NOT "
        "return only the first entity column - that yields the wrong entity for some "
        "matches. Infer each pairing from the column names and sample values. This keeps "
        "the answer complete for any table with repeated columns."
    )


def compress_probe_result(result: Dict[str, Any], max_rows: int = 10) -> str:
    """Compress an exec_sql() result into a compact statistical summary."""
    if not result:
        return "(empty)"
    fields = result.get("fields") or []
    data = result.get("data") or []
    row_count = len(data)
    if row_count == 0:
        return f"0 rows. fields: {list(fields)}"

    sample_rows = data[:max_rows]
    summary: List[str] = [f"rows: {row_count} (showing {len(sample_rows)})"]
    summary.append(f"fields: {list(fields)}")

    # Per-field stats
    for f in fields:
        values = [r.get(f) for r in data]
        non_null = [v for v in values if v is not None]
        distinct = set()
        for v in non_null:
            try:
                distinct.add(v if isinstance(v, (str, int, float, bool)) else str(v))
            except Exception:
                pass
            if len(distinct) > 20:
                break
        null_count = row_count - len(non_null)
        summary.append(
            f"  {f}: null={null_count}/{row_count}, distinct≈{len(distinct)}"
        )

    # Compact sample
    try:
        rendered = json.dumps(sample_rows, ensure_ascii=False, default=str)
        if len(rendered) > 2000:
            rendered = rendered[:2000] + "...(truncated)"
        summary.append(f"sample: {rendered}")
    except Exception:
        pass

    return "\n".join(summary)
