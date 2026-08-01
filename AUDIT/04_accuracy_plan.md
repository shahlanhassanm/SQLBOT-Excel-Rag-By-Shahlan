# AUDIT / 04 — Accuracy Improvement Plan (analysis only)

**No code was changed and nothing here was implemented.** Where a lever is
quoted with a number, that number was **measured** by re-executing committed
predictions against the live `bird_dev` database, not estimated. Where it was not
measured, it says so.

Baseline for everything below: `bird_final_32b.json` — qwen2.5-coder:32b with
`--descriptions --promptfix --repairs 1`, **78/150 = 52.0 % EX**, the best
result in the repo. 72 residual failures.

---

## 1. Residual failures clustered by root cause

Produced with the repo's own `bird_analyze.py`, then re-grouped.

| Cluster | Count | % of failures | % of bank | What actually went wrong |
|---|---:|---:|---:|---|
| **C1 · Right shape, wrong values** | **44** | **61 %** | 29 % | The query executes, returns the right number of rows and columns, and the numbers are wrong. Wrong filter, wrong aggregate, wrong interpretation of the `evidence` hint. |
| **C2 · Wrong row count** | 13 | 18 % | 9 % | Join fan-out or a filter that is too loose/strict. Ranges from `19 vs 1` to `2875 vs 1` and `1 vs 155`. |
| **C3 · Wrong column count** | 7 | 10 % | 5 % | Returned 2 columns where gold returns 3, etc. One of these has gold's values present and only extra columns. |
| **C4 · Empty result** | 3 | 4 % | 2 % | Filter matched nothing. |
| **C5 · SQL error / nonexistent column** | 3 | 4 % | 2 % | Includes 1 hallucinated column and 1 type mismatch. |
| **C6 · Float precision only** | 1 | 1 % | 0.7 % | Numerically correct; already recovered by EX-tolerant. |
| **C7 · Column order only** | 1 | 1 % | 0.7 % | Same values, wrong order. |

By difficulty: 36 moderate, 20 challenging, 16 simple.
By database (top): `formula_1` 13, `thrombosis_prediction` 13,
`codebase_community` 9, `financial` 7, `card_games` 6, `california_schools` 5.

### Cross-cutting signals inside those 72 failures

| Signal | Count | Note |
|---|---:|---|
| `ORDER BY … DESC` with **no** `NULLS LAST` | **17** | PostgreSQL sorts NULLs **first** on DESC, so `ORDER BY x DESC LIMIT 1` returns a NULL row instead of the maximum. **`PROMPT_V2` already contains an explicit rule telling the model to add `NULLS LAST`, and this run used `--promptfix`.** The rule was in the prompt and was ignored 17 times. |
| Gold uses `DISTINCT`, prediction does not | 12 | |
| Query has a `LIMIT` | 24 | superlative / top-N shapes |
| Uses `CAST(… AS REAL)` | 23 | the division rule *did* stick |
| Uses `NULLIF` | 19 | ditto |
| Has a `JOIN` | 18 | |
| Has a subquery | 13 | |

### Three concrete C1 examples

| q | Question | Model's SQL | What is wrong |
|---|---|---|---|
| 82 | "grade span offered in the school with the **highest longitude**" | `SELECT gsoffered FROM schools ORDER BY ABS(longitude) DESC LIMIT 1` | Invented `ABS()`. "Highest longitude" is a plain maximum; California longitudes are negative, so `ABS` inverts the ordering. Pure semantic error — no schema, dialect or syntax signal could catch it. |
| 50 | "postal street address for the school with the **7th highest** Math average" | `… ORDER BY T1.avgscrmath DESC OFFSET 6 LIMIT 1` | Ranking logic is right; NULL handling is not. NULLs sort first on DESC, so "7th" counts NULL rows. Fixed deterministically — see Lever A. |
| 95 | "account numbers of clients who are **youngest and have highest average salary**" | nested `IN (SELECT district_id … ORDER BY a11 DESC LIMIT 1)` then `ORDER BY birth_date DESC LIMIT 1` | Two superlatives composed in the wrong order, and `a11` (average salary) is a *district* attribute being used as a *client* one. Requires reading the column descriptions properly. |

**The shape of the residue is unambiguous: this is a semantic/reasoning residue,
not a syntax residue.** Every syntactic class combined (C5 + C6 + C7) is
5 questions, 3.3 % of the bank. Sixty-one per cent of what is left is "the model
understood the question wrongly".

---

## 2. Fixable without a better model vs requiring one

| Cluster | Fixable with prompt / post-processing / validation / retry | Requires a better model or more compute |
|---|---|---|
| C1 (44) | **~5–8** — the NULL-ordering subset (measured below) and possibly the "extra label column" subset | **~36–39.** Wrong interpretation of the question or the evidence hint. Nothing outside the model has the information needed to fix these. |
| C2 (13) | ~2–4 — fan-out is detectable (`COUNT(*)` vs `COUNT(DISTINCT pk)`) and could drive a targeted retry | ~9–11 |
| C3 (7) | ~3–5 — column count vs question shape is checkable before execution | ~2–4 |
| C4 (3) | **3** — already covered by the existing empty-result grader + `--zero-row-retry`; this run had `--repairs 1` so the budget was nearly exhausted | 0 |
| C5 (3) | **3** — `sql_validate.py` already exists and would catch the hallucinated column; it was not in the harness path | 0 |
| C6 (1) | 1 (metric, already handled by EX-tolerant) | 0 |
| C7 (1) | 1 (column ordering is deterministic given the question) | 0 |
| **Total** | **~18–25 questions (12–17 pp)** | **~47–54 questions** |

---

## 3. Levers, priced

Cost is in **extra LLM calls per question**, because on this deployment one call
is 30–120 s (`important.md:3`) and that is the binding constraint.

### ✅ Lever A — Deterministic `NULLS LAST` rewrite · **MEASURED**

Rewrite every `DESC` in a generated `ORDER BY` to `DESC NULLS LAST` before
execution (an AST transform, or the regex used for this measurement).

**Measured on the 150-question bank, both directions:**

```
Candidates among the 72 failures ......... 17
  → flipped to CORRECT ................... 5   (q32, q50, q82, q879, q1122)
  → still wrong .......................... 12
  → rewrite errored ...................... 0

Currently-CORRECT questions the rewrite touches ... 9
  → still correct ........................ 9
  → regressed ............................ 0
```

| | |
|---|---|
| **Expected gain** | **+5 questions = +3.3 pp EX (52.0 % → 55.3 %)** |
| **Expected regressions** | **0 measured** (9 correct answers touched, none broken) |
| **Cost** | **zero extra LLM calls, ~0 ms** |
| **How to measure** | Already measured. Re-run the same script after implementing to confirm 5/5. |
| **Risk** | Low. `NULLS LAST` is a no-op when the column has no NULLs. The one real risk is a query that *intends* NULLs first — no such question exists in this bank, and none is plausible in BI usage. |
| **Where it belongs** | `apps/chat/task/agentic.py`, next to `raise_sql_limit`, applied in `_maybe_raise_limit`'s sibling position in `llm.py:2388`. Dialect-gated to PostgreSQL-family (`_PG_FAMILY` already exists in `apex_helpers.py`). |

This is the single best lever in the audit: **+3.3 pp for zero marginal cost and
zero measured regressions**, and it also *removes* a prompt rule that demonstrably
does not stick — a prompt-budget saving on top.

### ✅ Lever B — Run the existing identifier check in the benchmark path · not measured

`apps/chat/task/sql_validate.py` (396 LOC, 30 tests) already does what C5 needs.
The 52.0 % run went through `bird_eval.lint_sql`, a *different* implementation
(`03_eval_integrity.md` §5.1). C5 is 3 questions.

| | |
|---|---|
| **Expected gain** | +1 to +3 questions (0.7–2 pp), upper-bounded by C5's size |
| **Cost** | zero extra LLM calls (the check is deterministic); a retry costs 1 call, but only on the ~2 % of questions that fail it |
| **How to measure** | Replace `lint_sql` with `validate_sql_identifiers` in the harness and re-run |
| **Risk** | Low. The module fails open by design. |
| **Secondary benefit** | Removes divergence **E-03** — the harness would measure production code |

### ✅ Lever C — Port `_owners_hint` into production · not measured

`bird_eval.py::_owners_hint` (`:488`) names the table that actually owns a missing
column ("`frequency` … belongs to `trans` — join that table or qualify it there").
Its own comment records that without this, 28 of 47 repair attempts returned
byte-identical SQL. **Production's `format_identifier_feedback` does not have
this** — it says only "column X does not exist".

| | |
|---|---|
| **Expected gain** | Not measured in isolation. The harness's own evidence is that it converted a ~60 %-useless repair loop into a useful one. |
| **Cost** | zero extra LLM calls (it makes an *existing* retry more likely to succeed) |
| **How to measure** | A/B the retry-success rate on the questions that trigger an identifier finding |
| **Risk** | Low — additive text in an existing feedback string |

### ✅ Lever D — Benchmark the pipeline in the configuration it ships in · not a lever, a prerequisite

`--finish sql` is the default and it **cancels** the self-consistency candidates
and never runs the execution-retry loop or the grader
(`03_eval_integrity.md` §5.2). The two completed pipeline runs therefore measured
a crippled pipeline and still came in **3.3 pp below the raw model**
(p = 0.47, `03_eval_integrity.md` §5.3).

| | |
|---|---|
| **Expected gain** | 0 accuracy. It buys the ability to make any further decision honestly. |
| **Cost** | one 150-question pass, ~10 h GPU |
| **Why it is #1 by priority despite gaining nothing** | Every other agentic-layer decision — keep APEX off, keep self-consistency off, keep multi-candidate on — is currently being made on a measurement of a different system. |

### ❌ Lever E — Blanket `SELECT DISTINCT` · **MEASURED, REJECTED**

12 failures have gold using `DISTINCT` where the prediction does not, which looks
like a free win. It is not.

```
Applied to all 60 non-DISTINCT failing predictions:
  → gained ............... 0
  → still wrong .......... 35
  → errored .............. 25   (DISTINCT conflicts with ORDER BY
                                 expressions absent from the select list)

Applied to all 70 non-DISTINCT correct predictions:
  → regressed ............ 0
  → errored .............. 11
```

**Gains exactly zero questions and breaks 25 queries outright.** Rejected with a
number. The duplicate rows those 12 questions produce come from a wrong join, not
from a missing `DISTINCT` — the join is the defect and `DISTINCT` merely hides it.

### ❌ Lever F — Turn `AGENTIC_SELF_CONSISTENCY_ENABLED` back on · REJECTED, already measured

`docker-compose.yaml:65-70` records the measurement: on BIRD-150 it changed the
answer roughly once per 20 questions and moved EX by **0.0 pp (McNemar p = 1.00)**
while raising median latency **19 s → 47 s**. With temperature 0 the extra
candidate is a near-duplicate of the primary, so there is nothing to vote on.

**Cost: 2 extra LLM calls on every question, for 0.0 pp.** Correctly off.

### ❌ Lever G — Turn `APEX_ENABLED` back on · REJECTED on the available evidence

`bird_apex40.json` scores 35.0 % EX at a **median of 121.9 s/question** against
37.5 s for the tuned non-APEX run — 3.3× the latency. The slice is 40 questions,
not 150, and the project's own notes record that slice as unrepresentative, so
the accuracy comparison is not sound. What *is* sound is the cost: 5–6 extra
sequential LLM calls per question.

**Verdict: cannot be justified on current evidence; would need a full 150-question
run at ~5 h before reconsidering.** Note that Lever D's run should be done first.

### ❌ Lever H — Larger context window · REJECTED, already measured

`bird_32b20k_150.json` (20k context) scores **35.3 %** against the tuned run's
52.0 %, with **15 NO-SQL** — the model producing nothing at all. Raising context
made things dramatically worse on this hardware, because the 32B at Q4 is ~20 GB
on a 24 GB card and the KV cache decides whether the model fits
(`bird_eval.py:738-742`). More context ⇒ spill to CPU ⇒ timeouts and empty
answers.

### ⚠️ Lever I — Column-count pre-check against the question · not measured

C3 is 7 questions where the column count is wrong. "What is the highest X"
expects one column; the model adds a label. `PROMPT_V2` has an explicit
`OUTPUT SHAPE` rule for exactly this — and the project's own notes record that
this rule **backfired**, raising column counts and suppressing `DISTINCT`.

| | |
|---|---|
| **Expected gain** | +2 to +4 questions (1.3–2.7 pp) if implemented as a *deterministic post-check with retry* rather than a prompt rule |
| **Cost** | 1 extra LLM call on the ~5 % of questions that trip the check |
| **Risk** | **Medium-high.** The prompt version of this idea already made things worse once. Any implementation must be measured on aggregate column-count statistics, not on the headline alone. |
| **Flag** | This is a lever that could improve the benchmark without improving real usage — BI users generally *want* the label column that BIRD scores as wrong. See §6. |

### ⚠️ Lever J — A better model · the only lever that touches C1

C1 (44 questions, 29 % of the bank) is semantic. The measured evidence that model
choice is the dominant variable:

```
gpt-oss:20b  → qwen2.5-coder:32b (+ flags):  +11.3pp, McNemar p = 0.0213  ← significant
raw model    → full SQLBot pipeline:          -3.3pp, McNemar p = 0.4731  ← noise
```

| | |
|---|---|
| **Expected gain** | Reference points in `docs/BENCHMARK-BIRD.md`: GPT-4o ≈ 55–58 % on full mini-dev. A frontier model would plausibly reach the high 50s / low 60s here. |
| **Cost** | Either a per-token API bill, or hardware. The current card cannot hold anything larger than the 32B at Q4. |
| **Risk** | Changes the self-hosted-first positioning the README leads with. |

---

## 4. Ranked by (expected gain) / (risk × cost)

| # | Lever | Gain | Extra LLM calls | Risk | Status |
|---|---|---|---|---|---|
| 1 | **A · `NULLS LAST` rewrite** | **+3.3 pp measured** | **0** | Low | Ready — measured both directions |
| 2 | **D · Benchmark the shipping pipeline** | 0 pp (buys trust) | 0 | None | Prerequisite for 5–8 |
| 3 | **B · Use production's identifier check** | +0.7 to +2 pp | 0 (retry only on ~2 %) | Low | Also removes divergence E-03 |
| 4 | **C · Port `_owners_hint` to production** | unmeasured, positive | 0 | Low | Cheap |
| 5 | I · Column-count post-check | +1.3 to +2.7 pp | 1 on ~5 % | **Med-high** | Prompt version already backfired once |
| 6 | J · Better model | +5 to +10 pp | — | Positioning | Only lever that touches C1 |
| — | E · Blanket `DISTINCT` | **0, measured** | 0 | breaks 25 queries | **Rejected** |
| — | F · Self-consistency | **0.0 pp, measured** | **+2 every question** | — | **Rejected** |
| — | G · APEX | unproven, 3.3× latency | +5–6 every question | — | **Rejected on current evidence** |
| — | H · 20k context | **−16.7 pp, measured** | 0 | — | **Rejected** |

**Levers 1–4 together: +4 to +5.3 pp for zero extra model calls.**
Realistic post-lever figure: **56–57 % EX**, from 52.0 %.

---

## 5. Realistic ceiling, and what cannot be fixed

**Ceiling with the current 32B model and no extra LLM calls: ~57 %.**
That is 52.0 % + Lever A (measured) + Levers B/C (bounded by C5 at 3 questions).

**Ceiling with the current model and unlimited extra calls: ~60 %.**
Adding lever I and an aggressive multi-attempt repair loop could reach into C2/C3,
but every one of those calls is 30–120 s on this hardware, so a question would
take minutes. The benchmark would improve; the product would become unusable.

**What cannot be fixed at all with the current design:**

1. **C1's 36–39 semantic failures.** q82's `ABS(longitude)` is not detectable by
   any schema, dialect, value or execution signal. The query is valid, runs, and
   returns a plausible row. Only a model that reads "highest longitude" correctly
   fixes it. **~26 % of the bank is a hard floor for this model.**
2. **Compositional superlatives** (q95). Requires multi-step planning that
   survives into the SQL. APEX was the attempt at this and it costs 5–6 calls
   with unproven benefit.
3. **Evidence-hint interpretation.** BIRD's `evidence` field frequently *is* the
   answer's formula. The project's own notes record that qwen32b refused 13/150
   questions by **arguing with the hint**. The refusal retry recovers those, but a
   model that silently misapplies a hint is invisible.
4. **The float-ULP EX noise** (`03_eval_integrity.md` §3.3). ±1 question is
   irreducible without diverging from BIRD's official metric.
5. **Anything BIRD does not test.** Excel ingestion, island detection, cross-file
   fanout, permissions, multi-turn, charting — the actual product — has no
   accuracy measurement at all. See §7.

---

## 6. Levers that would improve the benchmark but not real usage

Flagged explicitly, as the brief requires.

| Lever | Why it is benchmark-only |
|---|---|
| **I · Strict output-shape enforcement** | BIRD scores "return exactly the asked-for column and nothing else". A BI user asking "what is the highest free-meal rate?" almost always wants to know *which school*. Enforcing BIRD's shape makes the product worse for humans. The project already discovered this: the OUTPUT SHAPE prompt rule raised column counts and suppressed `DISTINCT`. |
| **Suppressing `NEAR`/float differences** | EX-tolerant already handles it. Chasing strict EX on 6-dp float equality has no user-visible meaning. |
| **Removing the LIMIT lift** (`_maybe_raise_limit`) | BIRD questions never say "list all X"; the completeness lift can only cost EX there. In the product it is the difference between a complete answer and a truncated one. **Do not tune this against BIRD.** |
| **Tuning `TABLE_EMBEDDING_COUNT` on BIRD** | BIRD databases have 3–13 tables. The product's workbooks and warehouses have different distributions entirely. |
| **Optimising for `--finish sql`** | It is not what ships. |

---

## 7. The measurement gap that outranks every lever here

Every number in this document is about **NL→SQL over 11 clean relational
databases**. The differentiators this fork actually adds —
Excel ingestion, header/island detection, cross-file fanout, row permissions,
charting, multi-turn — have **no accuracy measurement of any kind**. The
in-house 55-question bank scores by "does the expected value appear somewhere in
the rows", which cannot distinguish a right answer from a superset
(`03_eval_integrity.md` E-10).

Two defects found in this audit would each have been caught immediately by a
benchmark of the product surface, and neither is visible on BIRD:

- **D-12** — cross-file fanout is dead for every user except id 1.
- **D-02** — table samples and value hints bypass row-level permissions.

**Recommendation, ahead of every accuracy lever above:** give the in-house bank
gold SQL and set equality, add ~20 questions covering multi-file, multi-sheet and
permission-restricted cases, and run it as a regression gate. It costs no GPU
(the questions are small), it measures the product rather than a proxy for it,
and it is the only way the fanout and permission defects become *measurable*
rather than merely *reported*.
