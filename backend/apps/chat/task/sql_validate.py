"""Deterministic identifier validation for generated SQL.

The SQL system prompt asks the model, across three separate `priority="critical"`
rules and a dedicated <SQL-Generation-Process> step, to copy table and column
identifiers verbatim out of <m-schema> — no case changes, no
traditional/simplified Chinese conversion. Nothing enforced that: the only
pre-execution check was JSON parsing, so an invented or transliterated
identifier was discovered by the database, several steps later, as an opaque
driver error.

This module closes the gap by parsing the generated SQL and set-comparing its
identifiers against the schema string that was actually placed in the prompt.
Two findings are reported, both chosen to be high-confidence:

  unknown-table / unknown-column
      The identifier has no counterpart in the schema under ANY casing. The
      model invented it.
  case-mismatch
      A counterpart exists but differs in case or character form. This is the
      failure the prompt's identifier rules are written against, and it is fatal
      on quoted-identifier dialects (the Excel datasource is PostgreSQL).

Contract (mirrors agentic.py / apex_helpers.py): the comparison logic is pure
and dependency-free, so it is unit-testable without a database, a model, or
sqlglot. Only `extract_identifiers` touches sqlglot, and every failure path in
this module FAILS OPEN — an unparseable statement, an unsupported dialect or a
missing dependency yields "nothing to report". A validator bug can slow the
pipeline down; it must never be able to block a correct query.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from apps.chat.task.apex_helpers import parse_schema

# ---------------------------------------------------------------------------
# Dialect mapping: SQLBot datasource type -> sqlglot dialect name.
# Types absent from this map are validated with sqlglot's permissive default
# parser; types in _SKIP_DIALECTS are not validated at all because their query
# language is not SQL enough for identifier extraction to mean anything.
# ---------------------------------------------------------------------------
_DIALECT_BY_DS_TYPE: Dict[str, str] = {
    'excel': 'postgres',      # Excel/CSV imports live in the bundled Postgres
    'pg': 'postgres',
    'kingbase': 'postgres',   # PG-derived
    'dm': 'postgres',         # close enough for identifier extraction
    'redshift': 'redshift',
    'mysql': 'mysql',
    'doris': 'mysql',         # MySQL wire protocol / syntax
    'starrocks': 'mysql',
    'ck': 'clickhouse',
    'sqlite': 'sqlite',
    'oracle': 'oracle',
    'hive': 'hive',
    'sqlserver': 'tsql',
    'mssql': 'tsql',
}

_SKIP_DIALECTS = frozenset({'es'})

# Dialects where a case difference in an identifier actually breaks the query.
# These are the ones SQLBot quotes with '"' (see apps/db/constant.py), so the
# quoted name must match the stored name exactly. The backtick dialects
# (MySQL/Doris/StarRocks) resolve column names case-insensitively, and ClickHouse
# /SQLite/Hive are likewise forgiving — reporting a case mismatch there would
# force a regeneration for SQL that would have executed perfectly.
_CASE_SENSITIVE_DS_TYPES = frozenset({
    'excel', 'pg', 'kingbase', 'dm', 'redshift', 'oracle', 'sqlserver', 'mssql',
})


def case_sensitive_identifiers(ds_type: Optional[str]) -> bool:
    """True when an identifier's case must match the schema exactly."""
    if not ds_type:
        return False
    return str(ds_type).strip().lower() in _CASE_SENSITIVE_DS_TYPES


def sqlglot_dialect(ds_type: Optional[str]) -> Optional[str]:
    """Return the sqlglot dialect for a datasource type, or None to use the
    permissive default parser."""
    if not ds_type:
        return None
    return _DIALECT_BY_DS_TYPE.get(str(ds_type).strip().lower())


def should_validate(ds_type: Optional[str]) -> bool:
    """False for datasource types whose query language is not SQL."""
    if not ds_type:
        return True
    return str(ds_type).strip().lower() not in _SKIP_DIALECTS


# ---------------------------------------------------------------------------
# Normalisation. Identifiers are compared on a casefolded, quote-stripped form.
# Casefold (not lower) so non-ASCII identifiers compare sanely; the ORIGINAL
# spelling is always retained so feedback can name the exact expected string.
# ---------------------------------------------------------------------------
_QUOTE_CHARS = '"`\'[]'


def normalize_identifier(name: str) -> str:
    if not name:
        return ''
    return str(name).strip().strip(_QUOTE_CHARS).strip().casefold()


# ---------------------------------------------------------------------------
# Schema index — built from the same schema string that went into the prompt.
# ---------------------------------------------------------------------------
def build_schema_index(schema_str: str) -> Dict[str, Any]:
    """Index the prompt's schema string for identifier lookup.

    Returns::

        {
          'tables':  {norm_table: original_table},
          'columns': {norm_table: {norm_col: original_col}},
          'all_columns': {norm_col: original_col},   # union across tables
        }

    An empty index means "unknown schema" and disables validation downstream.
    """
    index: Dict[str, Any] = {'tables': {}, 'columns': {}, 'all_columns': {}}
    parsed = parse_schema(schema_str or '')
    for table in parsed.get('tables') or []:
        raw_name = (table.get('table_name') or '').strip()
        if not raw_name:
            continue
        norm_table = normalize_identifier(raw_name)
        index['tables'][norm_table] = raw_name
        col_map: Dict[str, str] = index['columns'].setdefault(norm_table, {})
        for column in table.get('columns') or []:
            raw_col = (column.get('name') or '').strip()
            if not raw_col:
                continue
            norm_col = normalize_identifier(raw_col)
            col_map[norm_col] = raw_col
            index['all_columns'].setdefault(norm_col, raw_col)
    return index


def schema_index_is_usable(index: Optional[Dict[str, Any]]) -> bool:
    """Validation requires at least one table with at least one column;
    anything less means we cannot tell an invented name from an unindexed one."""
    if not index:
        return False
    if not index.get('tables'):
        return False
    return bool(index.get('all_columns'))


# ---------------------------------------------------------------------------
# sqlglot adapter. The ONLY impure function here. Returns None (= "cannot tell",
# skip validation) on any failure whatsoever.
# ---------------------------------------------------------------------------
def extract_identifiers(sql: str, dialect: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Parse ``sql`` and return the identifiers it references::

        {
          'tables':  [table_name, ...],              # physical table refs only
          'columns': [(qualifier_or_None, col), ...],
          'locals':  {name, ...},   # CTE names, table aliases, output aliases
        }

    ``locals`` collects every name the statement defines for itself — CTE names,
    derived-table aliases and SELECT-list aliases. Those are legitimately absent
    from the schema (``SELECT SUM(x) AS total ... ORDER BY total`` references
    ``total`` as a column), so callers must exclude them before reporting an
    unknown identifier. Being generous here trades a little detection power for
    a much lower false-positive rate, which is the right trade for a check that
    can force a regeneration.
    """
    try:
        import sqlglot
        from sqlglot import exp
    except Exception:
        return None

    try:
        tree = sqlglot.parse_one(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return None
    if tree is None:
        return None

    try:
        locals_: Set[str] = set()

        # names the statement defines for itself
        for cte in tree.find_all(exp.CTE):
            alias = cte.alias
            if alias:
                locals_.add(alias)
        for alias_node in tree.find_all(exp.Alias):
            alias = alias_node.alias
            if alias:
                locals_.add(alias)
        for table_alias in tree.find_all(exp.TableAlias):
            name = table_alias.name
            if name:
                locals_.add(name)

        tables: List[str] = []
        for table_node in tree.find_all(exp.Table):
            name = table_node.name
            if not name:
                continue
            # a reference to a CTE is not a physical table
            if name in locals_:
                continue
            tables.append(name)
            alias = table_node.alias
            if alias:
                locals_.add(alias)

        columns: List[Tuple[Optional[str], str]] = []
        for column_node in tree.find_all(exp.Column):
            name = column_node.name
            if not name or name == '*':
                continue
            qualifier = column_node.table or None
            columns.append((qualifier, name))

        return {'tables': tables, 'columns': columns, 'locals': locals_}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pure comparison core. Everything below is stdlib-only and directly testable.
# ---------------------------------------------------------------------------
def diff_identifiers(extracted: Dict[str, Any], schema_index: Dict[str, Any],
                     report_case_mismatch: bool = True) -> List[Dict[str, str]]:
    """Compare extracted identifiers against the schema index.

    Returns a list of findings, each ``{'kind', 'identifier', 'expected'}``
    where ``kind`` is one of ``unknown-table``, ``unknown-column``,
    ``case-mismatch-table``, ``case-mismatch-column`` and ``expected`` is the
    schema's exact spelling ('' when the identifier is unknown outright).

    ``report_case_mismatch`` must be False on dialects that resolve identifiers
    case-insensitively (see `case_sensitive_identifiers`); the unknown-* findings
    are dialect-independent and always reported.

    Pure: no sqlglot, no database, no settings.
    """
    if not extracted or not schema_index_is_usable(schema_index):
        return []

    schema_tables: Dict[str, str] = schema_index.get('tables') or {}
    schema_columns: Dict[str, Dict[str, str]] = schema_index.get('columns') or {}
    all_columns: Dict[str, str] = schema_index.get('all_columns') or {}

    local_names = {normalize_identifier(n) for n in (extracted.get('locals') or set())}

    findings: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    def _record(kind: str, identifier: str, expected: str) -> None:
        key = (kind, identifier)
        if key in seen:
            return
        seen.add(key)
        findings.append({'kind': kind, 'identifier': identifier, 'expected': expected})

    # --- tables -------------------------------------------------------------
    # Qualified names ("db"."t") arrive as bare table names from sqlglot's
    # .name, so a plain lookup is correct here.
    for table in extracted.get('tables') or []:
        norm = normalize_identifier(table)
        if not norm or norm in local_names:
            continue
        if norm not in schema_tables:
            _record('unknown-table', table, '')
        elif (report_case_mismatch
              and schema_tables[norm] != str(table).strip().strip(_QUOTE_CHARS).strip()):
            _record('case-mismatch-table', table, schema_tables[norm])

    # --- columns ------------------------------------------------------------
    for qualifier, column in extracted.get('columns') or []:
        norm_col = normalize_identifier(column)
        if not norm_col or norm_col in local_names:
            continue

        norm_qual = normalize_identifier(qualifier) if qualifier else ''
        # Only attribute a column to a specific table when the qualifier names a
        # real schema table. A qualifier that is an alias or a derived table is
        # unresolvable here, so fall back to the union of all columns.
        scoped: Optional[Dict[str, str]] = None
        if norm_qual and norm_qual in schema_tables and norm_qual in schema_columns:
            scoped = schema_columns[norm_qual]

        lookup = scoped if scoped is not None else all_columns
        if norm_col not in lookup:
            # A column missing from its qualified table but present elsewhere is
            # a wrong-table error, not an invented name; report it as unknown
            # against the table it was qualified with.
            _record('unknown-column', _qualified_display(qualifier, column), '')
            continue

        if not report_case_mismatch:
            continue
        expected = lookup[norm_col]
        actual = str(column).strip().strip(_QUOTE_CHARS).strip()
        if expected != actual:
            _record('case-mismatch-column', _qualified_display(qualifier, column), expected)

    return findings


def _qualified_display(qualifier: Optional[str], column: str) -> str:
    return f'{qualifier}.{column}' if qualifier else str(column)


def owners_hint(column: str, schema_index: dict[str, Any] | None,
                exclude: str = '') -> str:
    """Name the tables that really DO own ``column``.

    Telling a model "column `frequency` does not exist" gives it nothing it did
    not already know, so it returns byte-identical SQL and the retry is wasted.
    The column is almost always real and sitting on a table the query failed to
    join — so say which one. Ported from the benchmark harness, where this
    rescued a repair loop that was ending "no change, giving up" on 28 of 47
    attempts (AUDIT lever L-C).
    """
    if not schema_index:
        return ''
    norm = normalize_identifier(column.split('.')[-1])
    excl = normalize_identifier(exclude) if exclude else ''
    owners = sorted(
        original for table, cols in (schema_index.get('columns') or {}).items()
        if norm in cols and table != excl
        for original in [(schema_index.get('tables') or {}).get(table, table)]
    )
    if not owners:
        return ''
    shown = ', '.join(f'"{t}"' for t in owners[:3])
    more = f' (and {len(owners) - 3} more)' if len(owners) > 3 else ''
    return f'; it belongs to {shown}{more} — join that table or qualify it there'


def format_identifier_feedback(findings: Sequence[Dict[str, str]],
                               schema_index: dict[str, Any] | None = None) -> str:
    """Render findings as the retry detail string.

    Deliberately explicit about the exact expected spelling — the whole point of
    the check is that the model got a character sequence wrong, so echoing the
    schema's spelling back is the actionable part.
    """
    if not findings:
        return ''
    lines: List[str] = []
    for finding in findings:
        kind = finding.get('kind', '')
        identifier = finding.get('identifier', '')
        expected = finding.get('expected', '')
        if kind == 'unknown-table':
            lines.append(f'- table "{identifier}" does not exist in the provided schema')
        elif kind == 'unknown-column':
            qualifier = identifier.split('.')[0] if '.' in identifier else ''
            lines.append(f'- column "{identifier}" does not exist in the provided '
                         f'schema{owners_hint(identifier, schema_index, qualifier)}')
        elif kind == 'case-mismatch-table':
            lines.append(f'- table "{identifier}" is spelled "{expected}" in the schema; '
                         f'copy it exactly')
        elif kind == 'case-mismatch-column':
            lines.append(f'- column "{identifier}" is spelled "{expected}" in the schema; '
                         f'copy it exactly')
    return ('The SQL references identifiers that do not match the schema verbatim:\n'
            + '\n'.join(lines)
            + '\nUse only the table and column names exactly as they appear in <m-schema>.')


def validate_sql_identifiers(sql: str, schema_str: str, ds_type: Optional[str] = None,
                             enabled: bool = True) -> List[Dict[str, str]]:
    """Full check: parse ``sql``, index ``schema_str``, return findings.

    Fails open at every step — returns [] when disabled, when the datasource is
    not SQL, when sqlglot is unavailable or cannot parse, or when the schema
    string yields no usable index.
    """
    if not enabled or not sql or not schema_str:
        return []
    if not should_validate(ds_type):
        return []
    index = build_schema_index(schema_str)
    if not schema_index_is_usable(index):
        return []
    extracted = extract_identifiers(sql, sqlglot_dialect(ds_type))
    if extracted is None:
        return []
    return diff_identifiers(extracted, index,
                            report_case_mismatch=case_sensitive_identifiers(ds_type))
