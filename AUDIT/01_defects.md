# AUDIT / 01 — Defect Sweep

**No code was changed.** Every entry below was produced by reading the file and,
where the entry makes a behavioural claim, by running a command whose output is
quoted. Entries where I could not decide between "bug" and "deliberate" are in
`05_open_questions.md`, not here.

**Severity key**
- **P0** — crash or data corruption
- **P1** — wrong answers, data exposure, or a measurement that cannot be trusted
- **P2** — degraded behaviour, resource waste, silent feature loss
- **P3** — cosmetic / hygiene

Sorted by severity, then blast radius.

---

## 1.5 Test-suite baseline (run first, so the rest has a reference point)

Both suites were run **three times** inside the live container
(`sqlbot:local`, verified byte-identical to the working tree — md5 match on all
9 pipeline files).

```
root tests/         24 failed, 272 passed, 3 skipped   (7.36s / 7.38s / 7.43s)
backend/tests/      28 passed                          (9.66s / 7.39s / 7.32s)
```

**Zero variance across the three runs. No flaky or order-dependent tests.**
The 3 skips are `test_minimax_integration.py` (`MINIMAX_API_KEY not set`) — a
correct skip. The 24 failures are all one file and are covered by **D-19**.

---

# P1 — Wrong answers, data exposure, untrustworthy measurement

---

### D-01 · Cross-tenant data exposure through the row-RAG fallback
`backend/apps/datasource/row_rag/store.py:126-129` · **P1** · blast radius: every question, every workspace

**What happens.** When the SQL pipeline returns no rows (or fails), the fallback
runs a vector search over `row_embeddings` and shows the matching rows to the
user. The query is:

```sql
SELECT table_name, content_text, 1 - (embedding <=> %s::vector) AS cosine
FROM row_embeddings ORDER BY embedding <=> %s::vector LIMIT %s
```

There is **no `datasource_id`, no `oid`, and no user filter**. Every row of every
imported spreadsheet in the entire instance is a candidate. The rows are then
rendered as the answer table and persisted into the asking user's
`chat_record.data`.

**Why.** `row_embeddings` has a nullable `datasource_id` column
(`store.py:30`) precisely because the datasource does not exist yet at import
time, and no later step backfills it. `row_rag_fallback(engine, question)`
(`fallback.py:28`) receives only the engine and the question — the datasource and
the user are never passed down, so the filter cannot be applied even if it were
written.

**Reproduction.** `ROW_RAG_ENABLED: "true"` is set in `docker-compose.yaml:42`,
so this is live. Import workbook A as workspace 1's datasource and workbook B as
workspace 2's. Ask a question in workspace 1 whose SQL returns zero rows and
whose wording is semantically close to workbook B's content. `store.query_topk`
returns B's rows; `_emit_table_result` (`llm.py:2095`) persists and renders them.

**Proposed fix.** Thread the datasource id (and the caller's authorised
datasource set) into `row_rag_fallback` → `query_topk`, and add
`WHERE table_name = ANY(%s)` built from the tables the caller may read.
Backfill `datasource_id` at `create_ds` time so the column becomes usable.
Until then, `ROW_RAG_ENABLED` should be treated as single-tenant-only.

**Test that would catch it.** Insert two rows into `row_embeddings` under two
different `table_name`s, call `query_topk` with an allow-list of one, assert the
other never appears.

---

### D-02 · Table samples and value hints bypass row-level permissions
`backend/apps/datasource/crud/datasource.py:503-614`, `backend/apps/datasource/value_index.py:78-118` · **P1** · blast radius: every question asked by a row-restricted user

**What happens.** Two blocks of raw cell data are put into the LLM prompt and
surfaced in the UI's reasoning stream:

1. `get_table_sample_data` issues
   `SELECT <fields> FROM <table> LIMIT 50` (line 544) and emits up to
   `TABLE_SAMPLE_CHAR_BUDGET` of those rows verbatim, plus a distinct-value
   profile over all 50.
2. `build_distinct_sql` issues
   `SELECT DISTINCT <col> FROM <table> WHERE <col> IS NOT NULL LIMIT 200`
   (`value_index.py:88`) and injects matches as `<value-hints>`.

**Neither applies the row-permission `WHERE` clause.** The column permission
filter *is* applied (`get_table_obj_by_ds` → `get_column_permission_fields`), so
the omission is specifically row-level.

**Why.** Row permissions are applied late, as an LLM-driven rewrite of the
*generated* SQL (`LLMService.generate_filter` → `build_table_filter`), not as a
property of the datasource connection. Anything that queries the datasource
outside that one path is unfiltered by construction.

**Reproduction.** Give a non-admin user a row rule restricting `orders` to their
own region, then ask any question that selects `orders`. The prompt's
`# Table: orders` sample block, visible in the chat's execution log, contains
rows from every region.

**Blast radius.** Every question, for every user who is not id 1 (see D-05), on
every datasource that has a row rule. Also affects APEX data-profiling probes
(`llm.py:1548`, `exec_sql(ds=self.ds, sql=probe_sql)`).

**Proposed fix.** Pass the row-permission filter map into `get_table_sample_data`
and `fetch_distinct_values` and append it as a `WHERE`; or apply row rules at the
connection layer so no query path can skip them. Also make the value cache key
include the filter identity (see D-11).

**Test that would catch it.** Two users, one row rule, one datasource: assert the
string returned by `get_tables_sample_data` for the restricted user contains no
value that only exists in a forbidden row.

---

### D-03 · IDOR: analysis / predict on any user's chat record
`backend/apps/chat/api/chat.py:415-439` · **P1** · blast radius: all chat history

**What happens.** The endpoint

```python
@router.post("/record/{chat_record_id}/{action_type}")
async def analysis_or_predict_question(session, current_user, current_assistant, chat_record_id, action_type)
```

has **no `@require_permissions`** decorator, and the record is loaded with

```python
stmt = select(ChatRecord.id, ChatRecord.question, ..., ChatRecord.chart, ChatRecord.data)
       .where(and_(ChatRecord.id == chat_record_id))
```

— no `create_by` predicate. The loaded `chart` and `data` are then fed to the LLM
and the analysis is streamed back to the caller.

**Why.** Every *other* read path in `apps/chat/curd/chat.py` does carry the
ownership predicate (lines 231, 241, 279, 361, 371, 565). This one query was
written inline in the API layer instead of going through those helpers, and the
predicate was not carried over.

**Reproduction.** Authenticate as user B, `POST /api/v1/chat/record/<A's record id>/analysis`.
The response streams an LLM analysis of user A's result set.

**Proposed fix.** Add `ChatRecord.create_by == current_user.id` to the `where`,
matching `get_chart_data_with_user`, and add the `@require_permissions` decorator
used by `/question`.

**Test that would catch it.** Create a record as user A; call the endpoint as
user B; assert 4xx.

---

### D-04 · IDOR: recommended questions on any user's chat record
`backend/apps/chat/api/chat.py:225-251`, `backend/apps/chat/curd/chat.py:24-34` · **P1** · blast radius: all chat history

**What happens.** `get_chat_record_by_id(session, record_id)` selects on
`ChatRecord.id` only. The endpoint has no ownership check and no
`@require_permissions`. The record's `question` and `datasource` become the seed
for a recommendation call, and `get_old_questions(session, record.datasource)`
then pulls other users' historical questions on that datasource into the prompt.

**Reproduction.** As user B: `POST /api/v1/chat/recommend_questions/<A's record id>`.

**Proposed fix.** Add the `create_by` predicate to `get_chat_record_by_id`, or
give it a `current_user` parameter as every sibling helper has.

**Test.** Same shape as D-03.

---

### D-05 · Permission model hardcodes user id 1 as an unconditional superuser
`backend/apps/datasource/crud/permission.py:76-77` · **P1** · blast radius: every permission decision

```python
def is_normal_user(current_user: CurrentUser):
    return current_user.id != 1
```

**What happens.** Row permissions, column permissions and the preview filter are
all gated on `is_normal_user(...)`. Any account whose primary key happens to be
`1` bypasses all of them, regardless of its role, workspace or `isAdmin` flag.
There is no other check.

**Why it matters beyond the obvious.** The same predicate is used *inversely* in
`LLMService.decompose_question` (`llm.py:1061`):

```python
if is_normal_user(self.current_user):
    return single
```

so cross-datasource fanout/split — documented in `important.md` as a headline
capability — is silently **disabled for every user except id 1**. See D-08.

**Proposed fix.** Replace with the real authorisation predicate
(`current_user.isAdmin` / weight / role), and split the two distinct questions
being conflated here: "may this user bypass row rules" vs "is it safe to run
unfiltered secondary legs for this user".

**Test.** Assert a non-id-1 admin still gets row rules applied, and that an id-2
user with no row rules can reach the fanout path.

---

### D-06 · Path traversal via `filePath` on two datasource endpoints
`backend/apps/datasource/api/datasource.py:584` and `:767` · **P1** · blast radius: any `ws_admin`

```python
save_path = os.path.join(path, req.filePath)      # /reparseExcel
save_path = os.path.join(path, import_req.filePath)  # /importToDb
if not os.path.exists(save_path): raise HTTPException(400, "File not found")
```

`filePath` is an unvalidated request-body string. `os.path.join` with an absolute
or `../`-prefixed value escapes `settings.EXCEL_PATH`. The file is then read by
`pd.read_csv(..., header=None, engine="c", dtype=object)` (any text file parses)
or `pd.read_excel`, and for `/importToDb` its contents are loaded into a new
PostgreSQL table and returned to the caller.

**Reproduction.** `POST /api/v1/datasource/importToDb` with
`{"filePath": "../../../../etc/hostname", "sheets": [...]}`.

**Mitigating context.** Both endpoints require `ws_admin`. This is privilege
*escalation from ws_admin to filesystem read*, not anonymous access.

**Proposed fix.** Resolve and confine:
`p = os.path.realpath(os.path.join(path, name)); assert p.startswith(os.path.realpath(path) + os.sep)`.
Better: never accept a path — accept the opaque id already returned by
`/parseExcel` and look the real path up server-side.

**Test.** Post `filePath="../x"`; assert 400 and that no read occurs.

---

### D-07 · Path traversal + broken error handling on the failure-report download
`backend/apps/settings/api/base.py:2, 16-40` · **P1** · blast radius: any authenticated user

Two defects in one 24-line endpoint:

1. **Traversal.** `file_path = os.path.join(path, req.file)` with `req.file`
   fully caller-controlled. The existence check runs on line 25, *before* the
   `_error.xlsx` suffix check on line 29 — so the ordering leaks file existence
   for arbitrary paths, and any `*_error.xlsx` anywhere on the filesystem is
   downloadable.
2. **Wrong exception class.** Line 2 is `from http.client import HTTPException`,
   not `from fastapi import HTTPException`. `http.client.HTTPException` is a bare
   `Exception` subclass that ignores the status argument, so `raise HTTPException(404, ...)`
   propagates to `global_exception_handler` and returns **500**, not 404/400.
   The same wrong import exists at `backend/apps/system/crud/user_excel.py:4`,
   where it breaks the `.xlsx/.xls` upload guard and three other guards.

**Proposed fix.** Import `HTTPException` from `fastapi` in both modules; check
the suffix before touching the filesystem; confine the resolved path.

**Test.** Assert `/system/download-fail-info` with `{"file":"../../etc/hostname"}`
returns 4xx, and that a bad extension returns 400 rather than 500.

---

### D-08 · SQL injection surface via spreadsheet column names — in the question path
`backend/apps/datasource/crud/datasource.py:530, 534-544` and `:350-388` · **P1** · blast radius: every question against an Excel datasource

**What happens.** Field names are interpolated into SQL with hand-written quote
characters and **no escaping of the quote character itself**:

```python
field_names = [f"{prefix}{field.field_name}{suffix}" for field in fields]   # line 530
query = f"SELECT {','.join(field_names)} FROM {prefix}{table_name}{suffix} LIMIT {probe_rows}"
```

and, in `preview()`:

```python
sql = f'''SELECT "{'", "'.join(fields)}" FROM "{conf.dbSchema}"."{data.table.table_name}" {where} LIMIT 100'''
```

`field_name` originates from the uploaded spreadsheet's header row.
`island_detection.region_columns` (line 140) does only
`str(v).strip()` — a header cell containing `"` survives verbatim into
`core_field.field_name`. The table itself is created safely with
`psycopg2.sql.Identifier`, so the injected name is *stored* and then *replayed*
unescaped on every subsequent question.

**Why this is worse than a normal admin-only injection.** `get_table_sample_data`
runs on **every chat question**, not just on an admin screen. The payload is
planted once at upload and fires on every user's questions thereafter.

**Reproduction.** Upload a workbook whose header cell A1 is
`a" , (SELECT version()) AS x, "b`. The generated sample query becomes
`SELECT "a" , (SELECT version()) AS x, "b", ... FROM ...`.

**Mitigating context.** `exec_sql` runs `check_sql_read` first, which rejects
statements whose first keyword is a write and rejects write nodes found by
sqlglot — so this is a **read**-injection, not a write one. It still exfiltrates
arbitrary readable data into the prompt and the UI.

**Proposed fix.** Route every identifier through one escaping function
(`apex_helpers.quote_ident` already doubles the quote character correctly) and
delete the four ad-hoc quoting sites (see `02_hardcoding.md` H-19). Sanitise
header names at import.

**Test.** `get_table_sample_data` with a field named `a"b` must produce
`"a""b"`, and `check_sql_read` must still accept the result.

---

### D-09 · `extract_nested_json` returns the *first* JSON object, not the answer
`backend/common/utils/utils.py:60-80` · **P1** · blast radius: every SQL parse, chart parse, brief and chart-type extraction

```python
if len(results) > 0 and results[0]:
    return results[0]
```

The function scans for balanced `{}`/`[]` spans, validates each with
`orjson.loads`, collects **all** of them, and returns the first. Every consumer
(`check_sql`, `get_chart_type_from_sql_answer`, `get_brief_from_sql_answer`,
`check_save_chart`, `_parse_candidate_sql`, `_run_secondary_leg`) treats that as
"the model's answer".

A model that restates the schema, echoes the requested output format, or emits a
worked example before its answer therefore has its **example** parsed as the
answer. With reasoning models the `<think>` block is stripped by `process_stream`
only when `PARSE_REASONING_BLOCK_ENABLED` and the tags match; JSON inside an
unstripped reasoning block is a candidate.

Secondary defect on the same lines: the inner validation uses a **bare
`except:`** (line 74), which swallows `KeyboardInterrupt` and `SystemExit`.

**Proposed fix.** Return the **last** valid top-level object (models put the
final answer last, which is the same reasoning `bird_eval.extract_sql` already
uses — it takes `m[-1]` of the fenced blocks), or prefer the first object that
contains a `sql` key. Replace the bare `except` with `except Exception`.

**Test.** Feed `'{"example": {"sql": "SELECT 1"}} ... {"success": true, "sql": "SELECT 2"}'`
and assert the extracted SQL is `SELECT 2`.

---

### D-10 · Soft-F1 is order-sensitive, therefore non-reproducible
`backend/tests/bird_eval.py:125-150` · **P1** · blast radius: every reported Soft-F1 number

`calculate_f1_score` compares `predicted[i]` against `ground_truth[i]`
positionally. Neither result set is sorted, and most BIRD queries have no
`ORDER BY`, so PostgreSQL is free to return rows in any order.

**Measured.** Re-scoring the *same committed file* twice, back to back, against
the *same unchanged database*:

```
bird_final_32b.json   recomputed f1 = 52.0    (run 1)
bird_final_32b.json   recomputed f1 = 52.5    (run 2)
bird_final_32b.json   recomputed f1 = 51.8    (earlier run)
stored (docs)         52.7
```

EX and EX-tolerant reproduced **exactly** in all three runs. Only Soft-F1 moved.

**Proposed fix.** Sort both row lists with a stable canonical key before
matching, or compute F1 over multisets of rows rather than positionally.
Until then, do not quote Soft-F1 to one decimal place.

**Test.** Score the same prediction twice with the row order reversed; assert
identical F1.

---

### D-11 · BIRD strict EX is non-deterministic for float aggregates
`backend/tests/bird_eval.py:73` (`set(predicted) == set(gold)`) · **P1** · blast radius: the headline metric

**Measured.** Executing the stored prediction and the gold query for q1473
(`debit_card_specializing`, `AVG(consumption)/12`, column type `real`) five times
in a row against an unchanged database:

```
0  459.9562642112432   459.9562642112432    EQUAL
1  459.9562642112432   459.9562642112432    EQUAL
2  459.956264211243    459.95626421124325   DIFFERENT
3  459.9562642112432   459.9562642112432    EQUAL
4  459.9562642112432   459.95626421124325   DIFFERENT
```

**2 of 5 runs disagree.** `AVG` over `real` accumulates in float; the summation
order depends on the plan (parallel workers / join order), and floating-point
addition is not associative, so the last ULP moves between runs. Strict set
equality then flips EX between 0 and 1 for the same SQL.

This is what produced the single per-question disagreement found when
re-scoring the committed files: `bird_postfix_gptoss_150.json` q1473 stored
`ex=0`, recomputed `ex=1` (see `03_eval_integrity.md`).

**Proposed fix.** This is inherent to BIRD's official metric and cannot be
"fixed" without diverging from the leaderboard. The correct action is
**reporting**: quote EX with an explicit note that float-aggregate questions
carry ±1 question of run-to-run noise, and treat **EX-tolerant** (which rounds to
6 dp and was stable across every run) as the internal decision metric.

**Test.** Not deterministically testable — flagged **HIGH RISK** per the ground
rules. The nearest deterministic test is asserting `_norm_cell` collapses the two
observed values to the same key.

---

# P2 — Degraded behaviour, resource waste, silent feature loss

---

### D-12 · Cross-datasource fanout/split is dead for every user except id 1
`backend/apps/chat/task/llm.py:1061` · **P2 (functional)** · blast radius: the whole multi-file feature

```python
if is_normal_user(self.current_user):
    return single
```

`is_normal_user` is `id != 1` (D-05), so `decompose_question` returns `single`
for every real user account. `AGENTIC_DECOMPOSE_ENABLED: "true"` in
`docker-compose.yaml:64` and `important.md` both describe cross-file answers as
active; in practice only the seeded `admin` account (id 1) can reach them.

The comment on the gate says the intent is "not a row-permission restricted
user", which is a different and much narrower condition than "not user 1".

**Proposed fix.** Gate on *actually having row rules* — e.g. reuse
`get_row_permission_filters(...)` returning empty — rather than on the id-1
proxy.

**Test.** As a non-admin user with no row rules, assert `decompose_question`
returns `mode != 'single'` when two datasources score above the fanout threshold.

---

### D-13 · Relation and value caches omit the permission dimension
`backend/apps/datasource/relations.py:567` and `backend/apps/datasource/value_index.py:139` · **P2**

- `relations`: key is `f"{ds_id}:{schema}"`. The `tables` dict passed in is built
  from `get_table_obj_by_ds`, which is **column-permission filtered per user**.
  The first caller's filtered view is cached for `SCHEMA_RELATIONS_CACHE_TTL`
  (3600 s) and served to every subsequent user of that datasource.
- `value_index`: key is `(ds_id, table, column)`. The cached values are raw cell
  values (D-02), so once a privileged question populates the cache, a restricted
  user's question is served from it.

**Proposed fix.** Include a hash of the effective (table, column) allow-list, and
of the row-filter identity, in both cache keys.

**Test.** Populate the cache as user A with a wide column set, then request as
user B with a narrower one; assert B's result does not contain A's extra columns.

---

### D-14 · Two module-global 200-thread pools on a single-worker server
`backend/apps/chat/task/llm.py:75`, `backend/common/utils/embedding_threads.py:6` · **P2 (scalability)**

`ThreadPoolExecutor(max_workers=200)` is declared twice, at import time, in two
modules. Combined ceiling: 400 OS threads. `start.sh` runs
`uvicorn --workers 1`, so the whole instance is one process.

Each chat task thread can hold: a scoped SQLAlchemy session against the app DB, a
`NullPool` connection to the target datasource (`db.py:203` — every `get_engine`
call opens a fresh connection, none are pooled), and one or more open LLM HTTP
streams. Nested pools inside a task (`ThreadPoolExecutor(max_workers=2)` for
planning and pruning, `max_workers=min(4, …)` for profiling) add more.

Configured limits that do *not* bound this: `PG_POOL_SIZE=20`,
`PG_MAX_OVERFLOW=30` apply only to the app-metadata engine.

**Proposed fix.** Make the pool size a setting, size it from
`PG_POOL_SIZE + PG_MAX_OVERFLOW`, and use one shared pool rather than two.

**Test.** Not unit-testable; a load test at N concurrent questions measuring peak
thread and connection counts.

---

### D-15 · The LLM instance cache is never invalidated when the model row changes
`backend/apps/ai_model/model_factory.py:138-144` · **P2**

```python
@classmethod
@lru_cache(maxsize=32)
def create_llm(cls, config: LLMConfig) -> BaseLLM:
```

`LLMConfig` is frozen and hashable, so the cache key includes model name,
endpoint, key and `additional_params`. Editing the `ai_model` row produces a
*different* config and therefore a new instance — that part is correct.

The problem is the reverse: **stale entries are never evicted**, and the API key
is part of the cache key, so a rotated credential leaves the old
authenticated client resident until 32 distinct configs push it out. On a
long-running process with one model, that is never.

Also: `@classmethod` above `@lru_cache` means `cls` participates in the key —
harmless here but fragile.

**Proposed fix.** Add an explicit `LLMFactory.invalidate()` called from
`aimodel` save/delete, or drop the cache (LangChain client construction is cheap
relative to a 30 s inference).

**Test.** Create config A, get instance; change the key; assert a new instance
and that the old one is not reachable.

---

### D-16 · `save_sql_data` stores unbounded result sets when the row limit is off
`backend/apps/chat/task/llm.py:2006-2019` · **P2**

```python
limit = 1000
if data_result and len(data_result) > limit and self.enable_sql_row_limit:
    data_obj['data'] = data_result[:limit]
```

The truncation is gated on `enable_sql_row_limit`, which
`docker-compose.yaml:61` sets to `false`
(`GENERATE_SQL_QUERY_LIMIT_ENABLED: "false"`). With the flag off, the **entire**
result set is `orjson.dumps`'d into `chat_record.data`, a `Text` column.

The comment in `docker-compose.yaml:56-58` justifying the flag says "display is
already capped at 1000 rows downstream (save_sql_data)" — that is the code above,
and it is capped only when the flag it is justifying is **on**. The two are
circular.

`_maybe_raise_limit` independently lifts model LIMITs up to
`AGENTIC_FULL_RESULT_LIMIT` (1000) for listing questions, but only for listing
questions; an aggregate over a large table has no cap at all.

**Proposed fix.** Split the two concerns: a hard persistence cap (always on,
configurable) and the prompt-level LIMIT rule (optional, as intended).

**Test.** With `enable_sql_row_limit=False`, execute a 5,000-row result and
assert the persisted payload is capped.

---

### D-17 · The LLM header override fabricates a confidence score
`backend/apps/datasource/utils/header_detection.py:322-323` · **P2**

```python
if llm_idx is not None and llm_idx != idx:
    logger.info(...)
    return llm_idx, max(confidence, CONFIDENCE_THRESHOLD)
```

The fallback only fires when the heuristic is *unsure*. On override it returns a
confidence of at least `CONFIDENCE_THRESHOLD` — i.e. it reports the answer as
confident precisely in the case where nothing measured its confidence. The
returned value feeds `headerConfidence` in the preview payload
(`excel.py:83`), which is what the UI uses to decide whether to prompt the user
to override. So the one case that most needs human review is the one that stops
asking for it.

**Mitigating context.** `HEADER_LLM_ENABLED` defaults to `False`.

**Proposed fix.** Return the original heuristic confidence (or a distinct
sentinel) and let the caller decide; never synthesise a score.

**Test.** Stub `llm_detect_header_row` to override; assert the returned
confidence equals the heuristic's, not the threshold.

---

### D-18 · `has_explicit_row_count` matches any small number anywhere in the question
`backend/apps/chat/task/agentic.py:468-476` · **P2**

```python
_SMALL_INT_RE = re.compile(r'(?<!\d)\d{1,3}(?!\d)')
def has_explicit_row_count(question): return bool(_SMALL_INT_RE.search(question))
```

Any standalone 1–3 digit number switches off the completeness lift in
`_maybe_raise_limit`. So "list all customers in region 3", "show every order over
50 dollars" and "list all Q4 items" are all treated as "the user asked for N
rows", and a model that emits `LIMIT 10` is left uncorrected — the exact failure
the lift exists to fix.

**Why.** The docstring says the intent is "top 10", "5 records" — a *quantity*
adjacent to a noun — but the implementation is position-independent.

**Proposed fix.** Require adjacency to a count word (`top|first|last|limit|
最多|前|상위`) or to a plural noun, and keep the numeric-only guard as a fallback.

**Test.** `has_explicit_row_count("list all customers in region 3")` → False;
`has_explicit_row_count("top 10 customers")` → True. (The current suite tests
only the second shape.)

---

### D-19 · 24 of 296 tests cannot pass under the project's own documented workflow
`tests/test_supplier_config.py:15` · **P2 (test integrity)**

```python
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

Every test in the file resolves repo paths relative to its own location. The
documented way to run tests (`docker cp` into the container, per every test
docstring and `docs/BENCHMARK-BIRD.md`) puts the file at `/tmp/roottests/…`, so
`PROJECT_ROOT` becomes `/tmp` and all 24 assertions fail on missing
`frontend/src/entity/supplier.ts`, `frontend/src/assets/model/*.png`,
`README.md` and `docs/README.en.md`.

**Verified.** The asserted content genuinely exists in the repo
(`supplier.ts` contains `id: 13` and 4 `MiniMax` occurrences; both READMEs
mention MiniMax; the PNG is present). The failure is 100 % path, 0 % content.

Measured, three identical runs: `24 failed, 272 passed, 3 skipped`.

**Proposed fix.** Locate the repo root by walking up for a marker
(`pyproject.toml` / `.git`), or skip the file when the marker is absent, so an
in-container run reports "skipped" rather than "failed".

**Test.** The fix *is* the test: run the suite from an arbitrary directory and
assert 0 failures.

---

### D-20 · Wide silent-exception swallowing in the persistence layer
`backend/apps/chat/curd/chat.py` (lines 131, 182, 225, 236, 272, 284, 295, 466, 507, 513, 518, 526, 628, 986) · **P2**

Fourteen `except Exception:` blocks whose body is `pass` or `return {}`.
`get_chart_data_with_user` is representative:

```python
for row in res:
    try:
        return orjson.loads(row.data)
    except Exception:
        pass
return {}
```

A record whose `data` column is corrupt, truncated (see D-16) or `None` is
indistinguishable from a record that does not exist or that the caller does not
own. The UI renders "no data" in all three cases and nothing is logged.

**Proposed fix.** Log at `warning` with the record id before returning the empty
value; keep the fail-soft behaviour.

**Test.** Store invalid JSON, assert an empty result *and* one log line.

---

### D-21 · Bare `except:` in five places
`apps/db/db.py:596`, `common/utils/utils.py:74`, `:244`, `common/audit/schemas/logger_decorator.py:317`, `:331` · **P2**

Bare `except:` catches `KeyboardInterrupt`, `SystemExit` and `GeneratorExit`.
`utils.py:244` sits in `prepare_model_arg`, on the model-config path;
`db.py:596` is inside `convert_value`'s BIT-type branch, which runs per cell of
every result row — so a `GeneratorExit` during streaming teardown is swallowed
per-cell.

**Proposed fix.** `except Exception:` in all five.

**Test.** Not directly testable; a lint rule (`ruff` `E722` is already in the
selected rule set but the pre-commit hook is evidently not enforced on this tree).

---

### D-22 · Duplicate committed benchmark artefacts
`backend/tests/bird_results/bird_32b20k_150.json`, `bird_qwen32b20k_150.json` · **P2 (data hygiene)**

```
afb15069230dbd6d48d6f02310da3094  bird_32b20k_150.json
afb15069230dbd6d48d6f02310da3094  bird_qwen32b20k_150.json
```

Byte-identical (verified by md5). Two filenames imply two experiments; both
re-score to exactly `EX=53/150 (35.3%)`. Any comparison that treats them as
independent runs double-counts one result.

**Proposed fix.** Delete one, or rename to make the aliasing explicit.

---

### D-23 · Mutable default argument on an upload endpoint
`backend/apps/system/api/assistant.py:115` · **P2**

```python
async def ui(session: SessionDep, data: str = Form(), files: List[UploadFile] = []):
```

The list is created once at import and shared across every request. FastAPI
normally rebinds it per request, so this is latent rather than active — but it is
the only mutable default in the codebase and it is on a file-upload path.

**Proposed fix.** `files: List[UploadFile] = File(default_factory=list)` or `= None`.

---

### D-24 · `insert_pg` retains the pre-fix COPY bug
`backend/apps/datasource/api/datasource.py:367-398` · **P2**

`_insert_df_to_pg` (line 596) documents the fix: *"Fixes the previous code where
the StringIO was never rewound, so COPY read nothing and the slow to_sql silently
did the insert."* `insert_pg` — still reachable through the deprecated
`POST /datasource/uploadExcel` — is that previous code, unchanged:
`df.to_sql(..., if_exists='replace')` (full insert), then `df.to_csv(output)`
with **no `output.seek(0)`**, then `copy_expert` reading an exhausted buffer.

Result is not corrupt (to_sql did the work) but the endpoint runs at row-by-row
speed while appearing to use COPY, and it has none of the newer path's
`NULL ''` handling or drop-on-failure cleanup.

**Proposed fix.** Delete the deprecated endpoint, or point it at `_insert_df_to_pg`.

---

### D-25 · Stacked statements are not rejected
`backend/apps/db/db.py:658-663, 789-833` · **P2**

`exec_sql` strips only trailing semicolons (`while sql.endswith(';')`) and
`check_sql_read` calls `sqlglot.parse(sql)` — which returns a **list** —
then checks each statement is not a write type and finally checks only that the
*first* keyword is in the read allow-list. A payload such as
`SELECT 1; SELECT pg_sleep(30)` therefore passes, and psycopg2/SQLAlchemy will
execute both.

Combined with D-08 this widens the injected-header surface from "extra
subquery" to "extra statement".

**Proposed fix.** Reject `len(statements) > 1` outright.

**Test.** `check_sql_read("SELECT 1; SELECT 2", pg_ds)` → False.

---

### D-26 · Schema name interpolated into constraint discovery SQL
`backend/apps/datasource/relations.py:120, 152, 173, 189, 207, 214` · **P2**

```python
s = (schema or "").replace("'", "''")
...  WHERE nspname = '{s}'
```

Only single quotes are escaped. The value comes from `conf.dbSchema` or
`conf.database` in the datasource configuration (admin-controlled, AES-encrypted
at rest), so exploitation requires datasource-edit rights. It is still the one
place in the join-graph module that builds SQL by interpolation rather than
parameters.

**Proposed fix.** Bind as a parameter; `exec_sql` would need a params argument,
which it currently lacks.

---

# P3 — Hygiene

| ID | Item | Location |
|---|---|---|
| D-27 | `from dis import specialized` — imported, never used | `apps/chat/task/llm.py:11` |
| D-28 | Four operator scripts target a non-existent `app/` package; `prestart.sh`/`tests-start.sh` call four files that do not exist | `backend/scripts/{lint,test,prestart,tests-start}.sh` |
| D-29 | `main.py` MCP `include_operations` lists `get_model_list`, whose route is commented out | `main.py:187` vs `apps/mcp/mcp.py:126-128` |
| D-30 | ~200 lines of commented-out endpoints and CRUD retained | `api/datasource.py:191-204, 265-326`; `api/chat.py:57-72, 119-132, 151-165` |
| D-31 | Dead module: `common/core/security_config.py` (161 LOC, incl. an unused password-strength validator) | zero importers |
| D-32 | Dead modules/functions: `common/utils/http_utils.py`, `common/utils/random.py`, `apps/db/engine.py::{create_table,insert_data,get_data_engine}`, `table_embedding.py::get_table_embedding`, `common/audit/schemas/log_utils.py`, `apps/ai_model/llm.py`, `apps/settings/{models,schemas}` | see `00_inventory.md` §8 |
| D-33 | SQLModel table `terms` declared with no migration creating it and no importer | `apps/settings/models/setting_models.py` |
| D-34 | `g2-ssr/package.json` `"name": "vite-project"` | `g2-ssr/package.json:2` |
| D-35 | `docs/BENCHMARK-BIRD.md` states "gold queries all execute (150/150)"; re-verified — true. No defect, recorded so it is not re-checked. | — |

---

## Coverage gaps (untested modules, ranked by blast radius × change frequency)

| Module | LOC | Modified | Automated tests | Note |
|---|---:|---|---|---|
| `apps/chat/task/llm.py` | 3,266 | 2026-07-31 | **none** | The orchestrator. 17 operations, the retry loop, fanout, self-consistency, permission rewrite — all exercised only through manual e2e scripts. |
| `apps/chat/curd/chat.py` | 1,177 | 2026-07-06 | **none** | All persistence + 14 silent swallows (D-20). |
| `apps/db/db.py` | 843 | 2026-07-31 | **none** | 14 connector branches, `check_sql_read`, `convert_value`. D-25 lives here. |
| `apps/datasource/crud/datasource.py` | 849 | 2026-07-31 | partial (`test_ds_sample_text.py`, 6 tests) | `get_table_schema` (M-Schema assembly, FK block, lost-table recovery) has no test. D-08 lives here. |
| `apps/datasource/api/datasource.py` | 854 | 2026-07-06 | e2e scripts only | D-06, D-24. |
| `apps/system/middleware/auth.py` | 233 | 2026-07-06 | **none** | Four token schemes, one with signature verification disabled. |
| `apps/datasource/crud/{permission,row_permission}.py` | 345 | 2026-07-06 | **none** | D-02, D-05. Security-critical. |
| `common/utils/utils.py::extract_nested_json` | — | 2026-07-06 | **none** | D-09. Parses every model answer. |

**Well covered:** `agentic.py` (30), `sql_validate.py` (30), `value_linking.py` (29),
`relations.py` (34), `island_detection.py` (23), `header_detection.py` (18),
`self_consistency` helpers (15), `skeleton.py` (14), `value_index.py` (14).
The pattern is clear — the **pure helper modules are tested, the impure
orchestration and data-access modules are not**.

## Missing test categories

- **No integration test runs the full `run_task` generator.** Every claim about
  the retry loop, the grader, the fanout union and the permission-rewrite gate is
  currently unverified by any automated test.
- **No security tests at all** — no IDOR test, no traversal test, no injection
  test, no authz-matrix test.
- **No concurrency/stress test** — D-14 is unmeasured.
- **No regression test pinning any accuracy number**; the only accuracy signal is
  a manual overnight harness.
- **No test asserts the two config surfaces agree** (`header_detection._DEFAULTS`
  vs `Settings.HEADER_*`).
