# AUDIT / 05 — Open Questions

Things I could **not** determine from the code, and that I am not willing to
guess at. Per the ground rules, anything where "bug" and "deliberate" are both
plausible readings is logged here rather than in `01_defects.md`.

Each entry states what I observed, why it is ambiguous, and what answer would
resolve it.

---

## A. Intent behind gates and guards

### Q-01 · Is `is_normal_user()` meant to mean "id 1" or "admin"?
`backend/apps/datasource/crud/permission.py:76-77`

```python
def is_normal_user(current_user: CurrentUser):
    return current_user.id != 1
```

`UserInfoDTO` carries an `isAdmin` flag and a `weight`, and `apps/mcp/mcp.py:67`
computes admin as `id == 1 and account == 'admin'` — a *stricter* test than this
one. Three different notions of "privileged" coexist.

The consequence is not only permission bypass (**D-05**) but silent feature loss:
`llm.py:1061` uses the same predicate to disable cross-datasource decomposition,
so fanout only runs for id 1 (**D-12**).

**What resolves it:** is id-1-as-superuser a deliberate single-tenant shortcut,
or a placeholder that was never replaced? And in `decompose_question`, was the
intent "user has no row rules" (what the comment says) or "user is the admin"
(what the code does)?

---

### Q-02 · Is row-level permission meant to apply to prompt context?
`backend/apps/datasource/crud/datasource.py:503`, `backend/apps/datasource/value_index.py:91`

Column permissions **are** applied to the sample block and value hints; row
permissions are **not** (**D-02**). That asymmetry looks deliberate — someone
thought about permissions in this code path and applied one of the two.

Two coherent readings:
1. Row rules are a *result* filter, not a *context* filter; showing 50 raw rows
   to the model is acceptable because the final answer is still filtered.
2. It is an oversight.

Reading 1 is defensible for the model, but the sample block and the
`<value-hints>` block are also rendered in the UI's execution log, so a
row-restricted user can read them directly.

**What resolves it:** is the execution log considered privileged output?

---

### Q-03 · Is `--finish sql` the intended default for pipeline benchmarking?
`backend/tests/bird_eval.py:885`

The docstring for `ask_pipeline` explicitly says `finish='sql'` "measures less
than the UI has", yet it is the argparse default. Every completed pipeline run in
the repo used it.

**What resolves it:** was `sql` chosen for speed during development with the
intention of switching to `data` for the real measurement, or is it considered
the right comparison because it isolates generation from execution?

---

### Q-04 · Was `SERVER_IMAGE_HOST` meant to be configured, or is MCP imaging unused?
`docker-compose.yaml:28` sets it to the literal placeholder
`http://YOUR_SERVE_IP:MCP_PORT/images/`.

`request_picture` (`llm.py:3110-3114`) posts to the renderer, swallows any
exception into `_error`, and returns a URL built from this placeholder. So MCP
and non-streaming API replies contain an unresolvable image URL and no error.

**What resolves it:** is anyone consuming chart images over MCP? If not, the
cleanest answer is to disable image generation rather than ship a placeholder.

---

### Q-05 · Is `ops/ollama-override.conf` supposed to be applied?
It sets `OLLAMA_KEEP_ALIVE=-1`; `important.md:81` and
`docs/BENCHMARK-BIRD.md:112` both state the live host runs `OLLAMA_KEEP_ALIVE=0`
and describe the ~15 s per-question reload cost that causes.

Nothing in the build or compose applies the file.

**What resolves it:** is the file aspirational (a recommendation), or was it
applied and later reverted? The benchmark timings in the docs assume `0`.

---

## B. Ambiguity in the pipeline's own design

### Q-06 · Should `_maybe_raise_limit` use the original or the decomposed question?
`backend/apps/chat/task/llm.py:888-900`

It reads `self._original_question`. But `run_task` overwrites
`self.chat_question.question` with the primary sub-question after decomposition
(`llm.py:2201`), and `_run_secondary_leg` sets it per leg. So for a fanout leg,
the listing/aggregation classification is done against the *pre-decomposition*
text.

Usually harmless — fanout legs are listing questions by construction — but it
means a "split" decomposition whose sub-question is an aggregate still gets the
listing treatment.

**What resolves it:** is `_original_question` deliberately the classification
anchor (so the user's intent governs every leg), or should each leg classify its
own sub-question?

---

### Q-07 · Is `extract_nested_json` supposed to return the first or the last object?
`backend/common/utils/utils.py:60-80`

It collects every valid balanced JSON span and returns `results[0]`. The
harness's own `extract_sql` (`bird_eval.py:609`) deliberately takes `m[-1]` —
the *last* fenced block — with the comment that models preface prose.

Two files in the same repo, opposite conventions.

**What resolves it:** was `results[0]` chosen for a reason (e.g. models here
reliably answer first), or is it the accident that **D-09** describes? If the
former, a comment saying so would prevent the next reader filing the same defect.

---

### Q-08 · Is the `sys_arg` override meant to outrank environment variables?
`backend/apps/chat/task/llm.py:239-256`

Three settings (`chat.sqlbot_name`, `chat.limit_rows`,
`chat.context_record_count`) are read from the database **after** the settings
object is built, and overwrite it per `LLMService` instance. So for those three
the precedence is `DB > env`, while for the other 115 it is `env > default`.

An operator who sets `GENERATE_SQL_QUERY_LIMIT_ENABLED=false` in compose (as this
deployment does) can have it silently re-enabled by a UI toggle.

**What resolves it:** which layer is meant to win?

---

### Q-09 · Why does `_with_output_cap` apply only to the `openai` provider?
`backend/apps/ai_model/model_factory.py:98-122`

`OpenAILLM._init_llm` calls `_with_output_cap`; `OpenAIvLLM` and
`OpenAIAzureLLM` do not. The repetition-loop failure the cap exists to prevent
(the documented 45-minute question) is a property of the *model*, not of the
provider adapter.

**What resolves it:** was vLLM/Azure omitted deliberately (e.g. they already
enforce a cap server-side), or was it just the path that was being debugged?

---

## C. Data, deployment and repository hygiene

### Q-10 · Are the workbooks in `data/sqlbot/excel/` fixtures or live user data?
20+ files including `finance_sample_100_rows_with_answers_*.xlsx`,
`Overdue Transactions_*.xlsx`, `DuitNow_Payment_Gateways_*.xlsx` and
`Indian_general_election_in_Madras,_1957_*.xlsx`, plus their
`.sqlbot_headers.json` sidecars.

They sit inside the repo working tree (bind-mounted at
`docker-compose.yaml:79`) and are gitignored, so they are not committed — but
they are in the checkout.

**What resolves it:** if this is customer data, the checkout directory is the
wrong place for it and the volume should point outside the repo.

---

### Q-11 · Is `privileged: true` required?
`docker-compose.yaml:9`

The container runs Postgres with a bind-mounted data directory, which sometimes
motivates it, but nothing in the code obviously needs kernel capabilities.

**What resolves it:** was it added to solve a specific permission error, or
copied from an example?

---

### Q-12 · Is unpinned dependency resolution intentional?
`.gitignore` excludes `*.lock` and `*-lock.json`. No `uv.lock` and no
`package-lock.json` exist in the repo, but the running container **has** a
`uv.lock` at `/opt/sqlbot/app/uv.lock` inherited from the published base image —
i.e. the deployed dependency set is reproducible but the repository cannot
reproduce it.

Compounding: the default Python index is
`http://mirrors.aliyun.com/pypi/simple` — **plain HTTP** — and `sqlbot-xpack`,
a hard runtime dependency, comes from **testpypi**.

**What resolves it:** is the `*.lock` gitignore inherited from upstream and
unintentional here?

---

### Q-13 · Is `data/postgresql/` supposed to be uid-`dnsmasq` owned?
The bind-mounted Postgres data directory is mode 0700 owned by uid 108
(`dnsmasq` on the host). That is the container's `postgres` uid colliding with a
host user — normal for bind mounts, but it means the directory is unreadable to
the repo owner and cannot be backed up without root.

**What resolves it:** is there a backup procedure? Nothing in the repo describes one.

---

### Q-14 · Which of the two test trees is canonical?
`tests/` (296 tests) and `backend/tests/` (28 tests + 13 harness scripts) overlap
(`test_header_detection.py` vs `test_header_detection_alltext.py`), share no
`conftest.py`, and neither is referenced by any runnable script — the only test
scripts (`backend/scripts/test.sh`) point at a non-existent `app/` package.

**What resolves it:** which directory should CI run, and from which working
directory? The answer also determines whether **D-19** is a bug or a
misunderstanding on my part.

---

### Q-15 · Are `bird_32b20k_150.json` and `bird_qwen32b20k_150.json` two runs or one?
They are byte-identical (md5 `afb15069…`). Two names imply two experiments.

**What resolves it:** was one a copy made before a re-run that never happened?
Until answered, neither can be cited.

---

### Q-16 · Is `docs/BENCHMARK-BIRD.md`'s "no completed pipeline run" statement stale?
The doc says the pipeline-versus-model comparison "is still open". Two completed
150-question pipeline runs exist as untracked files, and running the repo's own
`bird_significance.py` on them gives a clear (if null) answer: −3.3 pp,
p = 0.47.

**What resolves it:** were those runs considered invalid for some reason not
recorded in the file, or simply not yet written up?

---

### Q-17 · What is `sqlbot_xpack` actually doing?
Imported at `main.py:4` and in six other modules; provides license gating, custom
prompts, the audit resource query, dynamic CORS and `monitor_app(app)`. It is
closed-source, pinned to testpypi, and registers FastAPI routes **after** every
middleware and router in this repo (`main.py:214`).

Nothing in this audit can say what those routes are, what `monitor_app` monitors,
or where it sends anything.

**What resolves it:** access to the package source, or a statement of what it
registers and what it phones home about. Until then it is an unauditable
component sitting inside the authentication perimeter.

---

### Q-18 · Is the embedded-token two-step decode deliberate?
`backend/apps/system/middleware/auth.py:182-205`

The token is decoded once with `verify_signature: False` to read `appId` /
`embeddedId`, the assistant is looked up by that unverified id, and only then is
the token re-decoded against `assistant_info.app_secret`. The code carries its
own warning comment about the first decode.

The re-decode does make the flow sound — an attacker cannot forge a token — but
the unverified id is used for a database lookup first, which is an enumeration
oracle.

**What resolves it:** is the two-step decode required because the key id lives in
the payload (a standard problem, normally solved with a `kid` header), or could
the id be carried in a header instead?

---

## D. Things I deliberately did not test

| Item | Why not |
|---|---|
| Actually exploiting the path traversals (**D-06**, **D-07**) | Would read files outside the app's directory on a live system. The code path is unambiguous from reading it. |
| Actually exploiting the IDORs (**D-03**, **D-04**) | Would require creating a second user and reading another account's data on a live instance. |
| Planting a quote-bearing spreadsheet header (**D-08**) | Would create a real datasource with a malicious identifier in the live database. |
| Re-running BIRD generation | 2–10 h of exclusive GPU per pass. The scoring half was reproduced instead — see `03_eval_integrity.md` §3. |
| Load-testing the 400-thread ceiling (**D-14**) | Would take the running instance down. |
| Anything inside `sqlbot_xpack` | Source not available (Q-17). |
