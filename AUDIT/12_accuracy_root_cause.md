# AUDIT / 12 — Why accuracy is still low: root-cause pass

Date: 2026-08-03 · Branch `audit/phase5-fixes` · Method: read the whole generation
path, then **reproduce each defect live in the running container** against the
registered BIRD datasources. Every finding below has an executed proof, not an
inspection opinion.

---

## 0. The number that frames everything

| Run | Mode | EX |
|---|---|---|
| `bird_la_nullslast_32b.json` | **model-only** (schema + question, no SQLBot) | **55.3 %** |
| `bird_qwen32b20k_150.json` / `bird_32b20k_150.json` | **pipeline**, same model | **35.3 %** |
| `bird_la_nullslast_gptoss.json` | model-only | 43.3 % |
| `bird_postfix_gptoss_150.json` | pipeline, post-fix | 40.7 % |
| `bird_pipeline_gptoss_150.json` | pipeline | 37.3 % |

**The product pipeline scores 20 pp BELOW handing the same model the same schema
and question.** The accuracy problem is not the model. Everything SQLBot adds
between the question and the model is, net, subtracting 20 points. The findings
below are the mechanism.

---

## P0 — proven to convert correct answers into wrong ones

### F-01 · A `)` in a column comment silently destroys the schema index
`backend/apps/chat/task/apex_helpers.py:36`

```python
_COLUMN_RE = re.compile(r"\(\s*(?P<name>[^:)\s][^:]*?)\s*:\s*(?P<rest>[^)]*?)\s*\)")
```

`rest` is `[^)]*?` — the comment may not contain a closing paren. The source
comment says "comment may itself contain commas, so we capture greedily up to the
closing paren": commas were handled, parentheses were not. The first `)` inside a
comment ends the tuple early; the tail is re-scanned and produces a **phantom**
column whose name is the rest of the comment glued to the next real column name.

Measured across all 11 registered BIRD datasources (`probe_scale.py`):

```
75 tables, 798 columns
6 tables corrupted
9 real columns invisible to the identifier check
12 phantom identifiers indexed in their place
```

`california_schools.schools` loses `soctype`, `mailstrabr`, `admfname1` and gains
`'cea) • 13 – o),\n(soctype'`.

**Proven end-to-end** — valid, executable SQL is rejected and the model is told
its correct column does not exist:

```
SQL: SELECT "s"."soctype" FROM "california_schools"."schools" "s" LIMIT 5
  DB : EXECUTES FINE
  validator findings: [{'kind': 'unknown-column', 'identifier': 's.soctype'}]
  -> retry feedback: - column "s.soctype" does not exist in the provided schema
```

The model is right, the validator is wrong, a retry is burned, and the model's
best move is to "correct" to a genuinely wrong column.

Blast radius is far larger than BIRD: real spreadsheet headers and descriptions
routinely contain parentheses (`Revenue (USD)`, `Q3 (actual)`). `parse_schema` is
also what APEX pruning rebuilds the prompt from, so with `APEX_ENABLED=true` the
corrupted columns are dropped from the prompt outright.

**Fix:** balance-aware tuple scanning, or escape `)` when rendering
`custom_comment` in `get_table_schema`. Add a startup/ingest assertion that
`build_schema_index(get_table_schema(...))` round-trips every `core_field` row.

---

### F-02 · The identifier check goes blind whenever the model aliases a column to its own name
`backend/apps/chat/task/sql_validate.py:201-204` and `:306-308`

```python
for alias_node in tree.find_all(exp.Alias):     # EVERY SELECT-list alias
    locals_.add(alias_node.alias)               # becomes a "local name"
...
if not norm_col or norm_col in local_names:     # ...and is then never validated
    continue
```

A column is skipped if its **name** matches any alias declared anywhere in the
statement. So `SELECT "f"."Enrollmnt" AS "Enrollmnt"` validates clean while
`SELECT "f"."Enrollmnt" AS "enrollment"` is caught. Verified live:

```
hallucinated column, NOT aliased          -> findings: [unknown-column f.Enrollmnt]
hallucinated column aliased to ITS OWN name -> findings: []
```

This is not hypothetical, because **the shipped few-shot example teaches exactly
that pattern** — `backend/templates/sql_examples/PostgreSQL.yaml:93`:

```sql
SELECT "country" AS "country_name", ..., "year" AS "year", "gdp" AS "gdp"
```

Two of the four aliases are identity aliases. Combined with the mandatory rules
`函数字段必须加别名` and `中文/特殊字符字段需保留原名并添加英文别名`, the model
aliases nearly everything.

Measured over the stored predictions: **28 of 148 (19 %)** gpt-oss statements and
14 of 135 qwen statements contain at least one identity alias, i.e. at least one
column that the guard could not see.

**Fix:** only treat an alias as local when it is *referenced* outside its own
defining expression (ORDER BY / GROUP BY / outer query), and never let a
SELECT-list alias mask the column it aliases.

---

### F-03 · `LIMIT 1` on a superlative is silently rewritten to `LIMIT 1000`
`backend/apps/chat/task/llm.py:929-942`, `agentic.py:518-526`,
`config.py:299-302`

`_maybe_raise_limit` fires when the question looks like a listing question and
carries no explicit row count. `AGENTIC_AGGREGATION_KEYWORDS` contains
`highest, lowest, max, min` — but **not** `largest, biggest, longest, earliest,
latest, smallest, shortest, oldest, newest, most, least, best, worst`. Every one
of those is a singular superlative. Reproduced live:

```
Find the school with the largest enrollment.           listing=True explicit=False -> LIMIT 1 -> LIMIT 1000
Give the name of the school with the longest ...       listing=True explicit=False -> LIMIT 1 -> LIMIT 1000
Show the district of the school with the biggest ...   listing=True explicit=False -> LIMIT 1 -> LIMIT 1000
List the school that opened most recently.             listing=True explicit=False -> LIMIT 1 -> LIMIT 1000
Get the earliest opendate among charter schools.       listing=True explicit=False -> LIMIT 1 -> LIMIT 1000
What is the grade span ... with the highest longitude? listing=False               -> unchanged (correct)
```

A one-row correct answer becomes a 1000-row table. Only affects the execution
path (`--finish data` / the real UI), which is exactly the shipping configuration
— and is invisible to any `--finish sql` benchmark.

**Fix:** gate on sentence structure ("the X with the …" is singular), not on a
verb keyword; or only lift when the model's LIMIT is absent, never when it is 1.

---

### F-04 · Value linking emits false "use this exact value" hints on almost every question
`backend/apps/chat/task/value_linking.py:213-217`, `config.py:193`

```python
if value_norm in term_norm or term_norm in value_norm:
    shorter, longer = sorted((len(term_norm), len(value_norm)))
    return 0.80 + 0.19 * (shorter / longer)     # floor is 0.80
```

`VALUE_LINKING_MIN_SCORE = 0.72`. The minimum achievable containment score is
`0.80 + 0.19/60 = 0.803`. **Containment therefore always passes the floor**, and
the length-ratio scaling the comment describes ("long values containing a short
common term must not score high") does nothing. Measured:

| term | cell value | score | emitted? |
|---|---|---|---|
| `SAT` | `S` | 0.863 | yes |
| `Alameda` | `A` | 0.827 | yes |
| `Enrollment` | `M` | 0.819 | yes |
| `customer` | `us` | 0.848 | yes |
| **`Paid`** | **`Unpaid`** | **0.927** | **yes** |
| `2020` | `20` | 0.895 | yes |

The `Paid` → `Unpaid` case is accuracy-inverting: the block is headed *"Use these
exact values in WHERE/filter conditions — do not invent or re-case them"*.

Confirmed in the live prompt for BIRD q12 (an SAT-rate question), the sole hint
produced was noise:

```
<value-hints>
  - satscores.rtype contains: "S", "D"
</value-hints>
```

**Fix:** raise the floor above 0.80, or make containment score
`0.5 + 0.5*(shorter/longer)` so a 1-char match lands below any sane threshold.
Also require the matched value to be at least ~3 characters.

---

## P1 — systematically degrades quality

### F-05 · The sample-data budget is spent in arbitrary table order, so the relevant table gets nothing
`backend/apps/datasource/crud/datasource.py:682-696`, `:436-438`

`get_table_schema` ranks tables by embedding and returns `table_name_list` in
relevance order. `get_tables_sample_data` receives that list but iterates
`get_table_obj_by_ds(...)` — a `session.query(CoreTable)` with **no ORDER BY** —
and merely filters by membership. Relevance order is discarded; the total budget
is consumed by whichever tables the database happens to return first.

Live evidence, the run that built the prompt above:

```
sample data capped at 9000 chars: 1 table(s) omitted (2 included, 6032 chars)
```

The omitted table was **`satscores`** — the only table that can answer
"SAT excellence rate". `frpm` and `schools` consumed the budget.

`394` occurrences of this log line across the retained logs.

**Fix:** iterate in `table_list` order (it is already relevance-ranked), and give
each selected table a guaranteed minimum slice before the ranked spend.

---

### F-06 · Half the prompt is fixed chart-formatting boilerplate
Measured over 473 real `generate_sql` calls in the container logs:

```
n=473  min=24,728  p50=29,059  p90=37,953  max=46,976 chars
```

At the codebase's own measured 2.9 chars/token: **median ≈ 10.0k tokens, p90
≈ 13.1k, max ≈ 16.2k**. The fixed overhead before any schema is
`system 1,549 + rules 13,221 = 14,770 chars ≈ 5,090 tokens` — roughly 50 % of the
median prompt, on every single question, and it is entirely about JSON shape,
chart types, axis/series selection and identifier-copying exhortations.

The default model runs `num_ctx 20480`, so the worst case leaves ~4k tokens of
headroom for reasoning plus output. `qwen2.5-coder:7b` (the configured
`AGENTIC_SELF_CONSISTENCY_MODEL`) has **no `num_ctx` override at all** on the
host — Ollama's default. If self-consistency is ever switched on, those
candidates are truncated.

---

### F-07 · The SQL prompt is Chinese; the questions and schema are English
`backend/apps/chat/task/llm.py:3340-3351`

```python
def get_lang_name(lang):
    if not lang:            return '简体中文'   # default for an unknown/empty language
    if lang.startswith('en'): return '英文'
```

An English user gets the entire rule set, process checklist and worked examples in
Chinese, ending with a Chinese sentence instructing them (in Chinese) to answer in
"英文". Verified in the live prompt:

```
## 请根据上述要求，使用语言：英文 进行回答 ...
```

This is the most plausible mechanism behind the refusal bucket already recorded in
memory (qwen32b returning valid-JSON refusals that argue with the hint).

---

### F-08 · Chart rules inside the SQL prompt distort the SQL itself
`backend/templates/template.yaml`

| Line | Rule | Effect on correctness |
|---|---|---|
| 170-175 | "if a categorical field is present, numeric metrics **must** be aggregated, default `SUM`" | injects `SUM()` the question never asked for |
| 161-169 | "the dimension field **must** participate in the sort" | forces a dimension-first `ORDER BY`, breaking `ORDER BY metric DESC LIMIT n` |
| 186-194 | "if no order specified, sort time ascending"; "format dates as `yyyy-MM-dd`" | adds an unrequested `ORDER BY`; converts a date column to a formatted **string**, which fails set equality |
| 143-160 | one dimension + one metric caps | pushes the model to drop requested columns |
| 199-201 | "prefer joining on fields marked Primary key / ID / 主键" | steers joins by name rather than by the `【Foreign keys】` block right above it |

These are display concerns leaking into a correctness-critical prompt. Chart
configuration already has its own separate prompt (`chart.system` / `chart.rules`).

---

### F-09 · `PROMPT_TIMEZONE` is inert on the main path
`backend/apps/chat/task/llm.py:1691` and `:1696` call
`datetime.now().strftime(...)` directly instead of `current_prompt_time()`
(`llm.py:78`). Only 2 of the 4 call sites use the fix. Since the image pins
Asia/Shanghai, every "this year / last month / today" question is still answered
in CST for every tenant — the exact condition H-17 was filed for.

---

### F-10 · The SQL shown to the user is not the SQL that ran
`backend/apps/chat/task/llm.py:~2490-2500`

`_maybe_raise_limit` and `_apply_nulls_last` are applied only to
`real_execute_sql`. The `sql` persisted by `check_save_sql` and streamed to the UI
as `type: 'sql'` is the pre-rewrite text. A user who copies the displayed SQL gets
different rows than the ones on screen — and F-03's silent `LIMIT 1 → LIMIT 1000`
is invisible in the displayed statement.

---

### F-11 · The grader plus the retry template push the model off legitimately-empty answers
`llm.py:2562` rejects any empty result while retry budget remains;
`template.yaml:648` then tells the model:

> "If a filter returned no rows, consider case-insensitive matching, LIKE
> patterns, date-range widening, **or removing a wrong condition**"

For a question whose true answer is the empty set, the loop pressures the model to
relax filters until it returns *something*. Empty-is-correct is a real answer
class and the retry prompt has no way to say so.

---

### F-12 · Fanout overrides the router's own "single" verdict
`llm.py:1186-1191`

```python
if is_retrieval:                       # any of list/show/find/all/get/each/every
    for cid in co_relevant:            # cosine >= max(0.40, primary - 0.20)
        fanout_ids.append(cid)
```

The LLM router's explicit `"single"` decision is discarded whenever the question
contains a listing keyword and any other datasource clears a 0.40 cosine floor.
On a workspace of similar spreadsheets that is most of them, so "show me Q3
revenue" fans out to up to 5 datasources and the answer becomes a UNION with a
`Source` column. Gated off in the benchmark (`ds_auto_selected` is False when a
datasource is pinned), so **no benchmark run has ever exercised it**.

---

## P2 — real but lower-magnitude

### F-13 · `LLM_MAX_OUTPUT_TOKENS = 1500` also caps reasoning tokens
`config.py:242`, `model_factory.py:98-111`. Passed as `max_tokens`, which on
reasoning models (gpt-oss) budgets reasoning **and** answer. A long reasoning
trace exhausts the cap before the JSON is emitted → empty content →
`SQL answer is not a valid json object` → NO-SQL. Consistent with the residual
NO-SQL bucket. Needs a separate reasoning allowance, or a per-model cap.

### F-14 · Stored table embeddings are never invalidated when the embedding model changes
`embedding/utils.py:7-8` raises on a dimension mismatch;
`table_embedding.py:103-114` catches it and falls back to **schema order** with an
error log. Switching `EMBEDDING_API_MODEL` (bundled 768-dim ↔ `mxbai-embed-large`
1024-dim) therefore silently disables relevance ranking for every datasource
embedded under the old model, until each is re-ingested.

### F-15 · `TABLE_EMBEDDING_COSINE_FLOOR = 0.0`
`config.py:153`. The floor exists and is documented, but ships disabled, so the
selector always returns exactly `TABLE_EMBEDDING_COUNT = 10` tables — ten
distractors on a two-table question.

### F-16 · The imported column descriptions are corrupt (data, but it reaches the model)
Verified in `core_field.custom_comment` and in the live prompt:

- `County Name ||| County Code` — BIRD's own CSV mislabels it; the importer copies verbatim.
- Prose rendered under a literal `values:` label: `(irc:bigint, values: Not useful)`,
  `(admfname2:text, values: SAME as 1)`, `(lastupdate:date, values: when is this record updated last time)`.
- The `[:220]` cap in `bird_import_descriptions.py:66` truncates mid-literal,
  leaving **unterminated quoted strings** the model can copy into a `WHERE`:
  `values: 'Directly funded', 'Locally funded', 'Not in CS funding`
- Two contradictory `values:` clauses in one tuple (`mailstate`, `doctype`,
  `fundingtype`, `edopsname`, `rtype`).

### F-17 · Primary keys are missing from the loaded BIRD schema
`california_schools` has a PK only on `satscores`; `frpm` and `schools` have none,
so `get_table_schema` emits no `Primary Key` markers for them and
`template.yaml:199-201` ("prefer joining on Primary key") has nothing to bind to.
`bird_setup_schemas.py` should create the PKs.

### F-18 · The two model-mode paths disagree on context size
`bird_eval.py:906` (`chat()`) sets `num_ctx = 8192`; `bird_eval.py:933`
(`ask_model()`) sets no `num_ctx` at all. The published model-only baseline and
the repair-loop path therefore ran at different context sizes.

---

## Recommended order

1. **F-01** — one regex; unblocks the identifier check for every datasource.
2. **F-02** — one predicate; without it F-01's fix still leaves a 19 % blind spot.
3. **F-03** — bounded keyword/structure fix; directly recovers superlative questions.
4. **F-04** — one constant; removes actively wrong hints from every prompt.
5. **F-05** — iterate in ranked order; the relevant table stops losing its samples.
6. **F-08 + F-06 + F-07** — split display rules out of the SQL prompt and stop
   emitting a Chinese rule set to non-Chinese users. This is the largest single
   lever on the 20 pp pipeline-vs-model gap, and it is the one that needs a
   benchmark run to price rather than an argument.

F-01 … F-05 are deterministic and testable without a GPU. F-06 … F-08 need one
`--mode pipeline --finish data` run each to price.
