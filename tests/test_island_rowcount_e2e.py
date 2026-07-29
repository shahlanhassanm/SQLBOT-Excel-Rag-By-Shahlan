"""E2E that verifies rows ACTUALLY land in Postgres (guards the COPY seek fix).

Uploads a one-sheet/two-island xlsx, then queries the physical tables' real row
counts via the same engine the importer used. If the COPY were a no-op (the old
un-rewound StringIO bug), counts would be [0, 0] and this fails.

    docker cp tests/test_island_rowcount_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_island_rowcount_e2e.py"
"""
import io
import json
import sys
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401  (load production import order / app context)
from apps.db.engine import get_engine_conn  # noqa: E402
from sqlalchemy import text  # noqa: E402

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
    boundary = 'sqlbotRowcountBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + \
        f'\r\n--{boundary}--\r\n'.encode()
    h = dict(headers)
    h['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def run():
    headers = login()
    created = []
    try:
        r = upload(headers, 'e2e_island_rowcount.xlsx', make_two_island_xlsx())
        d = r.get('data', r)
        assert d.get('id'), f'upload failed: {r}'
        created.append((d['id'], d['name']))

        t = http('POST', f'{BASE}/datasource/tableList/{d["id"]}', b'', headers)
        tables = sorted(x['table_name'] for x in t.get('data', t))
        assert len(tables) == 2, f'expected 2 tables, got {tables}'

        engine = get_engine_conn()
        counts = {}
        with engine.connect() as conn:
            for tn in tables:
                counts[tn] = conn.execute(text(f'SELECT count(*) FROM "{tn}"')).scalar()
        vals = sorted(counts.values())
        assert vals == [2, 3], f'ACTUAL DB row counts {counts} != [2, 3] -> COPY insert is broken'
        print(f'DB ROWCOUNT OK -> {counts}')
        print('ROWCOUNT E2E PASS')
    finally:
        for ds_id, name in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{ds_id}/{name}', b'', headers)
                print(f'cleaned up ds {ds_id} ({name})')
            except Exception as e:
                print(f'cleanup failed for {ds_id}: {e}')


if __name__ == '__main__':
    run()
