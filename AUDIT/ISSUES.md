# ISSUES — consolidated register

**Single list of every bug, flag, risk and question found in the audit.**
One row per item. Detail lives in the phase document named in the last column.

- **Total: 90 items** — 11 P1, 27 P2, 15 P3, 19 hardcoding groups, 12 eval-integrity, 18 questions
  (D-36 added during Phase 5)
- Generated 2026-08-01 against `main` @ `d2d83bb` + uncommitted working tree
- Container `sqlbot:local` verified byte-identical to the working tree (md5 on all 9 pipeline files), so every measurement below reflects this source

**Status values:** `OPEN` · `FIXED` · `WONTFIX` · `QUESTION`
**Evidence:** ✅ = measured/executed during the audit · 📖 = established by reading the code

## Phase 5 progress — branch `audit/phase5-fixes`

| Issue | Status | Commit | Tests |
|---|---|---|---|
| **D-36** *(new, P2)* | ✅ **FIXED** | `d27d3c4` | +5 |
| **D-03** | ✅ **FIXED** | `cc8a208` | +7 (shared file with D-04) |
| **D-04** | ✅ **FIXED** | `cc8a208` | ↑ |
| **D-07** | ✅ **FIXED** | `740a5ff` | +13 |
| **D-01** | ✅ **FIXED** | `4d15453` | +8 |
| **D-02** | ✅ **FIXED** | `3b057a1` | +24 |
| **D-13** (value-cache half) | ⚠️ **PARTIAL** | `3b057a1` | ↑ |
| **D-08** | ✅ **FIXED** | `35825f6` | +29 |
| **D-09** | ✅ **FIXED** | `123f890` | +12 |
| **D-05 option 1** / **D-12** | ✅ **FIXED** | `2af61e7` | +7 |
| **D-05 option 2** | **DEFERRED — separately tracked** (see D-37) | — | — |
| **L-A** | ✅ **FIXED + BENCHMARKED** | `0026e3d` `34886ee` | +15 |
| **D-06** | ✅ **FIXED** | `40cb518` | +21 |
| **D-38** *(new)* | ✅ **FIXED** | `d7dbb80` | +43 |
| **D-10** / **E-01** | ✅ **FIXED** | `3b6717b` | +12 |
| **GROUP A** — E-06, E-08, E-09 | ✅ **FIXED** | `65e37dc` | +7 |
| **GROUP E** — D-25, D-26 | ✅ **FIXED** | *(pending)* | +21 |
| **D-39** *(new)* | **WONTFIX — architectural** (cycle passes through closed-source `sqlbot_xpack`) | — | — |
| **D-11** | **WONTFIX (by decision)** — official BIRD metric must stay compatible; documented instead, EX-tolerant adopted as internal KPI | — | — |

Test count: **272 → 392 passing** (+120) (root suite), `backend/tests` 28/28 throughout.
Gates in force (approved): per-file no-new-ruff, per-file no-new-mypy,
`import main` as build proxy, benchmarks only for changes that can affect SQL
generation / retrieval / ranking / prompting / execution / evaluation.

---

## 1. P1 — Wrong answers, data exposure, untrustworthy measurement

| ID | Category | Location | Issue | Evidence | Status |
|---|---|---|---|---|---|
| **D-01** | security · multi-tenant | `apps/datasource/row_rag/store.py:126` | Row-RAG fallback vector search has **no datasource, workspace or user filter** — `SELECT … FROM row_embeddings ORDER BY embedding <=> …` returns rows from every tenant's spreadsheets and renders them as the answer. `ROW_RAG_ENABLED: "true"` is live in compose. | 📖 | ✅ FIXED `4d15453` |
| **D-02** | security · authz | `apps/datasource/crud/datasource.py:503-614`, `apps/datasource/value_index.py:78-118` | Table samples (50 raw rows/table) and `<value-hints>` bypass **row-level permissions** and go into the prompt *and* the UI execution log. Column permissions are applied; row permissions are not. | 📖 | ✅ FIXED `3b057a1` |
| **D-03** | security · IDOR | `apps/chat/api/chat.py:415-439` | `POST /chat/record/{id}/{analysis\|predict}` has no `@require_permissions` and selects `ChatRecord` by id with **no `create_by` predicate** — any user can run analysis on any user's result set. | 📖 | ✅ FIXED `cc8a208` |
| **D-04** | security · IDOR | `apps/chat/api/chat.py:225`, `apps/chat/curd/chat.py:24` | `POST /chat/recommend_questions/{id}` — `get_chat_record_by_id` has no ownership filter; leaks another user's question and pulls their datasource's history into the prompt. | 📖 | ✅ FIXED `cc8a208` |
| **D-05** | security · authz | `apps/datasource/crud/permission.py:76` | `is_normal_user() → id != 1`. User id 1 bypasses **all** row and column permissions regardless of role. Three different notions of "privileged" coexist in the codebase. | 📖 | OPEN |
| **D-06** | security · traversal | `apps/datasource/api/datasource.py:584, 767` | `/reparseExcel` and `/importToDb` do `os.path.join(EXCEL_PATH, req.filePath)` with an unvalidated caller-supplied path; `/importToDb` then loads the file into Postgres and returns it. (`ws_admin` gated.) | 📖 | ✅ FIXED |
| **D-07** | security · traversal + error handling | `apps/settings/api/base.py:2, 22, 25-30` | `/system/download-fail-info`: traversal via `req.file`; existence check runs **before** the extension check; and `from http.client import HTTPException` means every guard returns **500** instead of 4xx. Same wrong import at `apps/system/crud/user_excel.py:4`. | 📖 | ✅ FIXED `740a5ff` |
| **D-08** | security · SQL injection | `apps/datasource/crud/datasource.py:530, 534-544, 350-388` | Field/table names interpolated into SQL with hand-written quotes and **no quote-escaping**. Names originate from spreadsheet headers, which `region_columns` does not sanitise. Fires on **every chat question** via `get_table_sample_data`, not just admin screens. | 📖 | ✅ FIXED `35825f6` |
| **D-09** | correctness | `common/utils/utils.py:60-80` | `extract_nested_json` returns the **first** balanced JSON object in the model's output, not the answer. Affects every SQL parse, chart parse, brief and chart-type extraction. Also a bare `except:`. The repo's own harness uses the opposite convention (`m[-1]`). | 📖 | ✅ FIXED `123f890` |
| **D-10** | measurement | `backend/tests/bird_eval.py:125-150` | **Soft-F1 is non-deterministic.** Positional row matching + no `ORDER BY`. Same file re-scored 3×: **51.8 / 52.0 / 52.5** (documented 52.7). EX was stable across all three. | ✅ | ✅ FIXED |
| **D-11** | measurement | `backend/tests/bird_eval.py:73` | **Strict EX flips on float aggregates.** Same SQL, same data, 5 consecutive runs: **2 of 5 disagreed** (`459.9562642112432` vs `459.95626421124325`, column type `real`). ±1 question of irreducible noise. | ✅ | OPEN |

---

## 2. P2 — Degraded behaviour, resource waste, silent feature loss

| ID | Category | Location | Issue | Evidence | Status |
|---|---|---|---|---|---|
| **D-12** | functional | `apps/chat/task/llm.py:1061` | **Cross-datasource fanout/split is dead for every user except id 1** — `if is_normal_user(...): return single`. The headline multi-file feature, documented as active in `important.md`, never runs for real accounts. | 📖 | ✅ FIXED `2af61e7` |
| **D-13** | correctness · cache | `apps/datasource/relations.py:567`, `apps/datasource/value_index.py:139` | Cache keys omit the permission dimension. Relations cached from user A's column-filtered view are served to user B for 3600 s; value caches serve privileged cell values to restricted users. | 📖 | ⚠️ PARTIAL `3b057a1` (value cache fixed; relations cache still open) |
| **D-38** *(new)* | security · traversal | `apps/datasource/api/datasource.py:339,555`, `apps/terminology/api/terminology.py:177`, `apps/data_training/api/data_training.py:172` | **Write-side traversal.** Four upload endpoints build the save path from `file.filename.split('.')[0]`, which is attacker-controlled. Measured: an **absolute** filename (`/etc/passwd.xlsx`) escapes `EXCEL_PATH` and is written outside it. `../` payloads happen to be neutralised by `split('.')` returning empty, but that is accidental. `/addExcelDatasource` is safe — it uses `os.path.basename`. | ✅ | ✅ FIXED |
| **D-39** *(new)* | correctness · imports | `apps/datasource/api/datasource.py` -> `apps/db/db.py` -> `apps/system/crud/assistant.py` -> `common/utils/aes_crypto.py` -> `sqlbot_xpack/__init__.py` | `apps.datasource.api.datasource` cannot be imported standalone: `ImportError: cannot import name get_assistant_info`. Same class as D-36 but the cycle passes through **closed-source `sqlbot_xpack`**, so it is not fixable in this repo. Blocks unit-testing that module by import; the D-06 audit tests read source from disk instead. | ✅ | ⛔ WONTFIX (architectural) |
| **D-37** *(new)* | security · authz | `apps/datasource/crud/permission.py:96` | **D-05 option 2, deferred by decision.** `is_normal_user() → id != 1` still lets user id 1 bypass all row/column permissions. Retargeting to `isAdmin` (a strict subset — can only tighten) requires auditing every `UserInfoDTO`/`BaseUserDTO` construction site first, incl. MCP and assistant paths. See `AUDIT/06_D05_authz_impact.md` §8 option 2. | 📖 | OPEN |
| **D-14** | scalability | `apps/chat/task/llm.py:75`, `common/utils/embedding_threads.py:6` | Two module-global `ThreadPoolExecutor(max_workers=200)` = 400 threads on a `--workers 1` uvicorn. Each can hold a DB session, a `NullPool` datasource connection and an LLM stream. `PG_POOL_SIZE=20` does not bound it. | 📖 | OPEN |
| **D-15** | correctness · cache | `apps/ai_model/model_factory.py:138` | `@lru_cache(maxsize=32)` on `create_llm` is never invalidated; a rotated API key leaves the old authenticated client resident indefinitely. | 📖 | OPEN |
| **D-16** | resource · data | `apps/chat/task/llm.py:2006-2019` | `save_sql_data` truncates to 1000 rows **only when `enable_sql_row_limit` is true** — which compose sets to `false`. Unbounded result sets are serialised into a `Text` column. The compose comment justifying the flag cites this cap, circularly. | 📖 | OPEN |
| **D-17** | correctness | `apps/datasource/utils/header_detection.py:322` | LLM header override returns `max(confidence, CONFIDENCE_THRESHOLD)` — **fabricates** a confident score for the one case nothing measured. Suppresses the UI's "please review" prompt exactly when it is needed. | 📖 | OPEN |
| **D-18** | correctness | `apps/chat/task/agentic.py:468` | `has_explicit_row_count` matches **any** standalone 1–3 digit number anywhere, so "list all customers in region 3" disables the completeness LIMIT lift. | 📖 | OPEN |
| **D-19** | test integrity | `tests/test_supplier_config.py:15` | `PROJECT_ROOT` derived from `__file__`; under the project's own documented in-container workflow it resolves to `/tmp` and **24 of 296 tests fail deterministically**. Content is present in the repo — 100 % path, 0 % content. | ✅ | OPEN |
| **D-20** | observability | `apps/chat/curd/chat.py` ×14 | Fourteen `except Exception: pass` in the persistence layer. Corrupt, truncated (see D-16) and missing records are indistinguishable, and nothing is logged. | 📖 | OPEN |
| **D-21** | correctness | `apps/db/db.py:596`, `common/utils/utils.py:74, 244`, `common/audit/schemas/logger_decorator.py:317, 331` | Five bare `except:` — swallow `KeyboardInterrupt` / `SystemExit` / `GeneratorExit`. `db.py:596` runs per cell of every result row. `ruff` E722 is in the selected rule set but not enforced on this tree. | 📖 | OPEN |
| **D-22** | data hygiene | `backend/tests/bird_results/` | `bird_32b20k_150.json` and `bird_qwen32b20k_150.json` are **byte-identical** (md5 `afb15069…`). Two names, one experiment. | ✅ | OPEN |
| **D-23** | correctness | `apps/system/api/assistant.py:115` | Mutable default argument `files: List[UploadFile] = []` on a file-upload endpoint. | 📖 | OPEN |
| **D-24** | performance | `apps/datasource/api/datasource.py:367-398` | `insert_pg` (reachable via the deprecated `/uploadExcel`) still has the un-rewound `StringIO` bug that `_insert_df_to_pg` documents as fixed: COPY reads nothing, `to_sql` silently does the work row-by-row. | 📖 | OPEN |
| **D-25** | security | `apps/db/db.py:658-663, 789-833` | Stacked statements are not rejected. `check_sql_read` type-checks each parsed statement but only keyword-checks the first; `SELECT 1; SELECT pg_sleep(30)` passes. Widens D-08. | 📖 | ✅ FIXED |
| **D-26** | security | `apps/datasource/relations.py` | **SEVERITY OVERSTATED IN THE AUDIT.** Doubling `'` IS the complete escape for a single-quoted literal (and `standard_conforming_strings` defaults on since PG 9.1), so the original code was substantially correct. A quote in a schema name is also legitimate — PostgreSQL allows it in a quoted identifier — and `tests/test_relations.py` already asserted escape-not-reject. Residual gap closed: control characters (NUL etc.) are not escapable and are now refused. | 📖 | ✅ FIXED (scope corrected) |
| **E-06** | measurement | `backend/tests/bird_eval.py` | Results files carry **no provenance** — no model, no flags, no git rev, no timestamp. Directly caused D-22. | 📖 | OPEN |
| **E-08** | measurement | `backend/tests/bird_eval.py:868` | `--resume` merges records produced under different flags into one file with nothing recording the mix. | 📖 | OPEN |
| **E-09** | measurement | `backend/tests/bird_eval.py:101 vs :125` | `ex_tol=1` and `f1=0` can coexist for the same record — EX-tolerant rounds to 6 dp, F1 does exact `Decimal` membership. The two metrics disagree about the same rows. | ✅ | OPEN |
| **E-10** | measurement | `backend/tests/accuracy_eval.py` | The in-house 55-question bank scores by "expected value appears somewhere in the rows" — cannot distinguish a correct answer from a superset. No gold SQL, no set equality. | 📖 | OPEN |
| **E-11** | measurement | `bird_results/bird_apex40.json` | The APEX comparison ran only 40 of 150 questions, on a slice the project's own notes call unrepresentative. No APEX conclusion is supported by it. | ✅ | OPEN |
| **C-01** | coverage | `apps/chat/task/llm.py` (3,266 LOC) | **Zero automated tests.** The orchestrator — retry loop, fanout, self-consistency, permission-rewrite gate — is exercised only by manual e2e scripts. | ✅ | OPEN |
| **C-02** | coverage | `apps/chat/curd/chat.py` (1,177 LOC) | Zero automated tests; contains all 14 silent swallows (D-20). | ✅ | OPEN |
| **C-03** | coverage | `apps/db/db.py` (843 LOC) | Zero automated tests. 14 connector branches, `check_sql_read`, `convert_value`. D-25 lives here. | ✅ | OPEN |
| **C-04** | coverage | `apps/system/middleware/auth.py` (233 LOC) | Zero automated tests. Four token schemes, one decoding with signature verification disabled. | ✅ | OPEN |
| **C-05** | coverage | `apps/datasource/crud/{permission,row_permission}.py` (345 LOC) | Zero automated tests on the security-critical permission translation. D-02 and D-05 live here. | ✅ | OPEN |
| **C-06** | coverage | repo-wide | **No security tests of any kind** — no IDOR, traversal, injection or authz-matrix test. No concurrency test. No accuracy regression gate. | ✅ | OPEN |

---

## 3. P1/P2 — Benchmark & evaluation integrity

| ID | Severity | Location | Issue | Evidence | Status |
|---|---|---|---|---|---|
| **E-01** | P1 | `bird_eval.py:125` | Soft-F1 order-sensitive → ±1 pp noise. Same as **D-10**. | ✅ | ✅ FIXED (same as D-10) |
| **E-02** | P1 | `bird_eval.py:73` | Strict EX flips on float aggregates. Same as **D-11**. | ✅ | OPEN |
| **E-03** | P1 | `bird_eval.py:373, 688, 216, 540` | The harness **re-implements four production capabilities** — identifier check, join graph, value sampling, repair loop — and calls none of the product code. `--mode model --all-fixes` measures a third system that does not ship. | 📖 | OPEN |
| **E-04** | P1 | `bird_eval.py:885` | `--finish sql` (the default) **cancels self-consistency and never runs the execution-retry loop or the grader**. The shipping configuration has never been benchmarked. | 📖 | OPEN |
| **E-05** | P1 | `docs/BENCHMARK-BIRD.md:149-151` | Doc states "no completed 150-question pipeline run"; **two exist** (untracked). Running the repo's own `bird_significance.py`: pipeline is **−3.3 pp vs the raw model, McNemar p = 0.4731 (noise)**, 20.7 % churn, +4 NO-SQL. The project's central open question has an answer and the docs do not reflect it. | ✅ | OPEN |
| **E-07** | P2 | `bird_results/` | Byte-identical duplicate result files. Same as **D-22**. | ✅ | OPEN |
| **E-12** | P3 | `bird_eval.py:1008` | Timeouts scored as wrong rather than excluded — a slow GPU depresses the score in a way a faster one would not. | 📖 | OPEN |

### Reproduction summary (what did and did not hold up)

| Metric | Files checked | Result |
|---|---|---|
| **EX (official)** | 9 | **Reproduced exactly on 8/9.** One disagreement (q1473) traced to E-02. |
| **EX (tolerant)** | 9 | **Reproduced exactly on 9/9.** The only stable metric. |
| **Soft-F1** | 9 | Reproduced on 4/9; drifts ±0.7 pp on the rest (E-01). |
| Headline numbers in `docs/BENCHMARK-BIRD.md` (40.7 / 52.0 / 43.3 %) | 3 | **All correct.** |

---

## 3b. File-write entry-point audit (D-38 follow-up)

Every write in `backend/` reviewed. `open(...,'w'/'wb')`, `Path.write_*`,
`shutil.*`, `os.rename/replace/remove`, `to_excel`, `to_csv`,
`NamedTemporaryFile`.

| Site | Path source | Verdict |
|---|---|---|
| `datasource.py` `/uploadExcel`, `/parseExcel`, `/addExcelDatasource` | `file.filename` | ✅ **fixed** — `safe_upload_name` + `safe_join` |
| `terminology.py` upload + error file | `file.filename` | ✅ **fixed** |
| `data_training.py` upload + error file | `file.filename` | ✅ **fixed** |
| `settings/api/base.py` download | `req.file` | ✅ fixed in D-07 |
| `datasource.py` `/reparseExcel`, `/importToDb` | `req.filePath` | ✅ fixed in D-06 |
| `header_detection.py:398` sidecar | `save_path + ".sqlbot_headers.json"` | ✅ justified — derives from an already-confined path |
| `user_excel.py:188` | `tempfile.NamedTemporaryFile` | ✅ justified — OS-generated name |
| `user_excel.py:332` `os.remove` | `_TEMP_FILE_MAP` registry | ✅ justified — server-generated uuid key, path confined to tempdir |
| `to_excel(writer, ...)` ×7 | `io.BytesIO` | ✅ justified — in-memory, never touches disk |
| `datasource.py:389,624` `to_csv` | `StringIO` | ✅ justified — in-memory |

**No unprotected user-controlled write remains.**

## 4. P3 — Hygiene

| ID | Location | Issue |
|---|---|---|
| **D-27** | `apps/chat/task/llm.py:11` | `from dis import specialized` — imported, never used |
| **D-28** | `backend/scripts/{lint,test,prestart,tests-start}.sh` | Target a non-existent `app/` package; call 4 files that do not exist |
| **D-29** | `main.py:187` vs `apps/mcp/mcp.py:126` | MCP `include_operations` lists `get_model_list`, whose route is commented out |
| **D-30** | `api/datasource.py:191-204, 265-326`; `api/chat.py:57-72, 119-132, 151-165` | ~200 lines of commented-out endpoints retained |
| **D-31** | `common/core/security_config.py` | Dead module, 161 LOC, includes an unused password-strength validator |
| **D-32** | 8 locations | Dead code: `common/utils/http_utils.py`, `common/utils/random.py`, `apps/db/engine.py::{create_table,insert_data,get_data_engine}`, `table_embedding.py::get_table_embedding`, `common/audit/schemas/log_utils.py` (shadowed by the xpack import), `apps/ai_model/llm.py` (contains only `# todo`), `apps/settings/{models,schemas}` |
| **D-33** | `apps/settings/models/setting_models.py` | SQLModel table `terms` declared; no migration creates it, nothing imports it |
| **D-34** | `g2-ssr/package.json:2` | `"name": "vite-project"` |
| **X-01** | `apps/api.py:11, 35` | `audit_api` router import and registration both commented out |
| **X-02** | `.gitignore` + `Dockerfile:34` | No `uv.lock` in the repo (the running container has one from the base image) — the deployed dependency set is not reproducible from source |
| **X-03** | `.gitignore` + `Dockerfile.overlay:9` | No `package-lock.json`; frontend build unpinned |
| **X-04** | `pyproject.toml` | Default Python index is `http://mirrors.aliyun.com/pypi/simple` — **plain HTTP** |
| **X-05** | `pyproject.toml` | `sqlbot-xpack`, a hard runtime dependency imported at `main.py:4`, is sourced from **testpypi** |
| **X-06** | `Dockerfile-base:27` | `curl … \| sh` of a remote license validator at build time, no checksum |
| **X-07** | `Dockerfile.overlay:15` | Base image is the floating tag `dataease/sqlbot:latest` — image contents change with no repo change |

---

## 5. Hardcoding & portability (grouped)

Full table with file:line, failure mode and proposed config key in
`02_hardcoding.md`.

| ID | Group | Highest-impact item |
|---|---|---|
| **H-01** | Hosts / URLs | `SERVER_IMAGE_HOST` ships as the literal placeholder `http://YOUR_SERVE_IP:MCP_PORT/images/` **in `docker-compose.yaml:28`** — every MCP/API chart reply returns an unresolvable URL, silently |
| **H-02** | CORS | `FRONTEND_HOST` defaults to `http://localhost:5173` and is **unconditionally** appended to `all_cors_origins` — a Vite dev origin is permitted in production |
| **H-03** | Cache | `CACHE_TYPE=redis` with an empty `CACHE_REDIS_URL` silently connects to `redis://localhost:6379/0` instead of failing |
| **H-04** | Credentials | `POSTGRES_PASSWORD=Password123@pg` declared in **three** places incl. baked into the image as an `ENV` (`Dockerfile:81`) |
| **H-05** | Ports | 8000/8001/3000/5432 as literals across `start.sh`, `main.py`, `g2-ssr/app.js`, `Dockerfile` |
| **H-06** | Eager defaults | `SCRIPT_DIR`, `EMBEDDING_TERMINOLOGY_*`, `EMBEDDING_DATA_TRAINING_*` are `X = <another field>` evaluated at class-definition time — **overriding the parent does not move the child** |
| **H-07** | `.env` location | `env_file="../.env"` points above `backend/`; anyone creating `backend/.env` is silently ignored |
| **H-08** | Token budget | `apex_helpers.py:184` uses **4 chars/token**; `llm.py:1643` measured this workload at **2.9** and says 4 "understated the true size by ~40 %". Pruning batches run ~38 % over budget |
| **H-09** | APEX caps | `tables[:8]`, `probes[:8]`, `total_cols <= 12`, `max_workers=2` — the most expensive stage in the pipeline is entirely un-tunable without a rebuild |
| **H-10** | Timeouts | `alt_future.result(timeout=60)` where a single call takes 30–120 s on this hardware — the alternate candidate is paid for and then discarded |
| **H-11** | Truncations | Eight different magic truncation lengths for model-facing error text (`[:1500]`, `[:800]`, `[:200]`, `[:2000]`, `[:8000]`, …) |
| **H-12** | Row limit | `1000` appears three times (`save_sql_data`, `AGENTIC_FULL_RESULT_LIMIT`, `raise_sql_limit` default) with no link between them |
| **H-13** | Ranking constants | BM25 `k1=1.5 b=0.75`, RRF `k=60`, containment score `0.80 + 0.19 * ratio`, `round(…, 6)` duplicated in two files |
| **H-14** | Embedding dim | `ROW_RAG_EMBED_DIM=1024` is configurable but **never validated against the live model**; a mismatch fails every insert into a `vector(1024)` column, and the failure is swallowed |
| **H-15** | Language names | `'简体中文' / '繁体中文' / '英文' / '韩语'` — the prompt's `{lang}` slot asks an English model to answer in "英文". Four languages hardcoded in an if-chain |
| **H-16** | i18n/logic mismatch | Bulk user import compares cells against `'已启用'` / `'已禁用'` / `'本地创建'` while the template those cells come from is generated through `trans(...)` — an English admin's template fails every row |
| **H-17** | Timezone | `Asia/Shanghai` baked into the image; every `datetime.now()` — including the `{current_time}` prompt slot — is CST. **Temporal questions are answered in the wrong timezone for any other tenant** |
| **H-18** | Single-tenant | `oid = 1` fallback in five places; `row_embeddings` with no tenant column; user id 1 as superuser |
| **H-19** | Quoting | **Four** identifier-quoting implementations. Collapsing them to `apex_helpers.quote_ident` is a prerequisite for fixing **D-08** — the highest-value item here, being both a hardcoding fix and a security fix |

---

## 6. Accuracy levers (measured)

Full pricing in `04_accuracy_plan.md`. Baseline: `bird_final_32b.json`, **52.0 % EX**, 72 residual failures.

### Residual failure clusters

| Cluster | Count | % of failures | Fixable without a better model |
|---|---:|---:|---|
| C1 · Right shape, wrong values | **44** | 61 % | ~5–8 |
| C2 · Wrong row count (join/filter) | 13 | 18 % | ~2–4 |
| C3 · Wrong column count | 7 | 10 % | ~3–5 |
| C4 · Empty result | 3 | 4 % | 3 |
| C5 · SQL error / hallucinated column | 3 | 4 % | 3 |
| C6 · Float precision only | 1 | 1 % | already handled by EX-tolerant |
| C7 · Column order only | 1 | 1 % | 1 |

**61 % of the residue is semantic, not syntactic.** All syntactic classes
combined are 5 questions (3.3 % of the bank).

### Levers

| ID | Lever | Measured gain | Extra LLM calls | Verdict |
|---|---|---|---|---|
| **L-A** | **Deterministic `DESC` → `DESC NULLS LAST` rewrite** | **+5 questions = +3.3 pp (52.0 → 55.3 %)**, **0 regressions** (9 correct answers touched, 9 survived) | **0** | ✅ **Accept — best lever in the audit.** Note: `PROMPT_V2` already contains a `NULLS LAST` rule and it was ignored 17 times in this run, so a deterministic rewrite also frees prompt budget |
| **L-B** | Use production's `sql_validate.py` in the benchmark path instead of `bird_eval.lint_sql` | +1 to +3 (bounded by C5) | 0 | ✅ Accept — also removes divergence **E-03** |
| **L-C** | Port `_owners_hint` (names the table that owns a missing column) from the harness into production feedback | unmeasured; harness evidence is it rescued a ~60 %-useless repair loop | 0 | ✅ Accept |
| **L-D** | Benchmark the pipeline with `--finish data` | 0 pp — buys the ability to decide honestly | 0 | ✅ **Prerequisite for every other agentic decision** |
| **L-I** | Column-count post-check with retry | +1.3 to +2.7 pp | 1 on ~5 % | ⚠️ Med-high risk — the prompt version of this idea already backfired |
| **L-J** | Better model | +5 to +10 pp | — | ⚠️ Only lever that touches C1's 44 |
| **L-E** | Blanket `SELECT DISTINCT` | **0 gained**, and **25 of 60 queries error outright** | 0 | ❌ **Rejected with a number** |
| **L-F** | Self-consistency ON | **0.0 pp, McNemar p = 1.00**; median latency 19 s → 47 s | **+2 every question** | ❌ Rejected (already measured by the project) |
| **L-G** | APEX ON | unproven (40-question unrepresentative slice); median 121.9 s vs 37.5 s | **+5–6 every question** | ❌ Rejected on current evidence |
| **L-H** | 20k context window | **−16.7 pp measured** (35.3 % vs 52.0 %), 15 NO-SQL | 0 | ❌ Rejected — the 32B spills to CPU |

**Levers A–D together: +4 to +5.3 pp for zero extra model calls → ~56–57 % EX.**
**Hard floor for this model: ~26 % of the bank (C1's semantic residue) is unreachable without a better model.**

### The measurement gap that outranks every lever

Every number above is NL→SQL over 11 clean relational databases. Excel
ingestion, header/island detection, cross-file fanout, row permissions, charting
and multi-turn have **no accuracy measurement of any kind**. Two defects found in
this audit would each have been caught immediately by a product-surface
benchmark and are invisible on BIRD: **D-12** (fanout dead for all non-id-1
users) and **D-02** (samples bypass row permissions).

---

## 7. Test-suite baseline

Run 3× in the live container, byte-identical source.

```
tests/            24 failed, 272 passed, 3 skipped   7.36s / 7.38s / 7.43s
backend/tests/    28 passed                          9.66s / 7.39s / 7.32s
```

**Zero variance. No flaky or order-dependent tests.** All 24 failures are **D-19**
(one file, path fragility). The 3 skips are correct (`MINIMAX_API_KEY not set`).

---

## 8. Open questions (not bugs — intent could not be determined)

Full text in `05_open_questions.md`.

| ID | Question |
|---|---|
| **Q-01** | Is `is_normal_user()` meant to mean "id 1" or "admin"? Three notions of privileged coexist |
| **Q-02** | Are row permissions meant to apply to prompt context, or only to results? Column permissions are applied there and row ones are not |
| **Q-03** | Is `--finish sql` the intended default for pipeline benchmarking, given its own docstring says it measures less than the UI |
| **Q-04** | Was `SERVER_IMAGE_HOST` meant to be configured, or is MCP imaging unused? |
| **Q-05** | Is `ops/ollama-override.conf` supposed to be applied? It contradicts the documented live host setting |
| **Q-06** | Should `_maybe_raise_limit` classify the original question or each decomposed leg? |
| **Q-07** | Should `extract_nested_json` return the first or the last JSON object? The harness uses the opposite convention |
| **Q-08** | Is the `sys_arg` DB override meant to outrank environment variables? It does, for 3 of 118 settings |
| **Q-09** | Why does the `max_tokens` output cap apply only to the `openai` provider and not vLLM/Azure? |
| **Q-10** | Are the 20+ workbooks in `data/sqlbot/excel/` fixtures or live customer data? |
| **Q-11** | Is `privileged: true` required? |
| **Q-12** | Is unpinned dependency resolution intentional? |
| **Q-13** | Is there a backup procedure for the root-owned `data/postgresql/`? |
| **Q-14** | Which of the two test trees is canonical, and from which working directory? |
| **Q-15** | Are the two byte-identical BIRD result files two runs or one? |
| **Q-16** | Is `docs/BENCHMARK-BIRD.md`'s "no completed pipeline run" statement stale, or were those runs considered invalid? |
| **Q-17** | What does `sqlbot_xpack` actually register and monitor? Closed source, inside the auth perimeter, imported at `main.py:4` |
| **Q-18** | Is the embedded-token two-step decode (first pass with `verify_signature: False`) deliberate? |

---

## 9. Suggested fix order (for your approval — nothing will be touched until you name IDs)

**Tier 1 — security, small diffs, testable**
`D-03` · `D-04` (add the ownership predicate — 2 lines each) →
`D-07` (fix the wrong `HTTPException` import + reorder the guards) →
`D-06` (confine the resolved path) →
`D-01` (filter the vector search by datasource)

**Tier 2 — correctness, measurable**
`L-A` (+3.3 pp, 0 regressions, already measured) →
`D-09` (last-JSON-object; ships with the failing test) →
`D-19` (unbreak 24 tests so the suite is a usable gate) →
`D-12` (restore fanout for non-admin users)

**Tier 3 — measurement trust**
`E-06` (provenance in results files) → `E-01` (sort before F1) →
`L-D` (re-run the pipeline with `--finish data`) → `E-03`/`L-B`/`L-C`

**Tier 4 — hardcoding**, never bundled with a correctness fix:
`H-19` first (it unblocks `D-08`), then `H-01`–`H-06`, then the rest.

Per the ground rules: one logical fix per commit, failing test written first in
the same commit, full suite **and** the eval harness run before and after, and a
stop-and-report if any number regresses.
