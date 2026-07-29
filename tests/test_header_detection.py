"""Unit + integration tests for Excel/CSV header-row detection.

These cover the fix for the "Unnamed header" bug: when the first physical row
of an uploaded file is a company name, a date, a report title or blank, pandas
would otherwise treat that junk row as the column header.

The ``header_detection`` module is loaded standalone (by file path) so the test
does not pull in the FastAPI / database backend.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest

import pandas as pd
from openpyxl import Workbook

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Resolve without assuming a checkout layout: hardcoding
# dirname(dirname(__file__))/backend/... made this file raise a collection error
# anywhere but a repo root -- including the deployed container -- which hid
# seven genuinely failing tests in here for as long as it has been broken.
_HD_CANDIDATES = [
    os.environ.get("HEADER_DETECTION_PATH", ""),
    os.path.join(PROJECT_ROOT, "backend", "apps", "datasource", "utils", "header_detection.py"),
    "/opt/sqlbot/app/apps/datasource/utils/header_detection.py",
]
_HD_PATH = next((p for p in _HD_CANDIDATES if p and os.path.exists(p)), "")
if not _HD_PATH:
    raise AssertionError(
        "header_detection.py not found in any of: "
        + ", ".join(p for p in _HD_CANDIDATES if p)
        + " -- set HEADER_DETECTION_PATH."
    )
_spec = importlib.util.spec_from_file_location("header_detection", _HD_PATH)
hd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hd)


def _raw(rows):
    """Build a header=None style frame (integer columns) from list-of-lists."""
    return pd.DataFrame(rows)


class TestHeuristicDetection(unittest.TestCase):
    """detect_header_row on in-memory frames."""

    def test_normal_file_header_on_row_zero(self):
        raw = _raw([
            ["product", "region", "units", "revenue"],
            ["Widget", "North", 100, 5000.0],
            ["Gadget", "South", 200, 9000.0],
            ["Gizmo", "East", 150, 7000.0],
        ])
        idx, conf = hd.detect_header_row(raw)
        self.assertEqual(idx, 0)
        self.assertGreaterEqual(conf, hd.CONFIDENCE_THRESHOLD)

    def test_title_row_then_header(self):
        raw = _raw([
            ["ACME Corporation Q3 Sales Report", None, None, None],
            ["product", "region", "units", "revenue"],
            ["Widget", "North", 100, 5000.0],
            ["Gadget", "South", 200, 9000.0],
        ])
        idx, conf = hd.detect_header_row(raw)
        self.assertEqual(idx, 1)
        self.assertGreaterEqual(conf, hd.CONFIDENCE_THRESHOLD)

    def test_company_name_date_blank_then_header(self):
        raw = _raw([
            ["ACME Corporation", None, None, None],
            ["Generated 2024-01-15", None, None, None],
            [None, None, None, None],
            ["product", "region", "units", "revenue"],
            ["Widget", "North", 100, 5000.0],
            ["Gadget", "South", 200, 9000.0],
            ["Gizmo", "East", 150, 7000.0],
        ])
        idx, _ = hd.detect_header_row(raw)
        self.assertEqual(idx, 3)

    def test_leading_blank_rows_are_skipped(self):
        raw = _raw([
            [None, None, None],
            [None, None, None],
            ["name", "score", "grade"],
            ["Alice", 91, "A"],
            ["Bob", 72, "C"],
        ])
        idx, _ = hd.detect_header_row(raw)
        self.assertEqual(idx, 2)

    def test_all_text_data_still_detects_row_zero(self):
        raw = _raw([
            ["first_name", "last_name", "city"],
            ["Alice", "Smith", "NYC"],
            ["Bob", "Jones", "LA"],
            ["Carol", "Lee", "SF"],
        ])
        idx, conf = hd.detect_header_row(raw)
        self.assertEqual(idx, 0)
        self.assertGreaterEqual(conf, hd.CONFIDENCE_THRESHOLD)

    def test_sparse_title_row_is_disqualified(self):
        raw = _raw([
            ["Big Report Title", None, None, None],
            ["a", "b", "c", "d"],
            [1, 2, 3, 4],
        ])
        self.assertEqual(hd._score_candidate(raw, 0), 0.0)
        self.assertGreater(hd._score_candidate(raw, 1), 0.0)

    def test_single_row_frame(self):
        idx, conf = hd.detect_header_row(_raw([["a", "b", "c"]]))
        self.assertEqual(idx, 0)
        self.assertEqual(conf, 1.0)

    def test_empty_frame(self):
        idx, conf = hd.detect_header_row(pd.DataFrame())
        self.assertEqual(idx, 0)
        self.assertEqual(conf, 0.0)


class TestResolveOrchestration(unittest.TestCase):
    """resolve_header_row: heuristic vs LLM fallback wiring.

    The fallback moved out of this module into apps.datasource.utils.header_llm
    and became opt-in behind settings.HEADER_LLM_ENABLED, so these patch the
    real seam rather than a `hd._llm_detect_header` attribute that no longer
    exists. (That stale name made the whole class error out, which went
    unnoticed because the file could not be collected at all.)
    """

    def setUp(self):
        from common.core.config import settings
        from apps.datasource.utils import header_llm

        self._settings = settings
        self._header_llm = header_llm
        self._orig_detect = hd.detect_header_row
        self._orig_llm = header_llm.llm_detect_header_row
        self._orig_enabled = settings.HEADER_LLM_ENABLED
        settings.HEADER_LLM_ENABLED = True

    def tearDown(self):
        hd.detect_header_row = self._orig_detect
        self._header_llm.llm_detect_header_row = self._orig_llm
        self._settings.HEADER_LLM_ENABLED = self._orig_enabled

    def test_confident_heuristic_never_calls_llm(self):
        def _boom(_raw_frame, _context=""):
            raise AssertionError("LLM must not be called for a confident result")

        self._header_llm.llm_detect_header_row = _boom
        raw = _raw([
            ["product", "region", "units", "revenue"],
            ["Widget", "North", 100, 5000.0],
            ["Gadget", "South", 200, 9000.0],
        ])
        self.assertEqual(hd.resolve_header_row(raw)[0], 0)

    def test_low_confidence_falls_back_to_llm(self):
        hd.detect_header_row = lambda _r: (0, 0.10)
        self._header_llm.llm_detect_header_row = lambda _r, _c="": 2
        self.assertEqual(hd.resolve_header_row(_raw([[1], [2], [3]]))[0], 2)

    def test_llm_failure_keeps_heuristic_guess(self):
        hd.detect_header_row = lambda _r: (4, 0.10)
        self._header_llm.llm_detect_header_row = lambda _r, _c="": None
        self.assertEqual(hd.resolve_header_row(_raw([[1], [2], [3]]))[0], 4)

    def test_fallback_stays_off_when_the_setting_is_off(self):
        """Default deployment must not gain an LLM call at ingest."""
        def _boom(_raw_frame, _context=""):
            raise AssertionError("LLM must not be called when HEADER_LLM_ENABLED is off")

        self._settings.HEADER_LLM_ENABLED = False
        self._header_llm.llm_detect_header_row = _boom
        hd.detect_header_row = lambda _r: (1, 0.10)
        self.assertEqual(hd.resolve_header_row(_raw([[1], [2], [3]]))[0], 1)


class TestFileReaders(unittest.TestCase):
    """read_raw / read_sheet on real .xlsx and .csv files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sqlbot_hdr_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _xlsx(self, name, rows, sheet_title="Data"):
        path = os.path.join(self.tmp, name)
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_title
        for row in rows:
            ws.append(row)
        wb.save(path)
        return path

    def _csv(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_xlsx_with_title_rows(self):
        path = self._xlsx("messy.xlsx", [
            ["ACME Corporation"],
            ["Quarterly Report - Generated 2024-01-15"],
            [],
            ["product", "region", "units", "revenue"],
            ["Widget", "North", 100, 5000],
            ["Gadget", "South", 200, 9000],
            ["Gizmo", "East", 150, 7000],
        ])
        raw = hd.read_raw(path, "Data")
        header_row, _confidence = hd.resolve_header_row(raw)
        self.assertEqual(header_row, 3)

        df = hd.read_sheet(path, "Data", header_row)
        self.assertEqual(
            list(df.columns), ["product", "region", "units", "revenue"]
        )
        self.assertFalse(
            any(str(c).startswith("Unnamed") for c in df.columns),
            "no Unnamed columns should remain",
        )
        self.assertEqual(len(df), 3)

    def test_xlsx_normal_file_unchanged(self):
        path = self._xlsx("normal.xlsx", [
            ["product", "region", "units"],
            ["Widget", "North", 100],
            ["Gadget", "South", 200],
        ])
        raw = hd.read_raw(path, "Data")
        self.assertEqual(hd.resolve_header_row(raw)[0], 0)
        df = hd.read_sheet(path, "Data", 0)
        self.assertEqual(list(df.columns), ["product", "region", "units"])
        self.assertEqual(len(df), 2)

    def test_csv_with_title_rows(self):
        path = self._csv("messy.csv", (
            "ACME Corporation,,,\n"
            "Generated 2024-01-15,,,\n"
            ",,,\n"
            "product,region,units,revenue\n"
            "Widget,North,100,5000\n"
            "Gadget,South,200,9000\n"
            "Gizmo,East,150,7000\n"
        ))
        raw = hd.read_raw(path)
        header_row, _confidence = hd.resolve_header_row(raw)
        self.assertEqual(header_row, 3)

        df = hd.read_sheet(path, None, header_row)
        self.assertEqual(
            list(df.columns), ["product", "region", "units", "revenue"]
        )
        self.assertFalse(any(str(c).startswith("Unnamed") for c in df.columns))
        self.assertEqual(len(df), 3)

    def test_csv_normal_file_unchanged(self):
        path = self._csv("normal.csv", (
            "name,score,grade\n"
            "Alice,91,A\n"
            "Bob,72,C\n"
        ))
        raw = hd.read_raw(path)
        self.assertEqual(hd.resolve_header_row(raw)[0], 0)
        df = hd.read_sheet(path, None, 0)
        self.assertEqual(list(df.columns), ["name", "score", "grade"])


class TestSidecarMeta(unittest.TestCase):
    """save_header_meta / load_header_meta round-trip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sqlbot_meta_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        path = os.path.join(self.tmp, "upload.xlsx")
        hd.save_header_meta(path, {"Sheet1": 3, "Data": 0})
        self.assertEqual(hd.load_header_meta(path), {"Sheet1": 3, "Data": 0})

    def test_missing_sidecar_returns_empty(self):
        path = os.path.join(self.tmp, "no_such_upload.xlsx")
        self.assertEqual(hd.load_header_meta(path), {})


if __name__ == "__main__":
    unittest.main()
