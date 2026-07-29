"""Header-row detection for uploaded Excel / CSV files.

Many real-world spreadsheets do NOT keep their column headers on the first
physical row. Row 0 is often a report title, a company name, an export
timestamp, a logo cell, or simply blank, and the real header sits a few rows
down. Pandas always treats row 0 as the header (``header=0``), which produces
``Unnamed: 0`` / ``Unnamed: 1`` ... columns and shifts the true header into the
data. The generated database table then has unusable column names and the
text-to-SQL model cannot map questions onto them.

This module finds the real header row *before* the file is read for real,
using an offline deterministic heuristic. The heuristic decides the easy
majority for free. When it is UNSURE (confidence < CONFIDENCE_THRESHOLD) there
is an OPT-IN LLM fallback (settings.HEADER_LLM_ENABLED, default off): a small
model is shown the first rows and asked which one is the header, and its answer
is used only if it parses to a valid in-range row index — otherwise the
heuristic pick stands. History note: an earlier build found small local models
guessed WORSE than the heuristic and caused silent bad imports, so the fallback
is (a) off by default, (b) scoped to only the low-confidence cases the heuristic
already flags, (c) validated + heuristic-guarded, and (d) model-configurable so
a stronger model can be used if a small one underperforms on your data.

To keep the preview step and the import step in agreement, the resolved header
row per sheet is persisted next to the uploaded file as a small JSON sidecar
(see ``save_header_meta`` / ``load_header_meta``).
"""

import datetime as _dt
import json
import logging
import math
import numbers
import os
import re

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "detect_header_row",
    "resolve_header_row",
    "read_raw",
    "read_raw_full",
    "read_sheet",
    "save_header_meta",
    "load_header_meta",
]

# --- tunables ---------------------------------------------------------------
# Defaults live in common.core.config so a deployment can retune the heuristic
# for its own spreadsheet conventions via environment variables instead of a
# code change and a rebuild. The literals below are the fallback for when this
# module is used standalone (the pure-function tests import it with no app
# config on the path), and they must stay in step with the Settings defaults.
# The names are exported and referenced elsewhere (e.g. tests/bulk_ingest.py),
# so they remain module-level constants rather than settings lookups.
_DEFAULTS = {
    "DETECT_ROWS": 25,          # how many leading rows to sample when detecting
    "DATA_SAMPLE": 20,          # rows below a candidate used to profile the data
    "MIN_FILL_RATIO": 0.6,      # a header must fill at least this fraction of columns
    "CONFIDENCE_THRESHOLD": 0.62,  # heuristic score below this triggers the LLM
    # composite-score weights (sum to 1.0)
    "W_FILL": 0.30,          # header rows are densely filled; title rows are sparse
    "W_UNIQUE": 0.16,        # header labels are (mostly) distinct (softened: a real header
                             # can legitimately repeat a label across parallel columns)
    "W_STRING": 0.14,        # header cells are text, not numbers/dates
    "W_DIVERGENCE": 0.20,    # header text sits on top of typed (numeric/date) data
    "W_BELOW": 0.05,         # the rows underneath should actually contain data
    "W_BREVITY": 0.15,       # header cells are SHORTER than the data beneath them. This is
                             # the key signal for all-text tables (where W_DIVERGENCE is
                             # blind), such as rosters of short text labels over longer
                             # free-text rows. Rewards a short candidate; never penalises
                             # a long one (clamped at 0).
    # Multiplicative position prior (NOT part of the weights above): real headers
    # sit near the top. Without it, a lucky short data row deep in the sheet can
    # out-score the genuine row-0/near-top header (the brevity bonus is noisy on a
    # tiny sample of rows below a deep candidate). Decays a deep candidate's
    # score; row 0 is untouched.
    "W_POSITION": 0.15,
}

# Setting name in config -> constant name here.
_SETTING_NAMES = {
    "DETECT_ROWS": "HEADER_DETECT_ROWS",
    "DATA_SAMPLE": "HEADER_DETECT_DATA_SAMPLE",
    "MIN_FILL_RATIO": "HEADER_DETECT_MIN_FILL_RATIO",
    "CONFIDENCE_THRESHOLD": "HEADER_DETECT_CONFIDENCE",
    "W_FILL": "HEADER_W_FILL",
    "W_UNIQUE": "HEADER_W_UNIQUE",
    "W_STRING": "HEADER_W_STRING",
    "W_DIVERGENCE": "HEADER_W_DIVERGENCE",
    "W_BELOW": "HEADER_W_BELOW",
    "W_BREVITY": "HEADER_W_BREVITY",
    "W_POSITION": "HEADER_W_POSITION",
}

try:  # app context: take the configured values
    from common.core.config import settings as _settings
    for _name, _default in _DEFAULTS.items():
        globals()[_name] = getattr(_settings, _SETTING_NAMES[_name], _default)
except Exception:  # standalone / no config on the path: use the literals
    globals().update(_DEFAULTS)

DETECT_ROWS: int
DATA_SAMPLE: int
MIN_FILL_RATIO: float
CONFIDENCE_THRESHOLD: float
W_FILL: float
W_UNIQUE: float
W_STRING: float
W_DIVERGENCE: float
W_BELOW: float
W_BREVITY: float
W_POSITION: float

_DATE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")


# --- cell classification ----------------------------------------------------
def _is_blank(value) -> bool:
    """True for None, NaN/NaT and empty/whitespace-only strings."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return isinstance(value, str) and value.strip() == ""


def _is_number_like(value) -> bool:
    """True if the cell holds (or textually looks like) a number."""
    if isinstance(value, bool):
        return False
    if isinstance(value, numbers.Number):
        try:
            return not math.isnan(float(value))
        except (TypeError, ValueError):
            return True
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("%", "").replace("$", "")
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _is_date_like(value) -> bool:
    """True for real date/datetime objects or date-shaped strings."""
    if isinstance(value, (_dt.date, _dt.datetime, pd.Timestamp)):
        return True
    return isinstance(value, str) and bool(_DATE_RE.match(value.strip()))


# --- heuristic --------------------------------------------------------------
def _score_candidate(raw: pd.DataFrame, h: int) -> float:
    """Score row ``h`` of ``raw`` as a potential header row (0.0 - 1.0).

    ``raw`` must have been read with ``header=None`` so every physical row is
    data and the columns are a plain integer range.
    """
    ncols = raw.shape[1]
    if ncols == 0:
        return 0.0

    row = raw.iloc[h]
    non_blank = [c for c in row if not _is_blank(c)]
    nfill = len(non_blank)
    if nfill == 0:
        return 0.0  # blank row -> never a header

    fill_ratio = nfill / ncols
    if fill_ratio < MIN_FILL_RATIO:
        return 0.0  # title / metadata rows fill only a cell or two

    below = raw.iloc[h + 1: h + 1 + DATA_SAMPLE]
    if below.shape[0] == 0:
        return 0.0  # a header with no data underneath is not a header

    # header labels should be (almost) all distinct
    normalized = [str(c).strip().lower() for c in non_blank]
    uniqueness = len(set(normalized)) / nfill

    # header cells should be text rather than numbers/dates
    text_cells = sum(
        1 for c in non_blank if not _is_number_like(c) and not _is_date_like(c)
    )
    string_ratio = text_cells / nfill

    # the rows underneath should actually contain data
    below_fills = [
        sum(1 for c in brow if not _is_blank(c)) / ncols
        for _, brow in below.iterrows()
    ]
    below_fill = sum(below_fills) / len(below_fills) if below_fills else 0.0

    # key signal: a text header cell sitting on top of a typed data column
    typed_cols = 0
    divergent_cols = 0
    for j in range(ncols):
        hc = row.iloc[j]
        if _is_blank(hc):
            continue
        col_vals = [v for v in below.iloc[:, j] if not _is_blank(v)]
        if not col_vals:
            continue
        typed_cols += 1
        typed = sum(1 for v in col_vals if _is_number_like(v) or _is_date_like(v))
        header_is_text = not _is_number_like(hc) and not _is_date_like(hc)
        if header_is_text and typed / len(col_vals) >= 0.6:
            divergent_cols += 1
    type_divergence = divergent_cols / typed_cols if typed_cols else 0.0

    # header labels tend to be SHORTER than the data values beneath them. This is
    # the decisive signal when the data is all-text (type_divergence is then 0, so
    # a real header row (short labels, some possibly repeated) can't be told apart
    # from the first data row by the other signals). Reward a candidate whose
    # cells are shorter than the rows below; never penalise a longer one (clamp 0).
    header_len = sum(len(str(c).strip()) for c in non_blank) / nfill
    below_cell_lens = [
        len(str(v).strip())
        for _, brow in below.iterrows()
        for v in brow if not _is_blank(v)
    ]
    data_len = (sum(below_cell_lens) / len(below_cell_lens)) if below_cell_lens else header_len
    brevity = max(0.0, (data_len - header_len) / data_len) if data_len > 0 else 0.0

    position_factor = 1.0 - W_POSITION * (h / max(1, DETECT_ROWS - 1))

    return position_factor * (
        W_FILL * fill_ratio
        + W_UNIQUE * uniqueness
        + W_STRING * string_ratio
        + W_DIVERGENCE * type_divergence
        + W_BELOW * below_fill
        + W_BREVITY * brevity
    )


def detect_header_row(raw: pd.DataFrame) -> tuple[int, float]:
    """Return ``(header_row_index, confidence)`` for a ``header=None`` frame.

    ``confidence`` is the winning composite score in the range 0.0 - 1.0.
    Falls back to ``(0, 0.0)`` when nothing scores, so callers always get a
    usable index.
    """
    if raw is None or len(raw) == 0:
        return 0, 0.0
    if len(raw) == 1:
        return 0, 1.0

    best_idx, best_score = 0, 0.0
    for h in range(min(DETECT_ROWS, len(raw))):
        score = _score_candidate(raw, h)
        if score > best_score:
            best_idx, best_score = h, score
    return best_idx, best_score


def build_header_llm_prompt(raw: pd.DataFrame, max_rows: int = 15) -> str:
    """Pure: render the first rows as a compact grid + a strict instruction to
    return only the 0-based header row index. No LLM/DB imports here."""
    n = min(max_rows, len(raw))
    lines = []
    for i in range(n):
        cells = []
        for v in list(raw.iloc[i]):
            s = "" if _is_blank(v) else str(v)
            cells.append(s[:40])
        lines.append(f"Row {i}: " + " | ".join(cells))
    grid = "\n".join(lines)
    return (
        "You are given the first rows of a spreadsheet as a grid. Exactly ONE row is "
        "the HEADER (the column titles); every row below it is data. The header row "
        "usually holds short label-like text, and the rows under it hold the actual "
        "values. Reply with ONLY the 0-based index of the header row as a single "
        f"integer, nothing else.\n\n{grid}\n\nHeader row index:"
    )


def parse_header_row_answer(text: str, n_rows: int):
    """Pure: pull a valid 0-based row index out of an LLM reply, else None (caller
    keeps the heuristic). Prefers the LAST in-range integer, since reasoning models
    tend to put their final answer at the end and may echo other numbers first."""
    if not text:
        return None
    ints = [int(x) for x in re.findall(r"-?\d+", str(text))]
    for idx in reversed(ints):
        if 0 <= idx < n_rows:
            return idx
    return None


def resolve_header_row(raw: pd.DataFrame, context: str = "") -> tuple[int, float]:
    """Resolve the header row: offline heuristic first, then an OPT-IN LLM fallback
    only when the heuristic is unsure.

    Returns ``(header_row, confidence)``. When confidence is below the LLM trigger
    threshold and settings.HEADER_LLM_ENABLED is on, a (small) model is asked to
    pick the header row; its answer is used only if it parses to a valid in-range
    index, otherwise the heuristic pick stands. Imports are lazy so this module
    stays importable without the app/LLM stack.
    """
    idx, confidence = detect_header_row(raw)

    # LLM fallback fires whenever heuristic confidence is below the LLM trigger
    # threshold (which may be set HIGHER than CONFIDENCE_THRESHOLD to also catch
    # borderline picks). Checked before any early return so it is never bypassed.
    try:
        from common.core.config import settings
        trigger = getattr(settings, "HEADER_LLM_TRIGGER_CONF", CONFIDENCE_THRESHOLD)
        if getattr(settings, "HEADER_LLM_ENABLED", False) and confidence < trigger:
            from apps.datasource.utils.header_llm import llm_detect_header_row
            llm_idx = llm_detect_header_row(raw, context)
            if llm_idx is not None and llm_idx != idx:
                logger.info("LLM header override row %s -> %s for %s", idx, llm_idx, context)
                return llm_idx, max(confidence, CONFIDENCE_THRESHOLD)
    except Exception as e:
        logger.warning("LLM header fallback failed (%s); keeping heuristic for %s", e, context)

    if confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            "Low-confidence header (row %s, %.2f) for %s; user override recommended",
            idx, confidence, context,
        )
    else:
        logger.debug("Header at row %s (confidence %.2f) %s", idx, confidence, context)
    return idx, confidence


# --- file readers -----------------------------------------------------------
def _is_csv(save_path: str) -> bool:
    return save_path.lower().endswith(".csv")


def read_raw(save_path: str, sheet_name=None) -> pd.DataFrame:
    """Read the first ``DETECT_ROWS`` rows with no header, for detection."""
    if _is_csv(save_path):
        return pd.read_csv(
            save_path, header=None, engine="c", nrows=DETECT_ROWS, dtype=object
        )
    return pd.read_excel(
        save_path,
        sheet_name=sheet_name if sheet_name is not None else 0,
        header=None,
        engine="calamine",
        nrows=DETECT_ROWS,
    )


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


def read_sheet(save_path: str, sheet_name, header_row: int, dtype=None) -> pd.DataFrame:
    """Read a full sheet using ``header_row`` as the column-name row.

    ``header_row=0`` reproduces the original pandas default, so normal files
    behave exactly as before.
    """
    if _is_csv(save_path):
        return pd.read_csv(save_path, header=header_row, engine="c", dtype=dtype)
    return pd.read_excel(
        save_path,
        sheet_name=sheet_name if sheet_name is not None else 0,
        header=header_row,
        engine="calamine",
        dtype=dtype,
    )


# --- sidecar persistence ----------------------------------------------------
def _meta_path(save_path: str) -> str:
    return save_path + ".sqlbot_headers.json"


def save_header_meta(save_path: str, mapping: dict) -> None:
    """Persist ``{sheet_name: header_row}`` next to the uploaded file."""
    try:
        with open(_meta_path(save_path), "w", encoding="utf-8") as f:
            json.dump(mapping, f)
    except OSError as e:
        logger.warning("Could not write header meta for %s: %s", save_path, e)


def load_header_meta(save_path: str) -> dict:
    """Load the ``{sheet_name: header_row}`` sidecar; ``{}`` if absent/invalid."""
    path = _meta_path(save_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logger.warning("Could not read header meta for %s: %s", save_path, e)
        return {}
