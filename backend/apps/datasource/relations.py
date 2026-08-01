# Author: SQLBot
"""Join-graph discovery: give the model the relationships between tables.

The schema block built by ``get_table_schema`` lists columns and types only, so
the model has to guess how tables join. Measured on BIRD Mini-Dev, that guessing
is the dominant failure axis -- single-table questions score roughly twice what
three-table questions do, and the two largest error buckets are "used more
tables than needed" and "missed a needed join".

Two sources of truth, in order:

1. **Declared constraints.** Relational datasources publish PRIMARY KEY /
   FOREIGN KEY in ``information_schema``; that is exact and free.
2. **Inference.** Spreadsheets have no constraints at all -- an Excel workbook
   becomes one table per sheet with nothing linking them -- so relationships are
   inferred from column naming plus actual value overlap. Name agreement alone
   is not enough (every sheet has an ``id``), so a candidate is only kept when
   the referenced side looks like a key and the values genuinely overlap.

Everything here is advisory and best-effort: any failure returns "no relations"
rather than propagating, because a missing join hint degrades an answer while an
exception would break SQL generation entirely.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from common.core.config import settings
from common.utils.utils import SQLBotLogUtil

# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Relation:
    """One directed join edge: ``table.column`` references ``ref_table.ref_column``."""

    table: str
    column: str
    ref_table: str
    ref_column: str
    source: str = "constraint"      # 'constraint' | 'inferred'
    confidence: float = 1.0

    def render(self) -> str:
        if self.source == "constraint":
            return f"{self.column} -> {self.ref_table}.{self.ref_column}"
        # Mark inferred edges so the model can weigh them appropriately rather
        # than trusting a heuristic as if it were a declared constraint.
        return (f"{self.column} ~> {self.ref_table}.{self.ref_column} "
                f"(inferred {self.confidence:.0%})")


@dataclass
class TableRelations:
    primary_key: List[str] = field(default_factory=list)
    foreign_keys: List[Relation] = field(default_factory=list)

    def render(self) -> str:
        """One-line summary for the ``# Table: name, <here>`` comment slot."""
        bits = []
        if self.primary_key:
            bits.append("PK: " + ", ".join(self.primary_key))
        if self.foreign_keys:
            bits.append("FK: " + "; ".join(r.render() for r in self.foreign_keys))
        return " | ".join(bits)


# ---------------------------------------------------------------------------
# cache -- schemas change rarely; discovery costs queries
# ---------------------------------------------------------------------------

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > settings.SCHEMA_RELATIONS_CACHE_TTL:
        return None
    return value


def _cache_put(key: str, value) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


def _cache_key(ds: Any, schema: str,
               tables: dict[str, list[str]] | None = None) -> str:
    """Cache key for a join graph, including the caller's PERMITTED view.

    `get_relations` filters its output down to `tables` -- the table/column set
    this caller is allowed to see. Keying on datasource+schema alone therefore
    served the first caller's filtered graph to everyone else for the whole TTL
    (AUDIT D-13): a narrowly-permissioned user could be handed edges naming
    tables they cannot query, and a broadly-permissioned one could silently lose
    joins. The view is part of the identity of the cached value.

    The digest is order-independent so that two callers with the same permitted
    view share an entry -- otherwise the cache would never hit.
    """
    if tables is None:
        view = "*"
    else:
        canonical = ";".join(
            f"{name}({','.join(sorted(cols or []))})"
            for name, cols in sorted(tables.items())
        )
        view = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    # ds_id stays the FIRST segment: clear_cache() prefix-matches on it.
    return f"{getattr(ds, 'id', 0)}:{schema}:{view}"


def clear_cache(ds_id: Optional[int] = None) -> None:
    """Drop cached relations, for one datasource or all. Call after a sync."""
    with _cache_lock:
        if ds_id is None:
            _cache.clear()
        else:
            for k in [k for k in _cache if k.startswith(f"{ds_id}:")]:
                _cache.pop(k, None)


# ---------------------------------------------------------------------------
# 1. declared constraints
# ---------------------------------------------------------------------------

# Characters that cannot appear in a SQL string literal at all. Doubling `'` is
# the complete escape for a single-quoted literal (and `standard_conforming_
# strings` has defaulted to on since PostgreSQL 9.1, so backslash is literal),
# but a NUL or other control byte is not escapable — it corrupts the statement
# rather than being quoted. Reject those; escape everything else.
_UNESCAPABLE_IN_LITERAL = re.compile(r"[\x00-\x1f\x7f]")


def _constraint_sql(ds, schema: str) -> Optional[str]:
    """information_schema query returning (table, column, ref_table, ref_column, kind).

    Returns None for dialects that do not expose constraints this way, and for a
    schema name carrying a control character — the caller then falls back to
    inference, the same degradation an unsupported dialect gets.

    The name is interpolated (exec_sql takes no bind parameters) with `'`
    doubled, which is the correct and complete escape for a single-quoted
    literal. Quotes in a schema name are legitimate — PostgreSQL allows them in
    a quoted identifier — so they are escaped, not rejected (AUDIT D-26).
    """
    t = (getattr(ds, "type", "") or "").lower()
    if _UNESCAPABLE_IN_LITERAL.search(schema or ""):
        SQLBotLogUtil.info(
            "relations: refusing constraint discovery for a schema name "
            "containing a control character")
        return None
    s = (schema or "").replace("'", "''")

    # Every dialect must project these six aliases in this order. exec_sql
    # returns dict rows keyed by column label, so unaliased expressions would
    # collapse onto one key and silently lose the reference target -- alias
    # them explicitly. rel_constraint exists so multi-column constraints can
    # be paired (or safely dropped) instead of cross-joined: pairing child and
    # parent columns by constraint NAME alone turns a 2-column FK into a 2x2
    # cross product with two fabricated edges.
    if t in ("pg", "excel", "kingbase"):
        # pg_catalog rather than information_schema: constraint_column_usage
        # has no ordinal position, so composite FKs cannot be paired there.
        # unnest WITH ORDINALITY pairs conkey/confkey positionally.
        return f"""
            SELECT rel.relname                        AS rel_table,
                   att.attname                        AS rel_column,
                   COALESCE(frel.relname, '')         AS rel_ref_table,
                   COALESCE(fatt.attname, '')         AS rel_ref_column,
                   CASE con.contype WHEN 'p' THEN 'PRIMARY KEY'
                        ELSE 'FOREIGN KEY' END        AS rel_kind,
                   con.conname                        AS rel_constraint
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord)
            JOIN pg_attribute att
              ON att.attrelid = con.conrelid AND att.attnum = ck.attnum
            LEFT JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS cfk(fattnum, ford)
              ON cfk.ford = ck.ord
            LEFT JOIN pg_class frel ON frel.oid = con.confrelid
            LEFT JOIN pg_attribute fatt
              ON fatt.attrelid = con.confrelid AND fatt.attnum = cfk.fattnum
            WHERE nsp.nspname = '{s}'
              AND con.contype IN ('p', 'f')
        """
    if t in ("redshift", "greenplum"):
        # Old PG forks: no LATERAL/unnest-with-ordinality. information_schema
        # cannot pair composite FK columns, so discover_constraints drops any
        # FK constraint that expands to a cross product (see _drop_cross_joined).
        return f"""
            SELECT tc.table_name                     AS rel_table,
                   kcu.column_name                   AS rel_column,
                   COALESCE(ccu.table_name, '')      AS rel_ref_table,
                   COALESCE(ccu.column_name, '')     AS rel_ref_column,
                   tc.constraint_type                AS rel_kind,
                   tc.constraint_name                AS rel_constraint
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            LEFT JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.table_schema = '{s}'
              AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
        """
    if t in ("mysql", "doris", "starrocks", "mariadb"):
        # KEY_COLUMN_USAGE carries REFERENCED_* per row, already positional.
        return f"""
            SELECT kcu.TABLE_NAME                              AS rel_table,
                   kcu.COLUMN_NAME                             AS rel_column,
                   COALESCE(kcu.REFERENCED_TABLE_NAME, '')     AS rel_ref_table,
                   COALESCE(kcu.REFERENCED_COLUMN_NAME, '')    AS rel_ref_column,
                   CASE WHEN kcu.REFERENCED_TABLE_NAME IS NULL
                        THEN 'PRIMARY KEY' ELSE 'FOREIGN KEY' END AS rel_kind,
                   kcu.CONSTRAINT_NAME                         AS rel_constraint
            FROM information_schema.KEY_COLUMN_USAGE kcu
            WHERE kcu.TABLE_SCHEMA = '{s}'
              AND (kcu.CONSTRAINT_NAME = 'PRIMARY'
                   OR kcu.REFERENCED_TABLE_NAME IS NOT NULL)
        """
    if t in ("sqlserver", "mssql"):
        # Child and parent sides joined on ORDINAL_POSITION via the FK's
        # unique-constraint pairing, so composite FKs pair correctly.
        return f"""
            SELECT fk.TABLE_NAME                      AS rel_table,
                   fk.COLUMN_NAME                     AS rel_column,
                   pk.TABLE_NAME                      AS rel_ref_table,
                   pk.COLUMN_NAME                     AS rel_ref_column,
                   'FOREIGN KEY'                      AS rel_kind,
                   rc.CONSTRAINT_NAME                 AS rel_constraint
            FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk
              ON fk.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE pk
              ON pk.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
             AND pk.ORDINAL_POSITION = fk.ORDINAL_POSITION
            WHERE fk.TABLE_SCHEMA = '{s}'
            UNION ALL
            SELECT tc.TABLE_NAME, kcu.COLUMN_NAME, '', '',
                   'PRIMARY KEY', tc.CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
            WHERE tc.TABLE_SCHEMA = '{s}'
              AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
        """
    return None


_REL_KEYS = ("rel_table", "rel_column", "rel_ref_table", "rel_ref_column",
             "rel_kind", "rel_constraint")


def _cells(row, keys=_REL_KEYS) -> Optional[List[str]]:
    """exec_sql yields dict rows on some dialects and tuples on others."""
    try:
        if isinstance(row, dict):
            lowered = {str(k).lower(): v for k, v in row.items()}
            return [str(lowered.get(k) or "") for k in keys]
        vals = list(row)[:len(keys)]
        if len(vals) < len(keys):
            return None
        return [str(v) if v is not None else "" for v in vals]
    except Exception:
        return None


def discover_constraints(ds, schema: str) -> Dict[str, TableRelations]:
    """Read declared PK/FK. Empty dict when unsupported or on any failure."""
    sql = _constraint_sql(ds, schema)
    if not sql:
        return {}
    try:
        from apps.db.db import exec_sql
        res = exec_sql(ds, sql, origin_column=True)
    except Exception as e:
        SQLBotLogUtil.info(f"relations: constraint read failed ({type(e).__name__}: {e})")
        return {}

    out: Dict[str, TableRelations] = {}
    fk_by_constraint: Dict[tuple, List[tuple]] = {}
    for row in _rows(res):
        cells = _cells(row)
        if not cells:
            continue
        tbl, col, ref_t, ref_c, kind, cname = cells
        if not tbl or not col:
            continue
        tr = out.setdefault(tbl, TableRelations())
        if kind.upper().startswith("PRIMARY"):
            if col not in tr.primary_key:
                tr.primary_key.append(col)
        elif ref_t:
            fk_by_constraint.setdefault((tbl, cname), []).append((col, ref_t, ref_c))

    for (tbl, cname), pairs in fk_by_constraint.items():
        if _is_cross_joined(pairs):
            # information_schema without ordinal pairing (redshift/greenplum
            # path): a composite FK arrives as the full cross product. A wrong
            # edge stated as a declared constraint is worse than no edge, so
            # drop the whole constraint rather than guess the pairing.
            SQLBotLogUtil.info(
                f"relations: dropping composite FK '{cname}' on {tbl} "
                f"(cannot pair columns on this dialect)")
            continue
        tr = out.setdefault(tbl, TableRelations())
        for col, ref_t, ref_c in pairs:
            rel = Relation(tbl, col, ref_t, ref_c, "constraint", 1.0)
            if rel not in tr.foreign_keys:
                tr.foreign_keys.append(rel)
    return out


def _is_cross_joined(pairs: List[tuple]) -> bool:
    """True when one FK constraint expanded into a child x parent cross
    product: more rows than distinct child columns AND several child columns.
    A correctly paired composite FK has exactly one row per child column."""
    child_cols = {p[0] for p in pairs}
    return len(child_cols) > 1 and len(pairs) > len(child_cols)


def _rows(res) -> Sequence:
    """exec_sql returns dict-with-data or a bare list depending on dialect."""
    if isinstance(res, dict):
        return res.get("data") or []
    return res or []


# ---------------------------------------------------------------------------
# 2. inference -- the spreadsheet path
# ---------------------------------------------------------------------------

_NORM = re.compile(r"[^a-z0-9]+")


def _norm(name: str) -> str:
    return _NORM.sub("", (name or "").lower())


def _key_like(col: str, table: str) -> bool:
    """Does this column name look like it points at something?

    Accepts `id`, `<table>_id`, `<table>id`, `<table>_code`, `<table>key`.
    Deliberately permissive: value overlap is what actually decides, this only
    keeps the candidate set small enough to probe.
    """
    c, t = _norm(col), _norm(table)
    # Business identifiers that are keys by convention rather than by suffix.
    # These carry no `_id` marker but are exactly what spreadsheets join on.
    if c in ("id", "key", "code", "uuid", "sku", "isbn", "ean", "upc", "ref",
             "msisdn", "iban"):
        return True
    for suf in ("id", "key", "code", "no", "num"):
        if c.endswith(suf) and len(c) > len(suf):
            return True
    return bool(t) and c.startswith(t)


_KEY_SUFFIXES = ("id", "key", "code", "no", "num", "uuid", "ref")


def _referenced_table(column: str, table_names: Sequence[str]) -> Optional[str]:
    """The table a column names, if any: `user_id` -> `users`, `sku` -> None.

    Strips a trailing key suffix and matches the remainder against the known
    table names, tolerating singular/plural. Returns None when the column does
    not name anything, which leaves the decision to value overlap.
    """
    c = _norm(column)
    stems = {c}
    for suf in _KEY_SUFFIXES:
        if c.endswith(suf) and len(c) > len(suf):
            stems.add(c[: -len(suf)])
    stems = {s for s in stems if len(s) >= 3}
    if not stems:
        return None
    for t in table_names:
        nt = _norm(t)
        for s in stems:
            if nt == s or nt == s + "s" or s == nt + "s":
                return t
    return None


def _quote(ident: str, ds) -> str:
    t = (getattr(ds, "type", "") or "").lower()
    if t in ("mysql", "doris", "starrocks", "mariadb"):
        return "`" + ident.replace("`", "``") + "`"
    return '"' + ident.replace('"', '""') + '"'


def _qualified(schema: str, table: str, ds) -> str:
    t = (getattr(ds, "type", "") or "").lower()
    if t in ("mysql", "es", "sqlite", "hive", "doris", "starrocks") or not schema:
        return _quote(table, ds)
    return f"{_quote(schema, ds)}.{_quote(table, ds)}"


def _bounded_select(ds, ref: str, col: str, limit: int, distinct: bool) -> str:
    """Dialect-aware, DETERMINISTIC bounded scan. ORDER BY matters: without it
    the database returns an arbitrary subset, so two true-FK columns with many
    distinct values can sample near-disjoint ranges and the edge is missed --
    differently on every cache expiry. Ordering both sides compares the same
    value range. SQL Server has no LIMIT; use TOP."""
    t = (getattr(ds, "type", "") or "").lower()
    kw = "DISTINCT " if distinct else ""
    if t in ("sqlserver", "mssql"):
        return (f"SELECT {kw}TOP {int(limit)} {col} FROM {ref} "
                f"WHERE {col} IS NOT NULL ORDER BY {col}")
    return (f"SELECT {kw}{col} FROM {ref} WHERE {col} IS NOT NULL "
            f"ORDER BY {col} LIMIT {int(limit)}")


def _sample_values(ds, schema: str, table: str, column: str, limit: int) -> Optional[set]:
    """Distinct non-null values, bounded. None means "could not read"."""
    ref = _qualified(schema, table, ds)
    col = _quote(column, ds)
    sql = _bounded_select(ds, ref, col, limit, distinct=True)
    try:
        from apps.db.db import exec_sql
        res = exec_sql(ds, sql, origin_column=True)
    except Exception:
        return None
    vals = set()
    for row in _rows(res):
        try:
            v = list(row.values())[0] if isinstance(row, dict) else list(row)[0]
        except Exception:
            continue
        if v is None:
            continue
        # Compare as trimmed lowercase text so '01' / 1 / ' 01 ' match across
        # sheets, which is the usual shape of a spreadsheet join key.
        vals.add(str(v).strip().lower())
    return vals


def infer_relations(ds, schema: str,
                    tables: Dict[str, List[str]]) -> Dict[str, TableRelations]:
    """Infer join edges for sources with no declared constraints (Excel, CSV).

    ``tables`` maps table_name -> [column names]. A pair (child.col, parent.col)
    is emitted when the names agree after normalisation, the referenced side is
    close to unique, and the sampled values genuinely overlap.
    """
    if len(tables) < 2:
        return {}

    limit = settings.SCHEMA_RELATIONS_SAMPLE
    min_overlap = settings.SCHEMA_RELATIONS_MIN_OVERLAP
    min_unique = settings.SCHEMA_RELATIONS_MIN_UNIQUENESS

    # Index columns by normalised name so same-named columns across sheets pair up.
    by_name: Dict[str, List[tuple]] = {}
    for tname, cols in tables.items():
        for c in cols:
            by_name.setdefault(_norm(c), []).append((tname, c))

    samples: Dict[tuple, Optional[set]] = {}
    uniqs: Dict[tuple, Optional[float]] = {}

    def sample(t, c):
        if (t, c) not in samples:
            samples[(t, c)] = _sample_values(ds, schema, t, c, limit)
        return samples[(t, c)]

    def uniq(t, c):
        if (t, c) not in uniqs:
            uniqs[(t, c)] = _uniqueness(ds, schema, t, c, limit)
        return uniqs[(t, c)]

    table_names = list(tables)
    out: Dict[str, TableRelations] = {}
    pairs_checked = 0
    for norm_name, occurrences in by_name.items():
        if len(occurrences) < 2 or len(norm_name) < 2:
            continue
        # A column appearing in every sheet is a label (e.g. "name"), not a key.
        if len(occurrences) > settings.SCHEMA_RELATIONS_MAX_FANOUT:
            continue
        for i, (t1, c1) in enumerate(occurrences):
            for t2, c2 in occurrences[i + 1:]:
                if t1 == t2:
                    continue
                # Spreadsheets routinely join on a business label -- a detail
                # sheet and a "Summary" sheet share `Category` or `Region`, with
                # no surrogate key anywhere. Rejecting those on name alone loses
                # the only relationship most workbooks have. So a name that
                # looks like a key is admitted on ordinary evidence, and any
                # other shared name has to clear a much higher overlap bar.
                looks_key = (_key_like(c1, t2) or _key_like(c2, t1)
                             or _key_like(c1, t1) or _key_like(c2, t2))
                threshold = (min_overlap if looks_key
                             else settings.SCHEMA_RELATIONS_MIN_OVERLAP_UNNAMED)

                if pairs_checked >= settings.SCHEMA_RELATIONS_MAX_PAIRS:
                    break
                pairs_checked += 1

                v1, v2 = sample(t1, c1), sample(t2, c2)
                if not v1 or not v2:
                    continue
                inter = len(v1 & v2)
                if not inter:
                    continue
                overlap = inter / min(len(v1), len(v2))
                if overlap < threshold:
                    continue

                u1, u2 = uniq(t1, c1), uniq(t2, c2)
                if u1 is None or u2 is None:
                    continue
                p1, p2 = u1 >= min_unique, u2 >= min_unique

                # A foreign key is many-to-one: the child side repeats, the
                # parent side is a key. Two columns that are BOTH unique over
                # the same 1..N range are independent surrogate keys that merely
                # collide -- the single biggest source of bogus edges on real
                # schemas, where every table has an `id`. Neither side unique
                # means a shared label (a status code), also not a key.
                if p1 == p2:
                    if not (p1 and p2):
                        continue
                    # Both unique: usually surrogate-id collision -- EXCEPT a
                    # genuine 1:1 whose column NAMES its target (satscores.cds
                    # -> schools, profiles.user_id -> users, a summary sheet
                    # keyed like its detail sheet). The name supplies the
                    # direction that uniqueness cannot.
                    if _referenced_table(c1, [t2]):
                        parent, pcol, child, ccol = t2, c2, t1, c1
                    elif _referenced_table(c2, [t1]):
                        parent, pcol, child, ccol = t1, c1, t2, c2
                    else:
                        continue
                elif p1:
                    parent, pcol, child, ccol = t1, c1, t2, c2
                else:
                    parent, pcol, child, ccol = t2, c2, t1, c1

                # When the column names its target (`user_id`, `customer_code`),
                # believe the name: it disambiguates between several tables that
                # all carry the same key, which value overlap alone cannot do.
                named = _referenced_table(ccol, table_names)
                if named and _norm(named) != _norm(parent):
                    continue

                rel = Relation(child, ccol, parent, pcol, "inferred",
                               round(overlap, 2))
                tr = out.setdefault(child, TableRelations())
                if rel not in tr.foreign_keys:
                    tr.foreign_keys.append(rel)
    if out:
        n = sum(len(v.foreign_keys) for v in out.values())
        SQLBotLogUtil.info(f"relations: inferred {n} edge(s) across "
                           f"{len(tables)} table(s) on ds {getattr(ds, 'id', '?')}")
    return out


def _uniqueness(ds, schema: str, table: str, column: str,
                limit: int) -> Optional[float]:
    """distinct/total over a bounded window. None when it cannot be measured."""
    ref = _qualified(schema, table, ds)
    col = _quote(column, ds)
    # Same deterministic window as _sample_values, so uniqueness is judged
    # over the very rows whose values were compared.
    sql = (f"SELECT COUNT(*) AS rel_total, COUNT(DISTINCT {col}) AS rel_distinct "
           f"FROM ({_bounded_select(ds, ref, col, limit, distinct=False)}) AS _s")
    try:
        from apps.db.db import exec_sql
        res = exec_sql(ds, sql, origin_column=True)
        row = list(_rows(res))[0]
        cells = _cells(row, ("rel_total", "rel_distinct"))
        if not cells:
            return None
        total, distinct = float(cells[0]), float(cells[1])
        return distinct / total if total else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def get_relations(ds, schema: str,
                  tables: Optional[Dict[str, List[str]]] = None
                  ) -> Dict[str, TableRelations]:
    """Join graph for a datasource, keyed by table name.

    Declared constraints win; inference fills in only the tables constraints did
    not cover, so a partially-constrained database keeps its real foreign keys
    and still gets hints for the rest.
    """
    if not settings.SCHEMA_RELATIONS_ENABLED:
        return {}

    key = _cache_key(ds, schema, tables)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        out = discover_constraints(ds, schema)

        # Excel/CSV datasources are materialised into the shared `public` schema
        # of SQLBot's own database, so information_schema also returns SQLBot's
        # internal tables (core_datasource, chat_log, ...). Keep only the tables
        # this datasource owns, and only edges between them -- otherwise the
        # prompt leaks internal schema and invites joins to tables the user
        # cannot query.
        if tables is not None:
            owned = {t for t in tables}
            out = {t: tr for t, tr in out.items() if t in owned}
            for tr in out.values():
                tr.foreign_keys = [r for r in tr.foreign_keys
                                   if r.ref_table in owned]

        if tables and settings.SCHEMA_RELATIONS_INFER:
            uncovered = {t: c for t, c in tables.items()
                         if not out.get(t) or not out[t].foreign_keys}
            if len(uncovered) >= 2:
                for tname, tr in infer_relations(ds, schema, uncovered).items():
                    existing = out.setdefault(tname, TableRelations())
                    for rel in tr.foreign_keys:
                        if rel not in existing.foreign_keys:
                            existing.foreign_keys.append(rel)
    except Exception as e:
        SQLBotLogUtil.info(f"relations: discovery failed ({type(e).__name__}: {e})")
        out = {}

    _cache_put(key, out)
    return out
