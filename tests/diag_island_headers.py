"""Diagnostic: does detect_island_specs resolve a CORRECT, DISTINCT header for
EACH island in one sheet? Run in-container:

    docker cp tests/diag_island_headers.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/diag_island_headers.py"
"""
import sys
import tempfile
import os

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from openpyxl import Workbook
from common.core.config import settings
from apps.datasource.utils.excel import detect_island_specs


def build(rows, name):
    wb = Workbook()
    ws = wb.active
    ws.title = "s1"
    for r in rows:
        ws.append(r)
    p = os.path.join(tempfile.gettempdir(), name)
    wb.save(p)
    return p


def show(title, rows, split=False):
    settings.EXCEL_SPLIT_SIDE_BY_SIDE = split
    p = build(rows, "diag.xlsx")
    specs = detect_island_specs(p)
    print(f"\n### {title}  (split_side_by_side={split}) -> {len(specs)} island(s)")
    for s in specs:
        cols = [str(f['fieldName']) for f in s['fields']]
        print(f"   region={s['region']} headerRow={s['headerRow']} cols={cols}")


# L1: two stacked tables, distinct headers, header at first row of each band
show("L1 stacked, headers at band start", [
    ["Name", "Age"],
    ["Alice", "30"],
    ["Bob", "25"],
    [],
    ["City", "Pop", "Country"],
    ["NYC", "8", "US"],
    ["LA", "4", "US"],
])

# L2: second table has a TITLE row above its real header
show("L2 stacked, 2nd table has a title row above header", [
    ["Name", "Age"],
    ["Alice", "30"],
    [],
    ["SALES REPORT 2024", "", ""],
    ["Product", "Price", "Qty"],
    ["Widget", "9.99", "5"],
    ["Gadget", "19.99", "3"],
])

# L3: two stacked tables, different column counts AND different left offset
show("L3 stacked, different shapes & offsets", [
    ["id", "name"],
    ["1", "a"],
    ["2", "b"],
    [],
    [],
    ["", "year", "month", "total"],
    ["", "2024", "01", "100"],
    ["", "2024", "02", "200"],
])

# L4: side-by-side tables (same rows, separated by a blank column)
sbs = [
    ["Name", "Age", "", "Product", "Price"],
    ["Alice", "30", "", "Widget", "9.99"],
    ["Bob", "25", "", "Gadget", "19.99"],
]
show("L4 side-by-side, DEFAULT", sbs, split=False)
show("L4 side-by-side, SPLIT ON", sbs, split=True)

# L5: two stacked tables with NO blank row between them (adjacent)
show("L5 adjacent stacked, NO blank row between", [
    ["Name", "Age"],
    ["Alice", "30"],
    ["Bob", "25"],
    ["Product", "Price", "Qty"],
    ["Widget", "9.99", "5"],
    ["Gadget", "19.99", "3"],
])

settings.EXCEL_SPLIT_SIDE_BY_SIDE = False
print("\nDONE")
