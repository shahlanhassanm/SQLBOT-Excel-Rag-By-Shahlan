# Agentic SQL Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade SQLBot's linear question→SQL pipeline into a bounded agentic loop: hybrid (BM25+embedding) datasource finding with top-K candidates, an execution grader with in-task SQL retry + datasource fallback, multi-candidate SQL self-consistency, and cross-datasource question decomposition with answer synthesis.

**Architecture:** All new pure logic lives in `backend/apps/chat/task/agentic.py` (mirroring the existing `apex_helpers.py` pattern: pure functions, no DB/LLM imports, unit-testable). Prompts go in `backend/templates/template.yaml` under the `sql:` and a new `agentic:` section, exposed via methods on `AiModelQuestion` in `chat_model.py`. Integration happens in `llm.py` (`select_datasource`, a new retry-wrapped execute block in `run_task`) and `ds_embedding.py` (hybrid re-ranking). Every behavior is gated by a settings flag and degrades gracefully to today's behavior on any internal error.

**Tech Stack:** Python 3.11, FastAPI/SQLModel, LangChain (existing `self.llm.stream` / `_invoke_llm_blocking`), pure-Python BM25 (no new dependencies), pytest (run inside container venv).

**Environment notes:**
- NOT a git repository → "commit" steps are replaced by `python -m py_compile` checkpoints.
- No local venv → tests run inside the running container: `docker cp` test file + `docker exec sqlbot /opt/sqlbot/app/.venv/bin/python -m pytest`.
- Deployment = rebuild overlay image (`docker compose build`) + `docker compose up -d` + health check.

**Retry-eligibility rule (critical correctness decision):** `check_sql` raises `SingleMessageError` both for parse failures AND for legitimate LLM refusals (`data['success'] == false`, e.g. "I can only answer data questions"). Refusals must NOT be retried. Retry triggers are exactly: (a) `SQLBotDBError` from `execute_sql`, (b) the three parse-failure messages produced inside `check_sql` (`'SQL answer is not a valid json object'`, `'Cannot parse sql from answer'`, `'SQL query is empty'`), (c) grader rejection of an empty result set. Nothing else.

---

## Settings flags (all in Task 1)

| Flag | Default | Meaning |
|---|---|---|
| `AGENTIC_SQL_RETRY_ENABLED` | True | master switch for in-task retry loop |
| `AGENTIC_SQL_MAX_ATTEMPTS` | 3 | total generate→execute attempts per record |
| `AGENTIC_GRADER_ENABLED` | True | LLM grading of empty results |
| `AGENTIC_MULTI_CANDIDATE_ENABLED` | True | parallel 2nd SQL candidate on retry attempts |
| `AGENTIC_DS_FALLBACK_ENABLED` | True | fall back to candidate #2 datasource after exhausting attempts (auto-selected ds only) |
| `AGENTIC_DECOMPOSE_ENABLED` | True | cross-datasource decomposition + synthesis |
| `AGENTIC_HYBRID_RANKING_ENABLED` | True | BM25+RRF fusion in datasource ranking |

### Task 1: Settings flags

**Files:**
- Modify: `backend/common/core/config.py:130-141`

- [ ] **Step 1: Add flags after line 132 (`DS_EMBEDDING_COUNT: int = 10`)**

```python
    # --- Agentic pipeline (retry loop, grader, hybrid ranking, decomposition) ---
    AGENTIC_SQL_RETRY_ENABLED: bool = True
    AGENTIC_SQL_MAX_ATTEMPTS: int = 3
    AGENTIC_GRADER_ENABLED: bool = True
    AGENTIC_MULTI_CANDIDATE_ENABLED: bool = True
    AGENTIC_DS_FALLBACK_ENABLED: bool = True
    AGENTIC_DECOMPOSE_ENABLED: bool = True
    AGENTIC_HYBRID_RANKING_ENABLED: bool = True
```

- [ ] **Step 2: Register bool flags in the `field_validator` list (line 136-142)** — add after `'TABLE_EMBEDDING_ENABLED',`:

```python
                     'AGENTIC_SQL_RETRY_ENABLED',
                     'AGENTIC_GRADER_ENABLED',
                     'AGENTIC_MULTI_CANDIDATE_ENABLED',
                     'AGENTIC_DS_FALLBACK_ENABLED',
                     'AGENTIC_DECOMPOSE_ENABLED',
                     'AGENTIC_HYBRID_RANKING_ENABLED',
```

- [ ] **Step 3: Checkpoint** — `python -m py_compile common/core/config.py` (run with any py3; pure syntax check)

### Task 2: Pure helpers module `agentic.py` + unit tests (TDD)

**Files:**
- Create: `backend/apps/chat/task/agentic.py`
- Create: `tests/test_agentic_helpers.py`

Functions (complete code in the implementation, summarized signatures here — the test file below is the authoritative spec):
- `tokenize(text: str) -> list[str]` — lowercase, ASCII word tokens + CJK bigrams (bilingual corpus).
- `bm25_scores(query_tokens, docs_tokens, k1=1.5, b=0.75) -> list[float]` — classic Okapi BM25, pure Python.
- `rrf_fuse(rank_lists: list[list[Any]], k: int = 60) -> dict[Any, float]` — Reciprocal Rank Fusion over id lists.
- `parse_grader_verdict(text) -> tuple[bool, str]` — robust JSON verdict extraction; unparseable → `(True, 'unparseable verdict; accepting')` (graceful-accept).
- `parse_decomposition(text, valid_ds_ids) -> list[dict]` — validated `{'ds_id': int, 'question': str}` list, invalid ds filtered, deduped, capped at 3; anything unparseable → `[]`.
- `build_result_preview(fields, rows, max_rows=8, max_chars=2000) -> str` — compact markdown-ish preview.
- `format_retry_feedback(stage, detail, sql) -> str` — `<error-msg>` block matching the existing llm.py:170 convention.
- `is_retryable_single_message(msg: str) -> bool` — matches exactly the three parse-failure markers from `check_sql`.

- [ ] **Step 1: Write the failing tests** (`tests/test_agentic_helpers.py` — full content in repo)
- [ ] **Step 2: Run in container — verify FAIL (module not found)**
- [ ] **Step 3: Implement `agentic.py`**
- [ ] **Step 4: Run tests in container — verify PASS**

```
docker cp tests/test_agentic_helpers.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_agentic_helpers.py -v"
```

### Task 3: Prompts (template.yaml + chat_model.py methods)

**Files:**
- Modify: `backend/templates/template.yaml` (add keys under `template.sql:` after the APEX `profile_probe_user` block ~line 617)
- Modify: `backend/apps/chat/models/chat_model.py` (new methods on `AiModelQuestion` after `profile_probe_user_question`, ~line 287)

New template keys (all under `template.sql:`): `grader_system`, `grader_user`, `retry_user`, `alt_candidate_hint`, `decompose_system`, `decompose_user`, `synthesize_system`, `synthesize_user`.

New `AiModelQuestion` methods: `grader_sys_question()`, `grader_user_question(sql, result_preview)`, `sql_retry_user_question(feedback, current_time)`, `decompose_sys_question()`, `decompose_user_question(candidates_json)`, `synthesize_sys_question()`, `synthesize_user_question(original_question, legs_json)`.

All grader/decompose prompts demand pure-JSON answers (match existing `process_check` JSON-only style). Grader verdict schema: `{"pass": true/false, "reason": "..."}`. Decompose schema: `{"multi": true/false, "subs": [{"ds_id": N, "question": "..."}]}`.

- [ ] **Step 1: Add yaml keys** (exact text in implementation)
- [ ] **Step 2: Add methods to chat_model.py**
- [ ] **Step 3: Checkpoint** — container python: `import yaml; yaml.safe_load(open('templates/template.yaml'))` + `py_compile`

### Task 4: Phase 2 — hybrid ranking + richer candidates + `self.ds_candidates`

**Files:**
- Modify: `backend/apps/datasource/crud/table.py:109-149` — extract schema-text builder `build_ds_schema_text(session, ds) -> str` from `save_ds_embedding`; reuse in both.
- Modify: `backend/apps/datasource/embedding/ds_embedding.py` — in both branches, after cosine sort: when `settings.AGENTIC_HYBRID_RANKING_ENABLED`, compute BM25 over lexical texts (non-assistant branch: `build_ds_schema_text`; assistant branch: existing `ds_schema`), fuse cosine-rank + bm25-rank via `rrf_fuse`, re-sort, THEN cut to `DS_EMBEDDING_COUNT`. Wrap in try/except → fall back to cosine order. Candidate payload gains `"tables": [first 15 table names]` (non-assistant branch only).
- Modify: `backend/apps/chat/task/llm.py:584-727` (`select_datasource`) — store `self.ds_candidates: list[dict]` (the ranked `_ds_list` minus the chosen one, in order) and `self.ds_auto_selected = not ignore_auto_select`; initialize both in `__init__` (`[]`, `False`).

- [ ] **Step 1: Extract `build_ds_schema_text`** (pure refactor; `save_ds_embedding` output text must be byte-identical)
- [ ] **Step 2: Add hybrid fusion to `get_ds_embedding`** (gated, try/except fallback)
- [ ] **Step 3: Store candidates in `select_datasource`**
- [ ] **Step 4: Checkpoint** — py_compile both files in container python

### Task 5: Refactor — extract `_apply_datasource` from `select_datasource`

**Files:**
- Modify: `backend/apps/chat/task/llm.py:661-727`

Extract the datasource-resolution block (lines 664-704: id validation, `CoreDatasource`/out_ds resolution, `chat_question.engine` setup, chat save) plus the filter/init block (lines 714-724) into:

```python
def _apply_datasource(self, _session: Session, ds_id: int) -> None:
    """Resolve ds_id, set self.ds + engine, persist chat.datasource, re-init prompts/messages."""
```

`select_datasource` calls it; the Phase-1 datasource fallback and Phase-3 secondary legs reuse it. Behavior of `select_datasource` must remain identical (same exceptions, same logging side effects).

- [ ] **Step 1: Extract method; `select_datasource` delegates**
- [ ] **Step 2: Checkpoint** — py_compile + careful diff review of moved code

### Task 6: Phase 1 — grader + bounded retry loop (+ Phase 3a multi-candidate)

**Files:**
- Modify: `backend/apps/chat/task/llm.py` — new methods `regenerate_sql(_session, feedback)` (modeled on `generate_sql` but appends a retry-feedback HumanMessage built from `sql_retry_user_question` instead of re-appending the full user question), `grade_sql_result(_session, sql, result) -> tuple[bool, str]` (blocking `_invoke_llm_blocking` call; ANY internal error → accept), `_try_alt_candidate(...)` (Phase 3a: blocking parallel second candidate via `executor.submit` on a copied message list with `alt_candidate_hint`; first-success-wins).
- Modify: `run_task` lines 1609-1712: wrap the generate→check→permissions→execute section in the bounded loop.

Loop skeleton (final code in implementation):

```python
attempt = 0
agentic_on = settings.AGENTIC_SQL_RETRY_ENABLED
max_attempts = max(1, settings.AGENTIC_SQL_MAX_ATTEMPTS) if agentic_on else 1
fallback_used = False
while True:
    attempt += 1
    try:
        # attempt 1: existing generate_sql(); attempts >=2: regenerate_sql(feedback)
        # [streaming yields preserved exactly as today]
        # check_sql -> permissions/dynamic block (unchanged, runs per attempt)
        # execute_sql -> result
        # grader: ONLY if AGENTIC_GRADER_ENABLED and result rows are empty
        break  # success
    except Exception as e:
        retryable = isinstance(e, SQLBotDBError) or \
            (isinstance(e, SingleMessageError) and is_retryable_single_message(str(e))) or \
            isinstance(e, _AgenticGraderReject)
        if not (agentic_on and retryable):
            raise
        if attempt >= max_attempts:
            if (settings.AGENTIC_DS_FALLBACK_ENABLED and self.ds_auto_selected
                    and not fallback_used and self.ds_candidates):
                fallback_used = True
                next_ds = self.ds_candidates.pop(0)
                self.sql_message = []
                self._apply_datasource(_session, next_ds['id'])
                attempt = max_attempts - 1   # grant exactly one attempt on fallback ds
                feedback = ''                # fresh ds, fresh prompt
                continue
            raise
        feedback = format_retry_feedback(stage, detail, last_sql)
```

Grader policy (cost + safety): grades ONLY empty result sets. Non-empty results are accepted without an extra LLM call. Tail behavior unchanged: on final failure the existing `except` in `run_task` reports the error exactly as today.

Multi-candidate (Phase 3a): on attempts ≥ 2 when `AGENTIC_MULTI_CANDIDATE_ENABLED`, submit candidate B (blocking, executor) in parallel with streamed candidate A; if A fails its parse/execute/grade and B is available, try B's SQL through the same checks before consuming another attempt.

- [ ] **Step 1: Add `_AgenticGraderReject` exception + `regenerate_sql` + `grade_sql_result` + `_try_alt_candidate`**
- [ ] **Step 2: Wrap run_task block in the loop** (streaming yields and permissions logic preserved per attempt)
- [ ] **Step 3: Checkpoint** — py_compile in container python

### Task 7: Phase 3b — cross-datasource decomposition + synthesis

**Files:**
- Modify: `backend/apps/chat/task/llm.py`

New methods: `decompose_question(_session) -> list[dict]` (blocking LLM call gated by `AGENTIC_DECOMPOSE_ENABLED` AND `self.ds_auto_selected` AND `len(self.ds_candidates) >= 1`; candidates JSON includes table names; parsed via `parse_decomposition`; ANY error → `[]`), `_run_secondary_leg(_session, sub) -> dict|None` (apply ds → init → ONE blocking generate+execute with one retry; returns `{ds_name, question, sql, preview}`; never raises), `synthesize_answer(_session, legs)` (streaming LLM call yielding chunks).

Integration in `run_task`:
1. After datasource selection (when auto-selected): `subs = self.decompose_question(_session)`. If `len(subs) >= 2`: the first sub assigned to the already-chosen ds becomes the primary leg — set `self.chat_question.question = subs[0]['question']` for SQL generation (original question kept in `self._original_question` for synthesis); stash `self._secondary_subs = subs[1:]`.
2. After the chart events, BEFORE the final `finish` yield (in_chat path) / before `yield json_result` (non-stream path): if `self._secondary_subs`: run each leg, then stream synthesis. in_chat: yield as `{'reasoning_content': chunk, 'type': 'sql-result'}` SSE events (renders in the existing reasoning panel; unknown-type risk avoided). MCP path (`in_chat=False, stream=True`): yield raw markdown. Non-stream: `json_result['synthesis'] = full_text`.
3. Whole block wrapped in try/except: a decomposition/synthesis failure logs and never breaks the already-delivered primary answer.

**Known limitation (documented, accepted):** synthesis text is delivered via the reasoning stream / MCP markdown / JSON field; it is not persisted as a first-class record column (no schema migration in this plan).

- [ ] **Step 1: Add the three methods**
- [ ] **Step 2: Wire into run_task (post-chart, pre-finish)**
- [ ] **Step 3: Checkpoint** — py_compile in container python

### Task 8: Verification & deployment

- [ ] **Step 1:** Full unit test run in container (`pytest /tmp/test_agentic_helpers.py -v`) — all PASS
- [ ] **Step 2:** Import smoke in container: `python -c "import main"` equivalent via uvicorn boot
- [ ] **Step 3:** `docker compose build` (cached base ok) + `docker compose up -d`
- [ ] **Step 4:** Health check `healthy`, `docker logs sqlbot` free of tracebacks, HTTP 200 on :8000
- [ ] **Step 5:** Honest final report: what's implemented, flags, limitations

## Self-Review

- **Spec coverage:** Phase 1 → Task 6; Phase 2 → Tasks 4 (+1,2,3); Phase 3 → Tasks 6 (3a) + 7 (3b). ✓
- **Refusal-retry hazard** addressed via `is_retryable_single_message`. ✓
- **Streaming protocol** unchanged for all existing event types; synthesis uses an already-rendered channel. ✓
- **Graceful degradation:** every agentic feature is flag-gated and try/except-wrapped to fall back to current behavior. ✓
- **Type consistency:** `ds_candidates: list[dict]` with `id/name/description/tables`; verdicts `tuple[bool, str]`; subs `list[{'ds_id': int, 'question': str}]`. ✓
