import pandas as pd

from .header_detection import (
    load_header_meta,
    read_raw,
    read_raw_full,
    read_sheet,
    resolve_header_row,
    save_header_meta,
    _is_blank,
)
from .island_detection import detect_islands, region_columns

FIELD_TYPE_MAP = {
    'int64': 'int',
    'int32': 'int',
    'float64': 'float',
    'float32': 'float',
    'datetime64': 'datetime',
    'datetime64[ns]': 'datetime',
    'object': 'string',
    'string': 'string',
    'bool': 'string',
}

USER_TYPE_TO_PANDAS = {
    'int': 'int64',
    'float': 'float64',
    'datetime': 'datetime64[ns]',
    'string': 'string',
}


def infer_field_type(dtype) -> str:
    dtype_str = str(dtype)
    return FIELD_TYPE_MAP.get(dtype_str, 'string')


def _raw_head_rows(raw: pd.DataFrame, max_rows: int = 25):
    """Return the first ``max_rows`` of ``raw`` as JSON-safe lists, for the
    frontend "Header row" dropdown preview."""
    rows = []
    for i in range(min(max_rows, len(raw))):
        cells = []
        for v in raw.iloc[i]:
            if v is None:
                cells.append("")
                continue
            try:
                if pd.isna(v):
                    cells.append("")
                    continue
            except (TypeError, ValueError):
                pass
            cells.append(str(v))
        rows.append(cells)
    return rows


def _preview_one_sheet(save_path, sheet_label, sheet_arg, header_override, max_rows):
    """Build the preview payload for a single sheet."""
    raw = read_raw(save_path, sheet_arg)

    if header_override is not None and 0 <= header_override < len(raw):
        header_row, confidence = header_override, 1.0
    else:
        header_row, confidence = resolve_header_row(
            raw, context=f"{save_path}::{sheet_label}"
        )

    df = read_sheet(save_path, sheet_arg, header_row)
    fields = [
        {"fieldName": col, "fieldType": infer_field_type(df[col].dtype)}
        for col in df.columns
    ]
    preview_df = df.head(max_rows).replace({pd.NA: None, float('nan'): None})
    return header_row, confidence, {
        "sheetName": sheet_label,
        "fields": fields,
        "data": preview_df.to_dict(orient='records'),
        "rows": len(df),
        "headerRow": header_row,
        "headerConfidence": round(float(confidence), 3),
        "rawHead": _raw_head_rows(raw),
    }


def parse_excel_preview(save_path: str, max_rows: int = 10, header_overrides: dict | None = None):
    """Preview an uploaded Excel/CSV file.

    The real header row is detected per sheet (the first physical row is often
    a title, company name, date or blank) and the resolved index is persisted
    in a sidecar so the later import step reads the file the same way.

    ``header_overrides`` is an optional ``{sheet_label: header_row}`` map; when
    a sheet appears in it the override is used verbatim and detection is
    skipped, so the user's manual choice always wins.
    """
    overrides = header_overrides or {}
    sheets_data = []
    header_meta = load_header_meta(save_path)

    if save_path.lower().endswith(".csv"):
        sheet_specs = [("Sheet1", None)]
    else:
        sheet_specs = [(name, name) for name in pd.ExcelFile(save_path).sheet_names]

    for sheet_label, sheet_arg in sheet_specs:
        header_row, _confidence, payload = _preview_one_sheet(
            save_path, sheet_label, sheet_arg,
            overrides.get(sheet_label), max_rows,
        )
        header_meta[sheet_label] = header_row
        sheets_data.append(payload)

    save_header_meta(save_path, header_meta)
    return sheets_data


def reparse_sheet(save_path: str, sheet_label: str, header_row: int, max_rows: int = 10):
    """Re-preview a single sheet with a user-chosen header row.

    Used by the preview UI when the user picks a different "Header row" in the
    dropdown — avoids re-uploading the file.
    """
    is_csv = save_path.lower().endswith(".csv")
    sheet_arg = None if is_csv else sheet_label
    _row, _conf, payload = _preview_one_sheet(
        save_path, sheet_label, sheet_arg, header_row, max_rows
    )

    header_meta = load_header_meta(save_path)
    header_meta[sheet_label] = header_row
    save_header_meta(save_path, header_meta)
    return payload


def _fields_for(df) -> list[dict]:
    """Field-type descriptors for a dataframe's columns."""
    return [{"fieldName": c, "fieldType": infer_field_type(df[c].dtype)}
            for c in df.columns]


def _nonblank_bbox(raw):
    """Bounding box (min_row, max_row, min_col, max_col) of every non-blank cell
    in a ``header=None`` frame, using the same blank semantics as island
    detection. Returns ``None`` when the frame is entirely blank.

    Used to decide whether a single detected island already covers the whole
    meaningful sheet content. This deliberately ignores trailing/leading all-blank
    rows and columns, so a clean file padded with blank rows is still recognised
    as "trivial" and keeps the unchanged whole-sheet import path (incl. pandas
    dtype inference) instead of falling through to the region path.
    """
    if raw is None or raw.shape[0] == 0 or raw.shape[1] == 0:
        return None
    values = raw.values
    n_rows, n_cols = raw.shape
    rmin = cmin = None
    rmax = cmax = -1
    for r in range(n_rows):
        for c in range(n_cols):
            if not _is_blank(values[r][c]):
                if rmin is None:
                    rmin = r
                rmax = r
                cmin = c if cmin is None else min(cmin, c)
                cmax = max(cmax, c)
    if rmin is None:
        return None
    return (rmin, rmax, cmin, cmax)


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

        # "Trivial" = the sheet has exactly one island that already covers all of
        # its non-blank content starting at the top-left origin. Compared against
        # the non-blank bounding box (not raw_full.shape) so trailing all-blank
        # rows/cols don't spuriously force the region path. Phantom header columns
        # make the island narrower than the bbox -> not trivial -> region path
        # (which trims them), as intended.
        bbox = _nonblank_bbox(raw_full)
        trivial = (
            len(islands) == 1 and bbox is not None
            and bbox[0] == 0 and bbox[2] == 0
            and (islands[0].min_row, islands[0].max_row,
                 islands[0].min_col, islands[0].max_col) == bbox
        )

        if not islands or trivial:
            # Reuse the in-memory raw frame for header detection when we have it
            # (avoids a second file read); fall back to read_raw on the error path.
            head_src = raw_full if raw_full is not None else read_raw(save_path, sheet_arg)
            hr, _conf = resolve_header_row(head_src, context=f"{save_path}::{sheet_label}")
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
