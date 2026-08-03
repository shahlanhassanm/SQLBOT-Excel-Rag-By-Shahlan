"""End-to-end test of the header detection + override in the running container."""
import os, sys, tempfile, json
sys.path.insert(0, '/opt/sqlbot/app')
import pandas as pd
from apps.datasource.utils.excel import parse_excel_preview, reparse_sheet

# Build an Excel file where the header is on row 3, not row 0
tmpdir = tempfile.mkdtemp()
xlsx = os.path.join(tmpdir, 'test.xlsx')

with pd.ExcelWriter(xlsx, engine='openpyxl') as w:
    pd.DataFrame([
        ['Quarterly Sales Report', '', '', ''],
        ['Generated 2026-05-21', '', '', ''],
        ['', '', '', ''],
        ['region', 'product', 'units', 'revenue'],
        ['North', 'Widget', 120, 4800.50],
        ['South', 'Gadget', 80, 3200.00],
        ['East',  'Widget', 95, 3800.25],
    ]).to_excel(w, sheet_name='Sales', index=False, header=False)

print('=== auto-detect parse ===')
sheets = parse_excel_preview(xlsx)
s = sheets[0]
print(f"headerRow={s['headerRow']}  confidence={s['headerConfidence']}  rows={s['rows']}")
print(f"fields={[f['fieldName'] for f in s['fields']]}")
print(f"first data row={s['data'][0]}")
print(f"rawHead has {len(s['rawHead'])} rows")
assert s['headerRow'] == 3, f"expected header on row 3, got {s['headerRow']}"
assert list(s['data'][0].keys()) == ['region','product','units','revenue']
print('OK auto-detect put header on row 3')

print()
print('=== user override to row 0 (wrong on purpose) ===')
s2 = reparse_sheet(xlsx, 'Sales', 0)
print(f"headerRow={s2['headerRow']}  fields={[f['fieldName'] for f in s2['fields']]}")
assert s2['headerRow'] == 0
assert 'Quarterly Sales Report' in s2['fields'][0]['fieldName']
print('OK override respected')

print()
print('=== sidecar persisted after override ===')
sidecar = xlsx + '.sqlbot_headers.json'
print(json.load(open(sidecar)))

print()
print('=== reparse to row 3, simulating user fix ===')
s3 = reparse_sheet(xlsx, 'Sales', 3)
print(f"headerRow={s3['headerRow']}  fields={[f['fieldName'] for f in s3['fields']]}")
assert s3['headerRow'] == 3
print('OK')
print()
print('ALL TESTS PASSED')
