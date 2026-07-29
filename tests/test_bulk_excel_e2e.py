"""End-to-end test for POST /datasource/addExcelDatasource.

Runs INSIDE the container against the live app on localhost:8000:
    docker cp tests/test_bulk_excel_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_bulk_excel_e2e.py"

Creates two tiny Excel/CSV files, uploads each (simulating the bulk UI's
one-request-per-file behavior), asserts each became its own datasource with
one table per sheet, then deletes the test datasources.
"""
import io
import json
import sys
import urllib.request

import pandas as pd

sys.path.insert(0, '/opt/sqlbot/app')

BASE = 'http://localhost:8000/api/v1'


def http(method, url, data=None, headers=None, raw=False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return body if raw else json.loads(body)


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
    if isinstance(res.get('data'), dict):
        token = res['data'].get('access_token')
    else:
        token = res.get('access_token') or res.get('data')
    assert token, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {token}'}


def make_xlsx() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as w:
        pd.DataFrame({'region': ['north', 'south'], 'sales': [100, 250]}).to_excel(
            w, sheet_name='sales2024', index=False)
        pd.DataFrame({'region': ['north', 'south'], 'target': [120, 200]}).to_excel(
            w, sheet_name='targets', index=False)
    return buf.getvalue()


def make_csv() -> bytes:
    return b'employee,dept\nalice,eng\nbob,ops\n'


def upload(headers, filename, content, ctype):
    boundary = 'sqlbotE2Eboundary'
    body = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
    h = dict(headers)
    h['Content-Type'] = f'multipart/form-data; boundary={boundary}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def main():
    headers = login()
    created = []
    try:
        r1 = upload(headers, 'e2e_sales_book.xlsx', make_xlsx(),
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        d1 = r1.get('data', r1)
        assert d1.get('id') and len(d1.get('sheets', [])) == 2, f'xlsx upload wrong: {r1}'
        created.append((d1['id'], d1['name']))
        print(f"xlsx OK -> ds id={d1['id']} name={d1['name']} sheets={len(d1['sheets'])}")

        r2 = upload(headers, 'e2e_employees.csv', make_csv(), 'text/csv')
        d2 = r2.get('data', r2)
        assert d2.get('id') and len(d2.get('sheets', [])) == 1, f'csv upload wrong: {r2}'
        created.append((d2['id'], d2['name']))
        print(f"csv OK -> ds id={d2['id']} name={d2['name']} sheets={len(d2['sheets'])}")

        # both datasources visible in the list with correct table counts
        lst = http('GET', f'{BASE}/datasource/list', headers=headers)
        items = lst.get('data', lst)
        by_id = {i['id']: i for i in items}
        assert d1['id'] in by_id and d2['id'] in by_id, 'created ds missing from list'
        print('list OK — both datasources present')

        # saved tables of the new datasource: exactly one per sheet
        t1 = http('POST', f'{BASE}/datasource/tableList/{d1["id"]}', b'', headers)
        tables1 = t1.get('data', t1)
        names1 = sorted(x['table_name'] for x in tables1)
        assert len(tables1) == 2, f'expected 2 saved tables, got {names1}'
        assert any(n.startswith('sales2024') for n in names1) and \
            any(n.startswith('targets') for n in names1), names1
        print(f'tableList OK — xlsx ds tables: {names1}')

        t2 = http('POST', f'{BASE}/datasource/tableList/{d2["id"]}', b'', headers)
        tables2 = t2.get('data', t2)
        assert len(tables2) == 1, f'expected 1 saved table, got {tables2}'
        print(f'tableList OK — csv ds tables: {[x["table_name"] for x in tables2]}')
        print('E2E PASS')
    finally:
        for ds_id, name in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{ds_id}/{name}', b'', headers)
                print(f'cleaned up ds {ds_id} ({name})')
            except Exception as e:
                print(f'cleanup failed for {ds_id}: {e}')


if __name__ == '__main__':
    main()
