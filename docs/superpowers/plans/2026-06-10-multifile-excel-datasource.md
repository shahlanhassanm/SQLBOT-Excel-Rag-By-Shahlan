# Multi-file Excel — Bulk Datasource Upload Implementation Plan (PIVOTED)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Pivot note (user feedback):** the original plan merged files into one workbook/datasource. The user explicitly rejected merging: *each Excel file must stay its own datasource* (a multi-sheet workbook = one datasource with one table per sheet), and the **agentic finder** (hybrid ranking + decomposition, built 2026-06-10) routes questions to the relevant file/table at chat time. `excel_merge.py` was removed.

**Goal:** Upload many Excel/CSV files in one action; each file automatically becomes a complete, ready-to-query datasource (auto header detection, one PG table per sheet, embeddings triggered) — no per-file form filling.

**Architecture:** New backend endpoint `POST /datasource/addExcelDatasource` = the whole per-file pipeline (save → `parse_excel_preview` → import sheets to PG → `create_ds` with aes-encrypted configuration → ds embedding). The import logic is extracted from `import_to_db`'s inner into a shared `_import_excel_sheets()` (pure refactor). The UI adds a multi-select `el-upload` button on the datasource list page whose `action` is the new endpoint — element-plus posts one request per file, giving natural per-file progress/error handling.

### Task 1: Backend
**Files:** Modify `backend/apps/datasource/api/datasource.py`
- [ ] Extract `_import_excel_sheets(save_path, sheets: List[SheetFields], trans) -> list` from `import_to_db` (behavior-identical; `import_to_db` delegates)
- [ ] Add `POST /addExcelDatasource` (ws_admin): validates ext, saves file, previews (auto headers), imports sheets, dedupes ds name from file stem (workspace-scoped), `configuration = aes_encrypt(json{filename, sheets})`, `tables=[CoreTable(table_name=...)]`, `await create_ds(...)`, `run_save_ds_embeddings([ds.id])`; returns `{id, name, sheets}`

### Task 2: Frontend
**Files:** Modify `frontend/src/views/ds/Datasource.vue`, `frontend/src/i18n/{en,zh-CN,zh-TW,ko-KR}.json`
- [ ] Next to "new data source": `el-upload` (`multiple`, accept `.xlsx,.xls,.csv`, `:action=/datasource/addExcelDatasource`, `X-SQLBOT-TOKEN` headers, no file list) with secondary button `datasource.bulk_excel_upload`
- [ ] Track in-flight count; on each success/error decrement; when 0 → `search()` refresh + summary message
- [ ] i18n keys in 4 locales

### Task 3: Verify + deploy
- [ ] Container test: generate xlsx (2 sheets, junk header rows) + csv; call the endpoint pipeline functions directly (`parse_excel_preview` + `_import_excel_sheets`) against the live PG; assert tables exist with right row counts; cleanup
- [ ] py_compile; `docker compose build`; `up -d`; healthy; HTTP 200; `/datasource/addExcelDatasource` visible in OpenAPI

**Why this satisfies "ask questions across files":** all per-file datasources are ranked by the hybrid finder per question; cross-file questions go through decomposition + synthesis; sheets within one file JOIN naturally inside that datasource.
