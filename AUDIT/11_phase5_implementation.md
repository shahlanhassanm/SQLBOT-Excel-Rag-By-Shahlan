# Phase 5 — Batched Implementation Report (Groups A–G)

Branch `audit/phase5-fixes`. Safety tag `pre-rewrite-phase5` intact.
All work validated in the live `sqlbot` container.

---

## 1. What shipped

| Group | Theme | Issues | Commit |
|---|---|---|---:|
| A | Benchmark integrity | D-10, E-01, E-06 | earlier |
| D | Correctness | D-17, D-18, D-20, D-21, D-23, D-24 | `17fbe1e` |
| — | Phase 0–4 reports committed | — | `916ba94` |
| F | Dead code | D-27, D-28, D-29, D-30, D-31, D-32, D-34 | `6f13b28` |
| C | Cache / resources | D-13, D-14, D-15, D-16 | `ec230eb` |
| G | Config validation & tunables | H-01, H-02, H-03, H-06, H-08, H-09, H-10, H-12, H-14 | `732478f` |
| G2 | Consolidation & locale | H-11, H-16, H-17, H-19 | `40afb8e` |

Test suite: **272 → 634 passing** (+362), **0 failures**, deterministic across
repeated runs. The 24 pre-existing failures were all **D-19** (one file, path
fragility) and were closed in `20bb1f0`. `backend/tests`
stayed 28/28 at every step. Per-file `ruff`/`mypy` finished **at or below
baseline for every file touched**; two files improved materially
(`engine.py` ruff 7→0, mypy 23→4; `datasource.py` mypy 112→107).

---

## 2. Findings the implementation proved wrong

An audit is a hypothesis. Five entries did not survive contact with the code,
and are corrected in `ISSUES.md` rather than quietly closed.

| ID | Audit claimed | Actually |
|---|---|---|
| **D-32** | `common/utils/http_utils.py` is dead | **Load-bearing.** Zero references in this repo, but the closed-source xpack imports it at startup (`core → authentication.api → cas_client`). Deleting it was an outright boot failure. |
| **D-33** | "no migration creates `terms`" | `alembic/versions/002_ddl_autogenerate.py:22` **does** create and index it. The table exists in every deployed database; dropping the model is a migration decision, not cleanup. |
| **D-15** | A rotated API key leaves the old client **served** | `LLMConfig.__hash__` already covers `api_key`, so a rotated key yields a different entry and a fresh client. The real residue was that the stale client could not be *evicted*. |
| **H-13** | Ranking constants duplicated in two files | They are function-parameter defaults at a single site in `agentic.py`, plus one occurrence in `value_linking.py`. No duplication to remove. |
| **D-26** | Schema-name escaping is broken | Doubling `'` **is** the complete escape for a single-quoted literal. Only the control-character case was real. (Corrected in an earlier group.) |

### The one that matters most

`http_utils.py` is not a bookkeeping correction — it is a **methodological**
one. `sqlbot_xpack` is Cython-compiled; scanning all 34 MB of its `.so`/`.py`/
`.pyc` for `(?:common|apps)[\w.]+` yields **one** string. The import names are
not recoverable from the artifacts.

> **No grep of this repository can establish that a `common/` module is unused.**

Every deletion in Group F was therefore cleared by an empirical probe that
imports `main` and then force-imports all 50 xpack submodules via
`pkgutil.walk_packages`, checking for a `ModuleNotFoundError` naming a
non-xpack module. `import main` alone is **not** sufficient — it does not reach
every submodule. The probe is preserved in `tests/test_group_f_deadcode.py`
(`XPACK_OWNED`).

---

## 3. Defects fixed that were reachable in production

Not everything in the register was equally real. These were:

- **D-18** — `has_explicit_row_count` matched *any* 1–3 digit number anywhere,
  so "list all customers in region 3" was read as "the user asked for 3 rows"
  and disabled the completeness LIMIT lift — the exact failure the lift exists
  to fix. Now the number must be a standalone token within one word of a count
  term. CJK still matches (`前10条`) because the guard rejects an ASCII prefix,
  not a non-ASCII one.
- **D-24** — `insert_pg` duplicated the COPY loader *with* the un-rewound
  `StringIO` bug, so COPY read nothing and the slow `to_sql` silently did the
  work. Reachable: `/datasource/uploadExcel` is live at `datasource.py:334` and
  the frontend calls it (`frontend/src/views/ds/form.vue:195`).
- **D-13** — the relations cache key omitted the caller's permitted table/column
  view, so one user's filtered join graph was served to every other caller for
  3600 s.
- **D-16** — the 1000-row persistence cap was gated on `enable_sql_row_limit`,
  which `docker-compose` sets to **false**; unbounded result sets were
  serialised whole into a `Text` column.
- **H-16** — the bulk-user-import template is generated through `trans()`, so an
  English admin downloads a sheet containing "Enabled" while the validator
  compared against the literal `'已启用'`. **Every row of an English template
  failed.**
- **H-06** — `EMBEDDING_TERMINOLOGY_SIMILARITY = EMBEDDING_DEFAULT_SIMILARITY`
  binds the class-body *value*; overriding the parent in the environment moved
  nothing.
- **H-19** — `relations._quote` knew backtick dialects but not bracket dialects,
  so SQL Server identifiers were double-quoted there and bracketed everywhere
  else — in a module that branches on `sqlserver`/`mssql` elsewhere.

---

## 4. Deliberately not implemented

Per your instruction at the time, **D-39, D-11 and Q-01…Q-18 were not touched.**
(**D-37 was later approved and implemented** — see `86079bd` + `33bb500`.)
In addition, these were documented rather than guessed at, because the correct
answer is a deployment or architecture decision not derivable from the code:

| ID | Why not |
|---|---|
| **H-04** | `POSTGRES_PASSWORD` in three places incl. an image `ENV`. Removing it changes the deployment contract and breaks existing installs on upgrade. Now flagged at startup instead. |
| **H-05** | Ports as literals across `start.sh`, `main.py`, `g2-ssr/app.js`, `Dockerfile` — cross-language, cross-artifact, no test to protect it. |
| **H-07** | `env_file="../.env"`. Changing the search path could silently pick up a *different* env file on an existing install — the riskiest class of config change. |
| **H-18** | `oid = 1` in five places, `row_embeddings` with no tenant column. A multi-tenancy design decision; overlaps **Q-01**, which you excluded. |
| **D-22** | The duplicate result file is your untracked local artifact. It was restored byte-for-byte (md5 `afb15069…`) — deleting your local work is out of scope. **E-06** provenance is the real fix. |

---

## 5. Working-tree separation

Your 25 uncommitted files were preserved throughout. Two commits required care:

- **`model_factory.py`** — carries your `_with_output_cap` work. Only my
  `clear_cache` hunk (11 insertions) was staged, by building the index entry
  from `HEAD` + my change and then restoring the working tree. Your changes
  remain uncommitted.
- **`db.py`** — your changes were already in the WIP commit from the history
  rewrite, so the diff was my one line only. No mixed commit.

`AUDIT/00`–`05` turned out never to have been committed by any earlier phase
(the history rewrite carried `06`–`10`, `ISSUES.md` and `tools/` but not those
six). The files were intact on disk; `916ba94` puts them under version control.
No code is touched by that commit.

---

## 6. Benchmark status

The pipeline was re-verified end-to-end after all commits, and the three smoke
questions match the same-model baseline **exactly** (no regression).

**No post-fix benchmark has been run yet.** Launches were held after
verification found two defects that would have invalidated the results — **D-40**
(pipeline runs recorded a model they did not use) and **D-41** (the supervisor
stacked five concurrent harnesses on one GPU). Both are fixed; see
`09_production_readiness.md` §10.
