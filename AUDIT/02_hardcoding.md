# AUDIT / 02 — Hardcoding & Portability Sweep

**No code was changed.** Every row was located by grep or by reading the file.

The codebase already has a large, well-documented settings surface (118 fields —
see `00_inventory.md` §4.1), and most tuning knobs *are* configurable. What
follows is what is **not**, grouped by the assumption it bakes in.

Legend for **Impact**: **BLOCKS** = a second deployment/tenant/dataset cannot
work without a code change. **DEGRADES** = works, but silently worse.
**RISK** = correctness or security consequence.

---

## A. Network identity, hosts and URLs

| Value | file:line | Why it's a problem | Should become | Default |
|---|---|---|---|---|
| `http://YOUR_SERVE_IP:MCP_PORT/images/` | `common/core/config.py:97` and **verbatim in `docker-compose.yaml:28`** | This is a placeholder shipped as the live value. `request_picture` (`llm.py:3114`) builds the returned image URL with `urljoin(SERVER_IMAGE_HOST, …)`, so every MCP/API chart reply returns an unresolvable URL. | already `SERVER_IMAGE_HOST` — the defect is that the deployment never set it | derive from request `Host` header, or fail startup if unset |
| `http://localhost:3000` | `config.py:96` (`MCP_IMAGE_HOST`) | Correct only while g2-ssr shares a network namespace with the app. Splitting the container (§Phase 6) breaks chart rendering with no error — `request_picture` swallows the exception into `_error` and still returns a URL. | already a setting; needs to be **required**, not defaulted | none |
| `http://localhost:5173` | `config.py:37` (`FRONTEND_HOST`) | Unconditionally appended to `all_cors_origins` (`config.py:46-48`), so a Vite dev origin is a permitted CORS origin in **production**. | `FRONTEND_HOST: str \| None = None`, appended only when set | unset |
| `redis://localhost:6379/0` | `common/core/sqlbot_cache.py:136` | Fallback when `CACHE_TYPE=redis` but `CACHE_REDIS_URL` is empty — connects to a *wrong* Redis instead of failing. | fail fast when `CACHE_TYPE=="redis"` and no URL | — |
| `https://fastapi.tiangolo.com/img/favicon.png` | `main.py:169` | `/docs` fetches a third-party asset; breaks in an air-gapped deployment and leaks a request per docs view. | serve locally | bundled |
| `localhost` / `5432` / `root` / `Password123@pg` | `config.py:55-59` **and again** `Dockerfile:79-81` **and again** `docker-compose.yaml:19-23` | The same credential is declared in three places with no single source of truth. `Dockerfile` bakes it into the image as an `ENV`, so it is the effective default even when compose is not used. | one source; image should carry no credential | must be supplied |
| `0.0.0.0`, ports `8000`/`8001`/`3000`/`5432` | `start.sh:9-15`, `main.py:218`, `g2-ssr/app.js:5`, `Dockerfile:94` | Ports are literals in four files. Changing the app port requires editing `start.sh`, the `EXPOSE`, the healthcheck and compose. | `SQLBOT_PORT`, `MCP_PORT`, `SSR_PORT` | 8000/8001/3000 |
| `host.docker.internal` | `docker-compose.yaml:11,38` | Correct for Docker Desktop / `host-gateway`; fails on plain Linux Docker without the `extra_hosts` entry, and on Kubernetes. | already env-driven via `EMBEDDING_API_BASE`; the `extra_hosts` line is the coupling | — |
| `clickhouse+http://` scheme | `apps/db/db.py:90,92` | ClickHouse over TLS (`clickhouse+https`) is unreachable — the scheme is a literal, and `conf.ssl` is only consulted for MySQL/Doris/StarRocks. | derive from `conf.ssl` | http |

**Failure modes when these break:** deployed behind a different domain → CORS
rejects the SPA and every chart URL 404s; g2-ssr moved to its own pod → charts
silently stop rendering with no error surfaced.

---

## B. Filesystem paths

| Value | file:line | Problem | Should become |
|---|---|---|---|
| `/opt/sqlbot` (`BASE_DIR`), `/opt/sqlbot/data/file` (`UPLOAD_DIR`), `/opt/sqlbot/data/excel` (`EXCEL_PATH`), `/opt/sqlbot/images` (`MCP_IMAGE_PATH`), `/opt/sqlbot/models` (`LOCAL_MODEL_PATH`), `/opt/sqlbot/db_client/oracle_instant_client` | `config.py:74-100, 320` | All are settings, but all default to an absolute container path. Running the backend outside the image (which is exactly what `backend/scripts/*.sh` assume) writes to `/opt`. | keep as settings; default to a path relative to `BASE_DIR`, and make `BASE_DIR` default to `os.getcwd()` |
| `SCRIPT_DIR = f"{BASE_DIR}/scripts"` | `config.py:75` | Computed from `BASE_DIR` **at class-definition time**, so overriding `BASE_DIR` via env does *not* move `SCRIPT_DIR`. Same bug pattern for `EMBEDDING_TERMINOLOGY_SIMILARITY = EMBEDDING_DEFAULT_SIMILARITY` (line 104) and the three other `X = Y` defaults at lines 105, 107, 108 — overriding the parent leaves the children at the old literal. | `@computed_field` properties |
| `../.env` | `config.py:27` | Points one level **above** `backend/`, i.e. the repo root. Nothing documents this, no `.env` exists, and `.gitignore` hides it. Anyone who creates `backend/.env` (the obvious place) is silently ignored. | document, or accept both |
| `save_path + ".sqlbot_headers.json"` | `utils/header_detection.py:392` | Sidecar written next to the upload. Breaks on read-only or object-storage volumes; also means header decisions live outside the DB and are lost if the volume is recycled. | store in `core_datasource.configuration` |
| `tempfile.gettempdir()` registry + `atexit` | `system/crud/user_excel.py:319-334` | Error workbooks live in a **process-global dict**. A restart orphans the files; a second uvicorn worker cannot see the other's ids. | persist to `UPLOAD_DIR` with a DB-backed id |
| `/tmp/bird_*` paths | `tests/bird_eval.py:54-65`, all `bird_run_*.sh` | The whole benchmark workflow assumes `/tmp` inside a container named `sqlbot`. | env-driven (already partly is: `BIRD_BANK`, `BIRD_DS_MAP`) |

---

## C. Model, prompt and inference parameters

| Value | file:line | Problem | Should become | Default |
|---|---|---|---|---|
| `approx_tokens_per_batch=10_000` | `llm.py:1370` (call site) and `apex_helpers.py:177` (signature) | The APEX pruning batch budget. Declared twice; the call site overrides the default with the same number. | `APEX_PRUNE_BATCH_TOKENS` | 10000 |
| `char_budget = approx_tokens_per_batch * 4` | `apex_helpers.py:184` | **4 chars/token.** `llm.py:1643` explicitly measured this workload at **2.9** chars/token and says using 4 "understated the true size by ~40%". The corrected figure was applied to the log line but not to the batcher, so pruning batches are ~38 % over budget. | `LLM_CHARS_PER_TOKEN` | 2.9 |
| `tables[:8]` (tables profiled), `probes[:8]` (probes per table) | `llm.py:1485`, `:1537` | Caps the most expensive stage in the pipeline. Not configurable, so APEX cost cannot be tuned without a rebuild — and APEX is the reason `APEX_ENABLED` had to be turned off wholesale. | `APEX_PROFILE_MAX_TABLES`, `APEX_PROFILE_MAX_PROBES` | 8, 8 |
| `total_cols <= 12` (prune skip threshold) | `llm.py:1358` | A 12-column schema is "small" on a BIRD database and enormous on a wide financial sheet. | `APEX_PRUNE_MIN_COLUMNS` | 12 |
| `max_workers=2` (plan sampling N), `max_workers=2` (prune passes) | `llm.py:1281`, `:1397` | The APEX paper's N=2 consensus is hardcoded; the docstring calls it "N=2 consensus" as if configurable. | `APEX_PLAN_SAMPLES` | 2 |
| `timeout=60` on the alt-candidate harvest | `llm.py:2502` | On this deployment a single call takes 30–120 s (`important.md:3`), so the alternate candidate is frequently discarded *after* being paid for. | `AGENTIC_ALT_CANDIDATE_TIMEOUT` | 180 |
| `max_rows=8, max_chars=2000` (grader preview) | `agentic.py:317` | Bounds what the grader sees. | `AGENTIC_GRADER_PREVIEW_*` | 8 / 2000 |
| `max_chars=12000` (fanout leg preview) | `llm.py:1202`, `:2716` | Written twice with the same literal. | `AGENTIC_LEG_PREVIEW_CHARS` | 12000 |
| `[:1500]`, `[:800]`, `[:200]`, `[:2000]`, `[:8000]` truncations | `llm.py:1178, 2469, 2479, 2484, 2537, 1599`; `agentic.py:381`; `apex_helpers.py:502` | Eight different truncation lengths for error text fed back to the model. Too short loses the actual error; too long burns context. None tunable, none derived from a token budget. | one `RETRY_FEEDBACK_MAX_CHARS` | 1500 |
| `limit = 1000` in `save_sql_data` | `llm.py:2009` | Duplicates `AGENTIC_FULL_RESULT_LIMIT` (also 1000) with no link between them. See defect **D-16**. | reuse the setting | 1000 |
| `1000` in `raise_sql_limit(sql, target=1000)` | `agentic.py:407` | Third copy of the same magic number (call site passes the setting, the default does not). | drop the default | — |
| `maxsize=32` LLM instance cache | `model_factory.py:139` | See **D-15**. | `LLM_CACHE_SIZE` | 32 |
| `'sk'`, `'bearer'`, `'assistant'`, `'embedded'` token schemes | `middleware/auth.py:79, 123, 159, 157` | Four auth schemes as string literals across 4 methods. | one enum | — |

---

## D. Retrieval, ranking and scoring magic numbers

| Value | file:line | Problem | Should become |
|---|---|---|---|
| BM25 `k1=1.5, b=0.75` | `agentic.py:40` | Okapi defaults, fine as defaults — but the corpus here is "tens of datasources" of *schema text*, not prose, and the values were never tuned. | `BM25_K1`, `BM25_B` |
| RRF `k=60` | `agentic.py:80` | The constant that decides how much the lexical ranking can move a strong embedding match. Used by both `ds_embedding` and `skeleton` re-ranking. | `RRF_K` |
| `0.80 + 0.19 * (shorter/longer)` containment score | `value_linking.py:217` | Two tuned constants with no name, feeding directly against `VALUE_LINKING_MIN_SCORE` (0.72, which *is* configurable). The floor is tunable but the function it floors is not. | named constants or a documented scoring mode |
| `_MAX_FALLBACK_TERMS = 8`, `_MIN_TERM_LEN = 2`, `_MAX_TERM_WORDS = 4` | `value_linking.py:66-72` | Bound the case-insensitive fallback pass — the pass that makes lowercase questions work at all. | `VALUE_LINKING_*` |
| `_CACHE_MAX_ENTRIES = 2000` | `value_index.py:36` | Cache bound; eviction is "clear everything" once hit. | `VALUE_LINKING_CACHE_MAX` |
| `max_terms: int = 400` schema domain terms | `skeleton.py:76` | Caps masking vocabulary; a wide workbook exceeds it silently. | `SKELETON_MAX_DOMAIN_TERMS` |
| `round(number, 6)` in the consensus fingerprint | `agentic.py:242` | Decides when two candidates "agree" numerically. Must stay in step with `bird_eval._norm_cell`'s own `round(…, 6)` — two independent copies of the same tolerance. | one shared `NUMERIC_COMPARE_DP` |
| `cutoff=0.75` difflib, `max_card=25`, `<= 12` values shown, `> 400` chars | `bird_eval.py:484, 216, 311, 252` | Harness-side tuning constants, all literals. | harness flags |
| `settings.ROW_RAG_EMBED_DIM = 1024` | `config.py:295` | Comment says "mxbai-embed-large dimension". If `EMBEDDING_API_MODEL` is changed to a model with a different dimension, `ensure_schema` has already created `vector(1024)` and every insert fails — with the failure swallowed by `ingest.embed_and_store_df`'s blanket `except`. **The dimension is configurable but not validated against the actual model.** | probe the model once at startup and assert |

---

## E. Locale, language and domain vocabulary

| Value | file:line | Problem |
|---|---|---|
| `'简体中文'` / `'繁体中文'` / `'英文'` / `'韩语'` | `llm.py:3229-3239`, `chat_model.py:230` | The prompt's `{lang}` slot is filled with a **Chinese-language name for the language**. An English deployment asks an English model to answer in "英文". Four supported languages are hardcoded in an if-chain; a fifth requires a code change. |
| `'已启用'`, `'已禁用'`, `'本地创建'` | `system/crud/user_excel.py:286-291` | Bulk user import compares cell values against Chinese literals. The **template** those cells come from is generated via `trans(...)` (i18n), so an English admin downloads an English template and every row then fails validation. Direct i18n/logic mismatch. |
| `"数据表列表"`, `"Sheet名称"`, `"表名"`, `"表备注"`, `"字段名"`, `"字段备注"` | `api/datasource.py:401-406` | Schema-comment import/export keys on Chinese sheet and column names. A workbook produced by a non-Chinese user cannot be re-imported. |
| `"Sheet1"` | `utils/excel.py:104,188`, `api/datasource.py:725,739` | The synthetic sheet label for CSV, in four places. Collides with a real Excel sheet genuinely named `Sheet1`, whose header-meta sidecar entry it would overwrite. |
| `Asia/Shanghai` timezone | `Dockerfile:70-71` | Baked into the image. Every `datetime.now()` in the app (used for the `{current_time}` prompt slot, `create_time`, log timestamps) is Shanghai-local regardless of deployment. **Temporal questions ("last month", "this quarter") are answered in the wrong timezone for any non-CST tenant.** |
| `%Y-%m-%d %H:%M:%S` current-time format | `llm.py:1614, 1152, 932` | Three copies; no locale awareness. |
| `AGENTIC_LISTING_KEYWORDS` / `AGENTIC_AGGREGATION_KEYWORDS` | `config.py:237-244` | **Good example** — multilingual, env-overridable, exactly the pattern the items above should follow. Noted as the model to copy. |

---

## F. Single-tenant / single-shape assumptions

| Assumption | file:line | Failure mode |
|---|---|---|
| **User id 1 is the superuser** | `crud/permission.py:76-77` | See defect **D-05**. Also silently disables fanout for everyone else (**D-12**). |
| `oid = 1` fallback | `llm.py:829, 2147`; `crud/datasource.py:77`; `api/datasource.py:838` | Five sites default the workspace to `1` when `oid` is falsy. In a multi-workspace instance this silently writes into workspace 1. |
| `row_embeddings` has no tenant filter | `row_rag/store.py:126` | See defect **D-01**. |
| `TABLE_EMBEDDING_COUNT = 10` fixed table cut | `config.py:131` | Configurable, but there is no *floor* by default (`TABLE_EMBEDDING_COSINE_FLOOR = 0.0`), so a 200-table warehouse always ships exactly 10 tables and a 3-table workbook ships all 3 padded with nothing. Scales by neither direction. |
| `DS_EMBEDDING_COUNT = 10` | `config.py:137` | Same, for datasource routing. With 500 Excel files the finder sees 10. |
| `VALUE_LINKING_MAX_TABLES = 5`, `MAX_COLUMNS = 12` | `config.py:172-173` | Value linking probes at most 5 tables × 12 columns *globally*. On a wide schema the literal being asked about is very likely outside that window, and the feature no-ops without saying so. |
| `SCHEMA_RELATIONS_MAX_PAIRS = 200` probe budget | `config.py:318` | Inference stops after 200 pairs with no signal to the caller; on a 30-sheet workbook the last sheets get no edges. |
| `MAX_SUB_QUESTIONS = 5` | `agentic.py:104` | Module constant, not a setting, while `AGENTIC_FANOUT_MAX_SOURCES` (5) *is* a setting. Two caps on the same quantity, one configurable. |
| `articles_number: int = 4` recommended questions | `llm.py:130` | Class attribute default. |
| Excel data materialised into the app's own Postgres `public` schema | `db/engine.py:16` (`dbSchema="public"`) | Every tenant's spreadsheets share one schema with SQLBot's own tables. `relations.get_relations` has to filter internal tables out by name (`relations.py:575-586`) precisely because of this. Table-name collisions are avoided only by a 10-hex-char hash suffix. |
| One container = app + DB + renderer | `start.sh` | Cannot scale any component independently; `--workers 1` is forced because the thread pools and caches are process-global. |

---

## G. Build / dependency pinning

| Item | Where | Problem |
|---|---|---|
| No `uv.lock` in the repo (gitignored by `*.lock`) | `.gitignore`, `Dockerfile:34` | Python dependency resolution is unpinned at build time. The container *has* a `uv.lock` (from the published base image) that the repo cannot reproduce. |
| No `package-lock.json` (gitignored by `*-lock.json`) | `.gitignore`, `Dockerfile.overlay:9` copies `package-lock.json*` optimistically | Frontend build is unpinned. |
| `default` index = `http://mirrors.aliyun.com/pypi/simple` — **plain HTTP** | `pyproject.toml` `[[tool.uv.index]]` | Every Python dependency is fetched over unauthenticated HTTP by default. |
| `sqlbot-xpack` from **testpypi** | `pyproject.toml` `[tool.uv.sources]` | A production hard-dependency (imported at `main.py:4`) sourced from a test index. |
| `curl … | sh` of a license validator | `Dockerfile-base:27` | Remote script piped to shell at build time, no checksum. |
| `dataease/sqlbot:latest` base | `Dockerfile.overlay:15` | Floating tag — the overlay's contents change without any repo change. |
| `[tool.ruff] target-version = "py310"` vs `requires-python = "==3.11.*"` | `pyproject.toml` | Lint targets an older Python than the project runs. |

---

## Proposed single typed config layer (design only — not implemented)

The existing `Settings` class is already 80 % of this. The proposal is to close
the remaining gaps rather than rewrite it.

**1 — One schema, one file, no second source.**
`common/core/config.py` stays the only place a default may be written. Delete
the mirrored literals in `header_detection._DEFAULTS` (D-9 in `00_inventory.md`
§9) and have that module import the settings object; the "standalone importable"
requirement is met by a tiny `_get(name, default)` shim rather than a duplicated
dict.

**2 — Fix the eager-default bug class.** Any field defined as
`X: T = <another field>` (`SCRIPT_DIR`, `EMBEDDING_TERMINOLOGY_SIMILARITY`,
`EMBEDDING_DATA_TRAINING_SIMILARITY`, `EMBEDDING_TERMINOLOGY_TOP_COUNT`,
`EMBEDDING_DATA_TRAINING_TOP_COUNT`) becomes a `@computed_field` property, so
overriding the parent actually moves the child.

**3 — Precedence, stated and enforced.**
```
CLI/explicit override  >  process env  >  ../.env  >  DB sys_arg  >  schema default
```
The DB layer (`sys_arg`, `ai_model`) is deliberately placed **below** env so an
operator can always override a bad DB value without a UI. Today `sys_arg` sits
*above* env for its three keys (`00_inventory.md` §4.2), which is the opposite;
that inversion should be a conscious decision, not an accident of where the read
happens.

**4 — Fail fast on startup.** Add a `model_validator(mode='after')` that raises
when:
- `CACHE_TYPE == "redis"` and `CACHE_REDIS_URL` is empty;
- `SERVER_IMAGE_HOST` still contains `YOUR_SERVE_IP`;
- `SECRET_KEY` equals the generated-per-boot default in a non-dev run (today a
  restart silently invalidates every issued JWT);
- `ROW_RAG_ENABLED` and the live embedding model's dimension ≠ `ROW_RAG_EMBED_DIM`;
- the header-detection weights do not sum to ≈1.0.

**5 — `extra="ignore"` → `extra="forbid"` for the `SQLBOT_`-prefixed namespace.**
Today a typo'd env var is silently dropped. Adopting a prefix lets unknown
prefixed vars be rejected while leaving the ambient environment alone.

**6 — Every literal in §C and §D gets a name.** They do not all need to be
*documented* knobs; they need to be *one* knob each, in one place, so a
deployment can move them without a rebuild and so the audit trail shows what was
tuned.

**7 — Identifier quoting becomes one function.** Four implementations exist
(`00_inventory.md` §9 D5). Collapsing them to `apex_helpers.quote_ident` is a
prerequisite for fixing defect **D-08**, and is the single highest-value item in
this document because it is both a hardcoding fix and a security fix.
