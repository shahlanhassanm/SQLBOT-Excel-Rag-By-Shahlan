# Auto-Generated Datasource Summary (Finder "Tag") — Design Spec

- **Date:** 2026-06-24
- **Status:** Approved design (pre-implementation)

## 1. Problem & evidence

The datasource finder (`select_datasource`) ranks candidates by embedding/BM25, then an LLM makes the final pick. The finder prompt (`template.yaml` `datasource:`) instructs the model to choose **based on name + description only**. But uploaded datasources have **empty or generic auto descriptions**, so the LLM has nothing to match on and returns `{"fail": "..."}` even when the right datasource is in the candidate list.

Captured finder payload for the question "money heist" (a Netflix show):
```
{"id":4,"name":"col","description":"","tables":["Movies_9dd361bff9"]}        <- a Movies table, empty description
{"id":11,"name":"thriller_2013_movies","description":"Excel file: thriller_2013_movies.csv", ...}
-> AI Response: {"fail":"没有找到匹配的数据源"}
```
The `col` datasource is literally movies, ranked as a candidate, but `description=""` and the prompt ignores the `tables` array → "no match". Same root cause makes `dataset_1000_rows` (which contains the bride-to-be plot and "Bert Kreischer") unfindable.

The content-aware embedding shipped earlier improves *ranking* but not this final LLM gate, because the gate reads `description`, which is empty/generic.

## 2. Goal / non-goals

**Goal:** auto-generate a concise natural-language summary of what each datasource contains and store it as the datasource **description**, so the finder's name+description LLM pick (and the embedding) route correctly — with no manual naming.

**Non-goals:**
- No change to the finder prompt (the summary populates the `description` it already uses).
- No per-row vector RAG.
- No frontend changes.
- Not summarizing per-table for display (routing is datasource-level).

## 3. Approach

Fold summary generation into the existing background embedding pass (`save_ds_embedding`), so one job produces: **summary → description → content-aware embedding**.

For each datasource id:
1. Build the schema text and value sample (reuse `build_ds_schema_text` / `build_ds_sample_text` from the prior feature).
2. **`generate_ds_summary(...)`** — invoke the configured default LLM with a generic prompt to produce a 1–3 sentence summary of the dataset's subject, key columns, and kind of values.
3. **Store policy:** write the summary to `core_datasource.description` **only if** the current description is empty **or** matches the generic auto pattern `^Excel file: `. Never overwrite a user-written description.
4. Build the embedding text (now including the rich description) and embed — as today.

Rejected alternatives: a separate summary job (two passes, more plumbing); inline-at-upload generation (blocks the upload tens of seconds on slow qwen).

## 4. Components

### 4.1 `generate_ds_summary` (new, in `apps/datasource/crud/table.py`)
```
def generate_ds_summary(ds, schema_text, sample_text, llm_invoke) -> str
```
- `llm_invoke(prompt) -> str` is injected (the real one calls the configured default LLM; a fake is used in tests), so the prompt-building/parsing logic is unit-testable without an LLM.
- Builds a generic prompt: instruct a 1–3 sentence, plain-text summary of *what data this datasource holds* (subject + key columns + example values), explicitly "for routing user questions to the right datasource". Input = schema text + sample values (both already bounded).
- Returns the trimmed summary, capped at `DS_SUMMARY_MAXLEN` chars; returns `''` on any failure or empty output (best-effort, never raises).

### 4.2 Default-LLM invoker (new small helper, e.g. in `apps/ai_model/model_factory.py` or a util)
```
def invoke_default_llm(prompt: str) -> str
```
- Synchronous wrapper usable from the background embedding thread: resolve `get_default_config()` (async → run via a fresh event loop), `LLMFactory.create_llm(config).llm.invoke(prompt)`, return `.content` as text. Raises on failure (caller treats as best-effort).

### 4.3 Wire into `save_ds_embedding` (`apps/datasource/crud/table.py`)
Per datasource, before computing the embedding text: if `settings.DS_SUMMARY_ENABLED` and the description is empty/generic, generate the summary and update `core_datasource.description`; then build the (sample-enriched) schema text and embed. All wrapped best-effort so a summary failure never blocks embedding.

### 4.4 Settings (`config.py`)
`DS_SUMMARY_ENABLED: bool = True`, `DS_SUMMARY_MAXLEN: int = 600`.

## 5. Data flow
```
upload -> create_ds -> run_save_ds_embeddings (background)
   per ds: schema_text + sample
           if description empty/"Excel file: ..." and DS_SUMMARY_ENABLED:
               summary = generate_ds_summary(..., invoke_default_llm)
               if summary: UPDATE core_datasource.description = summary
           embed( build_ds_schema_text(include_samples=True) )   # includes the new description
finder: ranks by embedding (now richer) + LLM pick reads description (now meaningful) -> routes
```

## 6. Testing
- **Unit `generate_ds_summary`:** injected fake `llm_invoke` returning a canned summary → asserts the prompt contains schema + sample text and the result is trimmed/capped; fake that raises → returns `''`; empty LLM output → `''`.
- **Unit store-policy:** a tiny pure predicate `should_replace_description(desc)` → True for `''`/`None`/`"Excel file: x.xlsx"`, False for a user description. Unit-tested.
- **E2E:** pick a generic-description datasource (e.g. `col`), run the summary+embed pass, assert `core_datasource.description` is no longer empty/`"Excel file:"` and reads like a real summary; then call the finder (`get_ds_embedding` + the pick) with a content question and assert it no longer returns the fail sentinel / selects a relevant datasource. Honest caveat: the *quality* of routing depends on the local qwen summary; the hard assertion is "description populated + finder no longer hard-fails", with the observed routing reported.

## 7. Risks & mitigations
- **LLM cost/latency:** one call per datasource at embed time; background only; re-embedding 14 datasources = 14 qwen calls (minutes, one-time). New uploads: one async call each.
- **Summary quality (qwen):** best-effort; a weak summary is still far better than an empty description. Off-switch `DS_SUMMARY_ENABLED`.
- **Never clobber user descriptions:** strict replace-only-if-empty/generic policy, unit-tested.
- **Background thread + async config:** `invoke_default_llm` runs `get_default_config()` on a fresh event loop inside the worker thread; failures are caught and treated as "no summary".

## 8. Out of scope
Finder-prompt changes; per-row RAG; multi-table per-table summaries; frontend.

## 9. Estimate
~0.5–1 day: `generate_ds_summary` + `invoke_default_llm` + store-policy + unit tests (~0.5 day); wire into `save_ds_embedding` + settings + E2E + re-summarize existing (~0.25–0.5 day).

Note: not a git repo — spec written, not committed; deploy via overlay image rebuild.
