# SQLBot — Excel RAG

**Ask questions about your spreadsheets and databases in plain language. Get SQL, data, and charts back.**

This repository is a fork of the open-source [dataease/SQLBot](https://github.com/dataease/SQLBot)
ChatBI system, extended with an **agentic text-to-SQL pipeline** and **Excel-native
ingestion** so it works on real, messy spreadsheets and runs entirely on
self-hosted models — no cloud LLM required.

Everything needed to run the system is in this repository. One `docker compose up`
brings up the API, the web UI, PostgreSQL and the chart renderer.

---

## Table of contents

- [What this fork adds](#what-this-fork-adds)
- [Quick start](#quick-start)
- [Connecting a model](#connecting-a-model)
- [Architecture](#architecture)
- [Configuration reference](#configuration-reference)
- [Benchmarks](#benchmarks)
- [Running the tests](#running-the-tests)
- [Repository layout](#repository-layout)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Credits and license](#credits-and-license)

---

## What this fork adds

Upstream SQLBot does one LLM call: schema in, SQL out. That is fine on clean
star schemas and fragile everywhere else. This fork adds a retry-and-verify
pipeline around the model, plus the Excel handling a real spreadsheet needs.

### Agentic SQL pipeline

| Capability | What it does | LLM cost |
|---|---|---|
| **Retry loop + grader** | Executes the SQL, grades the result against the question, and retries with the concrete error instead of failing to the user. | Only on failure |
| **Identifier check** | Before execution, verifies every table and column in the generated SQL exists verbatim in the schema. Turns an opaque driver error into a precise retry hint. | **Free** |
| **Value linking** | Retrieves the *actual* cell values behind literals in the question (`"Paid"`, `"R&D"`) and shows them to the model **before** it writes the `WHERE` clause, so it stops guessing spellings and casing. | **Free** (bounded read-only SQL) |
| **Skeleton few-shot** | Ranks SQL examples by question *shape* (`"top N X by Y"`) rather than topic similarity alone. | **Free** (CPU re-ranking) |
| **Self-consistency** | Generates N candidates, executes all of them, keeps the result the plurality agrees on. Catches answers that are wrong but run cleanly. | (N−1) extra calls — **off by default** |
| **Multi-candidate** | Writes a second parallel SQL candidate on retries. | 1 extra call, on retry only |
| **Cross-file decomposition** | Splits one question across several datasources and merges the legs. | ~1 extra call on cross-file questions |
| **APEX-SQL refinement** | Logical plan → schema prune → data-profiling probes before generation. Highest accuracy, highest latency. | 5–6 extra sequential calls |
| **Row-level vector RAG** | Semantic per-row retrieval as a last-resort fallback when SQL and the finder both come up empty. | **Free** (pgvector) |

### Excel-native ingestion

Real spreadsheets are not tables. These run at import time, no LLM call:

- **Header detection** — finds the real header row under title blocks, blank
  rows and merged cells, with a weighted heuristic (optional LLM fallback when
  the heuristic is unsure).
- **Island detection** — one sheet holding several separate tables, stacked
  *or* side by side, becomes several tables.
- **Shape-adaptive sampling** — columns whose value set is small are shown to
  the model as a closed set (`one of: Paid, Pending, Void`), which is what makes
  pivoted sheets answerable no matter how deep the relevant row sits.
- **Full-text search + content-aware routing** — datasource selection embeds
  sampled *cell values*, not just column names, so "which file mentions
  GreenGrid?" routes correctly.
- **Auto datasource summaries** — each imported file gets a generated
  description used for routing.

### Self-hosted first

The default configuration points at a local **Ollama** instance for both chat
and embeddings. Nothing leaves the machine. Any OpenAI-compatible endpoint
works equally well — see [Connecting a model](#connecting-a-model).

---

## Quick start

### Requirements

- **Docker** with the Compose plugin (Linux, macOS, or Windows/WSL2)
- **~8 GB disk** for the image, **4 GB RAM** for the stack itself
- **An LLM endpoint** — either a local [Ollama](https://ollama.com) or any
  OpenAI-compatible API key. No GPU is required by SQLBot itself; a GPU only
  matters if you run the model locally.

### 1. Clone and start

```bash
git clone http://10.100.100.102:3000/instagpu/SQL-Bot---Excel-RAG.git
cd SQL-Bot---Excel-RAG
docker compose up -d --build
```

The first build takes 5–15 minutes: it pulls the published `dataease/sqlbot`
base image, rebuilds the frontend from this repository's source, and layers the
extended backend on top.

> **Use `--build`, not a plain `up -d`.** All of this fork's changes live in the
> source tree, so a plain `up -d` will silently reuse a stale image.

### 2. Open the UI

| | |
|---|---|
| Web UI | <http://localhost:8000> |
| MCP endpoint | <http://localhost:8001> |
| Username | `admin` |
| Password | `SQLBot@123456` |

Change the password immediately — see [Security notes](#security-notes).

### 3. Add data

Upload an `.xlsx` / `.csv` under **Data Sources**, or connect an existing
database. Supported connectors:

`Excel/CSV` · `PostgreSQL` · `MySQL` · `Microsoft SQL Server` · `Oracle` ·
`SQLite` · `ClickHouse` · `Apache Doris` · `StarRocks` · `Apache Hive` ·
`Elasticsearch` · `AWS Redshift` · `Kingbase` · `达梦 (DM)`

### 4. Ask a question

Open **Chat**, pick the datasource, and ask in plain language. SQLBot returns
the generated SQL, the result table, and a chart.

---

## Connecting a model

Two things need a model, and they are configured in **two different places**.

### Chat / SQL model — configured in the UI

The chat model lives in the `ai_model` database table, **not** in
`docker-compose.yaml`. Add it under **System → Model**, mark it default, and it
takes effect on the next question — no restart needed.

<details>
<summary><b>Option A — local Ollama (fully offline)</b></summary>

```bash
# on the host, not in the container
ollama pull qwen2.5-coder:32b     # or :14b if you have less VRAM
ollama pull mxbai-embed-large
```

Then in the UI add a model with:

| Field | Value |
|---|---|
| Supplier | 通用 OpenAI 兼容 (Generic OpenAI-compatible) |
| API base | `http://host.docker.internal:11434/v1` |
| API key | `ollama` (any non-empty string) |
| Model name | `qwen2.5-coder:32b` |

**Model sizing.** `qwen2.5-coder:32b` at Q4 is ~20 GB. On a 24 GB card it
spills a few GB to CPU and gets noticeably slower; `qwen2.5-coder:14b` fits
entirely in VRAM. See [Benchmarks](#benchmarks) for the accuracy difference.

For a good local setup, install [ops/ollama-override.conf](ops/ollama-override.conf)
to `/etc/systemd/system/ollama.service.d/override.conf` — it keeps the model
resident so each question stops paying a ~15 s cold reload, and allows the chat
and embedding models to stay loaded together.

</details>

<details>
<summary><b>Option B — a hosted provider</b></summary>

Built-in supplier presets: OpenAI, DeepSeek, Gemini, Kimi, MiniMax,
阿里云百炼 (Alibaba Bailian), 千帆 (Qianfan), 腾讯混元 (Hunyuan), 讯飞星火
(Spark), 火山引擎 (Volcano), 腾讯云 (Tencent Cloud), and a generic
OpenAI-compatible option for anything else. Add the key under **System → Model**.

</details>

### Embedding model — configured in the environment

Set in `docker-compose.yaml`:

```yaml
EMBEDDING_API_BASE: "http://host.docker.internal:11434/v1"
EMBEDDING_API_MODEL: "mxbai-embed-large"
EMBEDDING_API_KEY:  "ollama"
```

**Leave all three empty** to fall back to the embedding model bundled in the
image (`shibing624/text2vec-base-chinese`, 768-dim) with no external
dependency. If you do that and you also want row-level RAG, set
`ROW_RAG_EMBED_DIM: "768"` to match — the default of `1024` is
`mxbai-embed-large`'s dimension.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
  Browser  ────────▶│  Vue 3 SPA          :8000                │
                    ├──────────────────────────────────────────┤
  MCP client ──────▶│  FastAPI            :8000 / :8001        │
                    │                                          │
                    │   ┌────────────────────────────────┐     │
                    │   │  LLMService (orchestrator)     │     │
                    │   │                                │     │
                    │   │  route datasource ─────────┐   │     │
                    │   │  build schema context      │   │     │──▶ LLM endpoint
                    │   │  value linking ────────────┤   │     │    (Ollama /
                    │   │  few-shot selection        │   │     │     OpenAI / …)
                    │   │  ▶ generate SQL ───────────┤   │     │
                    │   │  identifier check          │   │     │
                    │   │  read-only + row-perm      │   │     │
                    │   │  execute ──────────────────┤   │     │
                    │   │  grade → retry ◀───────────┘   │     │
                    │   │  chart JSON                    │     │
                    │   └────────────────────────────────┘     │
                    ├──────────────────────────────────────────┤
                    │  PostgreSQL  :5432   metadata,           │
                    │                      pgvector embeddings,│
                    │                      imported Excel      │
                    ├──────────────────────────────────────────┤
                    │  g2-ssr (Node)       chart PNG renderer  │
                    └──────────────────────────────────────────┘
                              │
                              ▼
                    External databases (MySQL, Oracle, Hive, …)
```

All four processes run in **one container**, started by [start.sh](start.sh).
Full detail, including a per-file map, is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
a before/after diagram of the agentic changes is in
[docs/architecture-diagrams.html](docs/architecture-diagrams.html).

---

## Configuration reference

Everything below is an environment variable set under `environment:` in
[docker-compose.yaml](docker-compose.yaml). Defaults live in
[backend/common/core/config.py](backend/common/core/config.py) — nothing is
hardcoded in the pipeline.

Apply changes with `docker compose up -d` (no rebuild needed for env-only edits).

### Accuracy vs latency

The single most important trade-off. Each extra LLM call costs one model
round-trip per question.

| Variable | Default | Cost | Effect |
|---|---|---|---|
| `APEX_ENABLED` | `true` | **5–6 calls/question** | Plan + prune + profile before generating. Biggest accuracy gain, biggest latency cost. **Set `false` on a slow local GPU.** |
| `AGENTIC_SELF_CONSISTENCY_ENABLED` | `false` | **N−1 calls/question** | Majority vote over N executed candidates. Catches confidently-wrong answers. |
| `AGENTIC_SELF_CONSISTENCY_N` | `3` | — | Candidates including the first. Extra candidates run in parallel. |
| `AGENTIC_MULTI_CANDIDATE_ENABLED` | `true` | 1 call **on retry only** | Second parallel candidate when the first attempt fails. |
| `AGENTIC_DECOMPOSE_ENABLED` | `true` | ~1 call on cross-file questions | Lets one question span multiple datasources. |
| `AGENTIC_SQL_RETRY_ENABLED` | `true` | on failure only | Retry loop. |
| `AGENTIC_SQL_MAX_ATTEMPTS` | `3` | — | Retry ceiling. |
| `AGENTIC_GRADER_ENABLED` | `true` | 1 call | Grades whether the result answers the question. |
| `AGENTIC_IDENTIFIER_CHECK_ENABLED` | `true` | **free** | Verify identifiers exist before executing. |
| `VALUE_LINKING_ENABLED` | `true` | **free** | Retrieve real cell values for the prompt. |
| `SKELETON_FEWSHOT_ENABLED` | `true` | **free** | Shape-based few-shot ranking. |

> **Note.** Self-consistency is skipped automatically for users with row-level
> permissions: permission filters are applied only to the main query, so voting
> in an unfiltered candidate could surface rows the user may not see.

The tuning actually used on the reference deployment — and *why* — is written up
in [important.md](important.md).

### Retrieval and schema context

| Variable | Default | Effect |
|---|---|---|
| `TABLE_EMBEDDING_ENABLED` | `true` | Question→table RAG. |
| `TABLE_EMBEDDING_COUNT` | `10` | Tables put in the prompt. |
| `TABLE_EMBEDDING_COSINE_FLOOR` | `0.0` | Relevance floor applied after the top-N cut. `0` = always exactly N tables, however irrelevant. **Raise this on wide schemas.** |
| `TABLE_SAMPLE_PROBE_ROWS` | `50` | Rows probed per table when building the sample. Raise if category values sit deeper. |
| `TABLE_SAMPLE_CHAR_BUDGET` | `3000` | Character budget for sample rows per table. |
| `TABLE_SAMPLE_DISTINCT_PER_COL` | `25` | Below this, a column is shown as a closed value set. |
| `VALUE_LINKING_MAX_TABLES` / `_MAX_COLUMNS` | `5` / `12` | Probe budget per question. |
| `VALUE_LINKING_MIN_SCORE` | `0.72` | Similarity floor for a value hint. |
| `AGENTIC_FANOUT_COSINE_FLOOR` / `_MARGIN` | `0.40` / `0.20` | When a second datasource joins a cross-file answer. |
| `AGENTIC_LISTING_KEYWORDS` / `_AGGREGATION_KEYWORDS` | EN/ZH/KO | Comma-separated. **Add your language here** — no code change needed. |

### Excel ingestion

| Variable | Default | Effect |
|---|---|---|
| `EXCEL_ISLAND_DETECTION_ENABLED` | `true` | Split multiple tables on one sheet. |
| `EXCEL_SPLIT_SIDE_BY_SIDE` | `true` | Also split tables separated by an empty column. Set `false` if your sheets use a genuine spacer column. |
| `EXCEL_FTS_ENABLED` | `true` | PostgreSQL full-text search over imported cells. |
| `HEADER_DETECT_ROWS` | `25` | Leading rows scanned for the header. |
| `HEADER_DETECT_CONFIDENCE` | `0.62` | Below this the heuristic is "unsure". |
| `HEADER_W_*` | see config | Header-scoring weights (fill, uniqueness, brevity, …). Should sum to ~1.0. |
| `HEADER_LLM_ENABLED` | `false` | Opt-in LLM fallback when the heuristic is unsure. Point `HEADER_LLM_MODEL` at a *small* model. |
| `DS_SUMMARY_ENABLED` | `true` | Auto-generate a datasource description for routing. |
| `EMBEDDING_SAMPLE_ENABLED` | `true` | Embed sampled cell values, not just column names. |
| `ROW_RAG_ENABLED` | `false` | Per-row vector fallback. **Requires** `ROW_RAG_EMBED_DIM` to match your embedding model. |

---

## Benchmarks

Two harnesses, answering two different questions. Both are in this repository.

### 1. BIRD Mini-Dev — "how good is the SQL, on a scale others use"

[BIRD](https://bird-bench.github.io/) is the standard text-to-SQL benchmark.
Mini-Dev is the officially sanctioned 500-question subset, and the only
mainstream option with an **official PostgreSQL port** — which matters here,
because this whole stack is Postgres and no SQLite→PG dialect conversion sits
between the model and its score.

This repository runs a **reproducible 150-question subset** sampled by
[bird_make_subset.py](backend/tests/bird_make_subset.py), which preserves both
the difficulty split (44 simple / 75 moderate / 31 challenging) and the
per-database share via largest-remainder allocation with a fixed seed.

**Results** — 150 questions, execution accuracy (EX), single L40-24Q vGPU:

| Run | Model | EX | EX (tolerant) | Soft-F1 | simple | moderate | challenging | median |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Baseline | `gpt-oss:20b` | **40.7%** | 45.3% | 41.0 | 66% | 35% | 19% | 41 s |
| Tuned | `qwen2.5-coder:32b` | **52.0%** | 52.7% | 52.7 | 64% | 52% | 35% | 38 s |
| Specialist | `XiYanSQL-QwenCoder-14B` | 43.3% | 44.7% | 41.8 | 68% | 35% | 29% | 24 s |

Raw per-question output for all three runs is committed under
[backend/tests/bird_results/](backend/tests/bird_results/).

**Read this honestly.** The 40.7% → 52.0% jump is *two* changes at once — the
model swap **and** the harness improvements (BIRD column descriptions, a revised
prompt, and one repair pass, which fired on 27 of 150 questions). It is not
attributable to the model alone. What the numbers do support: the tuned run
gains almost entirely on **moderate (+17pp)** and **challenging (+16pp)**
questions while simple questions stay flat — which is the shape you would
expect if better schema context is what is helping.

At n=150 a reported percentage carries roughly **±8pp** at 95% confidence.
**Treat any gap smaller than that as noise** — including the 43.3% vs 40.7% gap.

For reference, published full mini-dev numbers: GPT-4o ≈ 55–58%, open 20–32B
models typically 25–40%.

Methodology, setup, and caveats: **[docs/BENCHMARK-BIRD.md](docs/BENCHMARK-BIRD.md)**.

<details>
<summary><b>Running BIRD yourself</b></summary>

```bash
# 1. data (800 MB zip)
curl -sLO https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip
unzip -q minidev.zip

# 2. load. The PG dump flattens all 11 logical databases into one `public`
#    schema; loading it as-is would show every question all 75 tables at once,
#    a far harder schema-linking problem than published numbers assume.
docker exec sqlbot psql -U root -d postgres \
  -c "CREATE ROLE xiaolongli NOLOGIN;" -c "CREATE DATABASE bird_dev OWNER root;"
docker exec -i sqlbot psql -U root -d bird_dev < minidev/MINIDEV_postgresql/BIRD_dev.sql
python3 backend/tests/bird_setup_schemas.py --tables minidev/MINIDEV/dev_tables.json

# 3. register the 11 datasources (BIRD_DROP=1 removes them again)
docker cp backend/tests/bird_register_datasources.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_register_datasources.py"

# 4. run  (--resume makes an interrupted overnight run restartable)
docker cp backend/tests/bird_eval.py           sqlbot:/tmp/
docker cp backend/tests/bird_mini_dev_150.json sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
    .venv/bin/python /tmp/bird_eval.py --mode model --all-fixes --resume --out /tmp/bird.json"
```

`--mode model` prompts the LLM directly (raw model baseline). `--mode pipeline`
routes through `LLMService` exactly as the product does. The gap between them is
the honest answer to whether the agentic layer earns its latency.

Then explain *why* the wrong ones were wrong — pure Postgres, no GPU cost:

```bash
docker cp backend/tests/bird_analyze.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
    .venv/bin/python /tmp/bird_analyze.py --results /tmp/bird.json --out /tmp/bird_analysis.json"
```

[bird_run_all.sh](backend/tests/bird_run_all.sh) chains a full experiment
series, cheapest-and-most-decisive first.

</details>

### 2. In-house question bank — "does the product still work"

BIRD tests NL→SQL over clean relational schemas. It does **not** exercise Excel
ingestion, header/island detection, or cross-file decomposition — arguably this
fork's whole point. [accuracy_eval.py](backend/tests/accuracy_eval.py) runs 55
questions from [question_bank.json](backend/tests/question_bank.json) against
real Excel files end to end:

```bash
docker exec -e EVAL_APEX=off sqlbot \
    sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/accuracy_eval.py"
```

It reports `CORRECT / PARTIAL / WRONG / SQL-ERR`. **`WRONG` is the number to
watch**: the SQL ran fine but the expected value was not in the result — the
exact failure mode value linking and self-consistency exist to reduce.

---

## Running the tests

240 tests covering the agentic helpers, Excel header and island detection, value
linking, self-consistency, SQL validation, the BIRD linter, and the datasource
routing logic.

The tests import the backend from its deployed path, so they run **inside the
container**:

```bash
docker exec sqlbot sh -c "rm -rf /tmp/sbrepo" && \
docker cp . sqlbot:/tmp/sbrepo && \
docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
    .venv/bin/python -m pytest /tmp/sbrepo/tests -q -p no:cacheprovider"
```

Expected: **`237 passed, 3 skipped`**.

Copy the **whole repository**, not just `tests/`. Several tests assert against
`frontend/src/**` and the README files, and will fail with `FileNotFoundError`
if only the test directory is present.

---

## Repository layout

```
backend/                FastAPI application
  main.py                 startup, middleware, routers, MCP
  apps/chat/task/llm.py   the orchestration pipeline (start here)
  apps/db/                connectors, execution, read-only validation
  apps/datasource/        Excel import, header/island detection, embeddings
  common/core/config.py   every tunable, with its rationale in comments
  tests/                  BIRD harness, accuracy harness, question bank
frontend/               Vue 3 + Pinia + Element Plus SPA
g2-ssr/                 Node chart-image renderer (used by MCP)
tests/                  unit + e2e test suite
docs/                   architecture, BIRD methodology, upstream English README
ops/                    host-side config (Ollama systemd override)
docker-compose.yaml     ← the entry point
Dockerfile.overlay      fast build: published base + this source on top
Dockerfile              full from-source build
important.md            deployment tuning notes and why each flag is set
```

`data/` is created at runtime for the Postgres volume, uploaded files and logs.
It is deliberately **not** in version control.

---

## Security notes

This repository ships with upstream's **public default** credentials so a fresh
clone starts without configuration. Change all three before exposing the service
beyond localhost:

1. **`SECRET_KEY`** in `docker-compose.yaml` signs session tokens. The committed
   value is the well-known upstream default — anyone can forge a token against
   it.
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Replace it, then `docker compose up -d`. Existing sessions will need to log
   in again.

2. **`admin` / `SQLBot@123456`** — change in the UI on first login.

3. **`POSTGRES_PASSWORD`** — only reachable inside the container by default,
   since port 5432 is not published. Change it if you publish that port.

Also worth knowing:

- Generated SQL is validated as **read-only** before execution, and rewritten
  with row-level permission filters where they are configured.
- `privileged: true` in the compose file comes from upstream's single-container
  design (it manages the embedded Postgres). Remove it if your host allows.
- `BACKEND_CORS_ORIGINS` defaults to localhost only. Extend it deliberately.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Code changes have no effect | A plain `up -d` reuses the stale image | `docker compose up -d --build` |
| Every question times out | The local model is not loaded, or spilled to CPU | `ollama ps` — if the model is not 100% GPU, use a smaller one |
| ~15 s of dead time before every answer | Ollama unloads between calls | Install [ops/ollama-override.conf](ops/ollama-override.conf) (`OLLAMA_KEEP_ALIVE=-1`) |
| Answers are slow but correct | APEX is adding 5–6 LLM calls | `APEX_ENABLED: "false"` |
| Model invents column names | Not enough schema context reaches it | Raise `TABLE_EMBEDDING_COUNT`, or set `TABLE_EMBEDDING_COSINE_FLOOR` above `0` on wide schemas |
| Model gets literal values wrong | Value linking found nothing to link | Lower `VALUE_LINKING_MIN_SCORE`; confirm `VALUE_LINKING_ENABLED` |
| Excel imports with a garbage header | Header heuristic was unsure | Override the header row in the import preview, or enable `HEADER_LLM_ENABLED` |
| Row RAG errors on dimension mismatch | Embedding model changed | Set `ROW_RAG_EMBED_DIM` to your model's dimension (`mxbai-embed-large` = 1024, bundled = 768) |
| `host.docker.internal` unreachable on Linux | Missing host mapping | Already set via `extra_hosts` in the compose file — check the host firewall allows `:11434` |

Logs:

```bash
docker compose logs -f sqlbot     # stack
tail -f data/sqlbot/logs/*.log    # application
```

---

## Credits and license

Built on **[SQLBot](https://github.com/dataease/SQLBot)** by the
[DataEase](https://github.com/dataease) team at FIT2CLOUD — the entire ChatBI
foundation, web UI, connector layer, workspace/permission model and MCP
integration are theirs. This repository adds the agentic pipeline, Excel
ingestion improvements, and the benchmark harnesses described above.

Techniques implemented here draw on published text-to-SQL research: value
linking from CHESS, skeleton-masked few-shot selection from DAIL-SQL,
execution-based self-consistency from MBR-Exec / C3 / MCS-SQL, and the
refinement stage from [APEX-SQL](https://arxiv.org/abs/2602.16720).

Licensed under the [FIT2CLOUD Open Source License](LICENSE) — GPLv3 with
additional terms. You may build on this source, but:

- the SQLBot logo and copyright notices may not be replaced or modified;
- derivative works must honour GPLv3 obligations.

For commercial licensing, contact <support@fit2cloud.com>.
