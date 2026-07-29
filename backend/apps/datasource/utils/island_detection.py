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

from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import pandas as pd

from apps.datasource.utils.header_detection import _is_blank

__all__ = ["Bounds", "detect_islands", "region_columns"]


class Bounds(NamedTuple):
    """Inclusive 0-based bounding box of a detected table island."""
    min_row: int
    max_row: int
    min_col: int
    max_col: int


def detect_islands(raw: Optional[pd.DataFrame], split_side_by_side: bool = False,
                   min_rows: int = 1) -> List[Bounds]:
    """Find table islands in a ``header=None`` frame via recursive bisection.

    The region is split by a fully-empty row, each piece is split by a fully-empty
    column, and each column group is then re-split by empty rows *within its own
    columns* — recursing (alternating row/column splits) until nothing more
    divides. This separates tables arranged in a GRID, where one table's blank-row
    gap is "filled" by a neighbouring table in other columns (so no globally-empty
    row exists) — the case a single row-pass + column-pass cannot handle.

    Args:
        raw: the full sheet read with ``header=None``.
        split_side_by_side: when True, fully-empty interior columns also split a
            region into side-by-side tables; when False only row splits happen.
        min_rows: discard islands whose inclusive row span is below this.

    Islands are returned in top-to-bottom, left-to-right (depth-first) order.
    """
    if raw is None or raw.shape[0] == 0 or raw.shape[1] == 0:
        return []

    values = raw.values

    def cell_empty(r: int, c: int) -> bool:
        return _is_blank(values[r][c])

    out: List[Bounds] = []
    _bisect(0, raw.shape[0] - 1, 0, raw.shape[1] - 1, cell_empty, split_side_by_side, out)
    return [b for b in out if (b.max_row - b.min_row + 1) >= min_rows]


def _row_blank(r: int, c0: int, c1: int, cell_empty: Callable[[int, int], bool]) -> bool:
    return all(cell_empty(r, c) for c in range(c0, c1 + 1))


def _col_blank(c: int, r0: int, r1: int, cell_empty: Callable[[int, int], bool]) -> bool:
    return all(cell_empty(r, c) for r in range(r0, r1 + 1))


def _bisect(r0: int, r1: int, c0: int, c1: int,
            cell_empty: Callable[[int, int], bool], split_sbs: bool,
            out: List[Bounds]) -> None:
    """Recursively split the region ``[r0..r1] x [c0..c1]`` into atomic islands,
    appending each to ``out``."""
    # 1. Trim fully-blank edge rows/columns of the current region.
    while r0 <= r1 and _row_blank(r0, c0, c1, cell_empty):
        r0 += 1
    while r1 >= r0 and _row_blank(r1, c0, c1, cell_empty):
        r1 -= 1
    if r0 > r1:
        return
    while c0 <= c1 and _col_blank(c0, r0, r1, cell_empty):
        c0 += 1
    while c1 >= c0 and _col_blank(c1, r0, r1, cell_empty):
        c1 -= 1
    if c0 > c1:
        return

    # 2. Split on the first interior fully-blank row (within the current columns).
    for r in range(r0 + 1, r1):
        if _row_blank(r, c0, c1, cell_empty):
            _bisect(r0, r - 1, c0, c1, cell_empty, split_sbs, out)
            nxt = r
            while nxt <= r1 and _row_blank(nxt, c0, c1, cell_empty):
                nxt += 1
            _bisect(nxt, r1, c0, c1, cell_empty, split_sbs, out)
            return

    # 3. Else split on the first interior fully-blank column (side-by-side tables).
    if split_sbs:
        for c in range(c0 + 1, c1):
            if _col_blank(c, r0, r1, cell_empty):
                _bisect(r0, r1, c0, c - 1, cell_empty, split_sbs, out)
                nxt = c
                while nxt <= c1 and _col_blank(nxt, r0, r1, cell_empty):
                    nxt += 1
                _bisect(r0, r1, nxt, c1, cell_empty, split_sbs, out)
                return

    # 4. Atomic region: drop phantom header-only columns by trimming the column
    #    range to the extent of DATA rows (everything below the first/header row).
    if r1 > r0:
        left_data, right_data = c1 + 1, c0 - 1
        for c in range(c0, c1 + 1):
            for r in range(r0 + 1, r1 + 1):
                if not cell_empty(r, c):
                    if c < left_data:
                        left_data = c
                    if c > right_data:
                        right_data = c
                    break
        if left_data <= right_data:
            c0, c1 = left_data, right_data
    out.append(Bounds(r0, r1, c0, c1))


def region_columns(header_values: list) -> List[str]:
    """Column names from a region's header row.

    Blank cells become ``col_N`` (1-based position); duplicate names get a
    ``_2``, ``_3`` … suffix so the resulting DataFrame has unique labels (messy
    spreadsheets routinely repeat header text, and pandas would otherwise return
    a DataFrame for ``df[name]`` and crash type inference / COPY).
    """
    out: List[str] = []
    seen: Dict[str, int] = {}
    for j, v in enumerate(header_values):
        name = str(v).strip() if not _is_blank(v) else f"col_{j + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out
