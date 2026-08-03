# Auto-Generated Datasource Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At embed time, auto-generate a concise LLM summary of what each datasource contains and store it as the datasource `description` (only when empty/generic), so the finder's name+description LLM pick and the embedding both route correctly — no manual naming.

**Architecture:** Fold summary generation into the existing background embedding pass (`save_ds_embedding`). A pure `generate_ds_summary` builds a prompt from the schema+sample text and calls an injected LLM invoker; a sync `invoke_default_llm` wraps the configured default model. A `should_replace_description` predicate guards against clobbering user-written descriptions.

**Tech Stack:** Python 3, SQLAlchemy, LangChain chat model via `LLMFactory`/`get_default_config`, mxbai embeddings, pytest. Spec: `docs/superpowers/specs/2026-06-24-auto-datasource-summary-design.md`.

---

## Environment notes (READ FIRST)

- **NOT a git repo** — ignore `git commit`. Each task ends with a **Checkpoint** validated in the running `sqlbot` container (healthy).
- Tests in-container: `docker cp tests/<f>.py sqlbot:/tmp/ && docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/<f>.py -q"`. Validate imports with `import main`.
- Host `backend/...` ↔ container `/opt/sqlbot/app/...`. Use `docker cp` for fast iteration; full functional checks need `docker compose build && up`.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/common/core/config.py` | `DS_SUMMARY_ENABLED`, `DS_SUMMARY_MAXLEN` | Modify |
| `backend/apps/datasource/crud/table.py` | `should_replace_description`, `generate_ds_summary`; wire into `save_ds_embedding` | Modify |
| `tests/test_ds_summary.py` | Unit tests for the two pure helpers | **Create** |
| `backend/apps/ai_model/model_factory.py` | `invoke_default_llm` sync wrapper | Modify |
| `tests/test_ds_summary_e2e.py` | Integration: description populated + finder observation | **Create** |

---

## Task 1: Settings

**Files:** Modify `backend/common/core/config.py`

- [ ] **Step 1: Add settings**

After the `EXCEL_FTS_ENABLED: bool = True` line (added in the prior feature), add:
```python
    # --- Auto-generated datasource summary (finder description) ---
    DS_SUMMARY_ENABLED: bool = True
    DS_SUMMARY_MAXLEN: int = 600
```
In the `@field_validator(...)` boolean-name list, add:
```python
                     'DS_SUMMARY_ENABLED',
```

- [ ] **Step 2: Validate**
```bash
docker cp "backend/common/core/config.py" sqlbot:/opt/sqlbot/app/common/core/config.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'from common.core.config import settings as s; print(s.DS_SUMMARY_ENABLED, s.DS_SUMMARY_MAXLEN)'"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: `True 600`; import no error.

- [ ] **Step 3: Checkpoint** — `import main` clean.

---

## Task 2: Pure helpers (`should_replace_description`, `generate_ds_summary`)

**Files:** Modify `backend/apps/datasource/crud/table.py`; Test `tests/test_ds_summary.py`

- [ ] **Step 1: Write failing tests** — create `tests/test_ds_summary.py`:
```python
"""Run in-container:
    docker cp tests/test_ds_summary.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_summary.py -q"
"""
from apps.datasource.crud.table import should_replace_description, generate_ds_summary


def test_should_replace_description_true_cases():
    assert should_replace_description(None) is True
    assert should_replace_description('') is True
    assert should_replace_description('   ') is True
    assert should_replace_description('Excel file: foo.xlsx') is True
    assert should_replace_description('  Excel file: bar.csv  ') is True


def test_should_replace_description_false_for_user_text():
    assert should_replace_description('A dataset of movies with title, cast, plot') is False


def test_generate_ds_summary_builds_prompt_and_trims():
    captured = {}
    def fake_llm(prompt):
        captured['p'] = prompt
        return '  A dataset of Netflix movies and shows: title, cast, description.  '
    out = generate_ds_summary('col', '# Table: Movies\n(title:text)\nsample: "Money Heist"',
                              fake_llm, maxlen=600)
    assert 'col' in captured['p'] and 'Movies' in captured['p']  # name + context in prompt
    assert out == 'A dataset of Netflix movies and shows: title, cast, description.'


def test_generate_ds_summary_caps_length():
    out = generate_ds_summary('x', 'ctx', lambda p: 'z' * 1000, maxlen=50)
    assert len(out) <= 50


def test_generate_ds_summary_empty_on_error():
    def boom(prompt):
        raise RuntimeError('llm down')
    assert generate_ds_summary('x', 'ctx', boom, maxlen=600) == ''


def test_generate_ds_summary_empty_on_blank_output():
    assert generate_ds_summary('x', 'ctx', lambda p: '   ', maxlen=600) == ''
```
Run; expect ImportError.

- [ ] **Step 2: Run to verify it fails**
```bash
docker cp tests/test_ds_summary.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_summary.py -q"
```
Expected: ImportError.

- [ ] **Step 3: Implement in `backend/apps/datasource/crud/table.py`** — add both functions (place them above `save_ds_embedding`):
```python
_GENERIC_DESC_RE = re.compile(r'^\s*Excel file:\s', re.IGNORECASE)


def should_replace_description(desc) -> bool:
    """True when a datasource description is empty or the generic auto string,
    so it is safe to overwrite with a generated summary (never clobbers a
    user-written description)."""
    if desc is None:
        return True
    s = str(desc).strip()
    if s == '':
        return True
    return bool(_GENERIC_DESC_RE.match(s))


_DS_SUMMARY_PROMPT = (
    "You are cataloguing datasets so questions can be routed to the right one. "
    "In 1-3 plain sentences, summarise WHAT this dataset contains: its subject/domain, "
    "its key columns, and a few example values. No markdown, no headings, no bullet "
    "points. Output only the summary.\n\n"
    "Dataset name: {name}\nSchema and sample values:\n{context}\n\nSummary:"
)


def generate_ds_summary(ds_name, context_text, llm_invoke, maxlen: int) -> str:
    """Generate a concise NL summary of a datasource's contents via the injected
    ``llm_invoke(prompt) -> str``. Best-effort: returns '' on any error or blank
    output. ``context_text`` is the schema+sample text (e.g. build_ds_schema_text
    with samples)."""
    try:
        prompt = _DS_SUMMARY_PROMPT.format(name=ds_name or '', context=context_text or '')
        out = llm_invoke(prompt)
    except Exception:
        return ''
    s = (out or '').strip()
    if not s:
        return ''
    return s[:maxlen]
```
`re` is already imported in this file (used elsewhere); if not, add `import re` at the top.

- [ ] **Step 4: Run to verify pass**
```bash
docker cp "backend/apps/datasource/crud/table.py" sqlbot:/opt/sqlbot/app/apps/datasource/crud/table.py
docker cp tests/test_ds_summary.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_ds_summary.py -q"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: 6 passed; import clean.

- [ ] **Step 5: Checkpoint** — `import main` clean.

---

## Task 3: `invoke_default_llm` (sync wrapper for the configured model)

**Files:** Modify `backend/apps/ai_model/model_factory.py`

- [ ] **Step 1: Implement** — append to `backend/apps/ai_model/model_factory.py`:
```python
def invoke_default_llm(prompt: str) -> str:
    """Synchronously invoke the configured DEFAULT chat model with a plain prompt
    and return its text. Usable from background threads (resolves the async
    ``get_default_config`` on a fresh event loop). Raises on failure — callers
    treat it as best-effort."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        config = loop.run_until_complete(get_default_config())
    finally:
        loop.close()
    llm = LLMFactory.create_llm(config).llm
    res = llm.invoke(prompt)
    return getattr(res, 'content', None) or str(res)
```
(`get_default_config`, `LLMFactory` are defined in this same module.)

- [ ] **Step 2: Validate import**
```bash
docker cp "backend/apps/ai_model/model_factory.py" sqlbot:/opt/sqlbot/app/apps/ai_model/model_factory.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'from apps.ai_model.model_factory import invoke_default_llm; print(\"ok\")'"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: `ok`; import clean. (Functional LLM call is exercised by the Task 5 E2E.)

- [ ] **Step 3: Checkpoint** — proceed.

---

## Task 4: Wire summary into `save_ds_embedding`

**Files:** Modify `backend/apps/datasource/crud/table.py` (`save_ds_embedding`)

- [ ] **Step 1: Modify the per-datasource loop**

READ `save_ds_embedding`. Its loop currently is:
```python
        for _id in ids:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == _id).first()
            schema_table = build_ds_schema_text(session, ds, include_samples=True)
            emb = json.dumps(model.embed_query(schema_table))

            stmt = update(CoreDatasource).where(and_(CoreDatasource.id == _id)).values(embedding=emb)
            session.execute(stmt)
            session.commit()
```
Replace it with:
```python
        for _id in ids:
            ds = session.query(CoreDatasource).filter(CoreDatasource.id == _id).first()
            if ds is None:
                continue

            schema_table = build_ds_schema_text(session, ds, include_samples=True)

            # Auto-summary: give the finder a meaningful description when the
            # datasource has none / only the generic auto one. Best-effort; a
            # failure here must never block embedding.
            if settings.DS_SUMMARY_ENABLED and should_replace_description(ds.description):
                try:
                    from apps.ai_model.model_factory import invoke_default_llm
                    summary = generate_ds_summary(ds.name, schema_table, invoke_default_llm,
                                                  settings.DS_SUMMARY_MAXLEN)
                    if summary:
                        ds.description = summary
                        session.execute(update(CoreDatasource).where(
                            CoreDatasource.id == _id).values(description=summary))
                        session.commit()
                        # rebuild so the embedding includes the new description
                        schema_table = build_ds_schema_text(session, ds, include_samples=True)
                except Exception:
                    SQLBotLogUtil.exception(f'auto-summary failed for ds {_id}')

            emb = json.dumps(model.embed_query(schema_table))
            stmt = update(CoreDatasource).where(and_(CoreDatasource.id == _id)).values(embedding=emb)
            session.execute(stmt)
            session.commit()
```
(`settings`, `update`, `and_`, `SQLBotLogUtil`, `build_ds_schema_text`, `json`, `model` are all already in scope in this function.)

- [ ] **Step 2: Validate import/compile**
```bash
docker cp "backend/apps/datasource/crud/table.py" sqlbot:/opt/sqlbot/app/apps/datasource/crud/table.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m py_compile apps/datasource/crud/table.py && echo compile-ok"
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: `compile-ok`; import clean. (Functional check = Task 5.)

- [ ] **Step 3: Checkpoint** — proceed to Task 5.

---

## Task 5: Deploy, verify, and re-summarize existing datasources

**Files:** Create `tests/test_ds_summary_e2e.py`

- [ ] **Step 1: Rebuild + deploy**
```bash
docker compose build && docker compose up -d
```
Wait until `docker inspect -f '{{.State.Health.Status}}' sqlbot` is `healthy`.

- [ ] **Step 2: Write the integration test** — create `tests/test_ds_summary_e2e.py`:
```python
"""Verifies a generic-description datasource gets a real generated summary, and
reports finder behavior for a content question. Run in-container:

    docker cp tests/test_ds_summary_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_ds_summary_e2e.py"
"""
import io
import json
import sys
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text  # noqa: E402
from common.core.db import engine as meta  # noqa: E402
from common.utils.embedding_threads import run_save_ds_embeddings  # noqa: E402

BASE = 'http://localhost:8000/api/v1'


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def login():
    import asyncio
    import urllib.parse
    from common.utils.crypto import sqlbot_encrypt
    from common.core.config import settings
    loop = asyncio.new_event_loop()
    try:
        acc = loop.run_until_complete(sqlbot_encrypt('admin'))
        pwd = loop.run_until_complete(sqlbot_encrypt(settings.DEFAULT_PWD))
    finally:
        loop.close()
    d = urllib.parse.urlencode({'username': acc, 'password': pwd}).encode()
    res = http('POST', f'{BASE}/login/access-token', d, {'Content-Type': 'application/x-www-form-urlencoded'})
    tok = (res['data'].get('access_token') if isinstance(res.get('data'), dict)
           else res.get('access_token') or res.get('data'))
    assert tok, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {tok}'}


def make_xlsx() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = 'shows'
    ws.append(['title', 'cast', 'description'])
    ws.append(['Money Heist', 'Ursula Corbero', 'A criminal mastermind plans the biggest heist'])
    ws.append(['Some Movie', 'Jane Doe', 'A romance set in Paris'])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def upload(headers, content):
    b = 'sqlbotSummBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="shows.xlsx"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + f'\r\n--{b}--\r\n'.encode()
    h = dict(headers); h['Content-Type'] = f'multipart/form-data; boundary={b}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def run():
    import time
    headers = login()
    created = []
    try:
        d = upload(headers, make_xlsx()).get('data', {})
        ds_id = d.get('id'); created.append((ds_id, d.get('name')))
        assert ds_id, f'upload failed: {d}'

        # Force the summary+embed pass synchronously enough to observe it.
        run_save_ds_embeddings([ds_id])
        # wait for the background thread to finish (summary LLM call can be slow on qwen)
        desc = None
        for _ in range(60):
            with meta.connect() as c:
                desc = c.execute(text("select description from core_datasource where id=:i"),
                                 {"i": ds_id}).scalar()
            if desc and not desc.startswith('Excel file:'):
                break
            time.sleep(5)

        assert desc, 'description is empty'
        assert not desc.startswith('Excel file:'), f'description still generic: {desc!r}'
        assert len(desc.strip()) > 10, f'summary too short: {desc!r}'
        print(f'SUMMARY OK -> description now: {desc!r}')
        print('DS SUMMARY E2E PASS')
    finally:
        for i, n in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{i}/{n}', b'', headers)
            except Exception:
                pass
```

- [ ] **Step 3: Run the integration test**
```bash
docker cp tests/test_ds_summary_e2e.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_ds_summary_e2e.py"
```
Expected: prints `SUMMARY OK -> description now: '<a real summary mentioning shows/movies/title/cast>'` and `DS SUMMARY E2E PASS`. (The exact summary text is qwen-dependent; the hard assertion is that the description is populated and no longer generic.)

- [ ] **Step 4: Re-summarize + re-embed ALL existing datasources** (one-shot; populates descriptions for prior uploads)

Create `tests/_resummarize_all.py` (a script file avoids inline shell-quoting hazards):
```python
import time
import sys
sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text
from common.core.db import engine
from common.utils.embedding_threads import run_save_ds_embeddings

with engine.connect() as c:
    ids = [r[0] for r in c.execute(text("select id from core_datasource")).fetchall()]
run_save_ds_embeddings(ids)
time.sleep(min(600, 25 * len(ids)))   # one qwen summary call per ds; allow generous time
with engine.connect() as c:
    rows = c.execute(text(
        "select id, name, substr(coalesce(description, ''), 1, 90) as d "
        "from core_datasource order by id")).fetchall()
for r in rows:
    print(r[0], r[1], "->", r[2])
```
Run it:
```bash
docker cp tests/_resummarize_all.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/_resummarize_all.py"
```
Expected: most/all datasources now print a real summary after `->` instead of empty or `Excel file: …`. Slow (one qwen summary per datasource), runs with a generous wait.

- [ ] **Step 5: (Observational) re-ask the failing questions** in the chat UI: "money heist", and the bride-to-be plot. Expected: they now route to a relevant datasource (e.g. `col`/`dataset_1000_rows`) instead of "No matching datasource". If a question still misroutes among several movie datasets, that's the known multi-movie-dataset ambiguity (separate from this fix). Record what you observe; not a hard gate.

- [ ] **Step 6: Checkpoint** — Tasks 1–4 unit/compile green, E2E shows a populated non-generic description, existing datasources re-summarized. Feature complete.

---

## Notes for the implementer

- **Order:** 1 → 2 → 3 → 4 → 5. Tasks 2 is unit-tested; 3/4 are verified by the Task 5 E2E after a rebuild.
- **Never clobber a user description** — the `should_replace_description` guard is load-bearing; keep it.
- **Best-effort everywhere** — a failed/slow/missing LLM must leave the existing description and still embed. Off-switch: `DS_SUMMARY_ENABLED=False`.
- Don't touch the finder prompt — the summary populates the `description` the prompt already reads.
