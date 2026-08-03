# Content-Aware Finding + Full-Text Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make datasource *routing* aware of actual cell values (sampled into the embedding text), and give PostgreSQL-backed (Excel) datasources indexed full-text search for "contains/about" questions via a GIN index + scoped, dialect-gated SQL-prompt guidance.

**Architecture:** (1) A bounded per-table value sample is appended to the text that gets embedded — computed once at embed time, never per query. (2) Each text column of an imported Excel table gets a `gin(to_tsvector('simple', col))` index; the SQL system prompt gains FTS guidance *only* for PostgreSQL-family engines. All behind env-overridable settings, fully generic.

**Tech Stack:** Python 3, pandas, psycopg2 COPY/DDL, SQLAlchemy, Postgres FTS, mxbai-embed-large via Ollama, pytest. Spec: `docs/superpowers/specs/2026-06-23-content-aware-finding-and-fts-design.md`.

---

## Environment notes (READ FIRST)

- **NOT a git repo** — ignore `git commit`. Each task ends with a **Checkpoint** validated in the running `sqlbot` container.
- **Tests run inside the container** (no host venv):
  - Pure/unit: `docker cp tests/<f>.py sqlbot:/tmp/ && docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/<f>.py -q"`
  - Source validation: `docker cp` the changed file into `/opt/sqlbot/app/<same path>`, then `docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` (use `import main`; importing `apps.datasource.api.datasource` standalone hits a known `sqlbot_xpack` circular import).
  - E2E / full deploy: `docker compose build && docker compose up -d` (overlay copies host `backend/` → `/opt/sqlbot/app`).
- Host `backend/...` ↔ container `/opt/sqlbot/app/...`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/common/core/config.py` | `EMBEDDING_SAMPLE_*` + `EXCEL_FTS_ENABLED` settings | Modify |
| `backend/apps/datasource/crud/table.py` | `build_ds_sample_text`; `build_ds_schema_text(include_samples=)`; embed call | Modify |
| `tests/test_ds_sample_text.py` | Unit tests for the sampler | **Create** |
| `backend/apps/datasource/api/datasource.py` | `_create_fts_indexes` + call after table insert | Modify |
| `backend/apps/chat/task/apex_helpers.py` | `fts_prompt_addendum` (pure, gated) | Modify |
| `tests/test_fts_addendum.py` | Unit tests for the addendum | **Create** |
| `backend/apps/chat/task/llm.py` | inject FTS addendum into SQL prompt | Modify |
| `tests/test_content_fts_e2e.py` | Integration: sample-in-embedding + FTS index + prompt | **Create** |

---

## Task 1: Settings

**Files:** Modify `backend/common/core/config.py`

- [ ] **Step 1: Add the settings**

In the `Settings` class, right after the `EXCEL_ISLAND_MIN_ROWS` line (added earlier), add:

```python
    # --- Content-aware datasource routing (sampled cell values in embedding) ---
    EMBEDDING_SAMPLE_ENABLED: bool = True
    EMBEDDING_SAMPLE_ROWS: int = 50          # rows scanned per table (one query)
    EMBEDDING_SAMPLE_VALUES_PER_COL: int = 10
    EMBEDDING_SAMPLE_VALUE_MAXLEN: int = 100
    EMBEDDING_SAMPLE_TOTAL_BUDGET: int = 4000  # max chars of sample text per datasource
    # --- PostgreSQL full-text search for Excel datasources ---
    EXCEL_FTS_ENABLED: bool = True
```

In the `@field_validator(...)` boolean-name list (where `'EXCEL_SPLIT_SIDE_BY_SIDE'` etc. are), add:
```python
                     'EMBEDDING_SAMPLE_ENABLED',
                     'EXCEL_FTS_ENABLED',
```

- [ ] **Step 2: Validate**

```bash
docker cp "backend/common/core/config.py" sqlbot:/opt/sqlbot/app/common/core/config.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'from common.core.config import settings as s; print(s.EMBEDDING_SAMPLE_ENABLED, s.EMBEDDING_SAMPLE_ROWS, s.EMBEDDING_SAMPLE_VALUES_PER_COL, s.EMBEDDING_SAMPLE_VALUE_MAXLEN, s.EMBEDDING_SAMPLE_TOTAL_BUDGET, s.EXCEL_FTS_ENABLED)'"
```
Expected: `True 50 10 100 4000 True`

- [ ] **Step 3: Checkpoint** — `docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error.

---

## Task 2: Sampled values in the embedding text

**Files:** Modify `backend/apps/datasource/crud/table.py`; Test `tests/test_ds_sample_text.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ds_sample_text.py`:

```python
"""Run in-container:
    docker cp tests/test_ds_sample_text.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_sample_text.py -q"
"""
from apps.datasource.crud.table import build_ds_sample_text


class _DS:
    type = 'pg'


def test_distinct_values_and_dedup():
    def fake_exec(ds, sql):
        return {'fields': ['title', 'genre'],
                'data': [{'title': 'A', 'genre': 'X'},
                         {'title': 'B', 'genre': 'X'},
                         {'title': 'A', 'genre': 'Y'}]}
    out = build_ds_sample_text(_DS(), ['movies'], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert 'movies' in out
    assert '"A"' in out and '"B"' in out      # distinct titles, both kept
    assert out.count('"X"') == 1               # duplicate genre collapsed
    assert '"Y"' in out


def test_truncates_long_values_and_caps_per_column():
    long = "z" * 500
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': long}] + [{'c': f'v{i}'} for i in range(20)]}
    out = build_ds_sample_text(_DS(), ['t'], fake_exec,
                               n_rows=50, values_per_col=3, value_maxlen=10,
                               total_budget=4000)
    assert ('z' * 10) in out and ('z' * 11) not in out   # value truncated to 10
    assert out.count('"v') <= 2                            # <=3 values total incl the long one


def test_total_budget_cap():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': [{'c': f'value{i}'} for i in range(50)]}
    out = build_ds_sample_text(_DS(), [f't{i}' for i in range(10)], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=80)
    assert len(out) <= 80


def test_skips_table_on_exec_error():
    def boom(ds, sql):
        raise RuntimeError('no such table')
    out = build_ds_sample_text(_DS(), ['t'], boom,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert out == ''


def test_empty_table_yields_nothing():
    def fake_exec(ds, sql):
        return {'fields': ['c'], 'data': []}
    out = build_ds_sample_text(_DS(), ['t'], fake_exec,
                               n_rows=50, values_per_col=10, value_maxlen=100,
                               total_budget=4000)
    assert out == ''
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker cp tests/test_ds_sample_text.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_sample_text.py -q"
```
Expected: ImportError (`build_ds_sample_text` not defined).

- [ ] **Step 3: Implement `build_ds_sample_text` and extend `build_ds_schema_text`**

In `backend/apps/datasource/crud/table.py`, add this function immediately **above** `build_ds_schema_text`:

```python
def build_ds_sample_text(ds, table_names, exec_fn, *,
                         n_rows: int, values_per_col: int,
                         value_maxlen: int, total_budget: int) -> str:
    """Bounded sample of distinct cell values per column — makes datasource
    embeddings content-aware. ``exec_fn(ds, sql)`` runs read-only SQL and returns
    ``{'fields': [...], 'data': [ {col: value}, ... ]}`` (injected so this is
    unit-testable without a DB). One ``SELECT * ... LIMIT n`` per table; any table
    that fails to sample is skipped (best-effort, never raises)."""
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
            for row in rows:
                v = row.get(col) if isinstance(row, dict) else None
                if v is None:
                    continue
                s = str(v).strip()
                if not s:
                    continue
                if len(s) > value_maxlen:
                    s = s[:value_maxlen]
                if s not in seen:
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
```

Then change the `build_ds_schema_text` signature and append samples before its `return`:

```python
def build_ds_schema_text(session, ds: CoreDatasource, include_samples: bool = False) -> str:
```
…and immediately before `return schema_table` add:
```python
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
            pass
    return schema_table
```
(`tables` and `settings` are already in scope in this function.)

Finally, in `save_ds_embedding`, change the embed-text line (currently `schema_table = build_ds_schema_text(session, ds)`) to:
```python
            schema_table = build_ds_schema_text(session, ds, include_samples=True)
```
**Do not** change the BM25 `lexical_texts` call in `ds_embedding.py` — it must stay `include_samples` default (False) so no data is sampled per query.

- [ ] **Step 4: Run to verify pass**

```bash
docker cp "backend/apps/datasource/crud/table.py" sqlbot:/opt/sqlbot/app/apps/datasource/crud/table.py
docker cp tests/test_ds_sample_text.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_sample_text.py -q"
```
Expected: 5 passed.

- [ ] **Step 5: Checkpoint** — `docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error.

---

## Task 3: FTS GIN indexes at Excel ingest

**Files:** Modify `backend/apps/datasource/api/datasource.py`

- [ ] **Step 1: Add `_create_fts_indexes` helper**

Place it immediately **after** `_insert_df_to_pg` in `backend/apps/datasource/api/datasource.py` (`hashlib`, `sql` from psycopg2 are already imported):

```python
def _create_fts_indexes(df, table_name, engine):
    """Best-effort GIN full-text indexes on the TEXT columns of an imported table.

    `to_tsvector('simple', col)` (2-arg form) is IMMUTABLE, so it is index-eligible.
    'simple' is language-agnostic (no locale/stemming-dictionary dependency).
    """
    from common.core.config import settings
    if not settings.EXCEL_FTS_ENABLED:
        return
    text_cols = [str(df.columns[i]) for i in range(len(df.dtypes))
                 if str(df.dtypes[i]) in ('object', 'string')]
    if not text_cols:
        return
    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        for col in text_cols:
            idx = "ftsidx_" + hashlib.sha256((table_name + '::' + col).encode()).hexdigest()[:16]
            stmt = sql.SQL(
                "CREATE INDEX IF NOT EXISTS {idx} ON {tbl} "
                "USING gin (to_tsvector('simple', {col}))"
            ).format(idx=sql.Identifier(idx), tbl=sql.Identifier(table_name),
                     col=sql.Identifier(col))
            try:
                cursor.execute(stmt)
                conn.commit()
            except Exception:
                conn.rollback()
                continue
    finally:
        cursor.close()
        conn.close()
```

- [ ] **Step 2: Call it after each table insert in `_import_excel_sheets`**

In `_import_excel_sheets`, find the `try:` block that calls `_insert_df_to_pg(df, table_name, engine)` and add the FTS call right after it:

```python
        try:
            _insert_df_to_pg(df, table_name, engine)
            _create_fts_indexes(df, table_name, engine)
            results.append({
                "sheetName": sheet_name,
                "tableName": table_name,
                "tableComment": "",
                "rows": len(df),
            })
```

- [ ] **Step 3: Validate import/compile**

```bash
docker cp "backend/apps/datasource/api/datasource.py" sqlbot:/opt/sqlbot/app/apps/datasource/api/datasource.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m py_compile apps/datasource/api/datasource.py && echo compile-ok"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: `compile-ok` and no import error. (Functional check is the Task 5 E2E.)

- [ ] **Step 4: Checkpoint** — proceed to Task 5 for the live index check.

---

## Task 4: Dialect-gated FTS prompt guidance

**Files:** Modify `backend/apps/chat/task/apex_helpers.py`, `backend/apps/chat/task/llm.py`; Test `tests/test_fts_addendum.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fts_addendum.py`:

```python
"""Run in-container:
    docker cp tests/test_fts_addendum.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_fts_addendum.py -q"
"""
from apps.chat.task.apex_helpers import fts_prompt_addendum


def test_pg_family_enabled_returns_guidance():
    for t in ('excel', 'pg', 'kingbase', 'PG', 'Excel'):
        out = fts_prompt_addendum(t, True)
        assert 'to_tsvector' in out and 'plainto_tsquery' in out


def test_non_pg_returns_empty():
    for t in ('mysql', 'oracle', 'sqlServer', 'doris', '', None):
        assert fts_prompt_addendum(t, True) == ''


def test_disabled_returns_empty():
    assert fts_prompt_addendum('pg', False) == ''
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker cp tests/test_fts_addendum.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_fts_addendum.py -q"
```
Expected: ImportError (`fts_prompt_addendum` not defined).

- [ ] **Step 3: Implement the addendum**

Append to `backend/apps/chat/task/apex_helpers.py` (it already imports `Optional` from typing; if not, add it):

```python
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
        "words in a TEXT column), prefer PostgreSQL full-text search: "
        "to_tsvector('simple', <column>) @@ plainto_tsquery('simple', '<search words>'). "
        "Use normal comparisons (=, <, >, LIKE) for exact or structured filters."
    )
```

- [ ] **Step 4: Wire it into the SQL prompt** in `backend/apps/chat/task/llm.py` `init_messages`

Find:
```python
        _system_templates = self.chat_question.sql_sys_question(self.ds.type, self.enable_sql_row_limit)
        self.sql_message.append(SystemPromptMessage(content=_system_templates['system']))
```
and insert the addendum between those two lines:
```python
        _system_templates = self.chat_question.sql_sys_question(self.ds.type, self.enable_sql_row_limit)
        from apps.chat.task.apex_helpers import fts_prompt_addendum
        _fts = fts_prompt_addendum(self.ds.type, settings.EXCEL_FTS_ENABLED)
        if _fts:
            _system_templates['rules'] = (_system_templates.get('rules') or '') + _fts
        self.sql_message.append(SystemPromptMessage(content=_system_templates['system']))
```

- [ ] **Step 5: Run to verify pass + import**

```bash
docker cp "backend/apps/chat/task/apex_helpers.py" sqlbot:/opt/sqlbot/app/apps/chat/task/apex_helpers.py
docker cp "backend/apps/chat/task/llm.py" sqlbot:/opt/sqlbot/app/apps/chat/task/llm.py
docker cp tests/test_fts_addendum.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_fts_addendum.py -q"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: 3 passed; `import main` no error.

- [ ] **Step 6: Checkpoint** — proceed to Task 5.

---

## Task 5: Re-embed existing datasources + integration verification

**Files:** Create `tests/test_content_fts_e2e.py`

- [ ] **Step 1: Rebuild + deploy all changes**

```bash
docker compose build && docker compose up -d
```
Wait for healthy: poll `docker inspect -f '{{.State.Health.Status}}' sqlbot` until `healthy`.

- [ ] **Step 2: Write the integration test**

Create `tests/test_content_fts_e2e.py`:

```python
"""Verifies: (1) a datasource's embedding text now contains sampled cell values;
(2) FTS GIN indexes exist on text columns after upload; (3) FTS prompt guidance is
present for PG-family. Run in-container against the live app:

    docker cp tests/test_content_fts_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_content_fts_e2e.py"
"""
import io
import json
import sys
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text  # noqa: E402
from apps.db.engine import get_engine_conn  # noqa: E402

BASE = 'http://localhost:8000/api/v1'


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def login():
    import asyncio, urllib.parse
    from common.utils.crypto import sqlbot_encrypt
    from common.core.config import settings
    loop = asyncio.new_event_loop()
    try:
        acc = loop.run_until_complete(sqlbot_encrypt('admin'))
        pwd = loop.run_until_complete(sqlbot_encrypt(settings.DEFAULT_PWD))
    finally:
        loop.close()
    d = urllib.parse.urlencode({'username': acc, 'password': pwd}).encode()
    res = http('POST', f'{BASE}/login/access-token', d, {'Content-Type': 'application/x-www-form-urlencoded'})
    tok = (res['data'].get('access_token') if isinstance(res.get('data'), dict)
           else res.get('access_token') or res.get('data'))
    assert tok, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {tok}'}


def make_xlsx() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = 'concerts'
    ws.append(['title', 'note'])
    ws.append(['Glenn Fredly Tribute', 'virtual concert honoring revered Indonesian singer'])
    ws.append(['Jazz Night', 'live performance with special guests'])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def upload(headers, content):
    b = 'sqlbotCFBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="concerts.xlsx"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + f'\r\n--{b}--\r\n'.encode()
    h = dict(headers); h['Content-Type'] = f'multipart/form-data; boundary={b}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def run():
    headers = login()
    created = []
    try:
        d = upload(headers, make_xlsx()).get('data', {})
        ds_id = d.get('id'); created.append((ds_id, d.get('name')))
        assert ds_id, f'upload failed: {d}'
        table_name = d['sheets'][0]['tableName']

        # (1) embedding text contains a sampled cell value
        from apps.datasource.crud.table import build_ds_schema_text
        from common.core.db import engine as meta_engine  # session over app metadata
        from sqlalchemy.orm import Session
        from apps.datasource.models.datasource import CoreDatasource
        with Session(meta_engine) as s:
            ds = s.query(CoreDatasource).filter(CoreDatasource.id == ds_id).first()
            schema_text = build_ds_schema_text(s, ds, include_samples=True)
        assert 'Glenn Fredly Tribute' in schema_text, 'sample value missing from embedding text'
        print('(1) OK - embedding text includes sampled cell values')

        # (2) FTS GIN index exists for a text column of the imported table
        eng = get_engine_conn()
        with eng.connect() as c:
            idxdefs = [r[0] for r in c.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table_name}).fetchall()]
        assert any('to_tsvector' in d for d in idxdefs), f'no FTS index on {table_name}: {idxdefs}'
        print('(2) OK - GIN to_tsvector index present')

        # (3) FTS prompt guidance present for PG-family, absent otherwise
        from apps.chat.task.apex_helpers import fts_prompt_addendum
        assert 'to_tsvector' in fts_prompt_addendum('excel', True)
        assert fts_prompt_addendum('mysql', True) == ''
        print('(3) OK - FTS prompt guidance gated by engine')
        print('CONTENT+FTS E2E PASS')
    finally:
        for i, n in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{i}/{n}', b'', headers)
            except Exception:
                pass
```

- [ ] **Step 3: Run the integration test**

```bash
docker cp tests/test_content_fts_e2e.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_content_fts_e2e.py"
```
Expected: prints `(1) OK`, `(2) OK`, `(3) OK`, `CONTENT+FTS E2E PASS`.

- [ ] **Step 4: Re-embed all EXISTING datasources** (one-shot, so prior uploads gain content-aware embeddings)

```bash
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c '
import main
from sqlalchemy import text
from common.core.db import engine
from common.utils.embedding_threads import run_save_ds_embeddings
with engine.connect() as c:
    ids = [r[0] for r in c.execute(text(\"select id from core_datasource\")).fetchall()]
run_save_ds_embeddings(ids)
import time; time.sleep(min(60, 3*len(ids)))
print(\"re-embedded\", len(ids), \"datasources\")
'"
```
Expected: prints `re-embedded N datasources` (runs in a background thread; the sleep gives it time).

- [ ] **Step 5: (Observational) check the model uses FTS**

Ask a "contains" question in the chat UI against a PG/Excel datasource (e.g. *"concerts whose note mentions a virtual concert honoring an Indonesian singer"*) and inspect the generated SQL in Execution Details. Expected: it uses `to_tsvector('simple', …) @@ plainto_tsquery(...)`. **If the local qwen model does not emit FTS**, that is acceptable (ILIKE still valid → no regression); record the observed SQL. This step is not a hard gate.

- [ ] **Step 6: Checkpoint** — Tasks 1–4 unit/integration green, FTS index + sampled embedding confirmed live, existing datasources re-embedded. Feature complete.

---

## Notes for the implementer

- **Order:** Task 1 → 2 → 3 → 4 → 5. Tasks 2 and 4 are independently unit-tested; Task 3 + the prompt wiring are verified by the Task 5 E2E after a rebuild.
- **Do not** sample data in the BM25/query-time path — only `save_ds_embedding` passes `include_samples=True`. Sampling per query would scan data on every question.
- **Don't** add FTS guidance unconditionally — it must stay gated by `fts_prompt_addendum` (PG-family + enabled), or non-PG datasources will emit invalid SQL.
- Safety levers if anything misbehaves: `EMBEDDING_SAMPLE_ENABLED=False`, `EXCEL_FTS_ENABLED=False` (env), both default-on.
