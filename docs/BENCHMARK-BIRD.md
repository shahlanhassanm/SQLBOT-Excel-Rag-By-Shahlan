# BIRD Mini-Dev benchmark for SQLBot

Standard text-to-SQL benchmark, run against this deployment (a local L40-24Q
vGPU). It answers "**how good is our SQL brain, on a scale other people also
use**" — which the in-house [question_bank.json](../backend/tests/question_bank.json)
harness cannot, because its 55 Excel questions have no published comparison point.

The two harnesses answer different questions and both are worth keeping:

| | `accuracy_eval.py` (in-house) | `bird_eval.py` (this) |
|---|---|---|
| Data | your real Excel files | 11 BIRD relational databases |
| Exercises | Excel ingestion, header/island detection, cross-file decomposition | NL→SQL only |
| Scoring | expected values appear in result rows | result set equals gold's |
| Comparable to others | no | yes — BIRD leaderboard |

## Why BIRD Mini-Dev

Of the mainstream options it is the only one with an **official PostgreSQL
port**, and this whole stack is Postgres — so no SQLite→PG dialect conversion
sits between the model and its score. Its questions also carry external-knowledge
`evidence` and messy real-world schemas, closer to Excel-derived tables than
Spider's clean academic ones. 500 questions is the officially sanctioned subset,
so published numbers exist to compare against.

Reference points on the full mini-dev (PostgreSQL, EX): GPT-4o ≈ 55-58%,
open ~20-32B models typically **25-40%**.

## The 150-question subset

The full 500 would take ~35h/pass here. [bird_make_subset.py](../backend/tests/bird_make_subset.py)
samples 150 preserving both marginals — difficulty (44 simple / 75 moderate /
31 challenging, the same 30/50/20 shape) and per-database share — via
largest-remainder allocation with a fixed seed, so the subset is reproducible.

At 150 questions a reported percentage carries roughly **±8pp** at 95%
confidence. Treat gaps smaller than that as noise.

## Metrics

- **EX (official)** — execution accuracy, `set(predicted_rows) == set(gold_rows)`,
  ported verbatim from BIRD's `evaluation_ex.py`. This is the headline, directly
  comparable to the leaderboard.
- **EX (tolerant)** — same, after rounding floats/Decimals to 6dp and normalising
  dates. BIRD's gold queries cast with `AS REAL` (float4, ~6 significant digits);
  a model writing the same maths with `::numeric` returns full Decimal precision
  and fails strict set equality despite being *numerically right*
  (`0.904908` vs `0.9049079754601227` — observed on q12). Report both: the gap
  between them is measurement artefact, not model error.
- **Soft-F1** — partial credit for nearly-right result sets, ported verbatim from
  BIRD's `evaluation_f1.py`.

**Exact Match is deliberately not reported.** The pipeline emits valid SQL worded
differently from gold, which EM penalises even when the answer is right.

## Modes

Same questions, same scoring, so the difference is attributable:

- `--mode model` — prompts Ollama directly with schema DDL + evidence. Raw model
  baseline, no SQLBot in the loop.
- `--mode pipeline` — runs through `LLMService` as the product does, with the
  question's database pinned as the datasource. Includes value linking,
  identifier check, skeleton few-shot, multi-candidate.

The gap between them is the honest answer to whether the agentic layer earns its
latency.

## Setup (already done on this box)

```bash
# 1. data — 800MB zip, 1GB dump
curl -sLO https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip
unzip -q minidev.zip

# 2. load. The PG dump drops all 75 tables from all 11 logical databases into one
#    flat `public` schema; loading it as-is would show every question the schema of
#    all 11 databases at once — a far harder schema-linking problem than the
#    published numbers were measured under. The 11 share no table names (verified:
#    75 distinct, 0 collisions), so they split unambiguously.
docker exec sqlbot psql -U root -d postgres \
  -c "CREATE ROLE xiaolongli NOLOGIN;" -c "CREATE DATABASE bird_dev OWNER root;"
docker exec -i sqlbot psql -U root -d bird_dev < minidev/MINIDEV_postgresql/BIRD_dev.sql
python3 backend/tests/bird_setup_schemas.py --tables minidev/MINIDEV/dev_tables.json

# 3. register 11 datasources (named bird__<db_id>; BIRD_DROP=1 removes them)
docker cp backend/tests/bird_register_datasources.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/bird_register_datasources.py"
```

Verify before trusting any score: all 150 gold queries must execute
(they do — checked 150/150).

## Running

```bash
docker cp backend/tests/bird_eval.py sqlbot:/tmp/
docker cp backend/tests/bird_mini_dev_150.json sqlbot:/tmp/

docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
    .venv/bin/python /tmp/bird_eval.py --mode model    --resume --out /tmp/bird_model.json"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
    .venv/bin/python /tmp/bird_eval.py --mode pipeline --resume --out /tmp/bird_pipeline.json"
```

`--resume` skips question_ids already in the output file, so an interrupted
overnight run picks up where it stopped.

**Timing on this box:** model ~95s/question (~4h), pipeline ~250s/question (~10h).
The host runs `OLLAMA_KEEP_ALIVE=0`, so the model unloads after every answer and
each question pays ~15s to reload it. `--mode model` sets `keep_alive` per request
to avoid that; `--mode pipeline` goes through SQLBot's own LLM client and cannot,
so it still pays the reload. Setting `OLLAMA_KEEP_ALIVE=30m` in
`/etc/systemd/system/ollama.service.d/override.conf` would save ~40min per pass —
at the cost of ~13GB VRAM held while idle (see [important.md](../important.md)).

## Results

Committed raw per-question output lives in
[backend/tests/bird_results/](../backend/tests/bird_results/). Recompute any row
below straight from those files — nothing here is hand-copied.

| File | Model | Harness flags | EX | EX (tol) | Soft-F1 | median |
|---|---|---|---:|---:|---:|---:|
| `bird_model.json` | `gpt-oss:20b` | none (baseline) | **40.7%** | 45.3% | 41.0 | 41 s |
| `bird_final_32b.json` | `qwen2.5-coder:32b` | `--descriptions --promptfix --repairs 1` | **52.0%** | 52.7% | 52.7 | 38 s |
| `bird_xiyan14b.json` | `XiYanSQL-QwenCoder-14B` | same | 43.3% | 44.7% | 41.8 | 24 s |

By difficulty (EX):

| Run | simple (44) | moderate (75) | challenging (31) |
|---|---:|---:|---:|
| baseline `gpt-oss:20b` | 66% | 35% | 19% |
| tuned `qwen2.5-coder:32b` | 64% | **52%** | **35%** |
| `XiYanSQL-QwenCoder-14B` | 68% | 35% | 29% |

**What this does and does not show.** The 40.7% → 52.0% gap confounds two
changes made together: the model swap *and* the harness flags (BIRD's column
descriptions, the v2 prompt, and one repair pass — which fired on 27 of the 150
questions). It is not a clean measurement of either one alone.

What survives the ±8pp confidence interval is the *shape*: the gain is
concentrated in moderate (+17pp) and challenging (+16pp) questions while simple
questions are flat-to-slightly-down. That is the profile of better schema
context, not of a model that is uniformly stronger. The 43.3% vs 40.7% gap
between XiYanSQL-14B and the baseline is inside the interval and should be read
as "no measured difference".

`--mode pipeline` has no completed 150-question run — only a 12-question partial
at ~250 s/question, which is why the full-pipeline-versus-raw-model comparison
the [Modes](#modes) section describes is still open.

## Caveats

- BIRD tests NL→SQL over clean relational schemas. It does **not** exercise Excel
  ingestion, header/island detection, or cross-file decomposition — arguably this
  product's differentiator. Keep the in-house bank as the product regression.
- BIRD ships per-column descriptions that this harness does not load; only DDL +
  `evidence` reach the model. Published numbers that use those descriptions have
  a small advantage over ours.
- The benchmark datasources are real rows in `core_datasource` and are visible in
  the web UI. Remove them with `BIRD_DROP=1`.
