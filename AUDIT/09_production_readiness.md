# AUDIT / 09 — Production Readiness Summary

State after the approved Phase 5 P1 batch. Branch `audit/phase5-fixes`.

---

## 1. Remaining P0 issues

**None.** No P0 (crash / data-corruption) issue was found in the audit, and none
has been introduced.

## 2. Remaining P1 issues

| ID | Issue | Status | Why still open |
|---|---|---|---|
| **D-05** | `is_normal_user() → id != 1` lets user id 1 bypass all row/column permissions | **DEFERRED by decision** → tracked as **D-37** | Option 1 (fanout gate) shipped in `2af61e7`. Option 2 (retarget to `isAdmin`) requires auditing every `UserInfoDTO`/`BaseUserDTO` construction site incl. MCP and assistant paths, per your instruction |
| **D-06** | Path traversal via `filePath` on `/reparseExcel` and `/importToDb` | **OPEN** | Not in the approved batch. The confinement helper it needs (`common/utils/paths.py`) already exists from D-07, so the fix is now small |
| **D-10** | Soft-F1 is order-sensitive → ±0.7 pp run-to-run noise | **OPEN** | Harness fix (sort both row lists before matching). One line |
| **D-11** | Strict EX flips on float aggregates (2 of 5 runs measured) | **WONTFIX by decision** | Official BIRD metric compatibility retained, as instructed. Documented; EX-tolerant adopted as internal KPI |

**2 actionable P1s remain: D-06 and D-10.** Both are small and well-specified.

## 3. Accuracy delta

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| **BIRD EX (official)** | 52.0 % | **55.3 %** | **+3.3 pp** |
| **BIRD EX (tolerant)** | 52.7 % | **56.0 %** | **+3.3 pp** |
| Exact Match | 5.3 % | 5.3 % | 0 (not a meaningful metric here) |
| Regressions | — | **0** | — |
| Replication (gpt-oss:20b) | 40.7 % | 43.3 % | +2.7 pp, 0 regressions |

Per difficulty: simple **+6.8 pp**, moderate **+2.7 pp**, challenging +0.0 pp.
Cost: 1.5 ms/query CPU, **zero** extra model calls or tokens.

Two further changes affect generation but are not separately quantified:
**D-09** (answer-object selection) is a no-op unless a reply contains ≥2 JSON
objects, and **D-05 opt 1** enables fanout for ordinary users — a capability
change BIRD cannot measure, since it has no multi-file questions.

## 4. Security delta

| Issue | Before | After |
|---|---|---|
| **D-01** | Any workspace's spreadsheet rows could surface as another's answer | Vector search filtered to the caller's readable tables; **fails closed** |
| **D-02** | Row-restricted users saw every row in prompt samples and value hints | Row rules applied on both paths; cache key includes the filter; **fails closed** |
| **D-03** | Any user could run LLM analysis on any user's result set | Owner-scoped |
| **D-04** | Any user could read another's question text and datasource history | Owner-scoped |
| **D-07** | Arbitrary `*_error.xlsx` downloadable; 7 guards returned 500 not 4xx | Path confined; all guards return their intended status |
| **D-08** | Spreadsheet headers injected SQL on **every chat question** | Identifiers escaped through one function across all 14 dialects |
| **D-13** | Value cache served privileged values to restricted users | Closed (value cache); relations cache still open |

**6 of 8 P1 security issues closed.** Remaining: **D-06** (traversal, ws_admin
gated) and **D-37** (id-1 permission bypass, deferred by decision).

**No security control was weakened.** Every change is neutral or tightening, and
every new gate fails closed.

## 5. Technical debt delta

| | Before | After |
|---|---:|---:|
| Ruff violations (16 touched files) | 377 | **367** (−10) |
| Mypy errors (16 touched files) | 841 | 843 (+2) |
| Import cycles blocking standalone import | 1 (4 modules) | **0** |
| Duplicate identifier-quoting implementations | 4 | **3** (one consolidated) |
| Bare `except:` | 5 | **4** |
| Hardcoded values removed | — | 2 new settings (`AGENTIC_NULLS_LAST_*`); no new hardcoding introduced |

Debt **reduced** on every axis except mypy, where +7 additions of two
pre-existing error classes were partly offset by −5 removals.

## 6. Test count and coverage delta

| | Before | After | Delta |
|---|---:|---:|---:|
| Passing (root) | 272 | **392** | **+120** |
| Passing (backend) | 28 | 28 | 0 |
| Test files | 37 | **44** | +7 |
| **Security tests** | **0** | **~70** | **+70** |

New files: `test_authz_chat_records.py`, `test_module_imports.py`,
`test_path_confinement.py`, `test_row_rag_tenant_isolation.py`,
`test_row_permission_context.py`, `test_identifier_quoting.py`,
`test_extract_nested_json.py`, `test_fanout_gate.py`, `test_nulls_last.py`.

The audit's **C-06** finding — "no security tests of any kind" — is now closed
for the fixed issues. Coverage gaps **C-01 to C-05** (llm.py, curd/chat.py,
db.py, auth.py, permission.py have no automated tests) are partly addressed:
`curd/chat.py` and `permission.py` now have some, and D-36 made three of those
modules importable and therefore testable at all.

## 7. Benchmark status

| | |
|---|---|
| Eval-path integrity | ✅ Committed baseline re-scores identically after all changes |
| L-A validated | ✅ +3.3 pp, 0 regressions, replicated on a second baseline |
| Shipping pipeline benchmarked | ❌ **Still never run** (`--finish data`) — audit **E-04** |
| Product-surface benchmark | ❌ **Does not exist** — Excel ingestion, fanout, permissions, multi-turn, charting all unmeasured |

## 8. Production readiness

**~70 %** — up from an estimated ~45 % at the start of Phase 5.

The number is a judgement, so here is what it is made of.

**What is ready**
- No P0 issues; 6 of 8 P1 security issues closed, all fail-closed
- Deterministic test suite, zero flakes across 3× runs, +120 tests
- Clean startup, healthy container, no regressions on any gate
- Measured accuracy improvement with zero regressions

**What blocks 100 %**

| Blocker | Severity |
|---|---|
| **D-06** path traversal (ws_admin) still open | P1 |
| **D-37** id-1 permission bypass deferred | P1 (by decision) |
| **D-10** Soft-F1 non-determinism still open | P1 |
| **The shipping pipeline configuration has never been benchmarked** (E-04) | P1 measurement |
| Two 200-thread pools on a single-worker uvicorn (**D-14**) — unmeasured under load | P2 scalability |
| Unbounded result sets persisted to a TEXT column (**D-16**) | P2 |
| No dependency lockfile; default PyPI index is **plain HTTP**; `sqlbot-xpack` from testpypi | P2 supply chain |
| `sqlbot_xpack` unauditable — closed source, inside the auth perimeter | Unknown |
| No product-surface accuracy benchmark | P1 measurement |
| ~25 P2 and 15 P3 issues untouched | mixed |

**Per your own criterion — "not production-ready unless all P0 and P1 issues are
resolved" — the system is NOT production-ready.** Two actionable P1s (D-06,
D-10) plus one deferred by decision (D-37) remain, and the configuration that
actually ships has never been benchmarked.

That said, the **security posture has improved materially**: cross-tenant data
exposure, two IDORs, a traversal and a per-question SQL injection are closed and
regression-tested.

## 9. Recommended next steps

1. **D-06** — small now that `common/utils/paths.py` exists (~30 min)
2. **D-10** — one line in the harness, unlocks trustworthy Soft-F1
3. **E-04 / L-D** — run `--mode pipeline --finish data` once (~10 h GPU). This is
   the highest-value remaining item: every agentic-layer decision currently
   rests on a measurement of a different configuration
4. **D-37** — the `UserInfoDTO` construction-site audit, then option 2
5. **D-14** — load-test the thread ceiling before any real concurrency
6. Give the in-house 55-question bank gold SQL and set equality, so the
   product's own differentiators become measurable
