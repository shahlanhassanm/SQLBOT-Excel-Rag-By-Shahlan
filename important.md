# IMPORTANT — SQLBot settings we changed (to make it faster)

The local GPU is slow (a virtual L40 vGPU), so each LLM call takes ~30–120 seconds.
To make questions answer faster, we changed the settings below **on purpose.**
Check the **Value** column: `false` = turned off, `true` = kept on.

Set in: `docker-compose.yaml` (under `environment:`)

| Setting | Value | What it means | Trade-off |
|---|---|---|---|
| `APEX_ENABLED` | **false** | Extra AI "planning" step before writing SQL (was 5–6 extra LLM calls per question) | Biggest speed-up. Slightly less smart on very complex/messy tables. |
| `AGENTIC_MULTI_CANDIDATE_ENABLED` | **true (ON)** | Writes a 2nd backup SQL query on retries | Costs 1 extra LLM call **only when a query has to retry**. Those retries are now slower, but more likely to end up correct. |
| `AGENTIC_DECOMPOSE_ENABLED` | **true (ON)** | Lets one question pull data from **multiple** Excel files at once | Kept ON for cross-file questions. Costs ~1+ extra LLM call on those. |

**APEX is now the only thing we turn off.** `AGENTIC_MULTI_CANDIDATE_ENABLED` used to be
off as well; it was switched back on so the sole disabled feature is APEX, which was by
far the largest share of the delay anyway.

## Accuracy features (added after the LLM-based text-to-SQL survey review)

Four techniques from the survey were implemented. **Three cost no extra LLM calls**
and are ON by default; the expensive one is OFF by default.

| Setting | Default | What it means | Cost |
|---|---|---|---|
| `AGENTIC_IDENTIFIER_CHECK_ENABLED` | **true** | Before running the SQL, checks every table/column name really exists in the schema, spelled exactly right. If not, retries with "you wrote X, the schema says Y". | **Free** (no LLM call). Can turn a would-be DB error into a smarter retry. |
| `VALUE_LINKING_ENABLED` | **true** | Looks up the real cell values behind words in the question ("Paid", "R&D") and shows them to the AI *before* it writes the query, so it stops guessing spellings. | **Free of LLM calls.** A few small read-only DB lookups, cached, and **only** when the question actually contains a name/word to look up. |
| `SKELETON_FEWSHOT_ENABLED` | **true** | Picks better example queries to show the AI — matching on question *shape* ("top N X by Y") rather than just topic. | **Free** (pure CPU re-ranking). |
| `AGENTIC_SELF_CONSISTENCY_ENABLED` | **false** | Writes 3 queries at once, runs all 3, and keeps the answer that the majority agree on. Catches wrong answers that still run without error. | **Expensive: 2 extra LLM calls on EVERY question.** Left OFF because of the slow GPU. Turn on only if accuracy matters more than speed. |

If you turn self-consistency on, `AGENTIC_SELF_CONSISTENCY_N` controls how many
queries are written (default 3). The extra queries run **in parallel** with the
main one, so the delay is roughly one extra LLM call's worth of waiting, not two.

**Note:** self-consistency is skipped automatically for users with row-level
permissions, because the permission filters are applied only to the main query —
voting in an unfiltered one could show data the user is not allowed to see.

### How to measure whether these help
Run the accuracy harness before and after:
```
docker exec -e EVAL_APEX=off sqlbot \
    sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/accuracy_eval.py"
```
Compare the CORRECT / PARTIAL / **WRONG** / SQL-ERR counts. `WRONG` means the SQL
ran but the answer was not in the result — that is the number self-consistency
and value linking are meant to reduce.

## Bug fix — multi-file (cross-datasource) answers
A code bug made cross-file questions **wrong**: every file's sub-query was
copying the SQL from the first file, so all files returned the first file's rows
(e.g. "list all the games" returned only Action Games, 4 times).

- **Cause:** each sub-query's prompt was being fed the previous answer's SQL as
  "conversation history", and the model copied it instead of using that file's
  own tables.
- **Fix:** cross-datasource sub-queries no longer inherit conversation history.
  File: `backend/apps/chat/task/llm.py` (`_skip_leg_history` flag).
- **Verified:** "list all the games" now returns each file's own rows
  (action_games=10, rpg_games=10, game_infra=30 → 50 combined, previously 10 wrong).
- ⚠️ The fix lives in the source code, so it is only in the image after a
  **`docker compose up -d --build`** (a plain `up -d` reuses the old image).

## Ollama (the AI model server)
- Model in use: `qwen2.5-coder:32b` (chat/SQL) + `mxbai-embed-large` (embeddings).
  Switched from `gpt-oss:20b` on 2026-07-28 — it scored 52.0% on BIRD versus the
  20b model's lower result, so it is the more accurate SQL writer here.
- ⚠️ The 32b weights are 19.9 GB and the card is 24 GB, so with the KV cache only
  **~68% of the model fits on the GPU** (the rest runs on CPU). Expect answers to
  be noticeably slower than with `gpt-oss:20b`. `qwen2.5-coder:14b` fits entirely
  in VRAM if speed matters more than accuracy.
- The model is **not** set in `docker-compose.yaml` — it lives in the `ai_model`
  table in Postgres (the "Model" page in the UI). The row named `QWEN 32B` is the
  default; the old `chat` (gpt-oss:20b) row is still there, just no longer default.
  To switch back, make that row the default in the UI, or:
  ```
  docker exec sqlbot psql -U root -d sqlbot -c \
    "update ai_model set default_model = (base_model = 'gpt-oss:20b');"
  ```
  It is read per question, so no restart is needed.
- `OLLAMA_KEEP_ALIVE` = **0**  → model unloads from GPU memory right after each answer
  (frees ~20 GB VRAM when idle, but the next question waits for the model to reload —
  ~15s for the 20b, longer for the 32b).
  Set in: `/etc/systemd/system/ollama.service.d/override.conf`

## How to turn something back ON
1. Open `docker-compose.yaml`, change the setting to `"true"` (or delete the line for the default).
2. Run:  `docker compose up -d`   (recreates the container with the new setting)

_Everything here is safe to change and fully reversible._
