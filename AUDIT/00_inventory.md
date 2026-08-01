# AUDIT / 00 — Full Inventory

**Repo:** `/home/iguser/Downloads/SQLBOT-Excel-Rag-main`
**Branch:** `main` @ `d2d83bb` ("SQLBot Excel RAG: agentic text-to-SQL pipeline on top of dataease/SQLBot")
**Inventory date:** 2026-08-01
**Working tree:** 17 modified tracked files, 14 untracked files (uncommitted work in the agentic/BIRD area)

> **Phase 0 scope.** This document records *what exists*. It contains no defect
> claims, no severity ratings and no proposed fixes. Where I noticed something
> that may or may not be intentional, it is listed under §12 as a QUESTION.
> No code was modified.

---

## 1. Method and coverage

| | Count |
|---|---|
| Tracked files | 990 |
| Untracked (uncommitted) files | 14 |
| Python files | 304 (33,046 LOC tracked + ~1,100 untracked) |
| Vue SFCs | 176 |
| TypeScript / JS (own code) | 74 ts + 43 js |
| YAML (prompts + compose + CI) | 16 |
| JSON (locales, benchmarks, results) | 21 tracked + 7 untracked |
| Shell | 10 |
| Markdown | 8 |
| Binary/vendored assets (svg/png/woff/ttf/tinymce) | ~460 |

**Read in full (opened and read line by line):** every file that carries product
logic in the query path, ingestion path, auth path, config path and benchmark
path — 42 files, listed with ✅ in §6.

**Read structurally (AST-extracted docstring + top-level symbols + LOC + mtime,
plus targeted greps for every claim made in this document):** the remaining 262
Python files. This is how the one-line purposes in §6 were produced; nothing in
this document asserts behaviour that was not either read directly or verified
with a grep whose output is shown in §8/§9.

**Not read — see §11 for the full list and reasons.**

---

## 2. Repository map

```
.
├── backend/                 FastAPI app — the whole server (main.py, apps/, common/, alembic/, templates/, tests/)
├── frontend/                Vue 3 SPA (Vite, Pinia, Element Plus); public/tinymce is vendored
├── g2-ssr/                  Node service that renders chart PNGs for MCP/API consumers
├── docs/                    ARCHITECTURE.md, BENCHMARK-BIRD.md, README.en.md, diagrams (svg/png/html)
├── ops/                     ollama-override.conf (host systemd drop-in, NOT applied by any code here)
├── important/               EMPTY DIRECTORY
├── tests/                   Second test tree (root-level) — unit + e2e; see §10
├── data/                    Runtime volumes: postgresql/ (root-owned, unreadable), sqlbot/{excel,file,images,logs}
├── docker-compose.yaml      Single-container deployment (app + Postgres + g2-ssr all in one)
├── Dockerfile               Upstream 4-stage build (Aliyun base images)
├── Dockerfile-base          Base image build (Postgres 17.6 + Python 3.11 + Oracle/DM clients)
├── Dockerfile.overlay       ← the one docker-compose actually uses: overlays local backend onto dataease/sqlbot:latest
├── start.sh                 Container PID 1: postgres → pm2 g2-ssr → uvicorn :8001 (mcp) → uvicorn :8000 (app)
├── important.md             Operator notes on which accuracy features are on/off and why
├── README.md                26 KB fork README (quick start, config reference, benchmarks, security notes)
├── .pre-commit-config.yaml  ruff + ruff-format + basic hygiene hooks
├── .typos.toml, .gitattributes, .dockerignore, .gitignore
└── LICENSE
```

Two **parallel test trees** exist (`backend/tests/` and `/tests/`) with no shared
conftest and different execution assumptions. See §10.

---

## 3. Entry points, services, background jobs

### 3.1 Processes started by `start.sh` (container PID 1)

| Order | Process | Port | Notes |
|---|---|---|---|
| 1 | `docker-entrypoint.sh postgres` | 5432 | Bundled Postgres 17.6 — app metadata **and** all Excel-imported data live here |
| 2 | `wait-for-it 127.0.0.1:5432 --timeout=120 --strict` | — | Blocking gate |
| 3 | `pm2 start g2-ssr/app.js` | 3000 | Chart PNG renderer; `ecosystem.config.js` sets autorestart + 5 s delay |
| 4 | `uvicorn main:mcp_app` | 8001 | MCP server + `/images` static mount |
| 5 | `uvicorn main:app --workers 1 --proxy-headers` | 8000 | **Foreground**, single worker |

`docker-compose.yaml` publishes 8000 and 8001 only (3000 and 5432 stay internal),
`privileged: true`, `restart: always`, `extra_hosts: host.docker.internal`.

### 3.2 HTTP entry points

`backend/main.py` builds the app:

- Routers mounted at `settings.API_V1_STR` = `CONTEXT_PATH + "/api/v1"` (CONTEXT_PATH is `""`).
- `backend/apps/api.py` aggregates 16 routers: `login, user, workspace, assistant, aimodel, base(settings), terminology, data_training, datasource, chat, dashboard_api, mcp, table_relation, parameter, apikey, recommended_problem, variable_api`. One router is commented out (`audit_api`).
- Custom `/openapi.json` and `/docs` handlers with per-language OpenAPI caching (`_openapi_cache`, a process-global dict).
- `FastApiMCP` exposes exactly 6 operations: `mcp_datasource_list, get_model_list, mcp_question, mcp_start, mcp_assistant, mcp_ws_list`. Note `get_model_list` is in the include list but its route is **commented out** in `apps/mcp/mcp.py:126-128`.
- `sqlbot_xpack.init_fastapi_app(app)` — the proprietary extension registers its own routes last.

**Middleware stack (registration order; Starlette executes last-registered first):**
`RequestContextMiddlewareCommon` → `RequestContextMiddleware` → `ResponseMiddleware` → `TokenMiddleware` → `CORSMiddleware` (added first, so outermost).

**Primary user request path:**
`POST /api/v1/chat/start` → `POST /api/v1/chat/question` (SSE stream) →
`apps/chat/api/chat.py::stream_sql` → `LLMService.create()` →
`run_task_async()` (submitted to a module-level `ThreadPoolExecutor(max_workers=200)`) →
`LLMService.run_task()` generator → chunks pushed to `self.chunk_list` →
`await_result()` drains the list into the `StreamingResponse`.

### 3.3 Startup work (`lifespan` in `main.py`)

Runs synchronously on boot, in this order:
1. `run_migrations()` — `alembic upgrade head` (in-process, against `alembic.ini`)
2. `init_sqlbot_cache()` — fastapi-cache2 backend (memory or Redis per `CACHE_TYPE`)
3. `init_dynamic_cors(app)` — appends assistant domains to CORS at runtime
4. `fill_empty_terminology_embeddings()`
5. `fill_empty_data_training_embeddings()`
6. `fill_empty_table_and_ds_embeddings()`
7. `sqlbot_xpack.core.clean_xpack_cache()`
8. `async_model_info()` — encrypts existing model keys/endpoints in place
9. `sqlbot_xpack.core.monitor_app(app)`

### 3.4 Background / async work

| Mechanism | Where | What |
|---|---|---|
| `ThreadPoolExecutor(max_workers=200)` module-global | `apps/chat/task/llm.py:75` | Every chat task, APEX sub-calls, alt-candidates, self-consistency candidates |
| Nested `ThreadPoolExecutor(max_workers=2)` | `llm.py` `generate_logical_plan`, `prune_schema_dual_pathway` | N=2 plan sampling; negative/positive prune passes |
| Nested `ThreadPoolExecutor(max_workers=min(4,…))` | `llm.py` `profile_data_parallel` | Per-table probe agents |
| `common/utils/embedding_threads.py` | fire-and-forget threads | Terminology / data-training / table / datasource embedding refresh after every CRUD write |
| `asyncio.to_thread` | most API handlers | Blocking DB/pandas work off the event loop |
| `atexit` hook | `apps/system/crud/user_excel.py:334` | Deletes generated error-report temp files |
| `scoped_session(sessionmaker(...))` | `llm.py:80` | Thread-local session, `session_maker.remove()` in `finally` |

**No cron, no scheduler, no message queue, no Celery.** All async work is
thread-pool or fire-and-forget.

### 3.5 CLI / operator scripts

| Script | Purpose |
|---|---|
| `backend/scripts/format.sh` | `ruff check --fix` + `ruff format` on `apps scripts common` |
| `backend/scripts/lint.sh` | `mypy app` + `ruff check app` — **paths (`app`) do not exist in this tree** |
| `backend/scripts/test.sh` | `coverage run --source=app -m pytest` — same stale path |
| `backend/scripts/tests-start.sh` | calls `python app/tests_pre_start.py` — **file does not exist** |
| `backend/scripts/prestart.sh` | calls `app/backend_pre_start.py`, `app/initial_data.py` — **neither exists** |
| `backend/scripts/alembic/auto.sh` | `alembic revision --autogenerate -m "<msg>"` |
| `backend/scripts/alembic/exec.sh` | `alembic upgrade head` |
| `backend/tests/bird_run_all.sh` | Sequences 6 BIRD experiments (~16 h) via `docker exec` |
| `backend/tests/bird_run_specialists.sh` | Same 150 questions against Arctic-7B / XiYanSQL-14B |
| `backend/tests/bird_run_supervised.sh` | Host-side supervisor: restarts a pipeline BIRD run after container restarts, using `--resume` |

---

## 4. Configuration sources and precedence

There are **six** distinct configuration surfaces. They do not share a schema and
are not validated together.

### 4.1 Typed settings — `backend/common/core/config.py` (`pydantic-settings`)

Precedence, as pydantic-settings resolves it:

```
process environment  >  ../.env file (i.e. repo-root .env, NOT backend/.env)  >  class default
```

`env_ignore_empty=True` (an empty env var falls through to the default),
`extra="ignore"` (unknown env vars are silently dropped — a typo'd variable name
produces no error). A `field_validator(..., mode='before')` coerces the strings
`"true"`/`"false"` for **23 named boolean fields**; booleans not in that list are
left to pydantic's own coercion.

118 settings across these groups: project/auth, Postgres, cache, logging, paths,
embedding (local + external OpenAI-compatible), table/DS embedding, the agentic
pipeline (retry, grader, multi-candidate, DS fallback, decompose, hybrid ranking,
identifier check, self-consistency), value linking, skeleton few-shot, APEX,
table sampling budgets, statement timeout, LLM output cap, fanout, Excel island
detection, content-aware routing, FTS, parallel-column hints, header detection
weights, header LLM fallback, DS summary, row-RAG, schema relations, Oracle
client path.

**No `.env` file exists in the repo** (`.gitignore` excludes it). All non-default
configuration in this deployment comes from `docker-compose.yaml`'s
`environment:` block — 24 variables, including `POSTGRES_PASSWORD`,
`SECRET_KEY`, and the feature flags documented in `important.md`.

### 4.2 Runtime settings stored in Postgres (override `settings` per request)

Read in `LLMService.create()` via `get_groups(session, "chat")` → `sys_arg` table:

| Key | Overrides |
|---|---|
| `chat.sqlbot_name` | assistant display name in prompts |
| `chat.limit_rows` | `settings.GENERATE_SQL_QUERY_LIMIT_ENABLED` (per-instance) |
| `chat.context_record_count` | `settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT` |

So for these three, effective precedence is: **DB `sys_arg` > env > .env > default.**

### 4.3 The chat/SQL model — `ai_model` table, not env

`get_default_config()` (`apps/ai_model/model_factory.py:164`) reads
`AiModelDetail` where `default_model == True`. Endpoint and key are decrypted
on read. Model *type* maps `protocol == 1 → "openai"`, else `"vllm"`.
`important.md` documents that the active model is the `QWEN 32B` row.
Selected per question; no restart needed.

### 4.4 Module-level constants mirrored from settings

`apps/datasource/utils/header_detection.py:58-115` keeps a `_DEFAULTS` dict of 11
heuristic tunables and, at import time, tries to overwrite the module globals
from `settings`; on any exception it silently falls back to the literals. Both
copies must be kept in step by hand (the module says so in a comment).

### 4.5 Frontend build-time config

`frontend/.env.development` → `VITE_API_BASE_URL=http://localhost:8000/api/v1`
`frontend/.env.production` → `VITE_API_BASE_URL=./api/v1`
Baked in at `npm run build`; not runtime-configurable.

### 4.6 Host-level config not applied by this repo

`ops/ollama-override.conf` is a systemd drop-in for the **host's** Ollama service
(`OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_FLASH_ATTENTION=1`).
Nothing in the build or compose applies it. `important.md` and
`docs/BENCHMARK-BIRD.md` state the live host runs `OLLAMA_KEEP_ALIVE=0`, i.e.
the opposite of this file.

---

## 5. External dependencies

### 5.1 Runtime services

| Dependency | How reached | Configured by |
|---|---|---|
| PostgreSQL 17.6 (bundled, in-container) | `localhost:5432` | `POSTGRES_*` env |
| pgvector extension | `CREATE EXTENSION IF NOT EXISTS vector` in `row_rag/store.py` | implicit |
| Ollama (host) — chat/SQL model | `ai_model.api_domain` in DB | UI / DB row |
| Ollama (host) — embeddings | `http://host.docker.internal:11434/v1`, model `mxbai-embed-large` | `EMBEDDING_API_BASE/MODEL/KEY` |
| Bundled local embedding model | `/opt/sqlbot/models/embedding/shibing624_text2vec-base-chinese` | fallback when the two `EMBEDDING_API_*` are empty |
| g2-ssr chart renderer | `settings.MCP_IMAGE_HOST` = `http://localhost:3000` | env |
| Image URL base for MCP replies | `settings.SERVER_IMAGE_HOST` = `http://YOUR_SERVE_IP:MCP_PORT/images/` (placeholder, unset in compose… actually **set to the placeholder verbatim** in `docker-compose.yaml:28`) | env |
| `sqlbot-xpack` (proprietary, testpypi) | imported at `main.py:4` — hard dependency | pyproject index pin |
| License validator binary | curl'd from `fit2cloud-support.oss-cn-beijing.aliyuncs.com` in `Dockerfile-base` | build time |
| Oracle Instant Client | downloaded in `Dockerfile-base`, path `settings.ORACLE_CLIENT_PATH` | build time |
| DM (达梦) client libs | downloaded in `Dockerfile-base` | build time |

### 5.2 Datasource connectors supported (`apps/db/constant.py`)

14 types: `excel, redshift, ck, dm, doris, es, kingbase, sqlServer, mysql, oracle, pg, starrocks, sqlite, hive`.
Split by `ConnectType`: `sqlalchemy` (excel, ck, sqlServer, mysql, oracle, pg, sqlite) vs `py_driver` (redshift, dm, doris, es, kingbase, starrocks, hive).
Each carries `prefix`/`suffix` identifier quoting and a `template_name` that maps to a prompt YAML.

### 5.3 Python dependencies (`backend/pyproject.toml`, Python `==3.11.*`)

53 runtime deps. Notable: `fastapi[standard]`, `sqlmodel`, `alembic`, `psycopg[binary]` **and** `psycopg2-binary` (both), `langchain 0.3.x` (+ core/openai/community/huggingface), `langgraph 0.3.x`, `sentence-transformers`, `pgvector`, `llama_index>=0.12.35`, `sqlglot>=28.6.0`, `sqlparse`, `pandas 2.x`, `python-calamine`, `openpyxl`, `xlrd`, `xlsxwriter`, `pymysql`, `pymssql`, `oracledb`, `dmpython` (non-Darwin), `redshift-connector`, `elasticsearch[requests] 7.x`, `clickhouse-sqlalchemy`, `pyhive[hive_pure_sasl]`, `thrift-sasl`, `fastapi-mcp`, `fastapi-cache2`, `redis`, `ldap3`, `dashscope`, `sentry-sdk[fastapi]`, `numpy==2.3.5`, `sqlbot-xpack`.

Torch is an extra (`cpu` / `cu128`, declared conflicting); the Dockerfile installs `--extra cpu`.
Package indexes: default `mirrors.aliyun.com`, plus `pytorch-cpu`, `pytorch-cu128`, `testpypi` (for `sqlbot-xpack`).

**`backend/uv.lock` does not exist** (`.gitignore` has `*.lock`). `Dockerfile:34`
tests for it and skips the intermediate layer when absent, so the image resolves
dependencies fresh at build time. **There is no pinned dependency lock in this repo.**

Dev deps: `pytest>=7.4.3,<8`, `mypy`, `ruff`, `pre-commit`, `coverage`, `types-passlib`.
`[tool.mypy] strict = true`, `[tool.ruff] target-version = "py310"` (project requires 3.11).

### 5.4 Frontend dependencies (`frontend/package.json`)

Vue 3.5, Vite 6.3, Element Plus 2.10 (+ `element-plus-secondary`), `@antv/g2` 5.3,
`@antv/s2` 2.4, `@antv/x6` 3.1 (table-relationship graph), TinyMCE 7.9 (also
vendored under `public/`), `vue-i18n` 9.14, `vue-router` 4.5, `pinia` 3.0 (listed
under devDependencies), `axios` (devDependencies), `markdown-it`,
`vue-dompurify-html`, `crypto-js`, `json-bigint`, `html2canvas`, `web-storage-cache`.
No lockfile committed (`*-lock.json` is gitignored).

`g2-ssr/package.json`: `@antv/g2-ssr`, `pm2`, `node-canvas`, `lodash`. Name field
is still `"vite-project"`. No lockfile.

---

## 6. Source inventory

Legend: **✅** = read in full during this phase. All LOC are `wc -l`.

### 6.1 Backend — entry & core (`backend/`)

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `main.py` | 220 | 2026-07-06 | FastAPI app construction, middleware chain, lifespan startup, i18n OpenAPI, MCP mount, xpack init |
| ✅ `apps/api.py` | 36 | 2026-07-06 | Aggregate `APIRouter` for all 16 module routers |
| `alembic.ini` | — | 2026-07-06 | Alembic config (used by in-process `run_migrations`) |
| `alembic/env.py` | 97 | 2026-07-06 | Alembic environment; builds URL from settings |
| `alembic/script.py.mako` | — | 2026-07-06 | Migration template |
| `alembic/versions/001…066` | 29–131 ea. | 2026-07-06 | 66 schema migrations. Chronological subject list: `001 ddl`, `002 ddl_autogenerate`, `003 add_datasource`, `004 add_aimodel_auto`, `005 table_and_field`, `006 add_ds_field`, `007 add_chat`, `008 modify_field_type`, `009_0 modify_chat`, `009_1 add_core_dashboard`, `010 upgrade_user_language`, `011 update_dashboard`, `012 license_ddl`, `013/015/016/018/029 modify_chat`, `014/023/024/031 modify_chat_record`, `017 rsa_ddl`, `019 upgrade_model`, `020 workspace_ddl`, `021 user_ws_ddl`, `022 assistant_ddl`, `025 ds_num`, `026 row_column_permission`, `027 modify_permission`, `028 ds_oid`, `030 permission_oid`, `032/036 modify_assistant`, `033 chat_origin_ddl`, `034 field_sort`, `035 sys_arg_ddl`, `037 create_chat_log`, `038 remove_chat_record_cloumns`, `039 create_terminology`, `040 modify_ai_model`, `041 add_terminology_oid`, `042 data_training`, `043 modify_ds_id_type`, `044 table_relation`, `045 modify_terminolog`, `046 add_custom_prompt`, `047 table_embedding`, `048 authentication_ddl`, `049 user_platform_ddl`, `050 modify_ddl_py`, `051 modify_data_training_ddl`, `052 add_recommended_problem`, `053/058 update_chat`, `054 update_chat_record_dll`, `055 add_system_logs`, `056 api_key_ddl`, `057 update_sys_log`, `059 chat_log_resource`, `060 platform_token_ddl`, `061 assistant_oid_ddl`, `062/063 update_chat_log_dll`, `064 system_variable`, `065 user_bind_var`, `066 update_assistant_model` |

### 6.2 Backend — the query pipeline (`apps/chat/`)

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `task/llm.py` | 3,266 | **2026-07-31** | `LLMService` — the orchestrator. Datasource selection, RAG context assembly, APEX refinement (plan/prune/profile), SQL generation + agentic retry loop, identifier check, row-permission rewrite, execution, self-consistency vote, fanout/split decomposition, row-RAG fallback, chart generation, analysis/predict, SSE emission. Also `process_stream` (reasoning-tag parser), `request_picture`, `get_last_conversation_rounds`. |
| ✅ `task/agentic.py` | 477 | **2026-07-31** | Pure helpers: `tokenize` (EN + CJK bigrams), `bm25_scores`, `rrf_fuse`, `parse_grader_verdict`, `parse_decomposition`, `merge_union`, `result_fingerprint`, `select_by_consensus`, `build_result_preview`, `format_retry_feedback`, refusal tag/retry helpers, `raise_sql_limit`, `is_listing_question`, `has_explicit_row_count` |
| ✅ `task/apex_helpers.py` | 508 | 2026-07-06 | M-Schema string parse/rebuild, pruning application, token-budget batching, `safe_json_loads`, dialect `quote_ident`/`quoted_table_ref`, probe-result compression, `fts_prompt_addendum`, `detect_parallel_column_groups`/`parallel_columns_addendum` |
| ✅ `task/sql_validate.py` | 396 | **2026-07-31** | Deterministic identifier validation: dialect map, case-sensitivity map, `build_schema_index`, sqlglot `extract_identifiers` (with alias resolution + locals), `diff_identifiers`, `format_identifier_feedback`. Fails open at every step. |
| ✅ `task/value_linking.py` | 352 | 2026-07-28 | Pure value linking: candidate-term extraction (quoted/proper/code/CJK/fallback), `score_term_value`, `match_values`, `format_value_hints`, text-column selection |
| ✅ `api/chat.py` | 578 | 2026-07-06 | Chat HTTP+SSE endpoints: list/get/rename/delete, `/start`, `/question`, quick commands (`/regenerate`, `/analysis`, `/predict`), record data/log/usage, Excel export |
| `curd/chat.py` | 1,177 | 2026-07-06 | All chat persistence: create/save question, sql, sql answer, exec data, chart, analysis, predict, logs; chart-field formatting; JSON data formatting; log history assembly |
| ✅ `models/chat_model.py` | 506 | 2026-07-06 | SQLModel tables (`chat`, `chat_record`, `chat_log`), enums (`OperationEnum` 17 values, `ChatFinishStep`, `QuickCommand`), the `AiModelQuestion` prompt-rendering surface (24 `*_question()` methods), tagged prompt-message classes |

### 6.3 Backend — datasource, ingestion, retrieval (`apps/datasource/`)

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `api/datasource.py` | 854 | 2026-07-06 | 25 endpoints: CRUD, connection check, table/field listing + sync, preview, schema comment import/export, `/uploadExcel` (deprecated), `/parseExcel`, `/reparseExcel`, `/importToDb`, `/addExcelDatasource`. Contains both Postgres loaders and the FTS index builder. |
| ✅ `crud/datasource.py` | 849 | **2026-07-31** | DS lifecycle, table/field sync, `preview()` per-dialect SQL, `get_table_schema` (M-Schema assembly + embedding cut + FK block + manual relations), `get_table_sample_data` / `get_tables_sample_data` (budgeted sampling + value profile), workspace DS cache |
| ✅ `relations.py` *(untracked)* | 603 | 2026-07-30 | Join-graph discovery: per-dialect constraint SQL (pg_catalog / information_schema / KCU / SQL Server), composite-FK pairing + cross-join drop, value-overlap inference for spreadsheets, TTL cache with `clear_cache(ds_id)` |
| ✅ `value_index.py` | 273 | 2026-07-28 | Impure half of value linking: dialect-correct `SELECT DISTINCT` builder, TTL cache (2000-entry bound), `collect_column_values`, `build_value_hints` end-to-end |
| ✅ `embedding/table_embedding.py` | 116 | 2026-07-28 | Question→table cosine ranking from stored embeddings, `TABLE_EMBEDDING_COUNT` cut, optional cosine floor, degraded fallback path |
| ✅ `embedding/ds_embedding.py` | 143 | 2026-07-06 | Question→datasource ranking; BM25+cosine RRF hybrid rerank; returns candidates with `cosine` used by fanout |
| `embedding/utils.py` | 19 | 2026-07-06 | Pure-Python `cosine_similarity` |
| ✅ `utils/header_detection.py` | 416 | 2026-07-28 | Header-row heuristic (7 weighted signals + position prior), opt-in LLM fallback, raw/full/sheet readers, JSON sidecar persistence |
| ✅ `utils/island_detection.py` | 148 | 2026-07-06 | Recursive row/column bisection into table islands; phantom-column trim; `region_columns` de-duplication |
| ✅ `utils/excel.py` | 245 | 2026-07-06 | Preview orchestration (`parse_excel_preview`, `reparse_sheet`), `detect_island_specs`, non-blank bbox / "trivial sheet" decision, dtype inference maps |
| `utils/header_llm.py` | 35 | 2026-07-06 | Builds the header prompt, calls `invoke_small_llm`, parses the index |
| `utils/utils.py` | 20 | 2026-07-06 | `aes_encrypt` / `aes_decrypt` wrappers for datasource `configuration` |
| `crud/table.py` | 320 | 2026-07-06 | Table CRUD, table+DS embedding computation, `build_ds_schema_text`, `build_ds_sample_text`, `get_ds_table_names` |
| `crud/field.py` | 28 | 2026-07-06 | Field CRUD |
| `crud/permission.py` | 87 | 2026-07-06 | `get_row_permission_filters`, `get_column_permission_fields`, `is_normal_user` |
| `crud/row_permission.py` | 258 | 2026-07-06 | Row-permission filter-tree → WHERE translation, system-variable resolution |
| `crud/recommended_problem.py` | 45 | 2026-07-06 | Recommended-question CRUD |
| `models/datasource.py` | 213 | 2026-07-06 | `CoreDatasource`, `CoreTable`, `CoreField`, `DatasourceConf`, request/response schemas incl. `SheetFields`/`FieldInfo` (island import) |
| `api/table_relation.py` | 37 | 2026-07-06 | Save/get the hand-drawn table relation graph |
| `api/recommended_problem.py` | 32 | 2026-07-06 | Recommended-problem endpoints |
| ✅ `row_rag/store.py` | 136 | 2026-07-06 | pgvector `row_embeddings` table: `ensure_schema`, `delete_table`, `insert_rows`, `query_topk` (sets `ivfflat.probes=1000`) |
| `row_rag/ingest.py` | 49 | 2026-07-06 | Embed every row of an imported DataFrame in batches and store |
| `row_rag/fallback.py` | 60 | 2026-07-06 | Post-failure semantic row retrieval → `{fields, data}` table |
| `row_rag/select.py` | 28 | 2026-07-06 | Pure confidence filtering + fallback-table assembly |
| `row_rag/text.py` | 34 | 2026-07-06 | Pure row→content-text serialisation, capped |
| `row_rag/backfill.py` | 22 | 2026-07-06 | Manual re-embed of already-imported tables |

### 6.4 Backend — model, DB and prompts

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `apps/ai_model/model_factory.py` | 233 | **2026-07-31** | `LLMConfig` (frozen, hashable), `LLMFactory` (`lru_cache(32)`), OpenAI/vLLM/Azure adapters, `_with_output_cap` (max_tokens), `get_default_config`, `invoke_default_llm`, `invoke_small_llm` |
| ✅ `apps/ai_model/embedding.py` | 75 | 2026-07-06 | `EmbeddingModelCache` — external OpenAI-compatible endpoint or bundled HuggingFace model, double-checked locking |
| `apps/ai_model/openai/llm.py` | 202 | 2026-07-06 | `BaseChatOpenAI` subclass + delta→chunk conversion (reasoning-content aware) |
| `apps/ai_model/llm.py` | 1 | 2026-07-06 | Contains only `# todo` |
| ✅ `apps/db/db.py` | 843 | **2026-07-31** | Per-dialect URI/engine/connection, statement-timeout listener, `get_tables`/`get_fields`/`get_schema`/`get_version`, `exec_sql` (14 branches), `convert_value`, `_dedup_columns`, `check_sql_read` (keyword + sqlglot AST) |
| `apps/db/db_sql.py` | 332 | 2026-07-06 | Per-dialect catalog SQL for version/tables/fields |
| ✅ `apps/db/engine.py` | 72 | 2026-07-06 | Bundled-Postgres engine/session for Excel data; `create_table`/`insert_data` (unused, see §8) |
| `apps/db/es_engine.py` | 133 | 2026-07-06 | Elasticsearch connect/index/fields/query-over-HTTP |
| ✅ `apps/db/constant.py` | 54 | 2026-07-06 | `DB` enum: 14 types × (type, display name, quote prefix/suffix, connect type, prompt template name, illegal JDBC params) |
| ✅ `apps/template/template.py` | 65 | 2026-07-06 | YAML template loader with `functools.cache`; base template + per-dialect example template |
| `apps/template/*/generator.py` | 7–15 ea. | 2026-07-06 | 7 thin accessors: sql, chart, analysis, predict, guess-question, dynamic, permissions, select-datasource |
| `templates/template.yaml` | 1,127 | 2026-07-06 | **The prompt corpus.** All system/user prompts: SQL gen + rules, retry, grader, decompose, synthesize, alt-candidate, APEX planning/prune/profile, chart, analysis, predict, guess-question, permissions filter, dynamic SQL, datasource selection, terminology/training/custom-prompt blocks |
| `templates/sql_examples/*.yaml` (13) | 81–207 | 3 modified 2026-07-31 | Per-dialect quoting rules, limit rules, other rules, worked examples. Modified: `PostgreSQL`, `Kingbase`, `AWS_Redshift` |
| ✅ `apps/data_training/skeleton.py` | 148 | 2026-07-18 | DAIL-SQL skeleton masking + RRF re-ranking of few-shot examples |
| `apps/data_training/curd/data_training.py` | 643 | 2026-07-18 | Few-shot example CRUD, embedding, retrieval, Excel import/export |
| `apps/data_training/api/data_training.py` | 272 | 2026-07-06 | Training-example endpoints |
| `apps/data_training/models/data_training_model.py` | 47 | 2026-07-06 | `DataTraining` model + DTOs |
| `apps/terminology/curd/terminology.py` | 859 | 2026-07-06 | Glossary CRUD, embedding, similarity retrieval, Excel import/export |
| `apps/terminology/api/terminology.py` | 269 | 2026-07-06 | Terminology endpoints |
| `apps/terminology/models/terminology_model.py` | 35 | 2026-07-06 | `Terminology` model |

### 6.5 Backend — system, auth, dashboard, MCP

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `apps/system/middleware/auth.py` | 233 | 2026-07-06 | `TokenMiddleware`: whitelist bypass, API-key (`X-SQLBOT-ASK-TOKEN`, `sk` scheme), assistant (`assistant` scheme), embedded (`embedded` scheme), bearer user token; `xor_decrypt` for app ids |
| ✅ `common/utils/whitelist.py` | 94 | 2026-07-06 | 30-entry unauthenticated path list, glob→regex compilation, prefix stripping |
| `apps/system/api/user.py` | 322 | 2026-07-06 | User CRUD, Excel template/import/error download, workspace options |
| `apps/system/api/assistant.py` | 281 | 2026-07-06 | Assistant app config, validator, picture, UI, DS list |
| `apps/system/api/workspace.py` | 262 | 2026-07-06 | Workspace CRUD + membership |
| `apps/system/api/aimodel.py` | 178 | 2026-07-06 | Model CRUD, connectivity check, set-default |
| `apps/system/api/apikey.py` | 65 | 2026-07-06 | API-key grid/create/status/delete |
| `apps/system/api/login.py` | 52 | 2026-07-06 | Local login/logout |
| `apps/system/api/parameter.py` | 33 | 2026-07-06 | System parameter get/save (`sys_arg`) |
| `apps/system/api/variable_api.py` | 35 | 2026-07-06 | System-variable CRUD |
| `apps/system/crud/assistant.py` | 284 | 2026-07-06 | Assistant info/user, out-datasource factory (`AssistantOutDs`), dynamic CORS |
| `apps/system/crud/user_excel.py` | 340 | 2026-07-06 | Bulk user import from Excel: template, row validation, error workbook, temp-file registry + `atexit` cleanup |
| `apps/system/crud/user.py` | 95 | 2026-07-06 | User lookup/auth/cache |
| `apps/system/crud/system_variable.py` | 100 | 2026-07-06 | System-variable CRUD |
| `apps/system/crud/workspace.py` | 70 | 2026-07-06 | Workspace/oid reset |
| `apps/system/crud/assistant_manage.py` | 51 | 2026-07-06 | Assistant save + CORS upgrade |
| `apps/system/crud/parameter_manage.py` | 45 | 2026-07-06 | `sys_arg` read/write, `get_groups` |
| `apps/system/crud/aimodel_manage.py` | 33 | 2026-07-06 | Startup key/endpoint encryption |
| `apps/system/crud/apikey_manage.py` | 17 | 2026-07-06 | API-key lookup + cache clear |
| `apps/system/schemas/permission.py` | 138 | 2026-07-06 | `SqlbotPermission`, `require_permissions` decorator, `RequestContextMiddleware` |
| `apps/system/schemas/system_schema.py` | 225 | 2026-07-06 | User/workspace/assistant DTOs incl. `AssistantOutDsSchema` |
| `apps/system/schemas/{auth,ai_model_schema,logout_schema}.py` | 9–30 | 2026-07-06 | Login schema, cache namespaces, model DTOs |
| `apps/system/models/{system_model,user,system_variable_model}.py` | 20–84 | 2026-07-06 | `AiModelDetail`, workspace/user-ws, assistant, API key, user, system variable |
| ✅ `apps/mcp/mcp.py` | 185 | 2026-07-06 | MCP endpoints: `mcp_start` (username/password → token + chat), `mcp_ws_list`, `mcp_datasource_list`, `mcp_question`, `mcp_assistant` |
| `apps/dashboard/api/dashboard_api.py` | 82 | 2026-07-06 | Dashboard resource + canvas endpoints |
| `apps/dashboard/crud/dashboard_service.py` | 158 | 2026-07-06 | Dashboard resource tree CRUD |
| `apps/dashboard/models/dashboard_model.py` | 165 | 2026-07-06 | `CoreDashboard` + DTOs |
| `apps/settings/api/base.py` | 41 | 2026-07-06 | Generic Excel download endpoint |
| `apps/settings/models/setting_models.py` | 12 | 2026-07-06 | `term_model` (table `terms`) |
| `apps/settings/schemas/setting_schemas.py` | 7 | 2026-07-06 | `term_schema_creator` |
| `apps/swagger/i18n.py` | 120 | 2026-07-06 | OpenAPI tag/description translation loader |
| `apps/swagger/locales/{en,zh}.json` | — | 2026-07-06 | Swagger i18n strings |
| `locales/{en,zh-CN,zh-TW,ko-KR}.json` | — | **2026-07-30** | Backend i18n message catalogues (all 4 modified) |

### 6.6 Backend — common

| File | LOC | Modified | Purpose |
|---|---:|---|---|
| ✅ `common/core/config.py` | 359 | **2026-07-31** | The typed settings object (§4.1) |
| `common/core/db.py` | 26 | 2026-07-06 | App-metadata SQLModel engine + `get_session` |
| `common/core/deps.py` | 37 | 2026-07-06 | FastAPI deps: `SessionDep`, `CurrentUser`, `CurrentAssistant`, `Trans` |
| `common/core/sqlbot_cache.py` | 160 | 2026-07-06 | `@cache`/`@clear_cache` decorators over fastapi-cache2, custom key builder |
| `common/core/response_middleware.py` | 118 | 2026-07-06 | Uniform response envelope + global exception handlers |
| `common/core/pagination.py` | 103 | 2026-07-06 | `Paginator` |
| `common/core/schemas.py` | 62 | 2026-07-06 | `TokenPayload`, `Token`, pagination DTOs, `XOAuth2PasswordBearer` |
| `common/core/security.py` | 44 | 2026-07-06 | JWT creation, bcrypt + md5 password helpers |
| `common/core/security_config.py` | 161 | 2026-07-06 | `SecurityConfig` + password-strength validator — **no importers** (§8) |
| `common/core/models.py` | 19 | 2026-07-06 | `SnowflakeBase` |
| `common/core/file.py` | 5 | 2026-07-06 | `FileRequest` DTO |
| `common/error.py` | 20 | 2026-07-06 | `SingleMessageError`, `SQLBotDBError`, `SQLBotDBConnectionError`, `ParseSQLResultError` |
| `common/utils/utils.py` | 297 | 2026-07-06 | `extract_nested_json`, logging setup, `SQLBotLogUtil`, `deepcopy_ignore_extra`, `equals_ignore_case`, `prepare_for_orjson`, token helpers |
| `common/utils/data_format.py` | 180 | 2026-07-06 | `DataFormat`: large-number conversion, qualified-column normalisation, pandas conversions |
| ✅ `common/utils/embedding_threads.py` | 49 | 2026-07-06 | Fire-and-forget embedding refresh threads (4 entry points) |
| `common/utils/command_utils.py` | 100 | 2026-07-06 | `/regenerate` `/analysis` `/predict` quick-command parser |
| `common/utils/locale.py` | 65 | 2026-07-06 | `I18n` / `I18nHelper` |
| `common/utils/snowflake.py` | 57 | 2026-07-06 | Snowflake id generator |
| `common/utils/crypto.py` | 7 | 2026-07-06 | `sqlbot_encrypt`/`decrypt` (async, xpack-backed) |
| `common/utils/aes_crypto.py` | 16 | 2026-07-06 | AES helpers |
| `common/utils/excel.py` | 12 | 2026-07-06 | `get_excel_column_count` |
| `common/utils/time.py` | 6 | 2026-07-06 | `get_timestamp` |
| `common/utils/tree_utils.py` | 22 | 2026-07-06 | Generic tree builder |
| `common/utils/random.py` | 7 | 2026-07-06 | `get_random_string` — **no importers** (§8) |
| `common/utils/http_utils.py` | 31 | 2026-07-06 | `verify_url` — **no importers** (§8) |
| `common/audit/schemas/logger_decorator.py` | 697 | 2026-07-06 | `@system_log` audit decorator, expression evaluation, resource-name resolution |
| `common/audit/schemas/log_utils.py` | 145 | 2026-07-06 | Local `build_resource_union_query` — **shadowed by the xpack import** (§8/§9) |
| `common/audit/schemas/request_context.py` | 37 | 2026-07-06 | Request-scoped context middleware |
| `common/audit/models/log_model.py` | 108 | 2026-07-06 | `SystemLog` model + operation/module enums |

### 6.7 Frontend (`frontend/src/`, 55,480 LOC across ts/vue)

Grouped by directory; every file is listed with LOC.

**Bootstrap / infra**
`main.ts` 17 · `App.vue` 16 · `vite-env.d.ts` 3 · `auto-imports.d.ts` 19 · `vite.config.ts` 57 · `index.html` 13 · `embedded.html` 13 · `eslint.config.cjs` · `.prettierrc` · `.editorconfig` · `style.less`

**Routing** — `router/index.ts` 295 (route table) · `router/dynamic.ts` 121 (permission-driven route injection) · `router/watch.ts` 98

**State (Pinia)** — `stores/index.ts` 10 · `stores/user.ts` 226 · `stores/appearance.ts` 325 · `stores/assistant.ts` 248 · `stores/chatConfig.ts` 72 · `stores/dashboard/dashboard.ts` 109 · `stores/dashboard/snapshot.ts` 145

**API clients (`src/api/`, 19 files, 818 LOC)** — `chat.ts` 501 (chat models + streaming fetch) · `datasource.ts` 38 · `system.ts` 32 · `user.ts` 31 · `setting.ts` 26 · `embedded.ts` 22 · `login.ts` 19 · `training.ts` 18 · `workspace.ts` 17 · `professional.ts` 16 · `prompt.ts` 15 · `auth.ts` 14 · `dashboard.ts` 14 · `audit.ts` 12 · `assistant.ts` 11 · `recommendedApi.ts` 11 · `variables.ts` 9 · `license.ts` 7 · `permissions.ts` 5

**Utils** — `request.ts` 482 (axios instance, auth headers, assistant/embedded headers, SSE `fetch`) · `utils.ts` 317 · `xss.ts` 90 · `useEmitt.ts` 89 · `canvas.ts` 53 · `useCache.ts` 50 · `date.ts` 32 · `propTypes.ts` 27 · `markdown.ts` 25 · `RemoteJs.ts` 22 · `url.ts` 3

**Entities / i18n** — `entity/supplier.ts` 412 (LLM provider catalogue) · `entity/CommonEntity.ts` 22 · `i18n/index.ts` 56 (+4 locale JSONs)

**Shared components** — `layout/index.vue` 584 · `layout/LayoutDsl.vue` 394 · `layout/Person.vue` 414 · `layout/Apikey.vue` 381 · `layout/Workspace.vue` 242 · `layout/MenuItem.vue` 159 · `layout/Menu.vue` 153 · `layout/PwdForm.vue` 135 · `layout/SinglePage.vue` 11 · `about/index.vue` 261 + `index.ts` 11 · `filter-text/src/FilterText.vue` 206 + `index.ts` 89 · `drawer-main/src/DrawerMain.vue` 180 + `index.ts` 3 · `drawer-filter/src/{DrawerTreeFilter 127, DrawerEnumFilter 115, DrawerTimeFilter 96, DrawerFilter 90}.vue` + `index.ts` 3 · `icon-custom/src/Icon.vue` 60 + `index.ts` 9 · `rich-text/TinymceEditor.vue` 142 · `Language-selector/index.vue` 77

**Chat view (`views/chat/`, 16 + 4 + 3 + 7 + 6 + 8 files)** — `index.vue` 1,527 (the chat shell + SSE consumer) · `ChatList.vue` 450 · `ChatListContainer.vue` 351 · `ChatCreator.vue` 341 · `RecommendQuestion.vue` 259 · `ExecutionDetails.vue` 239 · `RecommendQuestionQuick.vue` 236 · `QuickQuestion.vue` 227 · `RecentQuestion.vue` 119 · `ErrorInfo.vue` 104 · `typed.ts` 88 · `ChatRow.vue` 83 · `ChatTokenTime.vue` 70 · `preview.vue` 62 · `ChatToolBar.vue` 56 · `ChatRecordFirst.vue` 48 — answers: `ChartAnswer.vue` 292, `PredictAnswer.vue` 295, `AnalysisAnswer.vue` 222, `BaseAnswer.vue` 178 — blocks: `ChartBlock.vue` 858, `ChartPopover.vue` 170, `UserChat.vue` 85 — chart components: `charts/Table.ts` 317, `charts/Bar.ts` 171, `charts/utils.ts` 161, `charts/Column.ts` 162, `charts/Line.ts` 144, `charts/Pie.ts` 71, `ChartComponent.vue` 132, `DisplayChartBlock.vue` 134, `BaseChart.ts` 35, `BaseG2Chart.ts` 29, `index.ts` 32, `MdComponent.vue` 35, `SQLComponent.vue` 29 — execution log: `LogWithAi.vue` 138, `LogSQLSample.vue` 93, `LogTerm.vue` 92, `LogCustomPrompt.vue` 88, `LogGeneratePicture.vue` 81, `LogDataQuery.vue` 78, `LogChooseTable.vue` 73, `BaseContent.vue` 14

**Datasource view (`views/ds/`, 20 files)** — `DataTable.vue` 1,081 · `DatasourceForm.vue` 1,049 · `TableRelationship.vue` 535 (X6 graph) · `Datasource.vue` 529 · `form.vue` 485 · `ExcelDetailDialog.vue` 371 · `Card.vue` 339 · `TableList.vue` 335 · `RecommendedProblemConfigDialog.vue` 228 · `SheetTabs.vue` 216 · `ChatCard.vue` 178 · `index.vue` 163 · `DatasourceItemCard.vue` 143 · `AddDrawer.vue` 133 · `DatasourceList.vue` 109 · `DatasourceListSide.vue` 106 · `ParamsForm.vue` 69 · `DelMessageBox.vue` 55 · `js/ds-type.ts` 52 · `js/aes.ts` 18

**Dashboard view (`views/dashboard/`, 33 files)** — `canvas/CanvasCore.vue` 1,298 · `common/ResourceTree.vue` 670 · `components/sq-view/index.vue` 400 · `editor/ChatChartSelection.vue` 361 · `editor/Toolbar.vue` 360 · `components/sq-tab/index.vue` 346 · `common/ResourceGroupOpt.vue` 269 · `common/AddViewDashboard.vue` 302 · `editor/DashboardChatList.vue` 234 · `preview/SQPreviewShow.vue` 225 · `components/sq-tab/CustomTab.vue` 200 · `components/sq-text/index.vue` 197 · `preview/SQPreview.vue` 172 · `editor/DashboardEditor.vue` 162 · `canvas/ComponentBar.vue` 160 · `editor/index.vue` 148 · `preview/SQPreviewHead.vue` 147 · `utils/treeDraggableChart.ts` 140 · `utils/canvasUtils.ts` 130 · `common/HandleMore.vue` 107 · `canvas/CanvasShape.vue` 99 · `preview/SQComponentWrapper.vue` 79 · `components/component-list.ts` 77 · `components/button-label/ComponentButtonLabel.vue` 77 · `common/EmptyBackground.vue` 75 · `canvas/ResizeHandle.vue` 72 · `canvas/DragHandle.vue` 66 · `preview/SQPreviewSingle.vue` 66 · `components/sq-text-t7/index.vue` 63 · `common/SQFullscreen.vue` 63 · `common/DashboardDetailInfo.vue` 61 · `editor/ChartSelection.vue` 49 · `common/EmptyBackgroundSvgMain.vue` 43 · `common/EmptyBackgroundSvg.vue` 42 · `utils/treeSortUtils.ts` 42 · `SQTextDemo/index.vue` 21 · `preview/SQPreviewSingle2.vue` 22 · `utils/treeNode.ts` 18 · `index.vue` 7 · `preview/index.vue` 7 · `components/sq-empty/index.vue` 7

**System views (`views/system/`, 45 files)** — `user/User.vue` 1,615 · `embedded/iframe.vue` 1,592 · `permission/index.vue` 1,137 · `appearance/index.vue` 1,054 · `professional/index.vue` 1,016 · `prompt/index.vue` 1,001 · `workspace/index.vue` 988 · `training/index.vue` 868 · `variables/index.vue` 850 · `embedded/Page.vue` 830 · `audit/index.vue` 632 · `model/ModelForm.vue` 603 · `model/Model.vue` 597 · `member/index.vue` 554 · `embedded/SetUi.vue` 551 · `user/UserImport.vue` 476 · `authentication/Oauth2Editor.vue` 470 · `user/SyncUserDing.vue` 423 · `permission/auth-tree/RowAuth.vue` 364 · `authentication/OidcEditor.vue` 352 · `workspace/AuthorizedWorkspaceDialog.vue` 352 · `excel-upload/UploaderRemark.vue` 347 · `appearance/LoginPreview.vue` 328 · `permission/SelectPermission.vue` 324 · `platform/common/InfoTemplate.vue` 318 · `permission/auth-tree/FilterFiled.vue` 317 · `embedded/Card.vue` 314 · `authentication/LdapEditor.vue` 307 · `authentication/CasEditor.vue` 290 · `parameter/index.vue` 273 · `embedded/DsCard.vue` 264 · `authentication/SAML2Editor.vue` 260 · `authentication/index.vue` 257 · `appearance/Person.vue` 251 · `permission/auth-tree/AuthTree.vue` 251 · `platform/PlatformInfo.vue` 251 · `platform/PlatformForm.vue` 248 · `model/Card.vue` 210 · `parameter/xpack/PlatformParam.vue` 210 · `excel-upload/Uploader.vue` 374 · `platform/common/SettingTemplate.ts` 142 · `permission/Card.vue` 128 · `model/ParamsForm.vue` 124 · `workspace/ParamsForm.vue` 112 · `model/ModelList.vue` 109 · `model/ModelListSide.vue` 104 · `platform/index.vue` 74 · `permission/options.ts` 52 · `embedded/index.vue` 25 · `embedded/Test.vue` 9

**Login / embedded / misc** — `login/index.vue` 231 · `login/xpack/Handler.vue` 461 · `login/xpack/QrcodeLdap.vue` 192 · `login/xpack/PlatformClient.ts` 191 · `login/xpack/QrTab.vue` 133 · `login/xpack/WecomQr.vue` 119 · `login/xpack/LdapLoginForm.vue` 104 · `login/xpack/DingtalkQr.vue` 88 · `login/xpack/LarkQr.vue` 77 · `login/xpack/LarksuiteQr.vue` 76 · `login/xpack/{Cas,Oauth2,Oidc}.vue` 55 ea. · `login/xpack/platformUtils.ts` 5 · `embedded/index.vue` 306 · `embedded/page.vue` 240 · `embedded/AssistantPreview.vue` 210 · `embedded/common.vue` 111 · `error/index.vue` 70 · `WelcomeView.vue` 55 · `work/index.vue` 124 · `work/DatasourceCard.vue` 52 · `public/assistant.js` (embed loader) · `public/swagger-ui-bundle.js` (vendored)

### 6.8 g2-ssr (Node chart renderer)

| File | LOC | Purpose |
|---|---:|---|
| ✅ `app.js` | 79 | Bare `http.createServer` on port 3000; POST body → `createChart` → `exportToFile(obj.path)` |
| `charts/bar.js` | 140 | Bar chart option builder |
| `charts/column.js` | 140 | Column chart option builder |
| `charts/line.js` | 123 | Line chart option builder |
| `charts/utils.js` | 151 | Shared axis/series helpers |
| `charts/pie.js` | 56 | Pie chart option builder |
| ✅ `ecosystem.config.js` | 12 | pm2 app definition (autorestart, 5 s delay) |
| `Arial_Unicode.ttf` | — | Font copied into the image for CJK glyphs |

---

## 7. Dependency graph and cycles

Computed by AST-walking every `import` / `from … import` in all 304 Python files
and resolving targets against the module map (script in
`scratchpad/graph.py`).

### 7.1 Layering (module → module, condensed)

```
main.py
 ├─ apps.api ──┬─ apps.chat.api.chat ──── apps.chat.task.llm ──┬─ apps.chat.task.agentic
 │             │        │                        │             ├─ apps.chat.task.sql_validate ─→ apps.chat.task.apex_helpers
 │             │        │                        │             ├─ apps.chat.task.apex_helpers
 │             │        │                        │             ├─ apps.chat.curd.chat
 │             │        │                        │             ├─ apps.chat.models.chat_model ─→ apps.template.*.generator ─→ apps.template.template ─→ templates/*.yaml
 │             │        │                        │             ├─ apps.ai_model.model_factory ─→ apps.ai_model.openai.llm
 │             │        │                        │             ├─ apps.datasource.crud.datasource ─┬─ apps.datasource.relations ─→ apps.db.db
 │             │        │                        │             │                                    ├─ apps.datasource.embedding.table_embedding ─→ apps.ai_model.embedding
 │             │        │                        │             │                                    └─ apps.datasource.crud.permission
 │             │        │                        │             ├─ apps.datasource.value_index ─→ apps.chat.task.value_linking
 │             │        │                        │             ├─ apps.datasource.embedding.ds_embedding ─→ apps.chat.task.agentic (bm25/rrf)
 │             │        │                        │             ├─ apps.datasource.row_rag.fallback ─→ .store, .select, .text
 │             │        │                        │             ├─ apps.db.db ─→ apps.db.{db_sql,engine,es_engine,constant}
 │             │        │                        │             ├─ apps.terminology.curd.terminology
 │             │        │                        │             ├─ apps.data_training.curd.data_training ─→ apps.data_training.skeleton ─→ apps.chat.task.agentic
 │             │        │                        │             └─ sqlbot_xpack.* (custom prompts, license, sys args)
 │             │        └─ common.utils.command_utils
 │             ├─ apps.datasource.api.datasource ─→ ..utils.{excel,header_detection,island_detection}, ..row_rag.ingest
 │             ├─ apps.mcp.mcp ─→ apps.chat.api.chat
 │             ├─ apps.system.api.* ─→ apps.system.crud.*
 │             └─ apps.{dashboard,terminology,data_training,settings}.api.*
 ├─ apps.system.middleware.auth ─→ common.utils.whitelist, common.core.security
 ├─ common.core.{config,db,sqlbot_cache,response_middleware}
 └─ common.utils.embedding_threads ─→ apps.{terminology,data_training}.curd.*, apps.datasource.crud.table
```

### 7.2 Most-depended-on modules

| Module | Inbound importers |
|---|---:|
| `common.core.config` | 64 |
| `common.core.deps` | 37 |
| `common.utils.utils` | 32 |
| `apps.datasource.models.datasource` | 30 |
| `common.core.db` | 20 |
| `apps.system.schemas.system_schema` | 20 |
| `apps.system.models.system_model` | 17 |
| `apps.swagger.i18n` | 17 |
| `common.utils.crypto` | 16 |
| `apps.chat.models.chat_model` | 16 |
| `common.audit.models.log_model` | 15 |
| `common.audit.schemas.logger_decorator` | 14 |
| `apps.datasource.crud.table` | 13 |
| `apps.system.schemas.permission` | 11 |
| `apps.db.db` | 11 |

### 7.3 Import cycles (Tarjan SCC over the module graph)

Two multi-module cycles exist:

1. **`apps.data_training.curd.data_training` ↔ `apps.terminology.curd.terminology` ↔ `common.utils.embedding_threads`**
   `embedding_threads` imports the two CRUD modules to schedule their embedding refresh; both CRUD modules import `embedding_threads` to trigger it after a write.

2. **`apps.datasource.utils.header_detection` ↔ `apps.datasource.utils.header_llm`**
   `header_llm` imports `build_header_llm_prompt`/`parse_header_row_answer` from `header_detection`; `header_detection.resolve_header_row` imports `header_llm` — the second import is **function-local** (deferred), which is what keeps this from being an import-time failure.

Additionally, several modules use **function-local imports** to avoid further
cycles (documented as such in comments): `llm.py` → `apex_helpers`,
`crud/datasource.py` → `relations`, `relations.py` → `apps.db.db`,
`value_index.py` → `crud.datasource` + `apps.db.db`,
`llm.py` → `row_rag.fallback`, `api/datasource.py` → `row_rag.ingest`,
`model_factory.py` → `common.core.config`.

---

## 8. Dead code (zero inbound references)

Verified by grep across `backend/`, `tests/`, `frontend/` — commented-out
references are called out explicitly.

| Item | File:line | Evidence |
|---|---|---|
| `get_table_embedding()` | `apps/datasource/embedding/table_embedding.py:13` | Only other occurrence is a comment: `apps/system/crud/assistant.py:219` |
| `create_table()` | `apps/db/engine.py:39` | Only in commented-out code at `api/datasource.py:290,312` |
| `insert_data()` | `apps/db/engine.py:64` | Only in commented-out code at `api/datasource.py:298,321` |
| `get_data_engine()` | `apps/db/engine.py:32` | Only in commented-out code at `api/datasource.py:279` |
| `common/core/security_config.py` (whole module: `SecurityConfig`, `get_security_config`, `validate_password_strength`) | 161 LOC | No importers anywhere |
| `common/utils/http_utils.py::verify_url` | 31 LOC module | No importers |
| `common/utils/random.py::get_random_string` | 7 LOC module | No importers |
| `common/audit/schemas/log_utils.py::build_resource_union_query` | 145 LOC | `logger_decorator.py:11` imports the **xpack** implementation of the same name; the local one is never referenced |
| `apps/ai_model/llm.py` | 1 LOC | File contains only `# todo` |
| `apps/settings/models/setting_models.py::term_model` (table `terms`) | 12 LOC | No importers; **no alembic migration creates a `terms` table either** |
| `apps/settings/schemas/setting_schemas.py::term_schema_creator` | 7 LOC | No importers |
| `apps/datasource/row_rag/backfill.py` | 22 LOC | Documented as "run manually in-container"; no importer (intentional) |
| `important/` directory | — | Empty |
| Commented-out router | `apps/api.py:11,35` | `audit_api` import and registration both commented out |
| Commented-out MCP route | `apps/mcp/mcp.py:126-128` | `get_model_list` is commented out but still listed in `main.py`'s `include_operations` |
| Commented-out endpoints | `api/datasource.py:191-204` (`/execSql`), `265-326` (old `/uploadExcel`); `api/chat.py:57-72`, `119-132`, `151-165` (superseded non-user-scoped variants) | Large commented blocks retained |
| Stale script targets | `backend/scripts/{lint,test,tests-start,prestart}.sh` | Reference `app/`, `app/tests_pre_start.py`, `app/backend_pre_start.py`, `app/initial_data.py` — none exist in this tree |
| `from dis import specialized` | `apps/chat/task/llm.py:11` | Imported, never used |

---

## 9. Duplicate / near-duplicate implementations

| # | Duplication | Locations |
|---|---|---|
| D1 | **Two DataFrame→Postgres loaders.** `insert_pg` (used only by the deprecated `/uploadExcel`) and `_insert_df_to_pg` (used by the live `/importToDb` + `/addExcelDatasource`). The newer one adds the `output.seek(0)` rewind, `NULL ''`, and drop-on-failure; the older one has none of these. | `apps/datasource/api/datasource.py:367` and `:596` |
| D2 | **Two hand-rolled TTL caches** with the same `_cache_get`/`_cache_put` shape but different eviction (relations: none; value_index: expired-sweep then full clear at 2000 entries), and a **third** cache abstraction in `common/core/sqlbot_cache.py`. | `apps/datasource/relations.py:79-107`, `apps/datasource/value_index.py:34-68`, `common/core/sqlbot_cache.py` |
| D3 | **Three functions named `clear_cache`** with unrelated semantics and signatures. | `relations.py:99` (`ds_id`), `value_index.py:65` (no args), `sqlbot_cache.py:92` (decorator factory) |
| D4 | **Two functions named `get_sql_template`** with different arities. `generate_sql/generator.py` imports the other under an alias to disambiguate. | `apps/template/template.py:32` (takes `db_type`) vs `apps/template/generate_sql/generator.py:7` (no args) |
| D5 | **Four identifier-quoting implementations.** | `apex_helpers.quote_ident` (dialect sets), `relations._quote` (backtick-vs-double-quote), `DB.prefix/suffix` in `apps/db/constant.py`, and inline per-dialect f-strings in `crud/datasource.py::preview` and `::get_table_sample_data` |
| D6 | **Three dialect row-limit builders.** | `value_index.build_distinct_sql` (TOP / FETCH FIRST / LIMIT), `relations._bounded_select` (TOP / LIMIT), `crud/datasource.get_table_sample_data` (TOP / ROWNUM / LIMIT) |
| D7 | **Two JSON-from-LLM extractors.** | `common/utils/utils.extract_nested_json` (used by `llm.py`) and `apex_helpers.safe_json_loads` (used by agentic/APEX helpers) |
| D8 | **Benchmark harness re-implements four pipeline capabilities.** The BIRD harness has its own identifier checker, join-graph block, low-cardinality value sampler and repair loop, none of which call the product code. | `backend/tests/bird_eval.py::lint_sql` vs `apps/chat/task/sql_validate.py`; `bird_eval.py::join_graph` vs `apps/datasource/relations.py`; `bird_eval.py::value_samples` vs `apps/datasource/value_index.py`; `bird_eval.py::generate_with_repair` vs the agentic retry loop in `llm.py` — **carried to Phase 3** |
| D9 | **Two header-detection tunable sets** that must be kept in step by hand (module `_DEFAULTS` literals vs `Settings.HEADER_*`). | `apps/datasource/utils/header_detection.py:58-103` vs `common/core/config.py:270-280` |
| D10 | **Two BM25/RRF consumers** sharing one implementation (not a duplicate — noted so it is not mistaken for one). | `ds_embedding._hybrid_rerank` and `skeleton.rerank_by_skeleton` both import from `agentic.py` |
| D11 | **Two `LogXxx` prompt-log Vue components per operation** and two parallel `chat.py` endpoint variants (user-scoped and non-user-scoped, the latter commented out). | `frontend/src/views/chat/execution-component/*`, `apps/chat/api/chat.py` |
| D12 | **Two test trees** with overlapping subject matter but no shared conftest: `backend/tests/test_header_detection_alltext.py` vs `tests/test_header_detection.py`; `backend/tests/test_row_rag_*` vs nothing in `tests/`. | `backend/tests/` and `tests/` |

---

## 10. Test and benchmark inventory

### 10.1 Automated tests (contain `def test_`)

**Root `tests/` — 17 automated files, 246 test functions**

| File | Tests | Subject |
|---|---:|---|
| `test_relations.py` *(untracked)* | 34 | Join-graph discovery/inference |
| `test_agentic_helpers.py` | 30 | `agentic.py` pure helpers |
| `test_sql_validate.py` *(modified)* | 30 | Identifier validation |
| `test_value_linking.py` | 29 | Term extraction + matching |
| `test_supplier_config.py` | 24 (5 classes) | MiniMax supplier config / i18n / icon / README consistency |
| `test_island_detection.py` | 23 | Island bisection algorithm |
| `test_header_detection.py` | 18 (4 classes) | Header heuristic + orchestration + readers + sidecar |
| `test_bird_lint.py` | 17 | BIRD harness's static SQL linter |
| `test_self_consistency.py` | 15 | Fingerprinting + consensus selection |
| `test_value_index.py` | 14 | Dialect DISTINCT SQL + cache |
| `test_skeleton_fewshot.py` | 14 | Skeleton masking + re-ranking |
| `test_ds_summary.py` | 8 | Datasource summary generation |
| `test_table_embedding.py` | 7 | Cosine floor behaviour |
| `test_refusal_retry.py` *(untracked)* | 6 | Refusal detection + output cap |
| `test_ds_sample_text.py` | 6 | Sample-text budget/dedup |
| `test_detect_island_specs.py` | 5 | Island→import-spec mapping |
| `test_fts_addendum.py` | 3 | PG-family FTS prompt gating |
| `test_minimax_integration.py` | 3 (1 class) | MiniMax API connectivity (skipped without key) |
| `test_read_raw_full.py` | 1 | Reads beyond 25 rows |

**`backend/tests/` — 6 automated files, 28 test functions**
`test_header_llm.py` 7 · `test_parallel_columns.py` 6 · `test_row_rag_text.py` 6 · `test_header_detection_alltext.py` 4 · `test_row_rag_select.py` 4 · `test_row_rag_e2e.py` 1

### 10.2 Manual scripts named `test_*` (no `def test_`; `__main__` entry, need a live server)

`tests/`: `test_auto_route_chat_e2e.py`, `test_bulk_excel_e2e.py`, `test_check_sql_tolerant.py`, `test_content_fts_e2e.py`, `test_ds_summary_e2e.py`, `test_fanout_decision.py`, `test_fanout_payment_e2e.py`, `test_fanout_union_e2e.py`, `test_finder_real_pick.py`, `test_finder_routing.py`, `test_importtodb_islands_e2e.py`, `test_island_edge_e2e.py`, `test_island_excel_e2e.py`, `test_island_rowcount_e2e.py`, `test_island_sidebyside_e2e.py`
Diagnostics: `diag_edge_cases.py`, `diag_island_headers.py`, `_resummarize_all.py`

### 10.3 Evaluation / benchmark harnesses (all `backend/tests/`)

| File | LOC | Purpose |
|---|---:|---|
| ✅ `bird_eval.py` *(modified)* | 1,071 | BIRD Mini-Dev harness. Two modes (`model` / `pipeline`), BIRD-ported EX + tolerant-EX + Soft-F1, `lint_sql`, `explain_sql`, repair loop, `--all-fixes`, stratified sampling, `--resume` |
| `bird_make_subset.py` | 90 | Deterministic 150-question subset (largest-remainder, fixed seed) |
| `bird_setup_schemas.py` | 89 | Splits BIRD's flat PG dump into 11 per-database schemas |
| `bird_register_datasources.py` | 129 | Registers the 11 schemas as SQLBot datasources (`BIRD_DROP=1` removes) |
| `bird_import_descriptions.py` *(untracked)* | 138 | Loads BIRD per-column descriptions into `core_field.custom_comment` |
| `bird_analyze.py` | 136 | Root-cause classification of one result file |
| `bird_diff.py` *(untracked)* | 279 | Per-question diff between two runs, with feature attribution |
| `bird_significance.py` *(untracked)* | 158 | Wilson intervals + exact McNemar between two runs |
| `accuracy_eval.py` | 250 | In-house 55-question Excel bank; scores CORRECT/PARTIAL/WRONG/SQL-ERR |
| `eval_harness.py` | 214 | Dynamic NL2SQL harness (LLM-generated questions) |
| `bulk_ingest.py` | 209 | Bulk-ingests a folder of Excel/CSV as one datasource each |
| `analyze_manifest.py` | 136 | Summarises a bulk-ingest manifest |
| `backfill_record_data.py` | 159 | Backfills `chat_record.data` where SQL exists but no result stored |

**Question banks:** `bird_mini_dev_150.json` (150 entries; fields `question_id, db_id, question, evidence, SQL, difficulty`), `question_bank.json` (55 entries; fields `id, ds, q, nums, strs`), `bird_ds_map.json` *(untracked)* (11 db_id → datasource id).

**Committed result files (`backend/tests/bird_results/`):** tracked — `bird_model.json`, `bird_final_32b.json`, `bird_xiyan14b.json`; untracked — `bird_32b20k_150.json`, `bird_apex40.json`, `bird_pipeline_gptoss_150.json`, `bird_pipeline_v2_150.json`, `bird_postfix_gptoss_150.json`, `bird_qwen32b20k_150.json`, `bird_pipeline_32b_partial.json`.

### 10.4 Test infrastructure

- **No `conftest.py`, no `pytest.ini`, no `[tool.pytest]` section** anywhere in the repo.
- `backend/scripts/test.sh` invokes `coverage run --source=app -m pytest`; the `app` package does not exist here (§8).
- Most automated tests carry a docstring saying "Run in-container", implying `PYTHONPATH=/opt/sqlbot/app` and a live Postgres for the `_e2e` ones.
- CI: **none**. No `.github/`, no `.gitlab-ci.yml`, no Jenkinsfile. Only `.pre-commit-config.yaml` (with a `ci:` block for pre-commit.ci, which is not wired to this fork).

---

## 11. Files NOT read, and why

| Group | Count | Reason |
|---|---:|---|
| `frontend/public/tinymce/**` and `frontend/public/tinymce-dataease-private/**` | ~250 (js, css, woff, md, skins, langs) | Vendored third-party WYSIWYG distribution, not authored here. Includes `tinymce.d.ts` (3,350 LOC). **Should still be covered in Phase 9 as a supply-chain/dependency item.** |
| `frontend/public/swagger-ui-bundle.js` | 1 | Vendored Swagger UI bundle |
| SVG / PNG / JPG / GIF assets (`frontend/src/assets/**`, `docs/*.png`, `docs/*.svg`) | 191 svg + 66 png + 1 jpg + 1 gif | Binary/vector art; no logic |
| Fonts (`*.woff`, `*.ttf`) incl. `g2-ssr/Arial_Unicode.ttf` | 3 | Binary |
| `data/postgresql/**` | — | **Permission denied** (owned by uid `dnsmasq`/root, mode 0700). This is the live Postgres data directory — a runtime volume, not source. |
| `data/sqlbot/{excel,file,images,logs}/**` | ~40 | Runtime artefacts (uploaded workbooks + their `.sqlbot_headers.json` sidecars, generated images, logs). Directory listing captured (§12 Q7); contents are user data, not source. |
| `frontend/src/assets/**/*.css`, `frontend/src/style.less`, `frontend/src/views/dashboard/css/*` | 55 css + 2 less | Styling only; will be revisited only if Phase 1 finds a rendering defect |
| `backend/locales/*.json`, `frontend/src/i18n/*.json`, `backend/apps/swagger/locales/*.json` | 10 | Message catalogues; key existence was spot-checked via the code that reads them, not read line by line |
| The 66 alembic migration bodies | 66 | Subjects captured from filenames + AST symbols (§6.1); full bodies deferred to Phase 1 §1.2 (schema handling), where they matter |
| `sqlbot_xpack` package source | — | **Not in this repository.** A closed-source PyPI/testpypi dependency imported at `main.py:4` and in 6 other modules. Its behaviour (license gating, custom prompts, audit query, dynamic CORS, `monitor_app`) is unauditable from here. |
| `.venv`, `node_modules`, `__pycache__` | — | Not present / excluded |

Everything else — every `.py`, `.yaml`, `.sh`, `.toml`, `.md`, `Dockerfile*`,
`docker-compose.yaml`, and every own-authored `.ts`/`.vue`/`.js` — is accounted
for in §6, either read in full (✅) or inventoried structurally as described in §1.

---

## 12. QUESTIONS carried to later phases

Logged as questions, **not** as defects, per the ground rules. Each records an
observation whose intent I cannot determine from the code alone.

| # | Observation | Where |
|---|---|---|
| Q1 | `SERVER_IMAGE_HOST` is set to the literal placeholder `http://YOUR_SERVE_IP:MCP_PORT/images/` in the deployed compose file. Is MCP image delivery in use here, or is this intentionally inert? | `docker-compose.yaml:28`, `config.py:97` |
| Q2 | `ops/ollama-override.conf` sets `OLLAMA_KEEP_ALIVE=-1`; `important.md` and `docs/BENCHMARK-BIRD.md` both state the live host runs `OLLAMA_KEEP_ALIVE=0`. Which is the intended host state, and is the file meant to be applied? | `ops/`, `important.md:81-84` |
| Q3 | `main.py`'s MCP `include_operations` lists `get_model_list`, but that route is commented out in `apps/mcp/mcp.py`. Intentional removal, or an orphaned entry? | `main.py:187`, `mcp.py:126-128` |
| Q4 | `backend/scripts/{lint,test,tests-start,prestart}.sh` all reference an `app/` package that does not exist in this tree. Are these upstream leftovers, or is a rename pending? | `backend/scripts/` |
| Q5 | Two test trees (`tests/`, `backend/tests/`) with no conftest and overlapping subjects. Is one of them the canonical suite? | §10 |
| Q6 | `apps/settings/models/setting_models.py` declares SQLModel table `terms`, but no migration creates it and nothing imports the model. Vestigial, or pending work? | `setting_models.py` |
| Q7 | `data/sqlbot/excel/` contains 20+ uploaded workbooks (including `finance_sample_100_rows_with_answers_*.xlsx` and `Overdue Transactions_*.xlsx`) checked into the working tree's volume mount. Are these fixtures, or live user data that should not be in the repo directory? | `data/sqlbot/excel/` |
| Q8 | No dependency lockfile exists for Python (`uv.lock` gitignored + absent) or for the frontend (`*-lock.json` gitignored), yet `Dockerfile.overlay` copies `frontend/package-lock.json*` optimistically. Is unpinned resolution intended? | `.gitignore`, `Dockerfile:34`, `Dockerfile.overlay:9` |
| Q9 | `docker-compose.yaml` runs the container `privileged: true` with the Postgres data directory bind-mounted. Required by something specific, or inherited? | `docker-compose.yaml:9` |
| Q10 | `validateEmbedded` decodes the embedded JWT once with `verify_signature: False` to read `appId`/`embeddedId`, then re-decodes with the assistant's secret. The first decode carries an explicit in-code warning. Is the two-step decode the intended design? | `apps/system/middleware/auth.py:182-205` |
| Q11 | `apps/chat/task/llm.py:11` imports `specialized` from the stdlib `dis` module and never uses it. Leftover from an editor autocomplete, or a placeholder? | `llm.py:11` |
| Q12 | `common/audit/schemas/log_utils.py` implements `build_resource_union_query`, but `logger_decorator.py` imports the same-named function from `sqlbot_xpack`. Is the local copy a fallback that was never wired, or dead? | `log_utils.py`, `logger_decorator.py:11` |

---

## 13. What I will do next (awaiting your go-ahead)

Phase 1 — defect sweep over every file, producing `AUDIT/01_defects.md` with one
entry per finding (id, file:line, severity, what/why, repro, proposed fix, blast
radius, catching test), including running the automated suite three times to
report variance. **No code changes.**

I will not start Phase 1 until you say continue.
