# AUDIT / 09 — Production Readiness Summary

State after **all** approved Phase 5 batches (Groups A–G).
Branch `audit/phase5-fixes`. Safety tag `pre-rewrite-phase5` intact.
Supersedes the interim version written after the P1 batch.

---

## 1. Remaining P0 issues

**None.** No P0 (crash / data-corruption) issue was found in the audit, and none
has been introduced.

## 2. Remaining P1 issues

| ID | Issue | Status |
|---|---|---|
| **D-37** | `is_normal_user() → id != 1` lets user id 1 bypass all row/column permissions | ✅ **FIXED** (`86079bd` + `33bb500`) — retargeted to `isAdmin` after the construction-site audit. Tightens two principals, loosens none, provably cannot move a benchmark number |
| **D-11** | Strict EX flips on float aggregates (2 of 5 runs measured) | **WONTFIX by your decision** — official BIRD metric compatibility retained. EX-tolerant adopted as the internal KPI |
| **D-19** | 24 tests fail on a path assumption | ✅ **FIXED** (`20bb1f0`) — suite is green: **634 passed, 0 failed**, deterministic |
| **E-04** | The shipping pipeline configuration had never been benchmarked | **NOT STARTED** — held; see §7 |

Everything else raised as P1 is closed. **D-05 / D-06 / D-10 are now fixed**
(D-06 in the path-confinement batch, D-10 in Group A, D-05 option 1 as approved).

## 3. Security delta

| Issue | Before | After |
|---|---|---|
| **D-01** | Any workspace's spreadsheet rows could surface as another's answer | Vector search filtered to the caller's readable tables; **fails closed** |
| **D-02** | Row-restricted users saw every row in prompt samples and value hints | Row rules applied on both paths; cache key includes the filter; **fails closed** |
| **D-03 / D-04** | Two IDORs: any user could run analysis on, or read, another user's records | Owner-scoped |
| **D-06 / D-07 / D-38** | Path traversal on `filePath`, on `*_error.xlsx`, and on 4 upload endpoints writing via `filename.split('.')[0]` | All routed through `common/utils/paths.py`; a repo-wide write audit found no unprotected user-controlled write remaining |
| **D-08** | Spreadsheet headers injected SQL on **every chat question** | Identifiers escaped through one function across all 14 dialects |
| **D-13** | Value **and** relations caches served privileged data across permission boundaries | Both cache keys now carry the permission dimension |
| **D-25** | `SELECT 1; SELECT pg_sleep(30)` passed the read-only gate | Stacked statements rejected in every dialect |

**No security control was weakened.** Every change is neutral or tightening, and
every new gate fails closed. **D-37 has since been closed too** (`86079bd` +
`33bb500`), after the construction-site audit you required: it tightens two
principals and loosens none.

## 4. Correctness and resource delta

| | Before | After |
|---|---|---|
| Bare `except:` in `backend/` | 5 | **0** |
| Identifier-quoting implementations | 4 | **2** (one canonical + one delegating wrapper) |
| Module-global threads on a `--workers 1` uvicorn | **400** (2 × 200, against `PG_POOL_SIZE=20`) | **96**, and configurable |
| Rows serialised into a `Text` column | unbounded whenever `GENERATE_SQL_QUERY_LIMIT_ENABLED=false` (the shipped value) | capped at `AGENTIC_PERSISTED_ROW_CAP`, unconditionally |
| Misconfigurations that boot silently wrong | 3 (`CACHE_TYPE=redis` w/o URL, dev CORS origin in prod, placeholder image host) | **0** — two are boot failures, one a startup warning |
| Embedding-dimension mismatch | every insert failed, silently | boot-time check with remediation text |

## 5. Technical debt delta

| | Before | After |
|---|---:|---:|
| Passing tests (root) | 272 | **634**, 0 failures |
| Test files | 37 | **53** |
| Security tests | **0** | ~90 across 11 files |
| Dead modules | 5 | **1** (`http_utils.py` — kept: xpack imports it) |
| Dead code deleted | — | ~570 LOC incl. a 65-line commented endpoint |
| Per-file ruff / mypy | baseline | **at or below baseline on every file touched** |

## 6. What the implementation proved wrong about the audit

Five register entries did not survive contact with the code and are corrected
in `ISSUES.md`: **D-32** (`http_utils` is load-bearing), **D-33** (`terms` *is*
created by migration 002), **D-15** (the stale LLM client was never *served*),
**H-13** (no duplication exists), **D-26** (quote-doubling was already correct).

The important one is D-32, because it is methodological:

> `sqlbot_xpack` is Cython-compiled and its import strings are not recoverable
> from the `.so` files. **No grep of this repository can prove a `common/`
> module unused.** Only an empirical probe that force-imports all 50 xpack
> submodules can. Deleting `http_utils.py` on grep evidence was a hard boot
> failure; the probe is preserved in `tests/test_group_f_deadcode.py`.

## 7. Benchmark status

| | |
|---|---|
| Eval-path integrity | ✅ Committed baseline re-scores identically after all changes |
| Soft-F1 determinism (D-10 / E-01) | ✅ Fixed — rows sorted before matching |
| Result provenance (E-06) | ✅ Added — model, flags, git rev, timestamp |
| L-A validated | ✅ +3.3 pp, 0 regressions, replicated on a second baseline |
| **E-04 shipping-config run** | ⏸️ **HELD** — see the D-40/D-41 note below |
| Product-surface benchmark | ❌ **Does not exist** — Excel ingestion, fanout, permissions, multi-turn, charting all unmeasured |

**Do not read partial benchmark numbers as a verdict.** At n=29 the in-flight
gpt-oss run showed churn in *both* directions against all three stored gpt-oss
baselines (4 gained / 4 lost against two of them). That is consistent with the
documented non-determinism of this pipeline — SQL generation runs at temperature
0.6 — not evidence of a regression or an improvement. The comparison that counts
is full-run vs full-run with McNemar, at n=150.

The three smoke questions run immediately after the final commit matched the
same-model baseline **exactly** (q12/q27/q32 are all WRONG in
`bird_32b20k_150.json` too). That is the evidence that these changes did not
break the pipeline.

### The 500-question runs

Two prerequisites are missing, neither resolvable from the code:

1. **`mini_dev_postgresql.json` (500 questions) is not on disk.** Only the
   sampled 150-subset is. `bird_make_subset.py` built it from that file via
   `--src`, seeded, preserving the 30/50/20 difficulty split and per-database
   proportions — so the subset is representative by construction.
2. **GPU time exceeds the licence window.** At measured rates (gpt-oss ≈ 173 s/q,
   qwen20k ≈ 193 s/q), 500 questions on both models is **~40 h** against a vGPU
   licence expiring 2026-08-02 07:50 GMT.

Runs are resumable (`bird_run_supervised.sh` + `--resume`), so a licence lapse
mid-run is recoverable rather than fatal.

## 8. Production readiness verdict

### NOT PRODUCTION READY — one gate remains

Against your stated criterion — *"only declare PRODUCTION READY if every gate
passes"* — the system does not qualify. **One gate fails**, down from three:
D-37 and D-19 are now closed.

| Blocker | Severity | Why it blocks |
|---|---|---|
| **E-04** benchmark incomplete | P1 measurement | Every agentic-layer decision still rests on a measurement of a *different* configuration. **No post-fix run exists yet** — launches were held after verification found D-40/D-41 |

Additional, unchanged by this work:

- No dependency lockfile; the default PyPI index is **plain HTTP**;
  `sqlbot-xpack` — a hard runtime dependency imported at `main.py:4` — comes
  from **testpypi**, is closed source, and sits inside the auth perimeter
- No product-surface accuracy benchmark. Excel ingestion, header/island
  detection, cross-file fanout, row permissions, charting and multi-turn have
  **no accuracy measurement of any kind**. Two defects found in this audit
  (D-12, D-02) would have been caught immediately by one, and are invisible on
  BIRD
- ~25 P2 and 15 P3 issues untouched
- The shipping default model is `qwen2.5-coder:32b-20k` — the variant this
  project measured **worst** (35.3 % EX, against 52.0 % at full context)

### What is ready

No P0s. Cross-tenant data exposure, two IDORs, three classes of path traversal
and a per-question SQL injection are closed and regression-tested. **634 passing
tests, 0 failures**, including ~90 security tests where there were none. Debt down on every
axis. Misconfiguration now fails at boot instead of running silently wrong.

## 9. Recommended next steps, in order

1. **D-19** — unbreak the 24 tests so the suite becomes a usable gate
2. **E-04** — let the in-flight runs finish; compare full-run vs full-run
3. **D-37** — the `UserInfoDTO` construction-site audit, then option 2
4. **Change the shipping default model** off the 20k variant, or document why a
   −16.7 pp configuration ships by default
5. **Pin dependencies**, move the index to HTTPS, get `sqlbot-xpack` off testpypi
6. Give the in-house 55-question bank gold SQL and set equality, so the
   product's own differentiators become measurable at all


---

## 10. Why the benchmark has not run yet (added 2026-08-01)

Verification before launch found two defects that would have invalidated the
results, so the runs were stopped rather than allowed to finish:

- **D-40** — `--model` is ignored in pipeline mode, but was written into
  provenance anyway. A run launched `--model gpt-oss:20b` was actually running
  `qwen2.5-coder:32b-20k` and would have produced a file claiming otherwise.
  This also means the three historical `*_gptoss_*.json` pipeline files (bare
  lists, no provenance) cannot be confirmed to be gpt-oss at all.
- **D-41** — killing the supervisor on the host orphans the harness inside the
  container. Relaunches stacked **five concurrent processes** on one GPU and one
  output file, which is what froze the run on question 1.

Both are fixed. The pipeline model is selected by the `ai_model` row whose
`default_model` is true, so a two-model comparison requires flipping that row
between runs — approved, not yet performed.
