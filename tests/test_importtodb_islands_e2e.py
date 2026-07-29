"""E2E for the STANDARD wizard path (parseExcel -> importToDb), which previously
bypassed island detection. Uploads the edge grid, runs the same two calls the UI
makes, and asserts importToDb now returns 4 tables (not 1 merged). Verifies real
PG columns, then drops the orphan tables it created.

    docker cp tests/test_importtodb_islands_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_importtodb_islands_e2e.py"
"""
import io
import json
import sys
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from apps.db.engine import get_engine_conn  # noqa: E402
from sqlalchemy import text  # noqa: E402
from psycopg2 import sql as _sql  # noqa: E402

BASE = 'http://localhost:8000/api/v1'

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


def make_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = 'EdgeCases'
    for row in GRID:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_excel(headers, content):
    boundary = 'sqlbotParseBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="edge.xlsx"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + \
        f'\r\n--{boundary}--\r\n'.encode()
    h = dict(headers)
    h['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    return http('POST', f'{BASE}/datasource/parseExcel', body, h)


def run():
    headers = login()
    pe = parse_excel(headers, make_xlsx())
    pe = pe.get('data', pe)
    file_path = pe['filePath']
    preview = pe['data']
    # Build the importToDb request exactly like the wizard would, from the preview.
    sheets = [{"sheetName": s["sheetName"], "headerRow": s.get("headerRow"),
               "fields": s["fields"]} for s in preview]

    res = http('POST', f'{BASE}/datasource/importToDb',
               json.dumps({"filePath": file_path, "sheets": sheets}).encode(),
               {**headers, 'Content-Type': 'application/json'})
    res = res.get('data', res)
    out_sheets = res['sheets']

    table_names = sorted(s['tableName'] for s in out_sheets)
    try:
        assert len(out_sheets) == 4, f'importToDb returned {len(out_sheets)} tables, expected 4: {table_names}'
        assert all('_r' in n for n in table_names), f'expected region-suffixed names: {table_names}'

        engine = get_engine_conn()
        col_sets = []
        with engine.connect() as conn:
            for tn in table_names:
                cols = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                    {"t": tn}).fetchall()
                col_sets.append(frozenset(c[0].lower() for c in cols))
        expected = [frozenset({"id", "value"}), frozenset({"single_column_island"}),
                    frozenset({"project", "owner", "status", "budget"}),
                    frozenset({"event", "start_date", "end_date"})]
        key = lambda s: sorted(s)
        assert sorted(col_sets, key=key) == sorted(expected, key=key), \
            f'columns wrong: {[sorted(s) for s in col_sets]}'
        print(f'importToDb OK -> {len(out_sheets)} tables: {table_names}')
        for cs in sorted(col_sets, key=key):
            print(f'   cols={sorted(cs)}')
        print('IMPORTTODB ISLANDS E2E PASS')
    finally:
        # drop the orphan tables (importToDb creates tables but no datasource)
        engine = get_engine_conn()
        conn = engine.raw_connection()
        cur = conn.cursor()
        for tn in table_names:
            try:
                cur.execute(_sql.SQL("DROP TABLE IF EXISTS {}").format(_sql.Identifier(tn)))
            except Exception:
                pass
        conn.commit()
        cur.close()
        conn.close()
        print(f'cleaned up {len(table_names)} orphan tables')


if __name__ == '__main__':
    run()
