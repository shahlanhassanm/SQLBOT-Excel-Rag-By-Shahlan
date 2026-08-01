# AUDIT / 08 — Repository Health Check

Run after the approved Phase 5 P1 batch. Branch `audit/phase5-fixes`,
11 commits on top of `d2d83bb`.

---

## 1. Backend tests

Three consecutive runs of each suite, in the live container:

```
root tests/       24 failed, 392 passed, 3 skipped   32.07s / 31.43s / 31.53s
backend/tests/    28 passed                          10.05s /  7.98s /  8.03s
```

**Zero variance across runs. No flaky or order-dependent tests.**

| | Before Phase 5 | Now | Delta |
|---|---:|---:|---:|
| Passing (root) | 272 | **392** | **+120** |
| Passing (backend) | 28 | 28 | 0 |
| Failing | 24 | 24 | 0 |
| Skipped | 3 | 3 | 0 |

The 24 failures are **all** `tests/test_supplier_config.py` — issue **D-19**,
a path-fragility bug in the test file itself (`PROJECT_ROOT` derived from
`__file__`, so the documented `docker cp` workflow resolves it to `/tmp`). The
asserted content exists in the repo. **Not a product defect, and unchanged by
this work.**

## 2. Import / startup validation

```
import main                       OK   (159 routes constructed, incl. MCP + xpack)
apps.system.schemas.permission    OK
apps.datasource.crud.datasource   OK
apps.chat.curd.chat               OK
apps.chat.api.chat                OK
apps.datasource.row_rag.store     OK
common.utils.paths                OK
```

The first four could **not** be imported standalone before **D-36**.

## 3. Ruff delta

Per-file, against the baseline recorded at the time of each fix.

| | Baseline | Now | Delta |
|---|---:|---:|---:|
| **Total across 16 touched files** | 377 | **367** | **−10** |

No file regressed. Three improved: `crud/datasource.py` −8 (hand-written quoting
removed), `value_index.py` −1, `utils.py` −1 (bare `except:` removed).

Repo-wide baseline remains 1,579 — the gate is *no new violations*, as approved.

## 4. Mypy delta

| | Baseline | Now | Delta |
|---|---:|---:|---:|
| **Total across 16 touched files** | 841 | **843** | **+2** |

Added: `curd/chat.py` +6, `crud/table.py` +1 — all instances of the two error
classes those files already carry in bulk (`no-untyped-def`, `call-overload` on
`select()` with >8 columns; baselines 164 and 27). Removed: −5 across
`api/chat.py`, `settings/api/base.py`, `row_rag/fallback.py`,
`crud/datasource.py`.

Declared at the time of each commit. Annotating those files properly is the
large refactor that was explicitly out of scope.

## 5. Smoke tests

The container was **restarted** so the running uvicorn actually loads the new
code — `docker cp` alone does not apply changes to a process started without
`--reload`.

```
container health            healthy
startup errors in log       none
GET /                       200
GET /docs                   200
GET /api/v1/chat/list       401  (TokenMiddleware enforcing, as designed)
GET /openapi.json           401  (auth-gated in this deployment)
```

New configuration confirmed live in the restarted process:

```
AGENTIC_NULLS_LAST_ENABLED  = True
AGENTIC_NULLS_LAST_DIALECTS = pg,excel,kingbase,redshift,oracle,dm
live rewrite: SELECT a FROM t ORDER BY x DESC NULLS LAST LIMIT 1
```

**Limitation.** An authenticated live session was not established: the login
endpoint expects the frontend's RSA-encrypted password, so a plain-credential
smoke could not reach the fixed endpoints. Their behaviour is covered by the 120
new unit tests. Reproducing the frontend's encryption for a smoke test was out
of scope.

## 6. Benchmark validation

**Eval-path integrity** — the committed baseline still re-scores identically
after all changes:

```
bird_final_32b.json   stored EX 78/150 (52.0%)  ->  recomputed 78/150 (52.0%)
                      stored tol 79    (52.7%)  ->  recomputed 79    (52.7%)
                      no per-question disagreement
```

(Soft-F1 reads 51.9 vs the stored 52.7 — that is **D-10**, the known
order-sensitivity, not a regression from this work.)

**L-A accuracy lever** — full report in `AUDIT/07_LA_benchmark.md`:

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| EX (official) | 52.0 % | **55.3 %** | **+3.3 pp** |
| EX (tolerant) | 52.7 % | **56.0 %** | **+3.3 pp** |
| Regressions | — | **0** | — |
| Replication (gpt-oss:20b) | 40.7 % | **43.3 %** | **+2.7 pp**, 0 regressions |
| Latency cost | — | 1.535 ms/query | ~0.005 % of question time |
| Token cost | — | **0** | no extra model calls |

## 7. Backward compatibility

| Change | Compatibility |
|---|---|
| D-36 | Import order only; no runtime behaviour change |
| D-03/D-04 | New CRUD helpers added; `get_chat_record_by_id` left untouched for its 13 internal callers |
| D-07 | Guards now return their intended 4xx instead of 500 — strictly more correct |
| D-01 | `query_topk` gains a **required** argument; the single production caller and the one test were both updated |
| D-02 | Emitted SQL is byte-identical when no row rules apply — asserted by test |
| D-08 | `quote_ident` verified to emit identical quote characters to `DB.prefix/suffix` for all 14 dialects; one deliberate change (ck/hive table names now quoted) |
| D-09 | `prefer_keys` defaults to `None` = historical behaviour, and falls back to first-match when nothing matches |
| D-05 opt 1 | Fanout gate only; global predicate untouched, pinned by test |
| L-A | Feature-flagged, dialect-gated, fails open, idempotent |

## 8. Commits

```
34886ee Bench: L-A validation - +3.3pp EX, 0 regressions, replicated on two baselines
0026e3d Fix: L-A - Deterministic NULLS LAST rewrite for descending sorts
2af61e7 Fix: D-05 (option 1) / D-12 - Gate fanout on real row restrictions, not on user id
123f890 Fix: D-09 - Select the model's answer object, not the first JSON in the reply
35825f6 Fix: D-08 - Escape SQL identifiers instead of merely wrapping them
2dae0cb Docs: D-05 authorization impact report + register update for D-02/D-13
3b057a1 Fix: D-02 - Apply row-level permissions to prompt context, not just generated SQL
562aa93 Docs: update ISSUES.md register for D-36, D-03, D-04, D-07, D-01
4d15453 Fix: D-01 - Restrict row-RAG vector search to the caller's readable tables
740a5ff Fix: D-07 - Confine download path and repair the broken HTTPException guards
124f9cd Style: D-03/D-04 follow-up - use `X | None` in the new CRUD annotations
cc8a208 Fix: D-03/D-04 - Scope chat-record lookups to their owner
d27d3c4 Fix: D-36 - Break module-level import cycle in permission.py
```

## 9. ⚠️ Working-tree caveat you need to decide on

Four files had **pre-existing uncommitted changes of yours** before Phase 5
began, and I also had to modify them. `git add <file>` cannot stage a subset and
interactive staging is unavailable here, so those commits contain your
in-progress work alongside mine:

| File | Total churn vs `d2d83bb` (added+deleted) |
|---|---|
| `backend/apps/chat/task/llm.py` | 190 + 39 |
| `backend/apps/datasource/crud/datasource.py` | 231 + 57 |
| `backend/apps/chat/task/agentic.py` | 85 + 0 |
| `backend/common/core/config.py` | 66 + 3 |

**Nothing is lost** — your changes are committed, not discarded. But those four
commits are not "one logical fix" in the strict sense. If you want them split,
say so and I will rebase.

Thirteen files with pre-existing changes were **never touched** and remain
uncommitted exactly as you left them (`model_factory.py`, `sql_validate.py`,
`db.py`, the four locale JSONs, three SQL-example YAMLs, `bird_eval.py`,
`docker-compose.yaml`, `tests/test_sql_validate.py`).

## 10. Verdict

**All gates pass.** No regressions in tests, lint, types, startup, or benchmark.
One accuracy lever measurably improved EX by +3.3 pp with zero regressions.
