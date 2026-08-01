# AUDIT / 03 — Benchmark & Evaluation Integrity

**No code was changed.** Every number below was produced by running the repo's
own tooling, or by re-executing committed predictions against the live
`bird_dev` database. Commands and raw output are quoted.

---

## 1. What evaluation assets exist

| Harness | File | What it measures | What it does NOT measure |
|---|---|---|---|
| **BIRD Mini-Dev** | `backend/tests/bird_eval.py` (1,071 LOC) | NL→SQL over 11 clean relational PostgreSQL databases, 150-question stratified subset. EX / EX-tolerant / Soft-F1. | Excel ingestion, header detection, island detection, cross-file fanout, charting, permissions — i.e. everything this fork adds |
| **In-house bank** | `backend/tests/accuracy_eval.py` + `question_bank.json` (55 Q) | "Do the expected numbers/strings appear in the result rows" over the real uploaded workbooks | Nothing comparable to any published baseline; no gold SQL, no result-set equality |
| **Dynamic harness** | `backend/tests/eval_harness.py` | LLM-generated questions against live datasources, self-classified | Non-deterministic question set → not a regression suite |
| **Bulk-ingest check** | `bulk_ingest.py` + `analyze_manifest.py` | Deterministic parsing across a folder of files (header confidence, schema snapshot) | Accuracy of answers |
| **Unit suites** | `tests/` (296 tests), `backend/tests/` (28) | Pure helpers | The orchestrator (`llm.py`, 3,266 LOC) — see `01_defects.md` coverage table |
| **Analysis tools** | `bird_analyze.py`, `bird_diff.py`, `bird_significance.py` | Root-cause bucketing, per-question diffs, Wilson CI + exact McNemar | — |

`bird_significance.py` is genuinely good: Wilson intervals, exact McNemar,
churn reporting. It is the strongest piece of measurement infrastructure here and
its conclusions are used throughout this document.

---

## 2. How scoring works, and where the tolerance lives

- **EX (official)** — `calculate_ex` (`bird_eval.py:73`): `set(pred) == set(gold)`.
  Ported verbatim from BIRD. Order-insensitive, type-sensitive, precision-sensitive.
- **EX (tolerant)** — `calculate_ex_tolerant` (`:101`): the same set comparison
  after `_norm_cell` maps `int→float`, rounds `float`/`Decimal` to **6 dp**,
  ISO-formats dates, strips strings. **This is where all the tolerance lives.**
- **Soft-F1** — `calculate_f1_score` (`:125`): BIRD's partial-credit metric,
  ported verbatim, comparing `predicted[i]` to `ground_truth[i]` **positionally**.
- **Status** — `CORRECT` if `ex`, else `NEAR` if `ex_tol`, else `WRONG`;
  `SQL-ERR` on execution failure, `NO-SQL`/`FAIL` when nothing was produced.

Timeouts: `EXEC_TIMEOUT_MS = 60_000` per query; `--timeout` (default 900 s) per
question. **A timeout is scored as a failure**, not excluded — `rec` keeps
`ex=0` and the exception text lands in `detail`. The docstring at `:869` records
that this cost three questions in the first run.

---

## 3. Reproduction: do the reported numbers hold up?

Re-running the *generation* half is not feasible for this audit — a single
150-question pass is 2–10 h of exclusive GPU (`docs/BENCHMARK-BIRD.md:109`).
What **is** reproducible, and what I did, is the *scoring* half: take each
committed per-question prediction, re-execute it and the gold query against the
same unchanged `bird_dev`, and recompute all three metrics.

```
docker exec sqlbot .venv/bin/python /tmp/rescore.py <each results file>
```

### 3.1 Results

| File | Reported EX | **Recomputed EX** | Reported tol | **Recomputed tol** | Reported F1 | **Recomputed F1** |
|---|---:|---:|---:|---:|---:|---:|
| `bird_model.json` | 40.7 % | **40.7 %** ✅ | 45.3 % | **45.3 %** ✅ | 41.0 | **41.0** ✅ |
| `bird_final_32b.json` | 52.0 % | **52.0 %** ✅ | 52.7 % | **52.7 %** ✅ | 52.7 | **51.8 / 52.0 / 52.5** ❌ |
| `bird_xiyan14b.json` | 43.3 % | **43.3 %** ✅ | 44.7 % | **44.7 %** ✅ | 41.8 | **40.4** ❌ |
| `bird_32b20k_150.json` | 35.3 % | **35.3 %** ✅ | 42.7 % | **42.7 %** ✅ | 38.6 | **38.6** ✅ |
| `bird_qwen32b20k_150.json` | 35.3 % | **35.3 %** ✅ | 42.7 % | **42.7 %** ✅ | 38.6 | **38.6** ✅ |
| `bird_pipeline_gptoss_150.json` | 37.3 % | **37.3 %** ✅ | 40.0 % | **40.0 %** ✅ | 42.5 | **41.8** ❌ |
| `bird_pipeline_v2_150.json` | 36.0 % | **36.0 %** ✅ | 42.7 % | **42.7 %** ✅ | 40.4 | **40.2** ❌ |
| `bird_postfix_gptoss_150.json` | 40.7 % | **41.3 %** ❌ | 48.7 % | **48.7 %** ✅ | 45.8 | **45.1** ❌ |
| `bird_apex40.json` (n=40) | 35.0 % | **35.0 %** ✅ | 40.0 % | **40.0 %** ✅ | 40.6 | **40.6** ✅ |

**Verdict: EX and EX-tolerant reproduce exactly on 8 of 9 files. The headline
numbers in `docs/BENCHMARK-BIRD.md` (40.7 % / 52.0 % / 43.3 %) are correct.**

Two deltas need explaining, and both are real defects, not transcription errors.

### 3.2 Delta 1 — Soft-F1 is non-deterministic (`01_defects.md` D-10)

Same file, three consecutive re-scores against an unchanged database:

```
bird_final_32b.json  recomputed f1 = 51.8   (run 1)
bird_final_32b.json  recomputed f1 = 52.0   (run 2)
bird_final_32b.json  recomputed f1 = 52.5   (run 3)
stored / documented  = 52.7
```

`calculate_f1_score` matches rows **by position**. Most BIRD queries have no
`ORDER BY`, so PostgreSQL may return rows in any order and the positional match
shifts. EX (a set comparison) is immune, which is exactly the pattern observed:
EX stable to the question, F1 drifting ±0.7 pp per run.

**Consequence.** Every Soft-F1 figure in `docs/BENCHMARK-BIRD.md` carries roughly
±1 pp of pure measurement noise. The table quotes them to one decimal place,
which implies a precision the metric does not have.

### 3.3 Delta 2 — strict EX itself flips on float aggregates (`01_defects.md` D-11)

One per-question disagreement across all nine files:
`bird_postfix_gptoss_150.json`, **q1473**, stored `ex=0`, recomputed `ex=1`.

Isolating that question and running its predicted and gold SQL five times back to
back against the unchanged database:

```
run  predicted            gold                  verdict
0    459.9562642112432    459.9562642112432     EQUAL
1    459.9562642112432    459.9562642112432     EQUAL
2    459.956264211243     459.95626421124325    DIFFERENT
3    459.9562642112432    459.9562642112432     EQUAL
4    459.9562642112432    459.95626421124325    DIFFERENT
```

**2 of 5 runs disagree on identical SQL against identical data.**
`debit_card_specializing.yearmonth.consumption` is `real` (float4).
`AVG` accumulates in floating point; the summation order depends on the chosen
plan (parallel workers, join order), and float addition is not associative — so
the last ULP moves between runs and BIRD's exact set equality flips.

**Consequence.** EX carries ~±1 question (±0.7 pp) of irreducible run-to-run
noise on this benchmark subset, independent of the model. Combined with the
±8 pp sampling interval already documented, **no reported EX gap below ~8 pp is
interpretable**, and even a repeat of the *same* run will not reproduce to the
question.

`EX (tolerant)` was stable across every run of every file. It is the metric that
should drive internal decisions.

### 3.4 Delta 3 — two committed result files are byte-identical

```
afb15069230dbd6d48d6f02310da3094  bird_32b20k_150.json
afb15069230dbd6d48d6f02310da3094  bird_qwen32b20k_150.json
```

Two filenames, one experiment (`01_defects.md` D-22). Any table that lists both
as separate runs double-counts.

---

## 4. Harness bugs, leakage and tolerance abuse

### 4.1 No leakage between prompt context and gold — verified

`ask_model`/`generate_with_repair` build the prompt from `schema_ddl(db_id)`,
`entry["evidence"]` and `entry["question"]`. `entry["SQL"]` (the gold) is read
only inside `main()` at scoring time (`:1001`). `ask_pipeline` passes
`question + "(Hint: evidence)"` only. **No gold leakage.** Confirmed by reading
every use of `entry["SQL"]`.

One nuance worth stating: `--values` (`value_samples`, `:216`) injects the actual
distinct values of every low-cardinality text column into the prompt. That is not
gold leakage — it is legitimate schema grounding that published BIRD baselines
also use — but it *is* a real advantage over the plain `--mode model` baseline,
so runs with and without it are not comparable.

### 4.2 The repair loop can consume the gold's execution behaviour

`generate_with_repair` with `--zero-row-retry` (`:574-583`) **executes the
candidate SQL** and, if it returns zero rows, tells the model to try again.
That is execution-guided feedback the production pipeline also has, so it is fair
— but it means the `--all-fixes` configuration is a fundamentally different
system from `--mode model` plain, and the two must never be presented as "the
same model, better prompt".

`docs/BENCHMARK-BIRD.md:137-140` is honest about exactly this: it states the
40.7 → 52.0 gap "confounds two changes made together". That caveat is correct and
should be preserved.

### 4.3 Non-determinism sources in the harness

| Source | Where | Controlled? |
|---|---|---|
| Model sampling | `options.temperature = 0` (`:769`, `:738`) | ✅ greedy |
| Subset selection | `bird_make_subset.py` fixed seed; `stratified_sample` has no RNG at all | ✅ deterministic |
| Question order | `sorted(... question_id)` | ✅ |
| Result-row order | **not controlled** | ❌ → D-10, D-11 |
| `--resume` | skips ids already in the output file | ⚠️ a resumed run mixes configurations if flags changed between invocations — nothing records which flags produced which record |
| Model identity | `BIRD_MODEL` env / `--model` | ⚠️ **not recorded in the output file** — the results JSON has no field for model, flags or date |

**The last two are the most consequential harness gaps.** A results file is
`[{question_id, db_id, difficulty, question, ex, ex_tol, f1, status, sql, detail, trace, seconds}]`
and nothing else. There is no provenance: which model, which flags, which code
revision. The information lives only in the filename and in
`docs/BENCHMARK-BIRD.md`'s prose, which is exactly how
`bird_32b20k_150.json` / `bird_qwen32b20k_150.json` became an unresolvable
duplicate.

### 4.4 Tolerance rules that could mask failures — assessed

`_norm_cell` rounds to 6 dp and coerces `int→float`. This could in principle mask
a genuinely wrong answer that differs below 1e-6, or one that returns `1` where
gold returns `1.0`. Measured impact across the three tracked files: the
EX→EX-tolerant gap is **7 questions** (baseline), **1** (32b), **2** (XiYan).
Inspecting those, they are the float-precision cases the tolerance was built for.
**No evidence of tolerance masking real errors.** The tolerance is honest.

The one place tolerance *is* too generous is `calculate_ex` vs `f1`: a record can
have `ex_tol=1` and `f1=0` simultaneously (observed on q1473), because F1 uses
exact `in` membership on `Decimal`s while EX-tolerant rounds. The two metrics
disagree with each other about the same rows.

---

## 5. **P1 — the harness measures a different system than production**

This is the finding this phase exists to surface.

### 5.1 Four capabilities are re-implemented in the harness

| Production module | Harness re-implementation | Divergence |
|---|---|---|
| `apps/chat/task/sql_validate.py` — sqlglot identifier check, alias resolution, case-sensitivity per dialect, fails open | `bird_eval.py::lint_sql` (`:373`) — its own sqlglot walk, its own CTE/alias/output-name handling, plus SQLite-function detection and a `difflib` "did you mean" | Two different notions of "this identifier is wrong". The harness's version has `_owners_hint` (names the table that owns a missing column) which production **does not have** — and which the harness's own comments say fixed a dead repair loop |
| `apps/datasource/relations.py` — declared FK + value-overlap inference, per dialect, cached | `bird_eval.py::join_graph` (`:688`) — plain `information_schema` FK list, PostgreSQL only | Harness gets a flat FK block; production gets an M-Schema `【Foreign keys】` block with inferred edges and confidence labels |
| `apps/datasource/value_index.py` — bounded, cached, dialect-aware `SELECT DISTINCT`, gated on question terms | `bird_eval.py::value_samples` (`:216`) — every text column with ≤25 distinct values, unconditionally | Harness shows values for *all* small columns; production shows only values matching a question term, capped at 5 tables × 12 columns |
| The agentic retry loop in `llm.py` (identifier check → grader → refusal retry → alt candidate → DS fallback) | `bird_eval.py::generate_with_repair` (`:540`) — lint → EXPLAIN → zero-row, up to `--repairs` | Completely different retry policies and feedback text |

**Consequence.** `--mode model --all-fixes` is not "the raw model" and it is not
"SQLBot". It is a third system that exists only inside the harness. The 52.0 %
headline was produced by that third system
(`--descriptions --promptfix --repairs 1`, per `docs/BENCHMARK-BIRD.md:126`).

### 5.2 `--mode pipeline` measures less than the UI ships

`ask_pipeline`'s own docstring (`:787-794`) is explicit:

> `finish='sql'` → stop after SQL generation (identifier-check retries only;
> self-consistency candidates are **CANCELLED** and execution retries/grader
> never run — **measures less than the UI has**).

`--finish` defaults to `sql`. So the default pipeline benchmark disables the
execution-error retry, the empty-result grader and the consensus vote — three of
the features the agentic layer exists for.

### 5.3 What the completed pipeline runs actually show

`docs/BENCHMARK-BIRD.md:149-151` states:

> `--mode pipeline` has no completed 150-question run — only a 12-question
> partial … which is why the full-pipeline-versus-raw-model comparison … is
> still open.

**This is out of date.** Two completed 150-question pipeline runs exist in the
working tree as untracked files. Running the repo's own significance tool:

```
$ python3 backend/tests/bird_significance.py \
    --a backend/tests/bird_results/bird_model.json \
    --b backend/tests/bird_results/bird_pipeline_gptoss_150.json

=== OVERALL  A=bird_model.json  B=bird_pipeline_gptoss_150.json  (n=150) ===
  A  :  61/150 =  40.7%  [95% CI 33.1-48.7]
  B  :  56/150 =  37.3%  [95% CI 30.0-45.3]
  delta          : -3.3pp
  fixed by B     : 13
  regressed by B : 18
  churn          : 20.7% of questions changed verdict
  McNemar exact p: 0.4731  <- NOT distinguishable from noise
```

**The full SQLBot pipeline scores 3.3 pp *below* the raw model on the same 150
questions, and the difference is not distinguishable from noise (p = 0.47).**
It changed the verdict on 20.7 % of questions — fixing 13, breaking 18.

The status mix shows where it went:

```
  NO-SQL    A=  0  B=  4   (+4)   pipeline produced no SQL at all
  WRONG     A= 71  B= 82  (+11)
  SQL-ERR   A=  8  B=  4   (-4)   the identifier check does help here
  NEAR      A=  7  B=  4   (-3)
```

The second pipeline run is worse still and much noisier:

```
$ ... --a bird_pipeline_v2_150.json --b bird_pipeline_gptoss_150.json
  A  :  54/150 = 36.0%    B  :  56/150 = 37.3%
  McNemar exact p: 0.8388  <- NOT distinguishable from noise
  NO-SQL    A= 13  B=  4   (-9)
```

13 NO-SQL out of 150 (8.7 %) in `bird_pipeline_v2` — the pipeline returning
nothing at all — matches the context-ceiling and refusal behaviour already
recorded in the project's own notes.

For contrast, the model swap **is** real:

```
$ ... --a bird_model.json --b bird_final_32b.json
  delta          : +11.3pp
  fixed by B     : 33   regressed by B : 16
  McNemar exact p: 0.0213  <- significant at 0.05
```

**Bottom line for Phase 4:** the measured evidence is that the *model* carries
the accuracy and the *agentic layer* currently does not — on BIRD. The
caveat that matters is §5.2: the pipeline runs used `--finish sql`, so three
agentic features were switched off in the measurement. That is a harness defect,
not proof the features are worthless — but it means **the pipeline has never been
benchmarked in the configuration it ships in**.

---

## 6. Per-category failure breakdown

Status × difficulty, all recomputed. `n=150` each.

**`bird_model.json` — gpt-oss:20b, no harness flags (baseline)**

| status | simple | moderate | challenging | total |
|---|---:|---:|---:|---:|
| CORRECT | 29 | 26 | 6 | 61 |
| NEAR | 1 | 5 | 1 | 7 |
| WRONG | 13 | 38 | 20 | 71 |
| SQL-ERR | 0 | 4 | 4 | 8 |
| FAIL | 1 | 2 | 0 | 3 |
| **EX** | **65.9 %** | **34.7 %** | **19.4 %** | 40.7 % |

**`bird_final_32b.json` — qwen2.5-coder:32b + descriptions + v2 prompt + 1 repair (best)**

| status | simple | moderate | challenging | total |
|---|---:|---:|---:|---:|
| CORRECT | 28 | 39 | 11 | 78 |
| NEAR | 1 | 0 | 0 | 1 |
| WRONG | 15 | 35 | 18 | 68 |
| SQL-ERR | 0 | 1 | 2 | 3 |
| **EX** | **63.6 %** | **52.0 %** | **35.5 %** | 52.0 % |

**`bird_xiyan14b.json` — XiYanSQL-QwenCoder-14B, same flags**

| status | simple | moderate | challenging | total |
|---|---:|---:|---:|---:|
| CORRECT | 30 | 26 | 9 | 65 |
| NEAR | 0 | 2 | 0 | 2 |
| WRONG | 12 | 34 | 12 | 58 |
| SQL-ERR | 2 | **13** | **10** | **25** |
| **EX** | **68.2 %** | **34.7 %** | **29.0 %** | 43.3 % |

**`bird_pipeline_gptoss_150.json` — full pipeline, `--finish sql`**

| status | simple | moderate | challenging | total |
|---|---:|---:|---:|---:|
| CORRECT | 27 | 22 | 7 | 56 |
| NEAR | 0 | 4 | 0 | 4 |
| WRONG | 16 | 47 | 19 | 82 |
| NO-SQL | 1 | 1 | 2 | 4 |
| SQL-ERR | 0 | 1 | 3 | 4 |
| **EX** | **61.4 %** | **29.3 %** | **22.6 %** | 37.3 % |

**`bird_pipeline_v2_150.json` — full pipeline, second config**

| status | simple | moderate | challenging | total |
|---|---:|---:|---:|---:|
| CORRECT | 24 | 24 | 6 | 54 |
| NEAR | 1 | 5 | 4 | 10 |
| WRONG | 15 | 41 | 15 | 71 |
| NO-SQL | 4 | 4 | 5 | 13 |
| SQL-ERR | 0 | 1 | 1 | 2 |
| **EX** | **54.5 %** | **32.0 %** | **19.4 %** | 36.0 % |

### Readings

1. **`WRONG` is the dominant bucket everywhere** — 45–55 % of all questions.
   These are queries that execute cleanly and return the wrong rows. No amount of
   syntax validation, dialect linting or identifier checking touches them.
2. **XiYanSQL-14B's 25 SQL-ERR** (17 % of the bank, vs 3 for qwen32b) is the
   whole story of its underperformance. Consistent with the project's own note
   that its M-Schema prompt format was likely mismatched — it is a formatting
   failure, not a reasoning one.
3. **`bird_pipeline_v2`'s 13 NO-SQL** is the pipeline producing nothing.
4. **Simple questions are flat-to-down for every "improvement"** (65.9 → 63.6 for
   the tuned 32b; 61.4/54.5 for the pipeline). Everything that helped moderate and
   challenging questions cost something on simple ones.
5. **Per-database spread is wide** and is where the remaining headroom sits —
   `bird_analyze.py` and `bird_diff.py` already exist to slice this and should be
   the first tools used in Phase 4.

---

## 7. Findings, as defect IDs

| ID | Severity | Summary |
|---|---|---|
| **E-01** | **P1** | Soft-F1 is order-sensitive → ±1 pp run-to-run noise. Same as `01_defects.md` **D-10**. |
| **E-02** | **P1** | Strict EX flips on float aggregates (measured 2/5). Same as **D-11**. |
| **E-03** | **P1** | The harness re-implements four production capabilities; `--mode model --all-fixes` measures a system that does not ship. §5.1 |
| **E-04** | **P1** | `--mode pipeline --finish sql` (the default) disables execution retries, the grader and the consensus vote — **the shipping configuration has never been benchmarked.** §5.2 |
| **E-05** | **P1** | `docs/BENCHMARK-BIRD.md` states no completed pipeline run exists; two do, and they show the pipeline **3.3 pp below** the raw model (p = 0.47). Documentation is materially out of date about the project's central open question. §5.3 |
| **E-06** | **P2** | Results files carry no provenance (model, flags, code revision, date). Directly caused **E-07**. §4.3 |
| **E-07** | **P2** | Two committed result files are byte-identical under different names. **D-22** |
| **E-08** | **P2** | `--resume` will happily merge records produced under different flags into one file, with nothing recording the mix. §4.3 |
| **E-09** | **P2** | `ex_tol=1` and `f1=0` can coexist for the same record: EX-tolerant rounds, F1 does exact `Decimal` membership. The two metrics disagree about the same rows. §4.4 |
| **E-10** | **P2** | The in-house 55-question bank scores by "expected value appears somewhere in the result", which cannot distinguish a correct answer from a superset. No result-set equality, no gold SQL. |
| **E-11** | **P2** | Only 40 of 150 questions were run for the APEX comparison (`bird_apex40.json`), and the project's own notes record that slice as unrepresentative. Any APEX conclusion drawn from it is not supported. |
| **E-12** | **P3** | Timeouts are scored as wrong rather than excluded or retried. Defensible, but it means a slow GPU depresses the score in a way a faster one would not. |

---

## 8. Recommended benchmark improvements (analysis only)

Ranked by how much measurement trust they buy per unit of work.

1. **Record provenance in every results file** — model, every flag, `git rev-parse HEAD`,
   UTC timestamp, and the harness version. One dict at the top of the JSON.
   Cost: ~15 lines. Removes E-06, E-07, E-08 outright.
2. **Sort both result sets before F1** — one line. Removes E-01.
3. **Make `--finish data` the default for `--mode pipeline`** and re-run.
   This is the single measurement that matters most: the shipping configuration
   has never been scored. Cost: one 150-question pass (~10 h).
4. **Report EX-tolerant as the primary internal metric**, with strict EX kept
   only for leaderboard comparability, and state the ±1-question float noise
   next to it. Removes the false precision in E-02.
5. **Delete the harness's re-implementations and call the product code**
   (`sql_validate.validate_sql_identifiers`, `relations.get_relations`,
   `value_index.collect_column_values`). Where the harness version is *better*
   — specifically `_owners_hint`, which names the table that owns a missing
   column — port it **into** production rather than keeping two copies.
   Removes E-03 and makes every future number attributable.
6. **Give the in-house bank gold SQL and set equality**, so it can report EX like
   BIRD does. Removes E-10 and makes the product's own differentiator measurable.

### Missing benchmark cases

The benchmark covers NL→SQL over clean relational schemas. It does **not** cover
any of the following, all of which are this fork's actual product surface:

- **Excel ingestion end-to-end** — header detection, island splitting, type
  inference, COPY load — has unit tests but no accuracy benchmark.
- **Cross-file fanout / decomposition** — the headline feature, which
  `01_defects.md` **D-12** shows is disabled for every non-id-1 user. No
  benchmark would have caught that.
- **Row/column permission correctness** — no test at all, and `01_defects.md`
  **D-02** shows raw rows bypass row rules into the prompt.
- **Multi-turn conversation** — `get_last_conversation_rounds` feeds 3 rounds of
  history into every prompt; no benchmark has more than one turn.
- **Chart generation** — a second LLM call on every question, entirely unmeasured.
- **Terminology / data-training few-shot injection** — `skeleton.py` has 14 unit
  tests for the ranking maths and zero end-to-end accuracy measurement.
- **Non-English questions** — the keyword lists ship Chinese and Korean; no
  benchmark question is in either.
- **Latency / cost per question as a tracked metric** — `seconds` is stored per
  question but never reported as a headline alongside accuracy, so a change that
  buys 1 pp for 3× the latency looks like a win.
- **Spider** — mentioned nowhere in the code. Adding it would test schema
  generalisation across 200 databases rather than 11.
