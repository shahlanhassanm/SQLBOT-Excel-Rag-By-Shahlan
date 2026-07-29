"""E2E: two SIDE-BY-SIDE tables in one sheet become two datasource tables, each
with its OWN correct header. Verifies actual Postgres columns + row counts.

    docker cp tests/test_island_sidebyside_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_island_sidebyside_e2e.py"
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


def make_side_by_side_xlsx() -> bytes:
    """One sheet: left table (Name/Age) + empty column + right table (Product/Price)."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'sbs'
    for row in [["Name", "Age", "", "Product", "Price"],
                ["Alice", "30", "", "Widget", "9.99"],
                ["Bob", "25", "", "Gadget", "19.99"],
                ["Carol", "40", "", "Gizmo", "4.99"]]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(headers, filename, content):
    boundary = 'sqlbotSbsBoundary'
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
        r = upload(headers, 'e2e_sbs.xlsx', make_side_by_side_xlsx())
        d = r.get('data', r)
        assert d.get('id'), f'upload failed: {r}'
        created.append((d['id'], d['name']))

        t = http('POST', f'{BASE}/datasource/tableList/{d["id"]}', b'', headers)
        tables = sorted(x['table_name'] for x in t.get('data', t))
        assert len(tables) == 2, f'expected 2 tables (side-by-side split), got {tables}'

        engine = get_engine_conn()
        col_sets = []
        counts = {}
        with engine.connect() as conn:
            for tn in tables:
                cols = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"), {"t": tn}).fetchall()
                col_sets.append(frozenset(c[0].lower() for c in cols))
                counts[tn] = conn.execute(text(f'SELECT count(*) FROM "{tn}"')).scalar()

        expected = [frozenset({"name", "age"}), frozenset({"product", "price"})]
        assert sorted(col_sets, key=lambda s: sorted(s)) == sorted(expected, key=lambda s: sorted(s)), \
            f'tables do not have distinct correct headers: {col_sets}'
        assert sorted(counts.values()) == [3, 3], f'row counts {counts} != [3,3]'
        # the merged-failure signature must be gone:
        for cs in col_sets:
            assert not any(c.startswith('unnamed') for c in cs), f'merged header leaked: {cs}'
        print(f'SIDE-BY-SIDE OK -> tables={tables}')
        print(f'   columns per table: {[sorted(s) for s in col_sets]}')
        print(f'   row counts: {counts}')
        print('SIDE-BY-SIDE E2E PASS')
    finally:
        for ds_id, name in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{ds_id}/{name}', b'', headers)
                print(f'cleaned up ds {ds_id} ({name})')
            except Exception as e:
                print(f'cleanup failed for {ds_id}: {e}')


if __name__ == '__main__':
    run()
