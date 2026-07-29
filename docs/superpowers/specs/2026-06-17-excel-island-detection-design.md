# Excel Island Detection — Design Spec

- **Date:** 2026-06-17
- **Status:** Approved design (pre-implementation)
- **Scope chosen:** Headless (vertical stacking + phantom-column trimming), side-by-side splitting available behind a default-off flag.
- **Reference:** `ETretyakov/rag-with-tables` — `internal/ingest/islands/detector.go` (+ `detector_test.go`). Algorithm ported, not vendored.

## 1. Problem & context

SQLBot imports each Excel/CSV **sheet as exactly one table** (`_import_excel_sheets` in
`backend/apps/datasource/api/datasource.py`). Real spreadsheets routinely place several
unrelated tables on one sheet (a logo + a blank row + a sales table + a budget table
beside it), and Excel auto-generates up to 16,384 column headers, so a naive whole-sheet
read produces one garbage table with thousands of phantom columns.

SQLBot already has the pieces this builds on:

- `utils/header_detection.py` — `resolve_header_row` (scores the real header row),
  `read_raw` (first 25 rows, `header=None`), `read_sheet` (full sheet at a chosen header),
  and a per-file header sidecar.
- `utils/excel.py` — `parse_excel_preview`, `_preview_one_sheet`, field-type inference.
- `api/datasource.py` — `/parseExcel` (interactive preview) and `/addExcelDatasource`
  (one-shot bulk upload) both funnel into `_import_excel_sheets`.

The two import paths differ and must be treated differently:

- **Bulk** `add_excel_datasource` auto-generates its layout via `parse_excel_preview`
  (one entry per sheet) — **this is where island detection is injected.**
- **Interactive** `import_to_db` passes a user-confirmed layout — **must stay byte-for-byte
  unchanged** in this headless scope.

## 2. Goals / non-goals

**Goals**
- Split one sheet into N tables when it contains N stacked tables (empty-row separators).
- Drop phantom columns (header present, no data) at the outer edges of a table.
- Reuse `resolve_header_row` *per region* (better than the reference, which assumes the
  band's first row is the header).
- Never regress clean single-table files.
- Fix the dead `COPY` insert path: the `StringIO` is never rewound, so `COPY` reads nothing
  and the slow `to_sql` silently does the insert (correct row count today, but slow and
  fragile).

**Non-goals (this iteration)**
- No frontend / preview-UI changes (headless).
- Side-by-side (2D) splitting ships but is **off by default**.
- No schema normalization / type-inference overhaul (separate audit item #4).
- No change to non-Excel datasources.

## 3. Chosen approach — region as an optional field

Add a pure `utils/island_detection.py`. Have the **bulk** path expand each sheet into one
spec **per detected region** and add an optional `region` box to `SheetFields`. When
`region is None` (interactive path, and clean single-table files) behavior is identical to
today; when set, `_import_excel_sheets` builds the table from a raw slice instead of a
whole-sheet read.

Rejected alternatives: a parallel `_import_excel_regions` importer (duplication); a
temp-file pre-pass that writes each island to disk (hacky I/O).

## 4. Detection algorithm

Ported from the reference `Detect`, operating on a full raw frame read with `header=None`.
A cell is empty iff `header_detection._is_blank(cell)` (None / NaN / whitespace-only).

1. **Row bands.** Walk rows top→bottom; a fully-empty row separates bands. Each maximal run
   of non-empty rows is a band.
2. **Column groups per band.**
   - *Multi-row band:* find `left_data`/`right_data` = leftmost/rightmost columns with any
     data in the **non-first** rows of the band. Columns outside `[left_data, right_data]`
     are dropped (phantom trimming, both leading and trailing).
   - *`split_side_by_side=False` (default):* keep `[left_data, right_data]` as a single
     column group → one region. (A genuine all-empty spacer column will **not** cut a table.)
   - *`split_side_by_side=True`:* within `[left_data, right_data]`, a column is "active" if it
     has any non-empty cell in the full band (header included); consecutive active columns
     form a group, interior empty columns separate side-by-side tables.
   - *Header-only single-row band:* group consecutive non-empty cells of that row.
3. Return `Bounds(min_row, max_row, min_col, max_col)` (inclusive, 0-based) per region.

## 5. Components

### 5.1 NEW `backend/apps/datasource/utils/island_detection.py` (pure, no I/O)

```python
class Bounds(NamedTuple):
    min_row: int; max_row: int; min_col: int; max_col: int

def detect_islands(raw: pd.DataFrame, split_side_by_side: bool = False,
                   min_rows: int = 1) -> list[Bounds]: ...
```

- `raw` is a `header=None` frame (the full sheet).
- `min_rows`: discard regions whose row span is below this (filters stray single cells when
  desired; default 1 keeps header-only tables).
- Deterministic, fully unit-testable, no settings/DB imports (mirrors `apex_helpers.py`).

### 5.2 EXTEND `utils/header_detection.py`

Add a full-sheet raw read (existing `read_raw` is capped at `DETECT_ROWS=25` and cannot be
reused for whole-sheet band detection):

```python
def read_raw_full(save_path: str, sheet_name=None) -> pd.DataFrame:
    # same read options as read_raw (header=None), without the nrows cap
```

### 5.3 EXTEND `utils/excel.py`

```python
def detect_island_specs(save_path: str) -> list[dict]:
    """For each sheet: read_raw_full -> detect_islands -> per region:
    slice raw -> resolve_header_row(slice) -> infer fields. Returns a list of
    region-bearing sheet specs: {sheetName, region:[r0,r1,c0,c1], regionIndex,
    headerRow, fields:[{fieldName, fieldType}]}."""
```

- Reuses the per-sheet logic of `_preview_one_sheet`, applied to each region slice.
- Header row is resolved **relative to the region slice**, then expressed as an absolute
  row index for the importer.
- Degradation rule (see §8) lives here: trivial single island → emit a `region=None` spec.

### 5.4 EXTEND `models/datasource.py`

```python
class SheetFields(BaseModel):
    sheetName: str
    fields: List[FieldInfo]
    headerRow: int | None = None
    region: list[int] | None = None       # [min_row, max_row, min_col, max_col]
    regionIndex: int | None = None         # 0-based ordinal within the sheet, for naming
```

Backward compatible: omitted `region` ⇒ today's behavior. `import_to_db` payloads are
unaffected.

### 5.5 EXTEND `_import_excel_sheets`

- When `sheet_info.region is None`: **current code path, unchanged.**
- When `region` is set: take the (cached, per sheet) full raw frame, slice
  `rows[min_row:max_row+1], cols[min_col:max_col+1]`. The header index **within the slice**
  is `headerRow - min_row` (`headerRow` is stored absolute); use that row as column names,
  drop it and everything above it, coerce dtypes from `fields`, then create the table.
- **Insert-path fix:** today the function calls `df.to_sql(..., if_exists='replace')` (which
  inserts all rows) **and then** `cursor.copy_expert(COPY … FROM STDIN, file=output)` — but
  `output` (a `StringIO`) is never rewound (`output.seek(0)` is commented out), so `COPY`
  reads from end-of-buffer, inserts **zero** rows, and the slow `to_sql` is silently doing
  the real insert. Implement the intended design: `df.head(0).to_sql(if_exists='replace')`
  for schema only, `output.seek(0)`, then one `COPY` (fast path) with `NULL ''` so blank
  cells map to NULL. Net effect: same row count, much faster. Extracted into a shared
  `_insert_df_to_pg(df, table_name, engine)` used by **both** branches.

### 5.6 EXTEND `add_excel_datasource`

In `inner()`, replace the `parse_excel_preview` → one-spec-per-sheet construction with
`detect_island_specs(save_path)` → region-bearing specs, then call `_import_excel_sheets`
as today. Datasource creation, naming, and `run_save_ds_embeddings` are unchanged (a sheet
that yields multiple regions simply contributes multiple `CoreTable` rows).

### 5.7 Settings (`common/core/config.py`)

```python
EXCEL_ISLAND_DETECTION_ENABLED: bool = True   # master switch (off ⇒ exact current behavior)
EXCEL_SPLIT_SIDE_BY_SIDE: bool = False         # 2D interior-empty-column splitting
EXCEL_ISLAND_MIN_ROWS: int = 1
```

Env-overridable like the existing `AGENTIC_*` flags.

## 6. Data flow (bulk path)

```
upload .xlsx
  └─ add_excel_datasource
       └─ detect_island_specs(save_path)
            for each sheet:
              read_raw_full ─► detect_islands ─► [Bounds...]
                 for each Bounds: slice ─► resolve_header_row ─► fields
            ─► [SheetFields(region=..., regionIndex=...) ...]
       └─ _import_excel_sheets(specs)
            region set ─► slice+header+dtype ─► one PG table (single insert)
       └─ create datasource (N CoreTable) ─► run_save_ds_embeddings
```

## 7. Table naming

- Sheet yields **one** region: `{sheetName}_{hash}` (unchanged).
- Sheet yields **multiple** regions: `{sheetName}_r{regionIndex}_{hash}`.

## 8. Graceful degradation / safety

- `EXCEL_ISLAND_DETECTION_ENABLED=False` ⇒ bulk path uses the old `parse_excel_preview`
  flow verbatim.
- **Trivial single island** (one region covering the whole used range, starting at row 0 /
  col 0, no phantom trim) ⇒ emit `region=None` ⇒ identical to today.
- A single **offset/trimmed** island (leading blank rows/cols or phantom columns) ⇒ use the
  region ⇒ phantom trimming benefit, still one table.
- Any exception in detection ⇒ log and fall back to a single `region=None` spec for that
  sheet. **Never worse than current behavior.**

## 9. Testing

**Unit (`tests/test_island_detection.py`)** — port the reference `detector_test.go` cases to
pandas frames:
nil/empty, single cell, one simple table, header-only, two tables (1 and multiple empty-row
separators), row+col offset, leading empty cols, leading+trailing empty cols, phantom
trailing headers, phantom leading column, interior-sparse-column-kept, sparse data rows,
variable-length/ragged rows, multi-table combos. Side-by-side cases asserted under both flag
values (single region when off; split when on).

**Unit** — `detect_island_specs` header resolution: a region with a title row above the
header resolves the correct absolute `headerRow`.

**Unit** — double-insert fix: row count after import equals source row count (no doubling).

**E2E (`tests/test_island_excel_e2e.py`, in-container against live app, admin login)** —
upload a 2-island `.xlsx` via `/addExcelDatasource`; assert the datasource has 2 tables with
the expected names and exact row counts.

## 10. Out of scope

Frontend/preview rendering of islands; user edit/merge/discard of islands; schema-header
normalization and richer type inference (audit #4); non-Excel sources; Parquet/DuckDB
storage (the reference's storage layer — SQLBot uses Postgres).

## 11. Risks

- **Full-sheet raw read** (`read_raw_full`, `dtype=object`) is memory-heavier than the 25-row
  `read_raw`; acceptable at Excel scale, noted for very large files.
- **Over-segmentation** if `split_side_by_side` is ever enabled on tables with genuine empty
  spacer columns — mitigated by default-off.
- **pandas vs excelize raggedness:** pandas reads rectangular (pads with NaN) where excelize
  trims trailing cells; the algorithm keys on cell-emptiness, not row length, so phantom
  trimming still holds — covered by the ragged-row unit cases.

## 12. Estimate (~1.5–2 days)

| Piece | Est. |
|---|---|
| `island_detection.py` + ported unit tests | 0.5–0.75 day |
| `read_raw_full` + `detect_island_specs` + `SheetFields.region` | 0.25 day |
| `_import_excel_sheets` region branch + double-insert fix | 0.25–0.5 day |
| Settings + `add_excel_datasource` wiring | small |
| In-container E2E + iteration on real messy files | 0.5 day |

Note: this workspace is **not a git repo**, so the design doc is written but not committed.
