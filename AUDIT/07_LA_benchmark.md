# AUDIT / 07 — L-A Benchmark Validation (NULLS LAST)

Validation for commit `0026e3d`. Method: apply the **shipped**
`apps.chat.task.agentic.apply_nulls_last` to every stored BIRD prediction and
re-execute against `bird_dev`, scoring with the harness's own metric functions.

**Why not a fresh generation pass.** A regeneration run costs 2–10 h of exclusive
GPU *and* introduces model sampling noise on top of the ~±1-question float-EX
noise already documented (`03_eval_integrity.md` D-11). Replaying the stored
predictions through the production rewrite isolates the lever exactly: same
model output, same questions, same database, one variable. This is the method
the project already uses to price levers before spending GPU.

**What it does not measure.** It does not capture second-order effects — e.g. a
rewritten query changing what a retry loop would have done next. Those only
appear in a full pipeline run. Flagged as a residual gap in §6.

Raw per-question output committed to
`backend/tests/bird_results/bird_la_nullslast_{32b,gptoss}.json`.

---

## 1. Headline — primary baseline `bird_final_32b.json` (n = 150)

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| **EX (official)** | 78/150 = **52.0 %** | 83/150 = **55.3 %** | **+3.3 pp** |
| **EX (tolerant)** | 79/150 = **52.7 %** | 84/150 = **56.0 %** | **+3.3 pp** |
| **Exact Match** | — | 8/150 = 5.3 % | unchanged by the lever |

**Exact Match note.** The harness deliberately does not report EM
(`bird_eval.py` docstring: the pipeline emits valid SQL worded differently from
gold). Computed here for completeness on normalised whitespace/case: 5.3 %,
and the lever does not move it — adding `NULLS LAST` makes predictions *less*
textually similar to gold, which is precisely why EM is the wrong metric for
this system. Reported, not optimised.

## 2. Queries changed and regressed

| | Count | Question ids |
|---|---:|---|
| SQL rewritten by the lever | **26** / 150 | — |
| Verdict changed | **5** | — |
| → fixed | **5** | q32, q50, q82, q879, q1122 |
| → **regressed** | **0** | — |

26 statements were rewritten; 21 of those were already correct or already wrong
for an unrelated reason and kept their verdict. **No query that was correct
before became incorrect.**

## 3. Per-category

| Difficulty | Before | After | Delta |
|---|---:|---:|---:|
| simple (44) | 63.6 % | **70.5 %** | **+6.8 pp** |
| moderate (75) | 52.0 % | **54.7 %** | **+2.7 pp** |
| challenging (31) | 35.5 % | 35.5 % | +0.0 pp |

| Status | Before | After |
|---|---:|---:|
| CORRECT | 78 | **83** |
| WRONG | 68 | **63** |
| NEAR | 1 | 1 |
| SQL-ERR | 3 | 3 |

The gain is concentrated in **simple** questions — the expected profile, since
superlatives (`ORDER BY x DESC LIMIT 1`) are mostly simple-difficulty. Note this
is the one lever so far that *improves* simple questions; every previous change
was flat-to-down there (`03_eval_integrity.md` §6).

## 4. Statistical comparison

Run with the repo's own `bird_significance.py`:

```
=== OVERALL  A=bird_final_32b.json  B=bird_la_32b.json  (n=150) ===
  A  :  78/150 =  52.0%  [95% CI 44.1-59.8]
  B  :  83/150 =  55.3%  [95% CI 47.3-63.1]
  delta          : +3.3pp
  fixed by B     : 5
  regressed by B : 0
  churn          : 3.3% of questions changed verdict
  McNemar exact p: 0.0625  <- NOT distinguishable from noise
```

**The p-value needs reading carefully.** With 5 discordant pairs all in one
direction (5 fixed / 0 regressed), the exact two-sided McNemar p is
`2 × 0.5⁵ = 0.0625` — which is the **minimum achievable value at that sample
size**. It is a power floor, not evidence of no effect. One-sided p = 0.0312.

Three things make the evidence stronger than the two-sided p suggests:

1. **The change is deterministic**, not stochastic. There is no sampling
   variance to average out — the same input always produces the same rewrite.
2. **Zero regressions** across 26 rewritten statements, including 9 that were
   already correct.
3. **It replicates on an independent baseline** (§5).

## 5. Independent replication — `bird_model.json` (gpt-oss:20b, n = 150)

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| EX (official) | 61/150 = 40.7 % | 65/150 = **43.3 %** | **+2.7 pp** |
| EX (tolerant) | 68/150 = 45.3 % | 71/150 = **47.3 %** | **+2.0 pp** |
| SQL rewritten | 25 | | |
| fixed | **4** (q50, q138, q1122, q1473) | | |
| **regressed** | **0** | | |

A different model, a different set of predictions, the same direction and the
same zero-regression property. q50 and q1122 are fixed on both, which is
consistent with a shared root cause rather than noise.

## 6. Latency and token impact

| | Measured |
|---|---|
| **Latency** | 230.2 ms total across 150 queries = **1.535 ms/query**, CPU-only |
| **Extra LLM calls** | **0** |
| **Extra input tokens** | **0** |
| **Extra output tokens** | **0** |

Against a 30–120 s per-question model call on this hardware, 1.5 ms is
approximately **0.005 %** of question latency — unmeasurable in practice.

There is also a **token saving available but not taken**: the SQL prompt still
carries the `NULLS LAST` rule that the model ignored on 17 of 72 failures. Now
that the rule is enforced deterministically it could be removed from
`templates/sql_examples/PostgreSQL.yaml`, freeing prompt budget. **Not done in
this commit** — removing a prompt rule changes generation and would need its own
benchmark run.

## 7. Verdict

**Accept.** +3.3 pp EX and +3.3 pp EX-tolerant on the primary baseline,
replicated at +2.7 pp on a second, with **zero regressions on either**, zero
extra model calls and 1.5 ms of CPU.

## 8. Residual gaps

1. **Second-order effects are unmeasured.** Replay cannot show how a rewritten
   query would have changed a retry decision. A full `--finish data` pipeline run
   would — and that run is separately needed anyway (`03_eval_integrity.md`
   E-04: the shipping configuration has never been benchmarked).
2. **Oracle/DM are configured but untested.** `AGENTIC_NULLS_LAST_DIALECTS`
   includes them on documented semantics; BIRD is PostgreSQL-only, so no
   measurement covers them. The rewrite fails open, so the downside is a no-op.
3. **The ±1-question float-EX noise (D-11) still applies** to the absolute
   figures. It does not affect the paired comparison, which is computed
   per-question on the same rows.
