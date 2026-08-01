# AUDIT / 10 — Phase 5 Handoff

**Written immediately before context compaction. This file is the source of
truth for resuming work.** Everything needed to continue without re-auditing is
here or in the files it references.

Date: 2026-08-01 · Repo: `/home/iguser/Downloads/SQLBOT-Excel-Rag-main`

---

## 1. Branch State

| | |
|---|---|
| **Current branch** | `audit/phase5-fixes` |
| **Base commit** | `d2d83bb` (SQLBot Excel RAG: agentic text-to-SQL pipeline on top of dataease/SQLBot) |
| **Completed range** | `d2d83bb..e243141` — **16 commits** (history rewritten WIP-first 2026-08-01) |
| **Pre-rewrite HEAD** | `d2a554d3f4363ee4e503d40207067fc73b1492e7` |
| **Safety tag** | `pre-rewrite-phase5` -> `d2a554d` (rollback: `git update-ref refs/heads/audit/phase5-fixes pre-rewrite-phase5`) |
| **Tree hash (unchanged by rewrite)** | `a48d00a1324d4186d1fc6937f42bffe68d8c340b` |
| **Files changed** | 33 (7,296 insertions, 153 deletions) |
| **Container** | `sqlbot:local`, restarted 2026-08-01 so uvicorn runs the new code; healthy |

### Remaining uncommitted files — 13, all pre-existing user work, never touched by me

```
backend/apps/ai_model/model_factory.py
backend/apps/chat/task/sql_validate.py
backend/apps/db/db.py
backend/locales/en.json
backend/locales/ko-KR.json
backend/locales/zh-CN.json
backend/locales/zh-TW.json
backend/templates/sql_examples/AWS_Redshift.yaml
backend/templates/sql_examples/Kingbase.yaml
backend/templates/sql_examples/PostgreSQL.yaml
backend/tests/bird_eval.py
docker-compose.yaml
tests/test_sql_validate.py
```

### History rewrite — old SHA -> new SHA (2026-08-01, WIP-first)

Your pre-existing work is now isolated in `657fad3` at the BASE of the branch,
so every fix commit below contains only its own change.

| Old | New | Commit |
|---|---|---|
| — | `657fad3` | **WIP: Preserve pre-existing local changes** |
| `d27d3c4` | `747bf5f` | Fix: D-36 |
| `cc8a208` | `b74b035` | Fix: D-03/D-04 |
| `124f9cd` | `c47c36f` | Style: D-03/D-04 follow-up |
| `740a5ff` | `ecd800b` | Fix: D-07 |
| `4d15453` | `af936f2` | Fix: D-01 |
| `562aa93` | `980f649` | Docs: register |
| `3b057a1` | `d620b8c` | Fix: D-02 |
| `2dae0cb` | `ae5a223` | Docs: D-05 report |
| `35825f6` | `aa681b5` | Fix: D-08 |
| `123f890` | `e18eca4` | Fix: D-09 |
| `2af61e7` | `9f06449` | Fix: D-05 option 1 / D-12 |
| `0026e3d` | `2cad175` | Fix: L-A |
| `34886ee` | `7838ad0` | Bench: L-A validation |
| `d98d5f5` | `8200b3a` | Docs: health check + readiness |
| `d2a554d` | `e243141` | Docs: handoff + harness |

Verified: tree hash identical, `git diff d2a554d e243141` empty, 392 tests
passing, 159 routes, benchmark artifacts intact. **No implementation logic was
rewritten** — every fix commit reuses its original blobs.

**Why WIP-first rather than at the tip:** D-02 modifies
`get_tables_sample_data()`, whose `TABLE_SAMPLE_TOTAL_CHAR_BUDGET` logic exists
only in the pre-existing work (0 occurrences at `d2d83bb`, 2 in it), and L-A's
`llm.py` hunks conflict 5 ways when rebased off it. Tip placement would have
required rewriting benchmarked code.

### ⚠️ HISTORICAL (pre-rewrite) — files that carried pre-existing user changes

`git add <file>` cannot stage a subset and interactive staging is unavailable
here, so these four commits contain the user's in-progress work alongside the
fixes. **Nothing lost.** Total churn vs `d2d83bb` (added+deleted):

| File | Churn | Appears in |
|---|---|---|
| `backend/apps/chat/task/llm.py` | 190 + 39 | `4d15453`, `123f890`, `2af61e7`, `0026e3d` |
| `backend/apps/datasource/crud/datasource.py` | 231 + 57 | `3b057a1`, `35825f6` |
| `backend/apps/chat/task/agentic.py` | 85 + 0 | `0026e3d` |
| `backend/common/core/config.py` | 66 + 3 | `0026e3d` |

**→ This is what the requested post-compaction rebase must separate. See §8.**

### Backend files I authored or modified

New: `backend/common/utils/paths.py`
Modified: `apps/system/schemas/permission.py`, `apps/chat/api/chat.py`,
`apps/chat/curd/chat.py`, `apps/settings/api/base.py`,
`apps/system/crud/user_excel.py`, `apps/datasource/row_rag/store.py`,
`apps/datasource/row_rag/fallback.py`, `apps/datasource/crud/table.py`,
`apps/datasource/crud/permission.py`, `apps/datasource/value_index.py`,
`apps/datasource/crud/datasource.py`, `common/utils/utils.py`,
`apps/chat/task/agentic.py`, `apps/chat/task/llm.py`, `common/core/config.py`,
`backend/tests/test_row_rag_e2e.py`

New test files (9): `tests/test_authz_chat_records.py`,
`test_module_imports.py`, `test_path_confinement.py`,
`test_row_rag_tenant_isolation.py`, `test_row_permission_context.py`,
`test_identifier_quoting.py`, `test_extract_nested_json.py`,
`test_fanout_gate.py`, `test_nulls_last.py`

---

## 2. Validation Harness

**Persisted at `AUDIT/tools/` — use these, do not recreate.**

| Tool | Purpose |
|---|---|
| `AUDIT/tools/validate.sh <file>...` | Syncs files into the container, per-file ruff + mypy, `import main` smoke, both suites |
| `AUDIT/tools/rescore.py <results.json>...` | Re-executes stored BIRD predictions, recomputes EX / EX-tol / Soft-F1, reports per-question disagreement |
| `AUDIT/tools/bench_la.py <in.json> <out.json>` | Applies a shipped SQL transform to stored predictions and re-scores — the lever-pricing method |

### Gates (approved substitutions — these remain in force)

| Gate | Policy |
|---|---|
| **Lint** | **No new ruff violations per file** vs baseline. Repo-wide baseline is **1,579** and has never passed; a clean repo is not the gate. |
| **Type check** | **No new mypy errors per file** vs baseline. `mypy --strict` is configured but flags even fully-annotated modules; repo-wide compliance is not the gate. |
| **Build** | Backend-only: `import main` succeeds and constructs **159 routes** (incl. MCP + `sqlbot_xpack`). Frontend build is **not runnable here** — no `node`/`npm`, no `frontend/node_modules`. If frontend changes land, run the real build then. |
| **Benchmark** | Only for changes that can affect SQL generation, retrieval, ranking, prompting, execution or evaluation. Security / CRUD / path / infra fixes do **not** require a run. |
| **Regression** | Any regression → **stop immediately**, report root cause, files, options. Do not continue until resolved. |

### Ruff baseline policy — critical detail

Ruff 0.15 changed its output format. **Must use `--output-format=concise`** and
count `grep -c ':[0-9][0-9]*:[0-9][0-9]*:'`. The old grep pattern silently
reported **false zeros** and hid two real violations I introduced.

### Baseline selection policy

- Files with **no** pre-existing user edits → baseline is `d2d83bb`.
- Files **with** pre-existing user edits (`llm.py`, `crud/datasource.py`,
  `agentic.py`, `config.py`) → baseline is the **commit immediately before my
  change to that file**, or verify by **hunk-line-range attribution** (confirm no
  finding falls inside the added lines). Using `d2d83bb` for these wrongly
  attributes the user's work to the fix.

### Startup validation method

`docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"`
plus standalone import of the six previously-uncyclable modules.
**`docker cp` alone does not apply changes** — uvicorn runs without `--reload`,
so a `docker restart sqlbot` is required for any live smoke test.

**Known limitation:** an authenticated live smoke is not achievable — the login
endpoint expects the frontend's RSA-encrypted password. Endpoint behaviour is
covered by unit tests instead.

---

## 3. Completed Issues

| ID | Commit | Status | Regression test added |
|---|---|---|---|
| **D-36** *(new, found in Phase 5)* | `d27d3c4` | ✅ FIXED | `tests/test_module_imports.py` — imports each module in a **fresh interpreter** (once any imports, the cycle resolves and the bug hides) |
| **D-03** | `cc8a208` | ✅ FIXED | `tests/test_authz_chat_records.py` |
| **D-04** | `cc8a208` | ✅ FIXED | `tests/test_authz_chat_records.py` |
| — | `124f9cd` | style follow-up | (lint delta → 0) |
| **D-07** | `740a5ff` | ✅ FIXED | `tests/test_path_confinement.py` — incl. sibling-prefix and check-order cases |
| **D-01** | `4d15453` | ✅ FIXED | `tests/test_row_rag_tenant_isolation.py` + leak assertion in `backend/tests/test_row_rag_e2e.py` |
| **D-02** | `3b057a1` | ✅ FIXED | `tests/test_row_permission_context.py` — per-dialect placement, Oracle/DM `AND` case, cache-key separation |
| **D-13** | `3b057a1` | ⚠️ **PARTIAL** | value cache closed; **relations cache still open** |
| **D-08** | `35825f6` | ✅ FIXED | `tests/test_identifier_quoting.py` — sqlglot parse-tree assertions + a test pinning that the *legacy* form was injectable |
| **D-09** | `123f890` | ✅ FIXED | `tests/test_extract_nested_json.py` — incl. backward-compat cases |
| **D-05 option 1** / **D-12** | `2af61e7` | ✅ FIXED | `tests/test_fanout_gate.py` — both directions, all 3 fail-closed branches, global predicate pinned |
| **L-A** | `0026e3d` + `34886ee` | ✅ FIXED + BENCHMARKED | `tests/test_nulls_last.py` — string-literal and `"desc"`-column cases |

Docs commits: `562aa93`, `2dae0cb`, `d98d5f5`.

---

## 4. Remaining Issues

### P0
**None.** None found in the audit; none introduced.

### P1 — resume order is exactly this

| ID | Issue | Note |
|---|---|---|
| **D-06** | Path traversal via `filePath` on `/reparseExcel` and `/importToDb` (`apps/datasource/api/datasource.py:584, 767`) | **Now small** — `common/utils/paths.py::safe_join` already exists from D-07. ws_admin gated. |
| **D-10** | Soft-F1 order-sensitive → ±0.7 pp run-to-run noise (`backend/tests/bird_eval.py:125-150`) | One line: sort both row lists before positional matching. **`bird_eval.py` is currently uncommitted user work — coordinate.** |
| **E-04** | **The shipping pipeline configuration has never been benchmarked.** `--finish sql` is the argparse default and *cancels* self-consistency, execution retries and the grader | Needs one `--mode pipeline --finish data` run, ~10 h GPU. Highest-value remaining item. |
| **D-37** *(new)* | D-05 option 2 — `is_normal_user() → id != 1` still lets user id 1 bypass all row/column permissions | **DEFERRED by decision.** Requires auditing every `UserInfoDTO`/`BaseUserDTO` construction site (incl. MCP `mcp.py:67`, assistant, embedded) **before** any change. `isAdmin` is a strict subset — retarget can only tighten. See `AUDIT/06_D05_authz_impact.md` §8. |
| **D-11** | Strict EX flips on float aggregates (2 of 5 runs measured) | **WONTFIX by decision** — official BIRD metric compatibility retained |

### P2 — ~25 open
Headline: **D-13** (relations cache, partial), **D-14** (two 200-thread pools on
`--workers 1`), **D-15** (LLM cache never invalidated), **D-16** (unbounded
result sets → TEXT column), **D-17** (fabricated header confidence), **D-18**
(`has_explicit_row_count` matches any small number), **D-19** (24 failing tests,
path fragility), **D-20** (14 silent swallows), **D-21** (4 bare `except:`
remain), **D-22** (duplicate result files), **D-23**–**D-26**, **E-06**–**E-11**,
coverage gaps **C-01**–**C-05**.

### P3 — 15 open
**D-27**–**D-34**, **X-01**–**X-07**. Dead code, stale scripts, no lockfiles,
plain-HTTP PyPI index, testpypi dependency, floating base image tag.

### Hardcoding — 19 groups, all open
`AUDIT/02_hardcoding.md`. **H-19** (collapse remaining 3 identifier-quoting
implementations) is now partly done by D-08.

---

## 5. Benchmark Baseline — **THIS IS THE NEW REFERENCE**

Supersedes the 52.0 % figure in `docs/BENCHMARK-BIRD.md`.

| Metric | Value |
|---|---|
| **EX (official)** | **55.3 %** (83/150) |
| **EX (tolerant)** | **56.0 %** (84/150) |
| **Exact Match** | **5.3 %** (8/150) — reported, not optimised; the harness deliberately does not use EM |
| **GPT-OSS:20b replication** | **43.3 %** (65/150), from 40.7 % |
| **Latency delta** | **+1.535 ms/query** (CPU only, ~0.005 % of a 30–120 s question) |
| **Token delta** | **0** input, **0** output, **0** extra LLM calls |

Derivation: baseline `bird_final_32b.json` 52.0 % + L-A (**5 fixed, 0
regressed**: q32, q50, q82, q879, q1122). Per difficulty: simple 63.6→**70.5 %**,
moderate 52.0→**54.7 %**, challenging 35.5→35.5 %.

Raw output committed:
`backend/tests/bird_results/bird_la_nullslast_32b.json`,
`bird_la_nullslast_gptoss.json`.

**Statistical note that must not be lost.** `bird_significance.py` prints
`McNemar exact p=0.0625 <- NOT distinguishable from noise`. With 5 discordant
pairs all one-directional, **0.0625 is the minimum achievable two-sided p at
that sample size** — a power floor, not evidence of no effect. One-sided
p = 0.0312. Deterministic transform + 0 regressions + independent replication.

**Pre-existing measurement noise still applies:** ±8 pp sampling interval at
n=150, plus ~±1 question of float-EX noise (**D-11**). Paired per-question
comparison is unaffected.

---

## 6. Repository Health

| | Before | After |
|---|---:|---:|
| **Tests passing (root)** | 272 | **392** (+120) |
| Tests passing (backend) | 28 | 28 |
| Tests failing | 24 | 24 (all **D-19**, path fragility, not a product defect) |
| Skipped | 3 | 3 (`MINIMAX_API_KEY not set`) |
| Test files | 37 | 44 |
| **Security tests** | **0** | **~70** |
| **Ruff** (16 touched files) | 377 | **367** (**−10**) |
| **Mypy** (16 touched files) | 841 | 843 (**+2**: +7 pre-existing classes, −5 removed) |
| Import cycles blocking standalone import | 1 (4 modules) | **0** |
| Bare `except:` | 5 | 4 |
| Duplicate quoting implementations | 4 | 3 |

**Flake check:** 3× runs of each suite, **zero variance**.
**Startup:** `import main` OK, 159 routes; container healthy after restart; no startup errors.
**Benchmark integrity:** committed baseline re-scores identically after all changes.

**Production readiness: ~70 %** (from ~45 %). **NOT production-ready** by the
stated criterion — two actionable P1s (D-06, D-10) plus deferred D-37, and E-04
means the shipping configuration has never been measured.

---

## 7. Outstanding Decisions — PRESERVED

1. **D-37 authorization redesign remains DEFERRED.** Do not modify
   `is_normal_user()` or any authorization semantics until every
   `UserInfoDTO`/`BaseUserDTO` construction site has been audited and the change
   explicitly approved. D-05 option 1 (fanout gate only) is shipped; option 2 is
   not.
2. **E-04 remains MANDATORY before production.** The shipping pipeline
   configuration (`--mode pipeline --finish data`) must be benchmarked.
3. **Soft-F1 reporting policy:** do not quote to one decimal place; it carries
   ~±0.7 pp of pure measurement noise until **D-10** is fixed. Use **EX-tolerant**
   as the primary internal KPI.
4. **Official BIRD metric remains UNCHANGED.** `calculate_ex` keeps exact set
   equality for leaderboard comparability. D-11 is documented, not fixed.
5. **Gate substitutions remain in force** (§2) for the remainder of Phase 5.

---

## 8. Next Implementation Order

**0. FIRST: rebase** to separate the user's unrelated in-progress changes from
the logical fix commits, keeping issue→commit mapping clean for review. Affects
the four files in §1. Requested explicitly; do this before new fixes.

Then:

1. **D-06** — path traversal; `safe_join` already exists
2. **D-10** — Soft-F1 sort; note `bird_eval.py` is uncommitted user work
3. **E-04** — `--mode pipeline --finish data` benchmark run (~10 h GPU)
4. **D-37** — only after a separate authorization review

## 9. Audit document index

| File | Contents |
|---|---|
| `AUDIT/00_inventory.md` | Full inventory, dependency graph, cycles, dead code, duplicates |
| `AUDIT/01_defects.md` | 35 defects with repro / fix / blast radius / catching test |
| `AUDIT/02_hardcoding.md` | 19 hardcoding groups + config-layer proposal |
| `AUDIT/03_eval_integrity.md` | Benchmark reproduction, E-01…E-12, per-category breakdown |
| `AUDIT/04_accuracy_plan.md` | Root-cause clusters C1–C7, levers L-A…L-J priced |
| `AUDIT/05_open_questions.md` | Q-01…Q-18, intent undetermined |
| `AUDIT/06_D05_authz_impact.md` | D-05 call-site trace, options, tests required |
| `AUDIT/07_LA_benchmark.md` | L-A validation in full |
| `AUDIT/08_health_check.md` | This phase's health check |
| `AUDIT/09_production_readiness.md` | Readiness breakdown |
| `AUDIT/ISSUES.md` | **Consolidated register — 90 items, statuses current** |
| `AUDIT/tools/` | validate.sh, rescore.py, bench_la.py |
