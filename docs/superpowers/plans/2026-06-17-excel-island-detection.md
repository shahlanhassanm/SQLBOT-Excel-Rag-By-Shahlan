# Excel Island Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split one uploaded Excel/CSV sheet into multiple Postgres tables when it contains multiple stacked tables, trimming phantom (header-only) columns, without regressing clean single-table files.

**Architecture:** A new pure module `island_detection.py` finds rectangular data "islands" in a `header=None` frame (ported from `ETretyakov/rag-with-tables`). `excel.detect_island_specs` turns each island into an import spec (reusing the existing `resolve_header_row` per island). The bulk `/addExcelDatasource` path consumes these specs; `_import_excel_sheets` gains a region branch that builds each table from a raw slice. The interactive `/parseExcel` → `/importToDb` path is untouched (`region=None`).

**Tech Stack:** Python 3, pandas, calamine/openpyxl/xlsxwriter, psycopg2 COPY, FastAPI, pytest. Spec: `docs/superpowers/specs/2026-06-17-excel-island-detection-design.md`.

---

## Environment notes (READ FIRST)

- **This workspace is NOT a git repo.** Ignore any habit of `git commit`. Each task ends with a **Checkpoint** that validates in the running container instead.
- **Tests run inside the `sqlbot` container**, not on the Windows host (no local venv). Two patterns:
  - **Pure/unit tests (pytest):**
    ```bash
    docker cp tests/<file>.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/<file>.py -q"
    ```
  - **Fast source validation (no rebuild):** `docker cp` a changed source file into `/opt/sqlbot/app/<same path>` then import-check:
    ```bash
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
    ```
    Validate with `import main` (production import order) — importing `apps.datasource.api.datasource` standalone hits a known `sqlbot_xpack` circular import.
  - **E2E (live app):** see Task 7.
- **Full deploy** (after all tasks): `docker compose build && docker compose up -d` (overlay image copies `backend/` into `/opt/sqlbot/app`).
- Source root on host: `backend/`. Same path inside container: `/opt/sqlbot/app/`. So `backend/apps/...` ↔ `/opt/sqlbot/app/apps/...`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/apps/datasource/utils/island_detection.py` | Pure island-detection algorithm | **Create** |
| `tests/test_island_detection.py` | Unit tests for the algorithm | **Create** |
| `backend/apps/datasource/utils/header_detection.py` | `read_raw_full` (uncapped raw read) | Modify |
| `tests/test_read_raw_full.py` | Unit test for `read_raw_full` | **Create** |
| `backend/common/core/config.py` | `EXCEL_ISLAND_*` settings | Modify |
| `backend/apps/datasource/models/datasource.py` | `SheetFields.region` / `regionIndex` | Modify |
| `backend/apps/datasource/utils/excel.py` | `detect_island_specs`, `_fields_for` | Modify |
| `tests/test_detect_island_specs.py` | Unit tests for spec expansion | **Create** |
| `backend/apps/datasource/api/datasource.py` | `_insert_df_to_pg`, region branch, wiring | Modify |
| `tests/test_island_excel_e2e.py` | Live E2E: one sheet → two tables | **Create** |

---

## Task 1: Pure island-detection module

**Files:**
- Create: `backend/apps/datasource/utils/island_detection.py`
- Test: `tests/test_island_detection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_island_detection.py`:

```python
"""Unit tests for the pure island-detection algorithm.

Run in-container:
    docker cp tests/test_island_detection.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_island_detection.py -q"
"""
import pandas as pd

from apps.datasource.utils.island_detection import detect_islands, region_columns, Bounds


def _grid(rows):
    """Build a header=None frame from list-of-lists; ragged rows padded with ''."""
    width = max((len(r) for r in rows), default=0)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=object)


def test_empty_grid():
    assert detect_islands(_grid([])) == []


def test_all_empty_rows():
    assert detect_islands(_grid([[], [""], ["", ""]])) == []


def test_single_cell():
    assert detect_islands(_grid([["A"]])) == [Bounds(0, 0, 0, 0)]


def test_one_simple_table():
    g = _grid([["Name", "Age"], ["Alice", "30"], ["Bob", "25"]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 1)]


def test_header_only_table():
    assert detect_islands(_grid([["ID", "Name", "Value"]])) == [Bounds(0, 0, 0, 2)]


def test_two_tables_one_empty_row():
    g = _grid([["A", "B"], ["1", "2"], [], ["X", "Y"], ["3", "4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 1), Bounds(3, 4, 0, 1)]


def test_two_tables_multiple_empty_rows():
    g = _grid([["H1", "H2"], ["v1", "v2"], [], [], [], ["H3", "H4"], ["v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 1), Bounds(5, 6, 0, 1)]


def test_table_offset_from_origin():
    g = _grid([
        [], [], [],
        ["", "", "", "", ""],
        ["", "", "H1", "H2", "H3"],
        ["", "", "v1", "v2", "v3"],
        ["", "", "v4", "v5", "v6"],
        [], [],
    ])
    assert detect_islands(g) == [Bounds(4, 6, 2, 4)]


def test_leading_empty_columns():
    g = _grid([["", "", "H1", "H2"], ["", "", "v1", "v2"], ["", "", "v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 2, 3)]


def test_leading_and_trailing_empty_columns():
    g = _grid([["", "H1", "H2", ""], ["", "v1", "v2", ""], ["", "v3", "v4", ""]])
    assert detect_islands(g) == [Bounds(0, 2, 1, 2)]


def test_phantom_trailing_headers_excluded():
    g = _grid([["H1", "H2", "H3_phantom", "H4_phantom"], ["v1", "v2"], ["v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 1)]


def test_phantom_leading_column_excluded():
    g = _grid([["H_phantom", "H1", "H2"], ["", "v1", "v2"], ["", "v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 1, 2)]


def test_interior_sparse_column_kept():
    g = _grid([["H1", "H2", "H3"], ["v1", "", "v3"], ["v4", "v5", ""]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 2)]


def test_variable_length_rows():
    g = _grid([["H1", "H2", "H3"], ["v1"], ["v4", "v5"], ["v7", "v8", "v9"]])
    assert detect_islands(g) == [Bounds(0, 3, 0, 2)]


def test_side_by_side_default_off_is_one_island():
    g = _grid([["A", "B", "", "D", "E"], ["1", "2", "", "3", "4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 4)]


def test_side_by_side_split_when_enabled():
    g = _grid([["A", "B", "", "D", "E"], ["1", "2", "", "3", "4"]])
    assert detect_islands(g, split_side_by_side=True) == [
        Bounds(0, 1, 0, 1), Bounds(0, 1, 3, 4)]


def test_multiple_tables_default_off():
    g = _grid([
        ["A", "B", "", "X", "Y"],
        ["1", "2", "", "5", "6"],
        [],
        ["", "", "", "P", "Q"],
        ["", "", "", "7", "8"],
    ])
    assert detect_islands(g) == [Bounds(0, 1, 0, 4), Bounds(3, 4, 3, 4)]


def test_multiple_tables_split_on():
    g = _grid([
        ["A", "B", "", "X", "Y"],
        ["1", "2", "", "5", "6"],
        [],
        ["", "", "", "P", "Q"],
        ["", "", "", "7", "8"],
    ])
    assert detect_islands(g, split_side_by_side=True) == [
        Bounds(0, 1, 0, 1), Bounds(0, 1, 3, 4), Bounds(3, 4, 3, 4)]


def test_min_rows_filters_small_islands():
    g = _grid([["A", "B"], ["1", "2"], [], ["solo"]])
    # default min_rows=1 keeps the 1-row island; min_rows=2 drops it
    assert detect_islands(g, min_rows=2) == [Bounds(0, 1, 0, 1)]


def test_region_columns_fills_blanks_and_dedupes():
    assert region_columns(["Name", "", "Name", None, "Age"]) == [
        "Name", "col_2", "Name_2", "col_4", "Age"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker cp tests/test_island_detection.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_island_detection.py -q"
```
Expected: collection/import error — `ModuleNotFoundError: No module named 'apps.datasource.utils.island_detection'`.

- [ ] **Step 3: Write the implementation**

Create `backend/apps/datasource/utils/island_detection.py`:

```python
"""Table-island detection for uploaded spreadsheets.

A single sheet often holds several unrelated tables (a title, a blank row, a
sales table, then a budget table) and Excel auto-generates thousands of empty
column headers. This module finds each rectangular "island" of data so the
importer can turn one sheet into N tables instead of one garbage table.

Ported from ETretyakov/rag-with-tables (internal/ingest/islands/detector.go). It
is pure: no DB, no settings, no file I/O. The caller reads the sheet with
``header=None`` and feeds the frame here; header resolution and slicing happen in
the integration layer (excel.py).
"""
from __future__ import annotations

from typing import Callable, List, NamedTuple

import pandas as pd

from apps.datasource.utils.header_detection import _is_blank


class Bounds(NamedTuple):
    """Inclusive 0-based bounding box of a detected table island."""
    min_row: int
    max_row: int
    min_col: int
    max_col: int


def detect_islands(raw: pd.DataFrame, split_side_by_side: bool = False,
                   min_rows: int = 1) -> List[Bounds]:
    """Find table islands in a ``header=None`` frame.

    Args:
        raw: the full sheet read with ``header=None`` (every physical row is data).
        split_side_by_side: when True, interior all-empty columns split
            side-by-side tables into separate islands; when False (default) a band
            yields one island spanning its full data column range.
        min_rows: discard islands whose inclusive row span is below this.

    Returns islands ordered top-to-bottom, then left-to-right within a band.
    """
    if raw is None or raw.shape[0] == 0 or raw.shape[1] == 0:
        return []

    n_rows, max_cols = raw.shape[0], raw.shape[1]
    values = raw.values

    def cell_empty(r: int, c: int) -> bool:
        return _is_blank(values[r][c])

    def row_empty(r: int) -> bool:
        return all(cell_empty(r, c) for c in range(max_cols))

    # Step 1 — row bands (maximal runs of non-empty rows).
    bands: List[tuple] = []
    band_start, in_band = 0, False
    for r in range(n_rows):
        if not row_empty(r):
            if not in_band:
                band_start, in_band = r, True
        elif in_band:
            bands.append((band_start, r - 1))
            in_band = False
    if in_band:
        bands.append((band_start, n_rows - 1))

    # Step 2 — column groups per band.
    result: List[Bounds] = []
    for b_start, b_end in bands:
        if b_start == b_end:
            result.extend(_col_groups_from_row(b_start, b_end, max_cols, cell_empty))
        else:
            result.extend(_col_groups_from_data(
                b_start, b_end, max_cols, cell_empty, split_side_by_side))

    return [b for b in result if (b.max_row - b.min_row + 1) >= min_rows]


def _col_groups_from_row(row_min: int, row_max: int, max_cols: int,
                         cell_empty: Callable[[int, int], bool]) -> List[Bounds]:
    """Consecutive non-empty columns of a single-row (header-only) band."""
    result: List[Bounds] = []
    col_start, in_group = 0, False
    for c in range(max_cols):
        active = not cell_empty(row_min, c)
        if active and not in_group:
            col_start, in_group = c, True
        elif not active and in_group:
            result.append(Bounds(row_min, row_max, col_start, c - 1))
            in_group = False
    if in_group:
        result.append(Bounds(row_min, row_max, col_start, max_cols - 1))
    return result


def _col_groups_from_data(header_row: int, band_end: int, max_cols: int,
                          cell_empty: Callable[[int, int], bool],
                          split_side_by_side: bool) -> List[Bounds]:
    """Column groups for a multi-row band using the data-extent rule.

    Find left/right data extent from the NON-header rows (phantom-header columns
    outside this range are dropped). With splitting off, return one island over
    the whole extent; with it on, interior all-empty columns separate islands.
    """
    left_data, right_data = max_cols, -1
    for c in range(max_cols):
        for r in range(header_row + 1, band_end + 1):
            if not cell_empty(r, c):
                left_data = min(left_data, c)
                right_data = max(right_data, c)
                break

    # No data rows have content — fall back to the header-row rule.
    if left_data > right_data:
        return _col_groups_from_row(header_row, band_end, max_cols, cell_empty)

    if not split_side_by_side:
        return [Bounds(header_row, band_end, left_data, right_data)]

    active = [False] * max_cols
    for c in range(left_data, right_data + 1):
        for r in range(header_row, band_end + 1):
            if not cell_empty(r, c):
                active[c] = True
                break

    result: List[Bounds] = []
    col_start, in_group = 0, False
    for c in range(left_data, right_data + 1):
        if active[c] and not in_group:
            col_start, in_group = c, True
        elif not active[c] and in_group:
            result.append(Bounds(header_row, band_end, col_start, c - 1))
            in_group = False
    if in_group:
        result.append(Bounds(header_row, band_end, col_start, right_data))
    return result


def region_columns(header_values: list) -> List[str]:
    """Column names from a region's header row.

    Blank cells become ``col_N`` (1-based position); duplicate names get a
    ``_2``, ``_3`` … suffix so the resulting DataFrame has unique labels (messy
    spreadsheets routinely repeat header text, and pandas would otherwise return
    a DataFrame for ``df[name]`` and crash type inference / COPY).
    """
    out: List[str] = []
    seen: dict = {}
    for j, v in enumerate(header_values):
        name = str(v).strip() if not _is_blank(v) else f"col_{j + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker cp backend/apps/datasource/utils/island_detection.py sqlbot:/opt/sqlbot/app/apps/datasource/utils/island_detection.py
docker cp tests/test_island_detection.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_island_detection.py -q"
```
Expected: all tests PASS.

- [ ] **Step 5: Checkpoint**

Confirm import order is clean: `docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error. (No git commit — workspace is not a repo.)

---

## Task 2: Uncapped raw read (`read_raw_full`)

**Files:**
- Modify: `backend/apps/datasource/utils/header_detection.py`
- Test: `tests/test_read_raw_full.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_read_raw_full.py`:

```python
"""Run in-container:
    docker cp tests/test_read_raw_full.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_read_raw_full.py -q"
"""
import pandas as pd

from apps.datasource.utils.header_detection import read_raw, read_raw_full


def test_read_raw_full_reads_beyond_25_rows(tmp_path):
    df = pd.DataFrame({"a": list(range(40)), "b": list(range(40))})
    p = tmp_path / "big.xlsx"
    df.to_excel(str(p), index=False)

    full = read_raw_full(str(p))
    capped = read_raw(str(p))

    assert full.shape[0] == 41          # 1 header row + 40 data rows, no cap
    assert capped.shape[0] <= 25        # existing detector cap unchanged
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
docker cp tests/test_read_raw_full.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_read_raw_full.py -q"
```
Expected: `ImportError: cannot import name 'read_raw_full'`.

- [ ] **Step 3: Implement `read_raw_full`**

In `backend/apps/datasource/utils/header_detection.py`, add `"read_raw_full"` to `__all__` (the list near the top), and add this function immediately after `read_raw` (after line ~225):

```python
def read_raw_full(save_path: str, sheet_name=None) -> pd.DataFrame:
    """Read an ENTIRE sheet with no header (every physical row is data).

    Like ``read_raw`` but without the ``DETECT_ROWS`` cap — island detection
    must scan the whole sheet to find row bands.
    """
    if _is_csv(save_path):
        return pd.read_csv(save_path, header=None, engine="c", dtype=object)
    return pd.read_excel(
        save_path,
        sheet_name=sheet_name if sheet_name is not None else 0,
        header=None,
        engine="calamine",
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
docker cp backend/apps/datasource/utils/header_detection.py sqlbot:/opt/sqlbot/app/apps/datasource/utils/header_detection.py
docker cp tests/test_read_raw_full.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_read_raw_full.py -q"
```
Expected: PASS (1 passed).

- [ ] **Step 5: Checkpoint**

`docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error.

---

## Task 3: Settings flags

**Files:**
- Modify: `backend/common/core/config.py`

- [ ] **Step 1: Add the settings**

In `backend/common/core/config.py`, in the `Settings` class right after the `AGENTIC_*` block (after `AGENTIC_AGGREGATION_KEYWORDS`, near line 165), add:

```python
    # --- Excel island detection (multi-table-per-sheet) ---------------------
    EXCEL_ISLAND_DETECTION_ENABLED: bool = True   # off => exact pre-island behavior
    EXCEL_SPLIT_SIDE_BY_SIDE: bool = False         # 2D interior-empty-column splitting
    EXCEL_ISLAND_MIN_ROWS: int = 1
```

If there is a list of boolean env-coerced field names nearby (the block listing `'EMBEDDING_ENABLED'`, `'AGENTIC_*'` etc. around lines 167-177), add `'EXCEL_ISLAND_DETECTION_ENABLED'` and `'EXCEL_SPLIT_SIDE_BY_SIDE'` to it so env-var overrides parse as booleans.

- [ ] **Step 2: Validate the settings load**

```bash
docker cp backend/common/core/config.py sqlbot:/opt/sqlbot/app/common/core/config.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'from common.core.config import settings; print(settings.EXCEL_ISLAND_DETECTION_ENABLED, settings.EXCEL_SPLIT_SIDE_BY_SIDE, settings.EXCEL_ISLAND_MIN_ROWS)'"
```
Expected output: `True False 1`

- [ ] **Step 3: Checkpoint**

`docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error.

---

## Task 4: Spec expansion (`detect_island_specs`) + model field

**Files:**
- Modify: `backend/apps/datasource/models/datasource.py:202-205` (`SheetFields`)
- Modify: `backend/apps/datasource/utils/excel.py`
- Test: `tests/test_detect_island_specs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detect_island_specs.py`:

```python
"""Run in-container:
    docker cp tests/test_detect_island_specs.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_detect_island_specs.py -q"
"""
import pandas as pd
from openpyxl import Workbook

from apps.datasource.utils.excel import detect_island_specs


def test_single_clean_table_yields_region_none(tmp_path):
    p = tmp_path / "one.xlsx"
    pd.DataFrame({"name": ["a", "b"], "age": [1, 2]}).to_excel(str(p), index=False)
    specs = detect_island_specs(str(p))
    assert len(specs) == 1
    assert specs[0]["region"] is None
    names = [f["fieldName"] for f in specs[0]["fields"]]
    assert "name" in names and "age" in names


def test_two_stacked_tables_yield_two_region_specs(tmp_path):
    wb = Workbook()
    ws = wb.active
    for row in [["Name", "Age"], ["Alice", "30"], ["Bob", "25"],
                [], [],
                ["City", "Pop"], ["NYC", "8"], ["LA", "4"]]:
        ws.append(row)
    p = tmp_path / "two.xlsx"
    wb.save(str(p))

    specs = detect_island_specs(str(p))
    assert len(specs) == 2
    assert [s["regionIndex"] for s in specs] == [0, 1]
    assert all(s["region"] is not None for s in specs)

    names0 = [str(f["fieldName"]) for f in specs[0]["fields"]]
    names1 = [str(f["fieldName"]) for f in specs[1]["fields"]]
    assert "Name" in names0 and "Age" in names0
    assert "City" in names1 and "Pop" in names1


def test_region_header_offset_resolved_absolute(tmp_path):
    # a title row sits above the second table's header
    wb = Workbook()
    ws = wb.active
    for row in [["Sales", "Q1"], ["x", "1"],
                [],
                ["MONTHLY REPORT", ""],     # title row inside the 2nd band
                ["Month", "Total"], ["Jan", "5"], ["Feb", "7"]]:
        ws.append(row)
    p = tmp_path / "offset.xlsx"
    wb.save(str(p))

    specs = detect_island_specs(str(p))
    assert len(specs) == 2
    # 2nd island spans rows 3..6 (0-based); resolve_header_row should pick the
    # "Month/Total" row (absolute row 4), not the title row.
    names1 = [str(f["fieldName"]) for f in specs[1]["fields"]]
    assert "Month" in names1 and "Total" in names1
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
docker cp tests/test_detect_island_specs.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_detect_island_specs.py -q"
```
Expected: `ImportError: cannot import name 'detect_island_specs'`.

- [ ] **Step 3a: Add the model fields**

In `backend/apps/datasource/models/datasource.py`, replace the `SheetFields` class (lines 202-205):

```python
class SheetFields(BaseModel):
    sheetName: str
    fields: List[FieldInfo]
    headerRow: int | None = None
    region: list[int] | None = None        # [min_row, max_row, min_col, max_col]
    regionIndex: int | None = None          # 0-based ordinal within the sheet
```

- [ ] **Step 3b: Implement `detect_island_specs` + `_fields_for`**

In `backend/apps/datasource/utils/excel.py`, extend the imports from `.header_detection` to include `read_raw_full`, and add a new import for the detector:

```python
from .header_detection import (
    load_header_meta,
    read_raw,
    read_raw_full,
    read_sheet,
    resolve_header_row,
    save_header_meta,
)
from .island_detection import detect_islands, region_columns
```

Then add these functions at the end of the file:

```python
def _fields_for(df) -> list:
    """Field-type descriptors for a dataframe's columns."""
    return [{"fieldName": c, "fieldType": infer_field_type(df[c].dtype)}
            for c in df.columns]


def detect_island_specs(save_path: str) -> list:
    """Expand each sheet into one import spec per detected table island.

    Each spec is shaped like a parse_excel_preview entry plus region/regionIndex:
      {sheetName, region:[r0,r1,c0,c1] | None, regionIndex, headerRow, fields:[...]}

    A sheet whose single island is the trivial whole-sheet box yields
    ``region=None`` so the importer uses the unchanged whole-sheet path. Any
    detection error degrades the same way (never worse than today).
    """
    from common.core.config import settings

    is_csv = save_path.lower().endswith(".csv")
    if is_csv:
        sheet_specs = [("Sheet1", None)]
    else:
        sheet_specs = [(name, name) for name in pd.ExcelFile(save_path).sheet_names]

    out: list = []
    for sheet_label, sheet_arg in sheet_specs:
        try:
            raw_full = read_raw_full(save_path, sheet_arg)
            islands = detect_islands(
                raw_full,
                split_side_by_side=settings.EXCEL_SPLIT_SIDE_BY_SIDE,
                min_rows=settings.EXCEL_ISLAND_MIN_ROWS,
            )
        except Exception:
            raw_full, islands = None, []

        trivial = (
            raw_full is not None and len(islands) == 1
            and islands[0].min_row == 0 and islands[0].min_col == 0
            and islands[0].max_row == raw_full.shape[0] - 1
            and islands[0].max_col == raw_full.shape[1] - 1
        )

        if not islands or trivial:
            # unchanged whole-sheet detection
            hr, _conf = resolve_header_row(
                read_raw(save_path, sheet_arg), context=f"{save_path}::{sheet_label}")
            df = read_sheet(save_path, sheet_arg, hr)
            out.append({
                "sheetName": sheet_label, "region": None, "regionIndex": 0,
                "headerRow": hr, "fields": _fields_for(df),
            })
            continue

        for idx, b in enumerate(islands):
            block = raw_full.iloc[b.min_row:b.max_row + 1,
                                  b.min_col:b.max_col + 1].reset_index(drop=True)
            rel_hr, _conf = resolve_header_row(
                block, context=f"{save_path}::{sheet_label}#r{idx}")
            body = block.iloc[rel_hr + 1:].reset_index(drop=True)
            body.columns = region_columns(block.iloc[rel_hr].tolist())
            out.append({
                "sheetName": sheet_label,
                "region": [b.min_row, b.max_row, b.min_col, b.max_col],
                "regionIndex": idx,
                "headerRow": b.min_row + rel_hr,
                "fields": _fields_for(body),
            })
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
docker cp backend/apps/datasource/models/datasource.py sqlbot:/opt/sqlbot/app/apps/datasource/models/datasource.py
docker cp backend/apps/datasource/utils/excel.py sqlbot:/opt/sqlbot/app/apps/datasource/utils/excel.py
docker cp tests/test_detect_island_specs.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_detect_island_specs.py -q"
```
Expected: PASS (3 passed).

- [ ] **Step 5: Checkpoint**

`docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"` → no error.

---

## Task 5: Region-aware importer + insert-path fix

**Files:**
- Modify: `backend/apps/datasource/api/datasource.py` — `_import_excel_sheets` (lines 594-667) and add `_insert_df_to_pg`.

This touches Postgres, so it is verified by the E2E in Task 7. Implement carefully.

- [ ] **Step 1: Add the shared insert helper**

In `backend/apps/datasource/api/datasource.py`, add this module-level function just above `_import_excel_sheets` (before line 594). Confirm `from io import StringIO` (or `StringIO` import), `from psycopg2 import sql`, `hashlib`, `uuid` are already imported in this file (they are used by the existing code).

```python
def _insert_df_to_pg(df, table_name, engine):
    """Create ``table_name`` from ``df``'s schema and bulk-load its rows via COPY.

    Schema is created with an EMPTY frame (no row insert), then a single rewound
    COPY loads the data. Fixes the previous code where the StringIO was never
    rewound, so COPY read nothing and the slow to_sql silently did the insert.
    """
    for i in range(len(df.dtypes)):
        if str(df.dtypes[i]) == 'uint64':
            df[str(df.columns[i])] = df[str(df.columns[i])].astype('string')

    df.head(0).to_sql(table_name, engine, if_exists='replace', index=False)

    conn = engine.raw_connection()
    cursor = conn.cursor()
    try:
        output = StringIO()
        df.to_csv(output, sep='\t', header=False, index=False)
        output.seek(0)  # CRITICAL: rewind so COPY reads the rows (was missing)
        query = sql.SQL("COPY {} FROM STDIN WITH CSV DELIMITER E'\t' NULL ''").format(
            sql.Identifier(table_name)
        )
        cursor.copy_expert(sql=query.as_string(cursor.connection), file=output)
        conn.commit()
    finally:
        cursor.close()
        conn.close()
```

- [ ] **Step 2: Replace the body of `_import_excel_sheets`**

Replace the whole `_import_excel_sheets` function (lines 594-667) with:

```python
def _import_excel_sheets(save_path: str, sheets: List[SheetFields], trans: Trans) -> list:
    """Import each spec in ``sheets`` into PostgreSQL.

    A spec with ``region`` set is a detected island: the table is built from a
    raw slice of the sheet. A spec with ``region is None`` keeps the original
    whole-sheet behavior. Shared by /importToDb and /addExcelDatasource.
    """
    engine = get_engine_conn()
    results = []
    is_csv = save_path.lower().endswith(".csv")
    header_meta = load_header_meta(save_path)
    raw_cache: dict = {}  # sheet_arg -> full header=None frame (region path only)

    for sheet_info in sheets:
        sheet_name = sheet_info.sheetName
        fields = sheet_info.fields

        field_mapping = {f.fieldName: f.fieldType for f in fields}
        dtype_dict = {
            col: USER_TYPE_TO_PANDAS.get(field_mapping.get(col, 'string'), 'string')
            for col in field_mapping.keys()
        }

        region = getattr(sheet_info, "region", None)
        if region is not None:
            sheet_arg = None if is_csv else sheet_name
            if sheet_arg not in raw_cache:
                raw_cache[sheet_arg] = read_raw_full(save_path, sheet_arg)
            raw_full = raw_cache[sheet_arg]

            min_row, max_row, min_col, max_col = region
            block = raw_full.iloc[min_row:max_row + 1,
                                  min_col:max_col + 1].reset_index(drop=True)
            abs_hr = sheet_info.headerRow if sheet_info.headerRow is not None else min_row
            rel_hr = abs_hr - min_row
            df = block.iloc[rel_hr + 1:].reset_index(drop=True)
            df.columns = region_columns(block.iloc[rel_hr].tolist())
            for col, dt in dtype_dict.items():
                if col in df.columns:
                    try:
                        df[col] = df[col].astype(dt)
                    except Exception:
                        pass

            ridx = sheet_info.regionIndex if sheet_info.regionIndex is not None else 0
            table_name = f"{sheet_name}_r{ridx}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"
        else:
            sheet_key = "Sheet1" if is_csv else sheet_name
            header_row = sheet_info.headerRow
            if header_row is None:
                header_row = header_meta.get(sheet_key)
            if header_row is None:
                raw = read_raw(save_path, None if is_csv else sheet_name)
                header_row, _conf = resolve_header_row(
                    raw, context=f"{save_path}::{sheet_key}")
            header_meta[sheet_key] = header_row
            save_header_meta(save_path, header_meta)

            try:
                if is_csv:
                    df = read_sheet(save_path, None, header_row, dtype=dtype_dict)
                    sheet_name = "Sheet1"
                else:
                    df = read_sheet(save_path, sheet_name, header_row, dtype=dtype_dict)
            except Exception as e:
                raise HTTPException(500, f"{trans('i18n_ds_upload_error')}: {str(e)}")
            table_name = f"{sheet_name}_{hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]}"

        try:
            _insert_df_to_pg(df, table_name, engine)
            results.append({
                "sheetName": sheet_name,
                "tableName": table_name,
                "tableComment": "",
                "rows": len(df),
            })
        except Exception as e:
            raise HTTPException(500, f"Insert data failed for {table_name}: {str(e)}")

    return results
```

- [ ] **Step 3: Add the new imports used by the region branch**

In `backend/apps/datasource/api/datasource.py`, extend the existing `from ..utils.header_detection import (...)` block (lines 37-43) to also import `read_raw_full`, and add an import for `region_columns`:

```python
from ..utils.header_detection import (
    load_header_meta,
    read_raw,
    read_raw_full,
    read_sheet,
    resolve_header_row,
    save_header_meta,
)
from ..utils.island_detection import region_columns
```

- [ ] **Step 4: Validate import + compile**

```bash
docker cp backend/apps/datasource/api/datasource.py sqlbot:/opt/sqlbot/app/apps/datasource/api/datasource.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: no error (validates production import order; the standalone-import circular issue is avoided by importing `main`).

- [ ] **Step 5: Checkpoint**

Full functional verification is Task 7. Do not claim this works until Task 7 passes.

---

## Task 6: Wire `add_excel_datasource` to use islands

**Files:**
- Modify: `backend/apps/datasource/api/datasource.py` — `add_excel_datasource` `inner()` (lines 706-712) and imports.

- [ ] **Step 1: Import `detect_island_specs`**

In `backend/apps/datasource/api/datasource.py`, extend the existing `from ..utils.excel import ...` line (line 34) to include `detect_island_specs`:

```python
from ..utils.excel import parse_excel_preview, reparse_sheet, USER_TYPE_TO_PANDAS, detect_island_specs
```

- [ ] **Step 2: Replace `inner()` inside `add_excel_datasource`**

Replace the `inner()` function (lines 706-712):

```python
    def inner():
        from common.core.config import settings
        if settings.EXCEL_ISLAND_DETECTION_ENABLED:
            specs = detect_island_specs(save_path)
            sheets_spec = [SheetFields(sheetName=s['sheetName'],
                                       headerRow=s.get('headerRow'),
                                       region=s.get('region'),
                                       regionIndex=s.get('regionIndex'),
                                       fields=[FieldInfo(**fld) for fld in s['fields']])
                           for s in specs]
        else:
            preview_sheets = parse_excel_preview(save_path)
            sheets_spec = [SheetFields(sheetName=s['sheetName'],
                                       headerRow=s.get('headerRow'),
                                       fields=[FieldInfo(**fld) for fld in s['fields']])
                           for s in preview_sheets]
        return _import_excel_sheets(save_path, sheets_spec, trans)
```

- [ ] **Step 3: Validate import**

```bash
docker cp backend/apps/datasource/api/datasource.py sqlbot:/opt/sqlbot/app/apps/datasource/api/datasource.py
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -c 'import main'"
```
Expected: no error.

- [ ] **Step 4: Checkpoint**

Proceed to Task 7 for functional verification.

---

## Task 7: End-to-end — one sheet, two tables

**Files:**
- Create: `tests/test_island_excel_e2e.py`

This mirrors the auth/upload harness of `tests/test_bulk_excel_e2e.py` and runs against the live app.

- [ ] **Step 1: Write the E2E test**

Create `tests/test_island_excel_e2e.py`:

```python
"""E2E: a single sheet containing two stacked tables becomes a datasource with
two tables. Run INSIDE the container against the live app:

    docker cp tests/test_island_excel_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_island_excel_e2e.py"
"""
import io
import json
import sys
import urllib.request

import pandas as pd
from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')

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
        account = loop.run_until_complete(sqlbot_encrypt('admin'))
        pwd = loop.run_until_complete(sqlbot_encrypt(settings.DEFAULT_PWD))
    finally:
        loop.close()
    data = urllib.parse.urlencode({'username': account, 'password': pwd}).encode()
    res = http('POST', f'{BASE}/login/access-token', data,
               {'Content-Type': 'application/x-www-form-urlencoded'})
    token = (res['data'].get('access_token') if isinstance(res.get('data'), dict)
             else res.get('access_token') or res.get('data'))
    assert token, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {token}'}


def make_two_island_xlsx() -> bytes:
    """One sheet, two stacked tables separated by a blank row."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'mixed'
    for row in [["region", "sales"], ["north", "100"], ["south", "250"],
                [],
                ["city", "pop"], ["nyc", "8"], ["la", "4"], ["sf", "1"]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(headers, filename, content):
    boundary = 'sqlbotIslandBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + \
        f'\r\n--{boundary}--\r\n'.encode()
    h = dict(headers)
    h['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def main():
    headers = login()
    created = []
    try:
        r = upload(headers, 'e2e_island_book.xlsx', make_two_island_xlsx())
        d = r.get('data', r)
        assert d.get('id'), f'upload failed: {r}'
        created.append((d['id'], d['name']))

        sheets = d.get('sheets', [])
        assert len(sheets) == 2, f'expected 2 tables from one sheet, got {sheets}'
        rows_by_idx = sorted(s['rows'] for s in sheets)
        assert rows_by_idx == [2, 3], f'expected row counts [2,3], got {rows_by_idx}'
        print(f"island OK -> ds id={d['id']} tables={len(sheets)} rows={rows_by_idx}")

        t = http('POST', f'{BASE}/datasource/tableList/{d["id"]}', b'', headers)
        tables = t.get('data', t)
        names = sorted(x['table_name'] for x in tables)
        assert len(tables) == 2, f'expected 2 saved tables, got {names}'
        assert all('_r' in n for n in names), f'expected region-suffixed names, got {names}'
        print(f'tableList OK -> {names}')
        print('E2E PASS')
    finally:
        for ds_id, name in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{ds_id}/{name}', b'', headers)
                print(f'cleaned up ds {ds_id} ({name})')
            except Exception as e:
                print(f'cleanup failed for {ds_id}: {e}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Rebuild the image with all changes and bring it up**

```bash
docker compose build && docker compose up -d
```
Wait for the healthcheck (~25-40s). Confirm: `docker exec sqlbot sh -c "curl -sf http://localhost:8000/api/v1/ >/dev/null && echo up"`.

- [ ] **Step 3: Run the E2E**

```bash
docker cp tests/test_island_excel_e2e.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_island_excel_e2e.py"
```
Expected output ends with `E2E PASS` and shows `tables=2 rows=[2, 3]` and region-suffixed (`*_r0_*`, `*_r1_*`) table names.

- [ ] **Step 4: Regression — bulk single-table path still works**

```bash
docker cp tests/test_bulk_excel_e2e.py sqlbot:/tmp/
docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_bulk_excel_e2e.py"
```
Expected: ends with `E2E PASS` (clean one-table-per-sheet files still produce one table per sheet; each xlsx sheet here is a single clean island → `region=None` path).

- [ ] **Step 5: Checkpoint**

All unit suites + both E2E scripts pass. Feature complete. (No git commit — workspace is not a repo; the rebuilt `sqlbot:local` image carries the changes.)

---

## Notes for the implementer

- **Order matters:** Tasks 1→2→4 are prerequisites for 5/6/7. Task 3 can be done any time before Task 6.
- **Pure tests (1,2,4)** run without a rebuild via `docker cp` into `/tmp` + the in-container pytest. **Integration (5,6)** is only meaningfully verified by the Task 7 E2E after a `docker compose build`.
- **Don't restructure** `llm.py` or anything outside the files listed — out of scope.
- If `detect_islands` ever returns unexpected splits on a real file, the safe lever is `EXCEL_SPLIT_SIDE_BY_SIDE=False` (default) and `EXCEL_ISLAND_DETECTION_ENABLED=False` (full opt-out).
