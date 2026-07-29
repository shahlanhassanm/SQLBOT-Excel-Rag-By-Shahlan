"""Reproduce the user's 'edge_cases' sheet and see what detect_island_specs does
with the CURRENT deployed code (split_side_by_side default).

    docker cp tests/diag_edge_cases.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/diag_edge_cases.py"
"""
import os
import sys
import tempfile

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from openpyxl import Workbook
from common.core.config import settings
from apps.datasource.utils.excel import detect_island_specs
from apps.datasource.utils.header_detection import read_raw_full
from apps.datasource.utils.island_detection import detect_islands

# Reconstructed from the screenshot (header=None grid). "" = blank cell.
GRID = [
    ["ID", "Value", "", "", "Project", "Owner", "Status", "Budget"],
    ["1", "X", "", "", "ProjectX", "TeamA", "Active", "50000"],
    ["2", "Y", "", "", "ProjectY", "TeamB", "", "75000"],
    ["", "", "", "", "ProjectZ", "TeamC", "Completed", ""],
    ["", "", "", "", "ProjectW", "", "Planning", "120000"],
    ["Single_Column_Island", "", "", "", "", "", "", ""],
    ["Alpha", "", "", "", "", "", "", ""],
    ["Beta", "", "", "", "Event", "Start_Date", "End_Date", ""],
    ["Gamma", "", "", "", "Conference", "2026-07-01", "2026-07-03", ""],
    ["", "", "", "", "Workshop", "2026-08-15", "2026-08-16", ""],
]


def build(grid):
    wb = Workbook()
    ws = wb.active
    ws.title = "EdgeCases"
    for r in grid:
        ws.append(r)
    p = os.path.join(tempfile.gettempdir(), "edge.xlsx")
    wb.save(p)
    return p


def main_run():
    print(f"settings.EXCEL_SPLIT_SIDE_BY_SIDE = {settings.EXCEL_SPLIT_SIDE_BY_SIDE}")
    p = build(GRID)

    raw = read_raw_full(p, "EdgeCases")
    print(f"raw_full shape = {raw.shape}")
    isl = detect_islands(raw, split_side_by_side=settings.EXCEL_SPLIT_SIDE_BY_SIDE,
                         min_rows=settings.EXCEL_ISLAND_MIN_ROWS)
    print(f"detect_islands -> {len(isl)} island(s): {list(isl)}")

    specs = detect_island_specs(p)
    print(f"\ndetect_island_specs -> {len(specs)} spec(s):")
    for s in specs:
        cols = [str(f['fieldName']) for f in s['fields']]
        print(f"   region={s['region']} headerRow={s['headerRow']} cols={cols}")


if __name__ == '__main__':
    main_run()
