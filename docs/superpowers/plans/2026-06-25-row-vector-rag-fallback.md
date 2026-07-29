# Row-Level Vector RAG Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the SQL pipeline fails, retrieve the most semantically-relevant rows from a per-row pgvector store and return them as a grounded table — never hallucinating.

**Architecture:** New `apps/datasource/row_rag/` package: pure text/selection helpers (unit-tested) + a pgvector store, an ingest hook fired after Excel import, and a fallback invoked after the SQL pipeline exhausts. Embeddings reuse the existing Ollama `mxbai-embed-large` endpoint; storage uses the already-installed pgvector 0.8.0 in the bundled Postgres.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy + psycopg2, pgvector 0.8.0, langchain `OpenAIEmbeddings` (Ollama), pytest.

---

## Workflow notes (this workspace — read before starting)

- **NOT a git repo.** Ignore "commit" conventions. Each task ends with an **in-container validation** step instead, per the established workflow.
- **No local venv.** Run tests inside the container:
  `docker cp tests/<file> sqlbot:/tmp/` then
  `docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/<file> -q"`.
- **Fast iteration:** `docker cp` changed source files directly into `/opt/sqlbot/app/...` and `docker restart sqlbot` (or import-compile in place) — no full image rebuild needed during development. Do a `docker compose build` only at the end.
- **Validate imports** with `import main` (production order), NOT `import apps.datasource.api.datasource` (pre-existing sqlbot_xpack circular import).
- pgvector is confirmed installed: `pg_available_extensions` shows `vector | 0.8.0 | 0.8.0`. The bundled Postgres runs inside the `sqlbot` container; Excel datasources and app metadata share that one instance, so `row_embeddings` is reachable from both the import engine and the chat session.

## File Structure

| File | Responsibility |
|---|---|
| `backend/apps/datasource/row_rag/__init__.py` (new) | package marker |
| `backend/apps/datasource/row_rag/text.py` (new) | pure: row dict → `content_text` |
| `backend/apps/datasource/row_rag/select.py` (new) | pure: cosine-threshold filter + build table result |
| `backend/apps/datasource/row_rag/store.py` (new) | pgvector DDL ensure, insert, top-k query |
| `backend/apps/datasource/row_rag/ingest.py` (new) | df → content_text → embed → store |
| `backend/apps/datasource/row_rag/fallback.py` (new) | question → embed → query → threshold → table |
| `backend/common/core/config.py` (modify) | `ROW_RAG_*` settings |
| `backend/apps/datasource/api/datasource.py` (modify ~748) | call `ingest` after table insert |
| `backend/apps/chat/task/llm.py` (modify) | call `fallback` on no-match + retry-exhaustion |
| `backend/tests/test_row_rag_text.py` (new) | unit |
| `backend/tests/test_row_rag_select.py` (new) | unit |
| `backend/tests/test_row_rag_e2e.py` (new) | in-container E2E |

---

## Task 1: Settings flags

**Files:**
- Modify: `backend/common/core/config.py`

- [ ] **Step 1: Add the settings**

Find the block where `AGENTIC_*` / `EXCEL_FTS_ENABLED` settings are declared (the `Settings` class) and add, following the existing style:

```python
    # --- Row-level vector RAG fallback ---
    ROW_RAG_ENABLED: bool = False            # master on/off (ingest + fallback)
    ROW_RAG_EMBED_BATCH: int = 64            # rows per embedding batch at import
    ROW_RAG_TOP_K: int = 10                  # candidate rows pulled from pgvector
    ROW_RAG_MIN_COSINE: float = 0.5          # confidence floor; below -> "no match"
    ROW_RAG_EMBED_DIM: int = 1024            # mxbai-embed-large dimension
    ROW_RAG_CONTENT_MAX_CHARS: int = 4000    # cap per-row content_text length
```

- [ ] **Step 2: Validate it loads**

Run:
```bash
docker cp backend/common/core/config.py sqlbot:/opt/sqlbot/app/common/core/config.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"from common.core.config import settings; print(settings.ROW_RAG_ENABLED, settings.ROW_RAG_MIN_COSINE, settings.ROW_RAG_EMBED_DIM)\""
```
Expected: `False 0.5 1024`

---

## Task 2: Pure `content_text` builder

**Files:**
- Create: `backend/apps/datasource/row_rag/__init__.py` (empty)
- Create: `backend/apps/datasource/row_rag/text.py`
- Test: `backend/tests/test_row_rag_text.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_row_rag_text.py
from apps.datasource.row_rag.text import build_row_content_text


def test_concatenates_all_columns_label_value():
    row = {"Title": "Lovebirds", "Plot": "a bride is visited by an angel"}
    out = build_row_content_text(row)
    assert out == "Title: Lovebirds | Plot: a bride is visited by an angel"


def test_skips_none_and_empty_cells():
    row = {"Title": "X", "Note": None, "Blank": "", "Year": 2013}
    out = build_row_content_text(row)
    assert out == "Title: X | Year: 2013"


def test_includes_numeric_columns():
    # whole-row embedding requirement: numbers are kept, not dropped
    row = {"Id": 7, "Amount": 12.5}
    assert build_row_content_text(row) == "Id: 7 | Amount: 12.5"


def test_truncates_to_max_chars():
    row = {"Plot": "x" * 100}
    out = build_row_content_text(row, max_chars=20)
    assert len(out) <= 20


def test_empty_row_returns_empty_string():
    assert build_row_content_text({}) == ""
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker cp backend/tests/test_row_rag_text.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_text.py -q"
```
Expected: FAIL — `ModuleNotFoundError: apps.datasource.row_rag.text`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/apps/datasource/row_rag/text.py
"""Pure helpers turning a source row into the text we embed.

Whole-row embedding: every non-empty column (text AND numeric) is included,
formatted as ``"col: value | col: value"``. No DB or model dependency here.
"""
from typing import Any, Dict, List


def build_row_content_text(row: Dict[str, Any], max_chars: int = 4000) -> str:
    parts: List[str] = []
    for key, value in row.items():
        if value is None:
            continue
        s = str(value).strip()
        if s == "":
            continue
        parts.append(f"{key}: {s}")
    text = " | ".join(parts)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    return text


def build_rows_content_text(rows: List[Dict[str, Any]], max_chars: int = 4000) -> List[str]:
    return [build_row_content_text(r, max_chars=max_chars) for r in rows]
```

Also create the empty package marker:
```python
# backend/apps/datasource/row_rag/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker cp backend/apps/datasource/row_rag/__init__.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/__init__.py
docker cp backend/apps/datasource/row_rag/text.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/text.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_text.py -q"
```
Expected: PASS (5 passed)

(Note: `docker cp` of a new dir file may need the dir first: `docker exec sqlbot mkdir -p /opt/sqlbot/app/apps/datasource/row_rag`.)

---

## Task 3: Pure threshold filter + table builder

**Files:**
- Create: `backend/apps/datasource/row_rag/select.py`
- Test: `backend/tests/test_row_rag_select.py`

A "candidate" is a dict: `{"table_name": str, "content_text": str, "cosine": float, "row": dict}`.
`row` is the reconstructed source row (column→value) for display.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_row_rag_select.py
from apps.datasource.row_rag.select import filter_by_threshold, build_fallback_table


def _cand(name, cos, row):
    return {"table_name": name, "content_text": "", "cosine": cos, "row": row}


def test_filter_keeps_only_at_or_above_floor():
    cands = [_cand("t", 0.7, {"a": 1}), _cand("t", 0.49, {"a": 2}), _cand("t", 0.5, {"a": 3})]
    kept = filter_by_threshold(cands, min_cosine=0.5)
    assert [c["cosine"] for c in kept] == [0.7, 0.5]


def test_filter_empty_when_all_below_floor():
    cands = [_cand("t", 0.2, {"a": 1})]
    assert filter_by_threshold(cands, min_cosine=0.5) == []


def test_build_table_adds_source_column_and_union_fields():
    cands = [
        _cand("movies_ab12", 0.8, {"Title": "Lovebirds", "Plot": "angel"}),
        _cand("col_cd34", 0.6, {"Title": "Other", "Genre": "Thriller"}),
    ]
    out = build_fallback_table(cands)
    assert out["fields"][0] == "source"
    assert set(out["fields"]) == {"source", "Title", "Plot", "Genre"}
    assert out["data"][0]["source"] == "movies_ab12"
    assert out["data"][0]["Title"] == "Lovebirds"
    # missing columns render blank, not KeyError
    assert out["data"][0].get("Genre", "") == ""


def test_build_table_empty_candidates_returns_empty_structure():
    out = build_fallback_table([])
    assert out == {"fields": ["source"], "data": []}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker cp backend/tests/test_row_rag_select.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_select.py -q"
```
Expected: FAIL — `ModuleNotFoundError: apps.datasource.row_rag.select`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/apps/datasource/row_rag/select.py
"""Pure selection logic for the RAG fallback: confidence filtering and turning
surviving candidates into the {fields, data} table the chat renderer expects."""
from typing import Any, Dict, List


def filter_by_threshold(candidates: List[Dict[str, Any]], min_cosine: float) -> List[Dict[str, Any]]:
    """Keep candidates whose cosine is >= min_cosine, preserving input order
    (caller passes them already sorted by cosine descending)."""
    return [c for c in candidates if float(c.get("cosine", 0.0)) >= min_cosine]


def build_fallback_table(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Outer-union the candidate rows into one table with a leading 'source'
    column (the table_name each row came from). Mirrors merge_union's shape so
    the existing Python table-chart renderer can display it."""
    fields: List[str] = ["source"]
    for c in candidates:
        for k in (c.get("row") or {}).keys():
            if str(k) not in fields:
                fields.append(str(k))
    data: List[Dict[str, Any]] = []
    for c in candidates:
        nr: Dict[str, Any] = {"source": c.get("table_name") or ""}
        for k, v in (c.get("row") or {}).items():
            nr[str(k)] = v
        data.append(nr)
    return {"fields": fields, "data": data}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker exec sqlbot mkdir -p /opt/sqlbot/app/apps/datasource/row_rag
docker cp backend/apps/datasource/row_rag/select.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/select.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_select.py -q"
```
Expected: PASS (4 passed)

---

## Task 4: pgvector store (DDL + insert + query)

**Files:**
- Create: `backend/apps/datasource/row_rag/store.py`

All functions take an explicit SQLAlchemy `engine` so ingest (which has the import
engine) and fallback (which uses `session.get_bind()`) can both call them. Uses the
raw psycopg2 connection like `_insert_df_to_pg` does. Embeddings are passed as
Python `list[float]` and bound as a pgvector literal string cast `::vector`.

- [ ] **Step 1: Write the implementation**

```python
# backend/apps/datasource/row_rag/store.py
"""pgvector-backed per-row embedding store.

Single table ``row_embeddings`` in the bundled Postgres. Keyed on table_name
(globally unique via import hash suffix); datasource_id is nullable because the
CoreDatasource row does not exist yet when tables are imported.
"""
from typing import Any, Dict, List, Optional

from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def _vec_literal(vec: List[float]) -> str:
    # pgvector accepts '[1,2,3]' text; cast with ::vector at the call site.
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def ensure_schema(engine) -> None:
    """Idempotently create the extension, table and indexes."""
    dim = int(settings.ROW_RAG_EMBED_DIM)
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS row_embeddings (
                id            bigserial PRIMARY KEY,
                table_name    text    NOT NULL,
                datasource_id integer,
                row_ordinal   integer NOT NULL,
                content_text  text    NOT NULL,
                embedding     vector({dim}) NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS row_embeddings_tn_idx ON row_embeddings (table_name)")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS row_embeddings_vec_idx
            ON row_embeddings USING ivfflat (embedding vector_cosine_ops)
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_table(engine, table_name: str) -> None:
    """Remove any existing rows for a table (used before re-inserting on re-import)."""
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM row_embeddings WHERE table_name = %s", (table_name,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def insert_rows(engine, table_name: str, contents: List[str],
                embeddings: List[List[float]], datasource_id: Optional[int] = None) -> int:
    """Insert one row per (content, embedding). row_ordinal = list index."""
    if not contents:
        return 0
    conn = engine.raw_connection()
    cur = conn.cursor()
    inserted = 0
    try:
        for i, (content, emb) in enumerate(zip(contents, embeddings)):
            if not emb:
                continue
            cur.execute(
                "INSERT INTO row_embeddings "
                "(table_name, datasource_id, row_ordinal, content_text, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector)",
                (table_name, datasource_id, i, content, _vec_literal(emb)),
            )
            inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    SQLBotLogUtil.info(f"row_rag: stored {inserted} row embeddings for {table_name}")
    return inserted


def query_topk(engine, q_embedding: List[float], top_k: int) -> List[Dict[str, Any]]:
    """Return up to top_k rows ordered by cosine similarity (descending).
    Each item: {table_name, content_text, cosine}. Returns [] if table absent."""
    if not q_embedding:
        return []
    conn = engine.raw_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regclass('public.row_embeddings')")
        if cur.fetchone()[0] is None:
            return []
        qv = _vec_literal(q_embedding)
        cur.execute(
            "SELECT table_name, content_text, 1 - (embedding <=> %s::vector) AS cosine "
            "FROM row_embeddings ORDER BY embedding <=> %s::vector LIMIT %s",
            (qv, qv, int(top_k)),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return [{"table_name": r[0], "content_text": r[1], "cosine": float(r[2])} for r in rows]
```

- [ ] **Step 2: Smoke-test the store in-container**

```bash
docker cp backend/apps/datasource/row_rag/store.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/store.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"
import main  # production import order
from sqlalchemy import create_engine
from common.core.config import settings
from apps.datasource.row_rag import store
eng = create_engine(f'postgresql+psycopg2://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}')
store.ensure_schema(eng)
store.delete_table(eng, '__rag_smoke__')
store.insert_rows(eng, '__rag_smoke__', ['hello world'], [[0.1]*settings.ROW_RAG_EMBED_DIM])
print(store.query_topk(eng, [0.1]*settings.ROW_RAG_EMBED_DIM, 3))
store.delete_table(eng, '__rag_smoke__')
\""
```
Expected: prints one result with `cosine` ≈ 1.0 and `table_name` `__rag_smoke__`. No exceptions.

---

## Task 5: Ingest (df → content → embed → store)

**Files:**
- Create: `backend/apps/datasource/row_rag/ingest.py`

- [ ] **Step 1: Write the implementation**

```python
# backend/apps/datasource/row_rag/ingest.py
"""Embed every row of an imported DataFrame and persist to the pgvector store.

Gated by settings.ROW_RAG_ENABLED at the call site. Best-effort: any failure is
logged and swallowed so a RAG hiccup never breaks the core Excel import.
"""
import traceback
from typing import Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.row_rag import store
from apps.datasource.row_rag.text import build_rows_content_text
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def embed_and_store_df(df, table_name: str, engine, datasource_id: Optional[int] = None) -> int:
    """Build content_text for each row, embed in batches, store. Returns count."""
    if not settings.ROW_RAG_ENABLED:
        return 0
    try:
        rows = df.to_dict(orient="records")
        contents = build_rows_content_text(rows, max_chars=settings.ROW_RAG_CONTENT_MAX_CHARS)
        # keep only non-empty content rows, remembering their original ordinal
        pairs = [(i, c) for i, c in enumerate(contents) if c]
        if not pairs:
            return 0
        store.ensure_schema(engine)
        store.delete_table(engine, table_name)  # replace on re-import

        model = EmbeddingModelCache.get_model()
        batch = max(1, int(settings.ROW_RAG_EMBED_BATCH))
        all_contents = [c for _, c in pairs]
        embeddings = []
        for start in range(0, len(all_contents), batch):
            chunk = all_contents[start:start + batch]
            embeddings.extend(model.embed_documents(chunk))
        return store.insert_rows(engine, table_name, all_contents, embeddings, datasource_id)
    except Exception:
        traceback.print_exc()
        SQLBotLogUtil.error(f"row_rag ingest failed for {table_name} (non-fatal)")
        return 0
```

- [ ] **Step 2: Validate import**

```bash
docker cp backend/apps/datasource/row_rag/ingest.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/ingest.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"import main; from apps.datasource.row_rag import ingest; print('ok', ingest.embed_and_store_df.__name__)\""
```
Expected: `ok embed_and_store_df`

---

## Task 6: Wire ingest into the Excel import path

**Files:**
- Modify: `backend/apps/datasource/api/datasource.py` (the `_create_fts_indexes(df, table_name, engine)` call, currently line ~748)

- [ ] **Step 1: Add the ingest call next to FTS**

Locate (around line 746-748):
```python
        try:
            _insert_df_to_pg(df, table_name, engine)
            _create_fts_indexes(df, table_name, engine)
```
Change to:
```python
        try:
            _insert_df_to_pg(df, table_name, engine)
            _create_fts_indexes(df, table_name, engine)
            if settings.ROW_RAG_ENABLED:
                from apps.datasource.row_rag.ingest import embed_and_store_df
                embed_and_store_df(df, table_name, engine)
```
(Import is function-local to avoid adding a module-level dependency / circular-import risk, matching the file's existing `from common.core.config import settings` local import in `_create_fts_indexes`.)

- [ ] **Step 2: Validate import order**

```bash
docker cp backend/apps/datasource/api/datasource.py sqlbot:/opt/sqlbot/app/apps/datasource/api/datasource.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"import main; print('import ok')\""
```
Expected: `import ok` (no circular-import error)

---

## Task 7: Fallback (question → embed → query → threshold → table)

**Files:**
- Create: `backend/apps/datasource/row_rag/fallback.py`

Reconstructs each candidate's `row` dict by parsing `content_text` back into
`{col: value}` (it was built as ``"col: value | col: value"``). This avoids a second
DB round-trip to the source table and keeps the fallback self-contained.

- [ ] **Step 1: Write the implementation**

```python
# backend/apps/datasource/row_rag/fallback.py
"""Last-resort semantic row retrieval, used only after the SQL pipeline fails.

Returns a {fields, data} table of grounded rows, or None when nothing clears the
confidence floor (caller then shows the normal 'no match' message). Never invents
content.
"""
import traceback
from typing import Any, Dict, List, Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.datasource.row_rag import store
from apps.datasource.row_rag.select import build_fallback_table, filter_by_threshold
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def _parse_content_text(text: str) -> Dict[str, Any]:
    """Inverse of build_row_content_text: 'a: 1 | b: x' -> {'a': '1', 'b': 'x'}."""
    row: Dict[str, Any] = {}
    for part in (text or "").split(" | "):
        if ": " in part:
            k, v = part.split(": ", 1)
            row[k] = v
    return row


def row_rag_fallback(engine, question: str) -> Optional[Dict[str, Any]]:
    if not settings.ROW_RAG_ENABLED:
        return None
    try:
        model = EmbeddingModelCache.get_model()
        q_emb = model.embed_query(question)
        raw = store.query_topk(engine, q_emb, settings.ROW_RAG_TOP_K)
        candidates: List[Dict[str, Any]] = [
            {"table_name": r["table_name"], "content_text": r["content_text"],
             "cosine": r["cosine"], "row": _parse_content_text(r["content_text"])}
            for r in raw
        ]
        kept = filter_by_threshold(candidates, settings.ROW_RAG_MIN_COSINE)
        SQLBotLogUtil.info(
            f"row_rag fallback: {len(raw)} candidates, "
            f"{len(kept)} >= {settings.ROW_RAG_MIN_COSINE}; "
            f"top_cosine={raw[0]['cosine'] if raw else 'n/a'}")
        if not kept:
            return None
        return build_fallback_table(kept)
    except Exception:
        traceback.print_exc()
        SQLBotLogUtil.error("row_rag fallback failed (non-fatal)")
        return None
```

- [ ] **Step 2: Validate import**

```bash
docker exec sqlbot mkdir -p /opt/sqlbot/app/apps/datasource/row_rag
docker cp backend/apps/datasource/row_rag/fallback.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/fallback.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"import main; from apps.datasource.row_rag.fallback import row_rag_fallback; print('ok')\""
```
Expected: `ok`

---

## Task 8: Wire fallback into the chat pipeline

**Files:**
- Modify: `backend/apps/chat/task/llm.py`

**How the codebase already renders an ad-hoc table:** the fanout path sets
`result = <{fields,data}>`, `chart_type='table'`, `self._fanout_union=True`, then a
self-contained block (currently lines ~2211-2230) builds a
`{'type':'table','title','columns'}` chart, calls `save_chart(...)`, and streams the
chart + a markdown table. That block depends only on `result['fields']`, the row
data, `self.record.id`, and a title — **it does not need a datasource**. We reuse
that exact rendering shape via one new generator method, then call it at the two
failure points. We do NOT thread RAG rows back into the SQL flow (too coupled);
the method is self-contained (isolation over a few lines of duplication).

The user's actual failing query ("As her wedding nears...") fails at **no
datasource matched** — so Step 2 is the primary, sufficient fix. Step 3 (SQL
exhaustion) is a secondary safety net.

- [ ] **Step 1: Add a self-contained emit generator on the chat task class**

Add this method near the other `run_task` helpers in `llm.py`. The body mirrors the
existing `_fanout_union` render block (lines ~2211-2230) — same `save_chart` call,
same stream events — so the frontend renders it identically.

```python
    def _emit_row_rag_fallback(self, _session, question, in_chat, stream, json_result):
        """Yield a grounded table from the RAG store, or yield nothing (caller then
        proceeds to its normal no-result/error path). Returns True if it emitted."""
        from common.core.config import settings
        if not settings.ROW_RAG_ENABLED:
            return False
        from apps.datasource.row_rag.fallback import row_rag_fallback
        try:
            engine = _session.get_bind()
        except Exception:
            return False
        table = row_rag_fallback(engine, question)
        if not table or not table.get('data'):
            return False

        fields = table.get('fields') or []
        data = table.get('data') or []
        # normalize the same way the SQL path does before rendering
        data = DataFormat.convert_large_numbers_in_object_array(data)
        data = DataFormat.normalize_qualified_sql_column_keys_in_object_array(data)

        title = (question or 'Closest matches').strip()[:40]
        chart = {'type': 'table', 'title': title,
                 'columns': [{'name': f, 'value': f} for f in fields]}
        save_chart(session=_session, chart=orjson.dumps(chart).decode(), record_id=self.record.id)

        if in_chat:
            yield 'data:' + orjson.dumps(
                {'content': '',
                 'reasoning_content': '\n[fallback] no SQL match; showing the closest '
                                      'rows by meaning (vector search)\n',
                 'type': 'sql-result'}).decode() + '\n\n'
            yield 'data:' + orjson.dumps(
                {'content': orjson.dumps(chart).decode(), 'type': 'chart'}).decode() + '\n\n'
            yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
        elif stream:
            if data and fields:
                df = pd.DataFrame(data, columns=fields)
                yield DataFormat.safe_convert_to_string(df).to_markdown(index=False) + '\n\n'
        else:
            json_result['data'] = data
            json_result['chart'] = chart
            yield json_result
        return True
```

Note: a bare `return True/False` inside a generator sets `StopIteration.value`; the
callers below use a sentinel flag instead of relying on that (see Steps 2-3).

- [ ] **Step 2: Hook the "no datasource matched" failure (primary)**

In `run_task`, the no-datasource branch is (lines ~1870-1882):
```python
            if not self.ds:
                ds_res = self.select_datasource(_session)
                for chunk in ds_res:
                    ...
                if in_chat:
                    yield 'data:' + orjson.dumps({'id': self.ds.id, ...}).decode() + '\n\n'
```
`select_datasource` raises `SingleMessageError` ("No matching datasource was
found...") when nothing matches. Wrap that branch so the fallback runs before the
error surfaces:

```python
            if not self.ds:
                try:
                    ds_res = self.select_datasource(_session)
                    for chunk in ds_res:
                        SQLBotLogUtil.info(chunk)
                        if in_chat:
                            yield 'data:' + orjson.dumps(
                                {'content': chunk.get('content'),
                                 'reasoning_content': chunk.get('reasoning_content'),
                                 'type': 'datasource-result'}).decode() + '\n\n'
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'id': self.ds.id, 'datasource_name': self.ds.name,
                             'engine_type': self.ds.type_name or self.ds.type,
                             'type': 'datasource'}).decode() + '\n\n'
                except SingleMessageError:
                    _emitted = False
                    for _chunk in self._emit_row_rag_fallback(
                            _session, self.chat_question.question, in_chat, stream, json_result):
                        if _chunk is True:
                            _emitted = True
                            continue
                        yield _chunk
                        _emitted = True
                    if _emitted:
                        return
                    raise  # no confident rows -> re-raise the original no-match error
```
(`SingleMessageError` is already imported/used in this module — confirm the import
near the top and reuse it.)

- [ ] **Step 3: Hook SQL retry-exhaustion (secondary safety net)**

Find `run_task`'s outer `except` that surfaces the final SQL failure to the user
(the handler that catches `SQLBotDBError` / `SingleMessageError` / generic
exceptions after the retry loop). Before it emits the failure message, attempt the
same fallback:

```python
            except Exception as _final_err:
                _emitted = False
                for _chunk in self._emit_row_rag_fallback(
                        _session, self.chat_question.question, in_chat, stream, json_result):
                    if _chunk is True:
                        _emitted = True
                        continue
                    yield _chunk
                    _emitted = True
                if _emitted:
                    return
                raise  # nothing confident -> existing error handling continues
```
Place this so it wraps only the SQL-generation/execution section, NOT the
datasource-selection branch from Step 2 (which has its own handler). If the existing
outer handler already catches everything, add the fallback attempt at the TOP of
that handler body and fall through to the existing emission when `_emitted` is False.

- [ ] **Step 4: Validate import + no syntax errors**

```bash
docker cp backend/apps/chat/task/llm.py sqlbot:/opt/sqlbot/app/apps/chat/task/llm.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"import main; print('llm import ok')\""
```
Expected: `llm import ok`

---

## Task 9: Backfill helper for existing datasources

**Files:**
- Create: `backend/apps/datasource/row_rag/backfill.py`

Existing datasources imported before RAG was enabled have no row embeddings. This
re-reads each Excel-derived table from Postgres and embeds it. Mirrors the existing
`run_save_ds_embeddings` one-liner pattern.

- [ ] **Step 1: Write the implementation**

```python
# backend/apps/datasource/row_rag/backfill.py
"""Re-embed already-imported tables. Run manually in-container after enabling
ROW_RAG_ENABLED. Reads each table back from Postgres and calls embed_and_store_df.
"""
import pandas as pd

from apps.datasource.row_rag.ingest import embed_and_store_df
from common.utils.utils import SQLBotLogUtil


def backfill_tables(engine, table_names) -> dict:
    """Embed each table in table_names. Returns {table_name: rows_stored}."""
    out = {}
    for tn in table_names:
        try:
            df = pd.read_sql(f'SELECT * FROM "{tn}"', engine)
            out[tn] = embed_and_store_df(df, tn, engine)
        except Exception as e:
            SQLBotLogUtil.error(f"backfill failed for {tn}: {e}")
            out[tn] = 0
    return out
```

- [ ] **Step 2: Validate import**

```bash
docker cp backend/apps/datasource/row_rag/backfill.py sqlbot:/opt/sqlbot/app/apps/datasource/row_rag/backfill.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"import main; from apps.datasource.row_rag.backfill import backfill_tables; print('ok')\""
```
Expected: `ok`

---

## Task 10: End-to-end test (in-container, live embeddings)

**Files:**
- Create: `backend/tests/test_row_rag_e2e.py`

Verifies the real pipeline: ingest movie rows → fallback retrieves the right movie
by paraphrased plot → a nonsense query returns None (no hallucination). Requires
Ollama running on the host (embeddings); skip cleanly if the endpoint is down.

- [ ] **Step 1: Write the test**

```python
# backend/tests/test_row_rag_e2e.py
import pandas as pd
import pytest

import main  # noqa: F401  (production import order)
from common.core.config import settings
from apps.db.engine import get_engine_conn  # builds the sqlbot-DB engine, URL-quotes user
from apps.datasource.row_rag.ingest import embed_and_store_df
from apps.datasource.row_rag.fallback import row_rag_fallback
from apps.datasource.row_rag import store


def _engine():
    # NOTE: do NOT hand-build the URL from POSTGRES_* — POSTGRES_USER is
    # "pg@localhost" and must be URL-quoted. get_engine_conn() does that and
    # points at the same `sqlbot` DB the app/ingest/fallback all use.
    return get_engine_conn()


@pytest.fixture(autouse=True)
def _enable_rag(monkeypatch):
    monkeypatch.setattr(settings, "ROW_RAG_ENABLED", True)


def test_semantic_row_fallback_finds_movie_and_rejects_nonsense():
    eng = _engine()
    table = "__rag_e2e_movies__"
    df = pd.DataFrame([
        {"Title": "Lovebirds", "Plot": "As her wedding nears, a bride-to-be is visited "
                                       "by an angel who shows what life would be with her "
                                       "childhood best friend."},
        {"Title": "Fast Cars", "Plot": "Street racers compete across the city at night."},
        {"Title": "Deep Sea", "Plot": "A documentary about ocean trenches and marine life."},
    ])
    try:
        stored = embed_and_store_df(df, table, eng)
    except Exception as e:
        pytest.skip(f"embedding endpoint unavailable: {e}")
    assert stored == 3

    # paraphrased plot -> should retrieve Lovebirds as the top row
    hit = row_rag_fallback(eng, "a movie where a bride sees an angel revealing a life "
                                 "with her best friend from childhood")
    assert hit is not None
    assert hit["data"], "expected at least one retrieved row"
    assert hit["data"][0].get("Title") == "Lovebirds"
    assert hit["fields"][0] == "source"

    # nonsense -> nothing should clear the floor -> None (no hallucinated row)
    miss = row_rag_fallback(eng, "quarterly depreciation schedule for industrial turbines")
    assert miss is None

    store.delete_table(eng, table)
```

- [ ] **Step 2: Run the E2E test**

```bash
docker cp backend/tests/test_row_rag_e2e.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_e2e.py -q -s"
```
Expected: PASS (or SKIP if Ollama is down — start `ollama serve` on the host and rerun). If the nonsense assertion fails, raise `ROW_RAG_MIN_COSINE`; if Lovebirds isn't top, lower it — tune the default in config.py accordingly.

---

## Task 11: Final integration — rebuild and live-check

- [ ] **Step 1: Run all RAG unit tests together**

```bash
docker cp backend/tests/test_row_rag_text.py sqlbot:/tmp/
docker cp backend/tests/test_row_rag_select.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_row_rag_text.py /tmp/test_row_rag_select.py -q"
```
Expected: all PASS

- [ ] **Step 2: Build the overlay image so changes persist**

```bash
docker compose build && docker compose up -d
```
Expected: image `sqlbot:local` rebuilt, `sqlbot` container healthy (~25-40s healthcheck).

- [ ] **Step 3: Enable + backfill + live UI check**

Set `ROW_RAG_ENABLED=true` (env in docker-compose.yaml or settings), restart, run the
backfill helper over existing Excel tables, then in the Data Q&A UI re-ask the
original failing query ("As her wedding nears, a bride-to-be is visited by an
angel...") and confirm it now returns the matching movie row(s) as a table instead
of "No matching datasource".

```bash
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c \"
import main
from apps.db.engine import get_engine_conn
from apps.datasource.row_rag.backfill import backfill_tables
eng = get_engine_conn()  # same sqlbot DB; URL-quotes the pg@localhost user
# replace with your real Excel table names (query information_schema if unsure):
import pandas as pd
tns = pd.read_sql(\\\"SELECT tablename FROM pg_tables WHERE schemaname='public'\\\", eng)['tablename'].tolist()
print(backfill_tables(eng, [t for t in tns if not t.startswith('__') and not t.startswith('pg_') and t != 'row_embeddings']))
\""
```
Expected: a dict of `{table_name: rows_stored}` with non-zero counts; UI query returns rows.

---

## Notes for the implementer

- **DRY watch:** the pure `build_fallback_table` (Task 3) produces the
  `{fields, data}`; `_emit_row_rag_fallback` (Task 8) only renders it, mirroring the
  existing `_fanout_union` block's `save_chart` + stream events. The small overlap
  with that block is deliberate isolation — the fallback stays self-contained rather
  than threading rows back into the SQL flow. Do not build a third table renderer.
- **Non-fatal everywhere:** ingest and fallback swallow their own errors. A broken
  embedding endpoint must never break Excel import or the SQL pipeline.
- **`row_ordinal`** is stored for future refresh/click-through; nothing in this plan
  reads it back yet (YAGNI — don't build click-through now).
- **Tuning:** `ROW_RAG_MIN_COSINE` default 0.5 is a starting guess for
  `mxbai-embed-large`; Task 10 is where you calibrate it against real data.
