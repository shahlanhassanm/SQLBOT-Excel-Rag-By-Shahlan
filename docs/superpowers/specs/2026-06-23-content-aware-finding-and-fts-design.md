# Content-Aware Datasource Finding + Full-Text Search — Design Spec

- **Date:** 2026-06-23
- **Status:** Approved design (pre-implementation)
- **Scope:** Two related improvements to datasource finding / text lookup, both fully generic (no domain rules):
  1. **Sampled content embedding** — make datasource *routing* aware of actual data values, not just names/columns.
  2. **PostgreSQL full-text search** — let "contains/about/mentions" text questions use an indexed FTS predicate instead of `ILIKE`, with scoped, dialect-gated prompt guidance.

## 1. Problem & context

- The finder (`select_datasource` + `get_ds_embedding`) routes a question to a datasource by embedding/BM25-ranking each datasource's **schema text** (`build_ds_schema_text` = name + description + table/column names/comments) and then an LLM pick. **Actual cell values are never embedded**, so a question phrased around *data content* (e.g. a plot synopsis) won't route to the datasource that contains it.
- Once routed, answering is **text→SQL** (lexical). Free-text lookups fall back to `ILIKE '%term%'`, which is slow (no index) and weak (no ranking).
- Current vector matching is brute-force Python cosine over a handful of *datasource* embeddings (`ds_embedding.py`), stored as JSON on `core_datasource.embedding`. Fine at the datasource granularity; we are **not** adding per-row vectors here.

Verified facts (2026-06-23): embedding model = `mxbai-embed-large` via host Ollama (1024-d), all datasources currently embedded; `build_ds_schema_text` feeds both the embedding writer and the BM25 lexical reranker.

## 2. Goals / non-goals

**Goals**
- Routing recall improves for value/content-style questions (Component 1).
- Indexed, ranked text search available for PG-backed (Excel) datasources (Component 2).
- Everything generic, bounded, env-overridable; no domain vocabulary, no magic literals in code.

**Non-goals**
- No per-row embeddings / vector RAG / vector index (that was Option A, explicitly deferred).
- No change to non-PostgreSQL datasource SQL generation.
- No frontend changes.

## 3. Component 1 — Sampled content embedding

**Where:** enrich the text that gets embedded, computed **once at embed time** (not at query time).

**Sampling (bounded, one query per table):**
- For each table of a datasource, run a best-effort `SELECT * FROM <dialect-quoted table> LIMIT N` via the existing `exec_sql(ds, sql)` (read-only). `N = EMBEDDING_SAMPLE_ROWS` (default 50).
- From those ≤N rows, per column collect up to `EMBEDDING_SAMPLE_VALUES_PER_COL` (default 10) **distinct non-null** values, each truncated to `EMBEDDING_SAMPLE_VALUE_MAXLEN` (default 100) chars.
- Format compactly, e.g. `(<col>: e.g. "v1", "v2", "v3")` per column, appended under each `# Table:` block.
- Cap total appended sample text at `EMBEDDING_SAMPLE_TOTAL_BUDGET` (default 4000) chars per datasource (so the embedding input stays within model context).
- Any error (dialect without `LIMIT`, permission, etc.) → skip samples for that table. **Never breaks embedding.**

**Wiring:**
- New pure-ish helper `build_ds_sample_text(ds, tables, fields, exec_fn=exec_sql) -> str`. The data-reading call is injected so the formatting/truncation/dedup logic is unit-testable without a DB.
- `build_ds_schema_text(session, ds, include_samples: bool = False)` gains the flag; when `include_samples` **and** `settings.EMBEDDING_SAMPLE_ENABLED`, it appends `build_ds_sample_text(...)`.
- **Only the embedding writer** (`save_ds_embedding`) passes `include_samples=True`. The query-time BM25 path (`get_ds_embedding` `lexical_texts`) keeps `include_samples=False` — so we never run data-sampling queries per question. Net: semantic ranking becomes content-aware; lexical ranking stays metadata-only (cheap).

**Settings (config.py, env-overridable):**
`EMBEDDING_SAMPLE_ENABLED=True`, `EMBEDDING_SAMPLE_ROWS=50`, `EMBEDDING_SAMPLE_VALUES_PER_COL=10`, `EMBEDDING_SAMPLE_VALUE_MAXLEN=100`, `EMBEDDING_SAMPLE_TOTAL_BUDGET=4000`.

**Re-embed existing datasources:** the sample only enters new embeddings. Provide a one-shot trigger to re-run `run_save_ds_embeddings` for all existing datasource ids (documented command), so already-uploaded data benefits.

## 4. Component 2 — PostgreSQL full-text search

**Ingest (index creation):**
- After a table is created in `_import_excel_sheets` (Excel datasources live in our PG via `get_engine_conn`), for each **text** column (pandas object/string dtype) create:
  `CREATE INDEX IF NOT EXISTS <name> ON "<table>" USING gin (to_tsvector('simple', "<col>"))`.
- `'simple'` config = language-agnostic, no locale/stemming-dictionary dependency; the **2-arg** `to_tsvector('simple', col)` form is IMMUTABLE, so it is index-eligible.
- Index name = `ftsidx_<table>_<col-hash>` (bounded length, unique). Best-effort: any failure logs and is skipped (import still succeeds). Gated by `EXCEL_FTS_ENABLED=True`.

**SQL-generation guidance (scoped, dialect-gated, code-side):**
- When (and only when) the datasource engine is **PostgreSQL-family** (`excel`, `pg`, `kingbase`), append a short instruction to the SQL system prompt: for free-text "find/about/mentions/containing words" questions over a text column, prefer
  `to_tsvector('simple', <col>) @@ plainto_tsquery('simple', '<terms>')`
  over `LIKE`/`ILIKE`; use normal predicates for exact/structured filters.
- Injected from **code** (conditional on engine type), not as static template text, so non-PG datasources are wholly unaffected and never emit invalid FTS.
- Setting `EXCEL_FTS_ENABLED` also gates the guidance (off ⇒ exact current prompt).

## 5. Data flow

```
UPLOAD (Excel)
  _import_excel_sheets -> create table -> [#2] create GIN FTS index per text col
  create_ds -> run_save_ds_embeddings
       -> save_ds_embedding -> build_ds_schema_text(include_samples=True)
            -> [#1] build_ds_sample_text: SELECT * LIMIT N per table -> distinct values
       -> embed (mxbai) -> store on core_datasource.embedding

ASK
  select_datasource -> get_ds_embedding (semantic, now content-aware) + BM25 (metadata)
       -> LLM pick (name/description/tables)
  generate_sql -> [#2] if PG-family: prompt includes FTS guidance -> model may emit
       to_tsvector @@ plainto_tsquery -> indexed search
```

## 6. Testing

**Component 1**
- Unit: `build_ds_sample_text` with a fake `exec_fn` returning canned rows → asserts distinct-value selection, per-value truncation, per-column cap, total budget cap, and graceful skip when `exec_fn` raises.
- E2E: upload a table with a distinctive text column, re-embed, then call the finder with a value-based question and assert it routes to that datasource (compare cosine ranking before/after, or assert the right ds id is chosen).

**Component 2**
- Ingest test: after `addExcelDatasource`, assert a GIN index on `to_tsvector('simple', <col>)` exists for each text column (query `pg_indexes`).
- E2E (honest): ask a "contains" question against a PG/Excel datasource; assert the generated SQL contains `to_tsvector(` `@@` and the expected row is returned. **If the local qwen model does not reliably emit FTS**, record the actual behavior; since `ILIKE` remains valid, this is no worse than today — the test then asserts the index exists and the answer is still correct, and the prompt-following gap is reported rather than failed silently.

## 7. Risks & mitigations

- **#2 prompt change touches the hot text→SQL path.** Mitigated: gated to PG-family engines, scoped to free-text questions, off-switch (`EXCEL_FTS_ENABLED`), and degrades to `ILIKE` (still valid) if unfollowed.
- **#1 sampling queries user data at embed time.** Mitigated: bounded `LIMIT N`, one query/table, runs in the existing background embedding thread, best-effort/skip on error. For non-PG real datasources with non-`LIMIT` dialects, samples are simply skipped.
- **Embedding-only samples** (not BM25) means a distinctive *lexical* token in data won't help BM25 routing — by design, to avoid per-query data scans; semantic embedding carries the content signal, and #2 covers in-datasource lexical search.
- **Re-embed required** for existing datasources (one-shot trigger provided).

## 8. Out of scope / explicitly not built

Per-row embeddings, pgvector ANN index, semantic row retrieval / hybrid SQL+vector merge (Option A / Design B). FTS for non-PostgreSQL datasources. Frontend changes.

## 9. Estimate

~1–1.5 days: Component 1 (helper + wiring + unit + E2E) ~0.5–0.75 day; Component 2 (ingest index + prompt injection + tests) ~0.5 day; re-embed trigger + verification ~0.25 day.

Note: this workspace is not a git repo — spec is written but not committed; deploy via overlay image rebuild.
