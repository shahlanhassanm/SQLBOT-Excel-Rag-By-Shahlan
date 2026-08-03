# Row-Level Vector RAG Fallback — Design

Date: 2026-06-25
Status: Draft (awaiting user review)

## Problem

The text-to-SQL pipeline (finder → APEX → agentic retry/grader) cannot answer
**semantic row-lookup** questions: ones where the user describes the *meaning* of
a row rather than referencing its literal keywords or the datasource name.

Concrete failure that motivated this: the user pasted a **movie plot synopsis** —
*"As her wedding nears, a bride-to-be is visited by an angel who reveals what could
have been if she'd followed feelings for her childhood best friend"* — expecting
SQLBot to return the movie whose stored plot matches. The query shares almost no
literal tokens with the stored plot, so:

- the **finder** (name/description + sampled-value embedding) matched no datasource
  → hardcoded "no matching datasource" message;
- even if a datasource matched, **FTS/ILIKE** match keywords, not meaning, so SQL
  would return nothing.

Only **per-row embeddings** can match paraphrased meaning to a stored row. The
existing system deliberately embeds only at the **datasource** level (schema +
bounded sampled values), never per row. This spec adds a per-row vector store and
wires it as a **fallback** that runs only after the SQL pipeline gives up.

## Goals

- When the SQL pipeline fails (unparseable SQL, no datasource matched, or grader
  rejects all attempts), retrieve the most semantically-relevant **rows** and
  return them **as a table**.
- **Fully embed every row of every imported file** — all columns concatenated, not
  just text columns (explicit user requirement: maximize recall robustness).
- **Never hallucinate.** The fallback returns *retrieved rows only* — no
  LLM-generated prose. If nothing clears a confidence threshold, it returns the
  normal "no match" message instead of guessing.

## Non-Goals

- No natural-language summarization of retrieved rows (grounded table only). A
  prose-answer phase may come later but is out of scope here.
- No replacement of the SQL/finder path. This is strictly a last-resort fallback;
  a working SQL answer always wins.
- No external vector database. Reuse the existing Postgres + Ollama stack.

## Decisions (locked with user)

| Decision | Choice |
|---|---|
| Purpose | Fallback when SQL fails |
| Output | Retrieved rows rendered as a table (zero LLM prose) |
| Storage | `pgvector` extension in the **same** Postgres |
| Embedding scope | **Whole row — every column concatenated**, every row, every file |
| Guardrail | Confidence threshold; below it → "no match", never a guess |

## Approach

### Why pgvector (vs. the current JSON-in-column + Python cosine)

Datasource-level embeddings today are stored as a JSON string in
`CoreDatasource.embedding` and compared with a Python `cosine_similarity` loop
(`ds_embedding.py`). That is acceptable for ~14 datasources. Row-level embeddings
are 2–4 orders of magnitude more vectors, so a Python brute-force scan per query
would be slow and memory-heavy. We therefore introduce the `pgvector` extension and
let Postgres do indexed approximate-nearest-neighbour search. This is additive — it
does not change the existing datasource-level path.

### 1. Storage

Enable the extension (idempotent, run at startup/migration):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

New table (embedding dim = 1024, matching Ollama `mxbai-embed-large`):

```sql
CREATE TABLE row_embeddings (
  id            bigserial PRIMARY KEY,
  table_name    text    NOT NULL,      -- unique per import (hash suffix); the join key
  datasource_id integer,               -- nullable: ds is created AFTER tables import
  row_ordinal   integer NOT NULL,      -- row position within the imported file
  content_text  text    NOT NULL,      -- the exact text embedded (display + debug)
  embedding     vector(1024) NOT NULL
);
CREATE INDEX ON row_embeddings USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX ON row_embeddings (table_name);
```

`content_text` = **all columns of the row**, concatenated as
`"col_a: value | col_b: value | ..."`, NULLs skipped. Embedding the whole row (not
just text columns) is the user's explicit requirement.

**Keying decision:** during import, tables are created *before* the `CoreDatasource`
row exists (`_import_excel_sheets` runs, then the caller assembles the datasource),
so `datasource_id` is not known at insert time. We therefore key on `table_name`
(already globally unique via its hash suffix) and leave `datasource_id` nullable.
The query-time `source` label is the `table_name` (best-effort resolved to the
datasource name if a mapping is cheaply available). `row_ordinal` is the row's
position in the imported DataFrame — used to refresh/replace on re-import and as a
stable handle; display does not depend on it.

### 2. Ingestion

Hook into the shared `_insert_df_to_pg` path (api/datasource.py) so **both** the
bulk-upload and the standard-wizard import routes get row embeddings (mirrors the
content-aware-finding fix that wired both import paths). After rows are COPY'd into
Postgres:

1. Build `content_text` per row from the DataFrame (all columns).
2. Batch-embed via `EmbeddingModelCache.get_model().embed_documents(batch)` against
   the existing `EMBEDDING_API_*` Ollama endpoint.
3. Bulk-insert into `row_embeddings`.

Gated behind a new flag `ROW_RAG_ENABLED` (default **off**). When off, nothing
changes. Batch size configurable (`ROW_RAG_EMBED_BATCH`, default e.g. 64) to bound
Ollama load. Embedding is a one-time cost per import.

A **backfill** helper re-embeds existing datasources (same shape as the existing
`run_save_ds_embeddings` one-liner), so the user can enable RAG on already-imported
files without re-uploading.

### 3. Fallback trigger

In `agentic.py`, **after** the retry loop + grader have exhausted every SQL attempt
(unparseable SQL, no datasource matched, or grader rejects all). Only then. The SQL
path stays primary and always wins when it produces a graded-acceptable answer.

### 4. Retrieval

1. Embed the question (`embed_query`).
2. `SELECT ... ORDER BY embedding <=> :q LIMIT :k` across **all** `row_embeddings`
   (the finder may have matched no datasource, so we do not filter by datasource).
   `k` = `ROW_RAG_TOP_K` (default 10).
3. Compute cosine similarity (`1 - (embedding <=> q)`); keep only rows ≥
   `ROW_RAG_MIN_COSINE` (default ~0.5, tunable).

### 5. Output + guardrail

- If **no** row clears `ROW_RAG_MIN_COSINE` → return the existing clean English
  "no match" message. **It stays silent when not confident.**
- Otherwise → render the surviving rows as a Python **table chart**, reusing the
  fanout `merge_union`-style renderer. Include a `source` column
  (`datasource / table`) so the user sees provenance. No LLM prose.

### 6. Settings (common/core/config.py)

| Setting | Default | Purpose |
|---|---|---|
| `ROW_RAG_ENABLED` | `False` | Master on/off for ingest + fallback |
| `ROW_RAG_EMBED_BATCH` | `64` | Rows per embedding batch at import |
| `ROW_RAG_TOP_K` | `10` | Candidate rows pulled from pgvector |
| `ROW_RAG_MIN_COSINE` | `0.5` | Confidence floor; below → "no match" |

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `row_rag/store.py` (new) | DDL ensure, insert, ANN query | pgvector, PG session |
| `row_rag/ingest.py` (new) | row→content_text, batch embed, bulk insert | embedding cache, store |
| `row_rag/fallback.py` (new) | embed question, query, threshold, build table | store, chart renderer |
| `_insert_df_to_pg` (edit) | call ingest after COPY when `ROW_RAG_ENABLED` | ingest |
| `agentic.py` (edit) | invoke fallback after pipeline exhaustion | fallback |
| backfill helper | re-embed existing datasources | ingest |

Pure logic (content_text builder, threshold filter, source-column merge) lives in
functions testable without a DB, matching the existing `apex_helpers` / island
pattern.

## Testing

- **Unit (no DB):** `content_text` builder (all columns, NULL skip, ordering);
  threshold filter (keeps ≥ floor, drops below, empty → no-match); source-column
  merge.
- **E2E (in-container, live app):** enable `ROW_RAG_ENABLED`, ingest the movies
  data, ask the bride/angel plot → assert the correct movie row is returned **and**
  that a nonsense query returns the "no match" message (not a spurious row).

## Risks / Limits

- **Ingestion latency** on the local Ollama stack for large files; bounded by
  `ROW_RAG_EMBED_BATCH`, one-time per import, off by default.
- **Whole-row embedding adds noise** from numeric/ID columns; accepted per user
  requirement for maximum recall. `ROW_RAG_MIN_COSINE` is the lever if recall is
  too loose.
- **Existing datasources are not retro-embedded** until the backfill helper is run
  (same caveat as island detection / FTS).
- **ivfflat** needs a populated table before `ANALYZE`/index is effective; for small
  tables a flat scan is fine. Revisit index type (hnsw) only if scale demands.
