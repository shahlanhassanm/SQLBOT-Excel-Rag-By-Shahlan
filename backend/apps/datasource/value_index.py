"""Bounded, cached retrieval of distinct cell values for value linking.

The impure half of apps/chat/task/value_linking.py: it issues the read-only
``SELECT DISTINCT`` probes and caches them, while all matching logic stays pure
and testable over there.

Cost discipline, because this runs inside the question path:
  * The caller gates on `has_candidate_terms` — a question with no literal never
    reaches this module, so aggregate questions issue no queries at all.
  * Only text-typed columns are probed, capped globally by
    VALUE_LINKING_MAX_COLUMNS (not per table), so the query count per question
    has a hard ceiling regardless of schema width.
  * Results are cached per (datasource, table, column) for
    VALUE_LINKING_CACHE_TTL seconds, so repeated questions against the same
    datasource are free.
  * Every failure is swallowed: value linking is an accuracy aid, and a probe
    that errors must degrade to "no hints", never to a failed question.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple

from apps.chat.task.apex_helpers import quote_ident, quoted_table_ref
from apps.chat.task.value_linking import select_probe_columns
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil

# (ds_id, table, column) -> (expires_at, values)
_CACHE: Dict[Tuple[int, str, str], Tuple[float, List[str]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 2000


def _cache_get(key: Tuple[int, str, str]) -> Optional[List[str]]:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        expires_at, values = entry
        if expires_at < time.time():
            _CACHE.pop(key, None)
            return None
        return values


def _cache_put(key: Tuple[int, str, str], values: List[str]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            # cheap bounded eviction: drop everything already expired, and if
            # that frees nothing, clear outright rather than grow without bound
            now = time.time()
            expired = [k for k, (exp, _) in _CACHE.items() if exp < now]
            for k in expired:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX_ENTRIES:
                _CACHE.clear()
        _CACHE[key] = (time.time() + settings.VALUE_LINKING_CACHE_TTL, values)


def clear_cache() -> None:
    """Drop all cached column values (used by tests and after re-import)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# Row limiting is not portable. Without this the probe SQL is simply invalid on
# Oracle/SQL Server, and since probe failures are swallowed, value linking would
# no-op across whole dialects with no visible symptom.
_FETCH_FIRST_TYPES = frozenset({'oracle', 'dm', 'kingbase'})
_TOP_TYPES = frozenset({'sqlserver', 'mssql'})


def build_distinct_sql(ds_type: Optional[str], table_ref: str, column_ref: str, limit: int) -> str:
    """Dialect-correct ``SELECT DISTINCT <col> ... <row limit>``."""
    normalized = str(ds_type or '').strip().lower()
    count = int(limit)
    where = f'WHERE {column_ref} IS NOT NULL'
    if normalized in _TOP_TYPES:
        return f'SELECT DISTINCT TOP {count} {column_ref} FROM {table_ref} {where}'
    if normalized in _FETCH_FIRST_TYPES:
        return (f'SELECT DISTINCT {column_ref} FROM {table_ref} {where} '
                f'FETCH FIRST {count} ROWS ONLY')
    return f'SELECT DISTINCT {column_ref} FROM {table_ref} {where} LIMIT {count}'


def fetch_distinct_values(ds, table: str, column: str, limit: int, exec_fn) -> List[str]:
    """Distinct non-null values of one column, capped at ``limit``.

    ``exec_fn(ds, sql)`` is injected so this is testable without a database; it
    must return ``{'fields': [...], 'data': [{col: value}, ...]}`` like
    apps.db.db.exec_sql.
    """
    ds_type = getattr(ds, 'type', None)
    table_ref = quoted_table_ref(ds_type, '', table)
    column_ref = quote_ident(column, ds_type)
    sql = build_distinct_sql(ds_type, table_ref, column_ref, limit)
    result = exec_fn(ds, sql)
    rows = (result or {}).get('data') or []
    values: List[str] = []
    for row in rows:
        if isinstance(row, dict):
            if not row:
                continue
            # exec_sql lowercases field names; the dict has exactly one key here
            value = next(iter(row.values()))
        else:
            value = row
        if value is None:
            continue
        text = str(value).strip()
        if text:
            values.append(text)
    return values


def collect_column_values(ds, tables: Sequence[Dict[str, Any]], exec_fn) -> Dict[Tuple[str, str], List[str]]:
    """Probe the most promising text columns and return ``{(table, col): values}``.

    ``tables`` is ``[{'table_name': str, 'fields': [{'name','type'}, ...]}, ...]``.
    Never raises: a column that fails to probe is skipped.
    """
    targets = select_probe_columns(tables,
                                   max_tables=settings.VALUE_LINKING_MAX_TABLES,
                                   max_columns=settings.VALUE_LINKING_MAX_COLUMNS)
    if not targets:
        return {}

    ds_id = int(getattr(ds, 'id', 0) or 0)
    limit = settings.VALUE_LINKING_DISTINCT_LIMIT
    out: Dict[Tuple[str, str], List[str]] = {}
    probed = 0

    for table, column in targets:
        key = (ds_id, table, column)
        cached = _cache_get(key)
        if cached is not None:
            if cached:
                out[(table, column)] = cached
            continue
        try:
            values = fetch_distinct_values(ds, table, column, limit, exec_fn)
            probed += 1
        except Exception:
            # a single unprobeable column must not abort the rest
            _cache_put(key, [])
            continue
        _cache_put(key, values)
        if values:
            out[(table, column)] = values

    if probed:
        SQLBotLogUtil.info(
            f'value linking: probed {probed} column(s) on ds {ds_id}, '
            f'{len(out)} with values')
    return out


def build_table_field_specs(table_objs, table_names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Adapt CoreTable/CoreField ORM objects into the plain dicts the pure
    selection logic expects, preserving schema order and honouring the pruned
    table list when one is supplied."""
    allowed = {str(t) for t in table_names} if table_names else None
    specs: List[Dict[str, Any]] = []
    for obj in table_objs or []:
        try:
            name = obj.table.table_name
        except Exception:
            continue
        if allowed is not None and str(name) not in allowed:
            continue
        fields = []
        for field in (getattr(obj, 'fields', None) or []):
            field_name = getattr(field, 'field_name', None)
            if not field_name:
                continue
            fields.append({'name': field_name, 'type': getattr(field, 'field_type', None)})
        if fields:
            specs.append({'table_name': name, 'fields': fields})
    return specs


def _question_stopwords() -> set:
    """Query-shaped words to keep out of value linking, from settings.

    value_linking.py owns an English list only. The configured
    AGENTIC_LISTING_KEYWORDS / AGENTIC_AGGREGATION_KEYWORDS are already
    multilingual and already meant to be extended per deployment without a code
    change, so reuse them here rather than growing a second hardcoded list that
    would drift out of sync.
    """
    words = set()
    for raw in (settings.AGENTIC_LISTING_KEYWORDS,
                settings.AGENTIC_AGGREGATION_KEYWORDS):
        for token in str(raw or '').split(','):
            token = token.strip().casefold()
            if token:
                words.add(token)
    return words


def _schema_identifiers(specs: Sequence[Dict[str, Any]]) -> set:
    """Table and column names, so they are not mistaken for cell values.

    A word that names a column is describing WHERE to look, not WHAT to look
    for. Both the whole identifier and its underscore-separated parts are
    excluded, since questions say "order date" for `order_date`.
    """
    names = set()
    for spec in specs or []:
        candidates = [spec.get('table_name')]
        candidates += [f.get('name') for f in (spec.get('fields') or [])]
        for raw in candidates:
            text = str(raw or '').strip().casefold()
            if not text:
                continue
            names.add(text)
            names.update(part for part in re.split(r'[_\s-]+', text) if len(part) > 1)
    return names


def build_value_hints(session, current_user, ds, table_names: Sequence[str], question: str) -> str:
    """End-to-end value linking for one question: returns a prompt block or ''.

    Safe to call unconditionally — returns '' when the feature is off, when the
    question holds no literal, or on any internal error.
    """
    if not settings.VALUE_LINKING_ENABLED or not question or not ds:
        return ''
    try:
        from apps.chat.task.value_linking import (format_value_hints,
                                                  extract_candidate_terms, match_values)

        from apps.datasource.crud.datasource import get_table_obj_by_ds
        from apps.db.db import exec_sql

        table_objs = get_table_obj_by_ds(session=session, current_user=current_user, ds=ds)
        specs = build_table_field_specs(table_objs, table_names)
        if not specs:
            return ''

        # Extract AFTER the schema is known so identifiers can be excluded. The
        # case-insensitive fallback pass would otherwise treat "revenue" in
        # "what is the total revenue" as a candidate VALUE and probe for it,
        # costing a round of column reads on exactly the aggregate questions
        # this stage is supposed to skip for free.
        terms = extract_candidate_terms(
            question,
            extra_stopwords=_question_stopwords() | _schema_identifiers(specs),
            include_content_words=True)
        if not terms:
            return ''

        values_by_column = collect_column_values(ds, specs, exec_sql)
        if not values_by_column:
            return ''

        hints = match_values(terms, values_by_column,
                             min_score=settings.VALUE_LINKING_MIN_SCORE,
                             max_hints=settings.VALUE_LINKING_MAX_HINTS)
        if not hints:
            return ''
        SQLBotLogUtil.info(
            f'value linking: {len(hints)} hint(s) for terms {terms[:5]}')
        return format_value_hints(hints)
    except Exception:
        traceback.print_exc()
        return ''
