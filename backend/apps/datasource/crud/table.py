import json
import re
import time
import traceback
from typing import List

from sqlalchemy import and_, select, update

from apps.ai_model.embedding import EmbeddingModelCache
from common.core.config import settings
from common.core.deps import SessionDep
from common.utils.utils import SQLBotLogUtil
from ..models.datasource import CoreTable, CoreField, CoreDatasource


def delete_table_by_ds_id(session: SessionDep, id: int):
    session.query(CoreTable).filter(CoreTable.ds_id == id).delete(synchronize_session=False)
    session.commit()


def get_tables_by_ds_id(session: SessionDep, id: int):
    return session.query(CoreTable).filter(CoreTable.ds_id == id).order_by(
        CoreTable.table_name.asc()).all()


def update_table(session: SessionDep, item: CoreTable):
    record = session.query(CoreTable).filter(CoreTable.id == item.id).first()
    record.checked = item.checked
    record.custom_comment = item.custom_comment
    session.add(record)
    session.commit()


def run_fill_empty_table_and_ds_embedding(session_maker):
    try:
        if not settings.TABLE_EMBEDDING_ENABLED:
            return

        session = session_maker()

        SQLBotLogUtil.info('get tables')
        stmt = select(CoreTable.id).where(and_(CoreTable.embedding.is_(None)))
        results = session.execute(stmt).scalars().all()
        SQLBotLogUtil.info('table result: ' + str(len(results)))
        save_table_embedding(session_maker, results)

        SQLBotLogUtil.info('get datasource')
        ds_stmt = select(CoreDatasource.id).where(and_(CoreDatasource.embedding.is_(None)))
        ds_results = session.execute(ds_stmt).scalars().all()
        SQLBotLogUtil.info('datasource result: ' + str(len(ds_results)))
        save_ds_embedding(session_maker, ds_results)
    except Exception:
        traceback.print_exc()
    finally:
        session_maker.remove()


def save_table_embedding(session_maker, ids: List[int]):
    if not settings.TABLE_EMBEDDING_ENABLED:
        return

    if not ids or len(ids) == 0:
        return
    try:
        SQLBotLogUtil.info('start table embedding')
        start_time = time.time()
        model = EmbeddingModelCache.get_model()
        session = session_maker()
        for _id in ids:
            table = session.query(CoreTable).filter(CoreTable.id == _id).first()
            fields = session.query(CoreField).filter(
            CoreField.table_id == table.id).order_by(CoreField.field_index.asc()).all()

            schema_table = ''
            schema_table += f"# Table: {table.table_name}"
            table_comment = ''
            if table.custom_comment:
                table_comment = table.custom_comment.strip()
            if table_comment == '':
                schema_table += '\n[\n'
            else:
                schema_table += f", {table_comment}\n[\n"

            if fields:
                field_list = []
                for field in fields:
                    field_comment = ''
                    if field.custom_comment:
                        field_comment = field.custom_comment.strip()
                    if field_comment == '':
                        field_list.append(f"({field.field_name}:{field.field_type})")
                    else:
                        field_list.append(f"({field.field_name}:{field.field_type}, {field_comment})")
                schema_table += ",\n".join(field_list)
            schema_table += '\n]\n'
            # table_schema.append(schema_table)
            emb = json.dumps(model.embed_query(schema_table[:6000]))  # protective cap for pathologically large schemas

            stmt = update(CoreTable).where(and_(CoreTable.id == _id)).values(embedding=emb)
            session.execute(stmt)
            session.commit()

        end_time = time.time()
        SQLBotLogUtil.info('table embedding finished in: ' + str(end_time - start_time) + ' seconds')
    except Exception:
        traceback.print_exc()
    finally:
        session_maker.remove()


def build_ds_sample_text(ds, table_names, exec_fn, *,
                         n_rows: int, values_per_col: int,
                         value_maxlen: int, total_budget: int) -> str:
    """Bounded sample of distinct cell values per column — makes datasource
    embeddings content-aware. ``exec_fn(ds, sql)`` runs read-only SQL and returns
    ``{'fields': [...], 'data': [ {col: value}, ... ]}`` (injected so this is
    unit-testable without a DB); ``fields`` must use the same (lowercased) column
    names as the row dict keys, as ``exec_sql`` returns by default. One
    ``SELECT * ... LIMIT n`` per table; any table that fails to sample is skipped
    (best-effort, never raises)."""
    from apps.chat.task.apex_helpers import quoted_table_ref
    out_parts = []
    used = 0
    for table_name in table_names:
        if used >= total_budget:
            break
        try:
            ref = quoted_table_ref(getattr(ds, 'type', None), '', table_name)
            res = exec_fn(ds, f"SELECT * FROM {ref} LIMIT {n_rows}")
        except Exception:
            continue
        fields = (res or {}).get('fields') or []
        rows = (res or {}).get('data') or []
        if not rows:
            continue
        col_lines = []
        for col in fields:
            seen = []
            seen_set = set()  # O(1) dedup membership (order preserved by `seen`)
            for row in rows:
                v = row.get(col) if isinstance(row, dict) else None
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                if len(s) > value_maxlen:
                    s = s[:value_maxlen]
                if s not in seen_set:
                    seen_set.add(s)
                    seen.append(s)
                if len(seen) >= values_per_col:
                    break
            if seen:
                col_lines.append(f"({col}: e.g. " + ", ".join('"' + x + '"' for x in seen) + ")")
        if col_lines:
            block = f"# Sample values for {table_name}:\n" + "\n".join(col_lines) + "\n"
            if used + len(block) > total_budget:
                block = block[:max(0, total_budget - used)]
            if block:
                out_parts.append(block)
                used += len(block)
    return "".join(out_parts)


def build_ds_schema_text(session, ds: CoreDatasource, include_samples: bool = False) -> str:
    """Textual schema of a datasource (name, description, tables, fields,
    comments). Single source of truth shared by the embedding writer and the
    lexical (BM25) side of hybrid datasource ranking."""
    schema_table = ''
    schema_table += f"{ds.name}, {ds.description}\n"
    tables = session.query(CoreTable).filter(CoreTable.ds_id == ds.id).all()
    for table in tables:
        fields = session.query(CoreField).filter(
            CoreField.table_id == table.id).order_by(CoreField.field_index.asc()).all()

        schema_table += f"# Table: {table.table_name}"
        table_comment = ''
        if table.custom_comment:
            table_comment = table.custom_comment.strip()
        if table_comment == '':
            schema_table += '\n[\n'
        else:
            schema_table += f", {table_comment}\n[\n"

        if fields:
            field_list = []
            for field in fields:
                field_comment = ''
                if field.custom_comment:
                    field_comment = field.custom_comment.strip()
                if field_comment == '':
                    field_list.append(f"({field.field_name}:{field.field_type})")
                else:
                    field_list.append(f"({field.field_name}:{field.field_type}, {field_comment})")
            schema_table += ",\n".join(field_list)
        schema_table += '\n]\n'
    if include_samples and settings.EMBEDDING_SAMPLE_ENABLED:
        try:
            from apps.db.db import exec_sql
            sample = build_ds_sample_text(
                ds, [t.table_name for t in tables], exec_sql,
                n_rows=settings.EMBEDDING_SAMPLE_ROWS,
                values_per_col=settings.EMBEDDING_SAMPLE_VALUES_PER_COL,
                value_maxlen=settings.EMBEDDING_SAMPLE_VALUE_MAXLEN,
                total_budget=settings.EMBEDDING_SAMPLE_TOTAL_BUDGET)
            if sample:
                schema_table += "\n" + sample
        except Exception:
            SQLBotLogUtil.exception(f'value sampling failed for ds {ds.id}; embedding schema only')
    return schema_table


def get_ds_table_names(session, ds_id: int, limit: int = 15) -> List[str]:
    """Table names of a datasource, for richer routing/candidate payloads.

    NOTE: this is CAPPED at ``limit`` and is a display/routing helper. Do not use
    it to build a security allow-list — use ``get_readable_table_names``, which
    is uncapped, because a truncated allow-list silently drops real tables.
    """
    tables = session.query(CoreTable.table_name).filter(CoreTable.ds_id == ds_id).limit(limit).all()
    return [t[0] for t in tables]


def get_readable_table_names(session, oid: int, ds_id: int | None = None) -> list[str]:
    """Every physical table name the caller may read — the row-RAG allow-list.

    Scoped to one datasource when ``ds_id`` is given, otherwise to every
    datasource in workspace ``oid``. Deliberately UNCAPPED: this feeds the
    ``WHERE table_name = ANY(...)`` filter in the row-embedding search
    (AUDIT D-01), and a cap would silently exclude a datasource's later tables
    from its own user's results.
    """
    query = session.query(CoreTable.table_name).join(
        CoreDatasource, CoreTable.ds_id == CoreDatasource.id)
    if ds_id is not None:
        query = query.filter(CoreTable.ds_id == ds_id)
    else:
        query = query.filter(CoreDatasource.oid == oid)
    return [t[0] for t in query.all()]


# The Excel/CSV importer hardcodes description=f"Excel file: {filename}" for every
# upload (xlsx/xls/csv alike), so this is the only auto-generated prefix to match.
# Extend this alternation if other import paths add their own generic prefixes.
_GENERIC_DESC_RE = re.compile(r'^\s*Excel file:\s', re.IGNORECASE)

# Models often answer a "Summary:" cue with a preamble/label; strip a leading one
# so it doesn't get stored as the description and pollute the embedding.
_SUMMARY_PREAMBLE_RE = re.compile(
    r'^\s*(here\s+is\s+(a\s+)?(brief\s+|concise\s+)?summary[:.]?\s*|summary[:.]?\s*)',
    re.IGNORECASE,
)


def should_replace_description(desc) -> bool:
    """True when a datasource description is empty or the generic auto string,
    so it is safe to overwrite with a generated summary (never clobbers a
    user-written description)."""
    if desc is None:
        return True
    s = str(desc).strip()
    if s == '':
        return True
    return bool(_GENERIC_DESC_RE.match(s))


_DS_SUMMARY_PROMPT = (
    "You are cataloguing datasets so questions can be routed to the right one. "
    "Write 1-3 plain English sentences summarising WHAT this dataset contains: "
    "its subject/domain, key columns, and a few example values. "
    "Rules: no preamble, no 'Here is a summary', no markdown, no headings, no bullets. "
    "Output only the sentences.\n\n"
    "Dataset name: {name}\nSchema and sample values:\n{context}\n\nSummary:"
)


def generate_ds_summary(ds_name, context_text, llm_invoke, maxlen: int) -> str:
    """Generate a concise NL summary of a datasource's contents via the injected
    ``llm_invoke(prompt) -> str``. Best-effort: returns '' on any error or blank
    output. ``context_text`` is the schema+sample text (e.g. build_ds_schema_text
    with samples)."""
    try:
        prompt = _DS_SUMMARY_PROMPT.format(name=ds_name or '', context=context_text or '')
        out = llm_invoke(prompt)
    except Exception:
        return ''
    s = (out or '').strip()
    s = _SUMMARY_PREAMBLE_RE.sub('', s).strip()  # drop a leading "Here is a summary:" label
    if not s:
        return ''
    return s[:maxlen]


def save_ds_embedding(session_maker, ids: List[int]):
    if not settings.TABLE_EMBEDDING_ENABLED:
        return

    if not ids or len(ids) == 0:
        return
    try:
        SQLBotLogUtil.info('start datasource embedding')
        start_time = time.time()
        model = EmbeddingModelCache.get_model()
        session = session_maker()
        for _id in ids:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == _id).first()
            if ds is None:
                continue

            schema_table = build_ds_schema_text(session, ds, include_samples=True)

            # Auto-summary: give the finder a meaningful description when the
            # datasource has none / only the generic auto one. Best-effort; a
            # failure here must never block embedding.
            if settings.DS_SUMMARY_ENABLED and should_replace_description(ds.description):
                try:
                    from apps.ai_model.model_factory import invoke_default_llm
                    summary = generate_ds_summary(ds.name, schema_table, invoke_default_llm,
                                                  settings.DS_SUMMARY_MAXLEN)
                    if summary:
                        # Single ORM write (no separate Core update) so the dirty
                        # attribute is flushed only by this explicit commit — avoids
                        # a premature autoflush during the later embedding execute.
                        ds.description = summary
                        session.commit()
                        # rebuild so the embedding includes the new description
                        schema_table = build_ds_schema_text(session, ds, include_samples=True)
                except Exception:
                    SQLBotLogUtil.exception(f'auto-summary failed for ds {_id}')

            emb = json.dumps(model.embed_query(schema_table[:6000]))  # protective cap for pathologically large schemas
            stmt = update(CoreDatasource).where(and_(CoreDatasource.id == _id)).values(embedding=emb)
            session.execute(stmt)
            session.commit()

        end_time = time.time()
        SQLBotLogUtil.info('datasource embedding finished in: ' + str(end_time - start_time) + ' seconds')
    except Exception:
        traceback.print_exc()
    finally:
        session_maker.remove()
