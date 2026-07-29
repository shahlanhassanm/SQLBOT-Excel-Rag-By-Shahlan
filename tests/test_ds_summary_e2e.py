"""Verifies a generic-description datasource gets a real generated summary. Run:
    docker cp tests/test_ds_summary_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_ds_summary_e2e.py"
"""
import io
import json
import sys
import time
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text  # noqa: E402
from common.core.db import engine as meta  # noqa: E402
from common.utils.embedding_threads import run_save_ds_embeddings  # noqa: E402

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
        acc = loop.run_until_complete(sqlbot_encrypt('admin'))
        pwd = loop.run_until_complete(sqlbot_encrypt(settings.DEFAULT_PWD))
    finally:
        loop.close()
    d = urllib.parse.urlencode({'username': acc, 'password': pwd}).encode()
    res = http('POST', f'{BASE}/login/access-token', d, {'Content-Type': 'application/x-www-form-urlencoded'})
    tok = (res['data'].get('access_token') if isinstance(res.get('data'), dict)
           else res.get('access_token') or res.get('data'))
    assert tok, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {tok}'}


def make_xlsx() -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = 'shows'
    ws.append(['title', 'cast', 'description'])
    ws.append(['Money Heist', 'Ursula Corbero', 'A criminal mastermind plans the biggest heist'])
    ws.append(['Some Movie', 'Jane Doe', 'A romance set in Paris'])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def upload(headers, content):
    b = 'sqlbotSummBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="shows.xlsx"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + content + f'\r\n--{b}--\r\n'.encode()
    h = dict(headers); h['Content-Type'] = f'multipart/form-data; boundary={b}'
    return http('POST', f'{BASE}/datasource/addExcelDatasource', body, h)


def run():
    headers = login()
    created = []
    try:
        d = upload(headers, make_xlsx()).get('data', {})
        ds_id = d.get('id'); created.append((ds_id, d.get('name')))
        assert ds_id, f'upload failed: {d}'

        run_save_ds_embeddings([ds_id])  # belt-and-suspenders (create_ds also triggers it)
        desc = None
        for _ in range(60):
            with meta.connect() as c:
                desc = c.execute(text("select description from core_datasource where id=:i"),
                                 {"i": ds_id}).scalar()
            if desc and not desc.startswith('Excel file:'):
                break
            time.sleep(5)

        assert desc, 'description is empty'
        assert not desc.startswith('Excel file:'), f'description still generic: {desc!r}'
        assert len(desc.strip()) > 10, f'summary too short: {desc!r}'
        print(f'SUMMARY OK -> description now: {desc!r}')
        print('DS SUMMARY E2E PASS')
    finally:
        for i, n in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{i}/{n}', b'', headers)
            except Exception:
                pass


if __name__ == '__main__':
    run()
