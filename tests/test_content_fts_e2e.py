"""Integration: (1) a datasource's embedding text now contains sampled cell
values; (2) FTS GIN indexes exist on text columns after upload; (3) FTS prompt
guidance is gated by engine. Run in-container against the live app:

    docker cp tests/test_content_fts_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_content_fts_e2e.py"
"""
import io
import json
import sys
import urllib.request

from openpyxl import Workbook

sys.path.insert(0, '/opt/sqlbot/app')
import main  # noqa: F401
from sqlalchemy import text  # noqa: E402
from apps.db.engine import get_engine_conn  # noqa: E402

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
    wb = Workbook(); ws = wb.active; ws.title = 'concerts'
    ws.append(['title', 'note'])
    ws.append(['Glenn Fredly Tribute', 'virtual concert honoring revered Indonesian singer'])
    ws.append(['Jazz Night', 'live performance with special guests'])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def upload(headers, content):
    b = 'sqlbotCFBoundary'
    ctype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="concerts.xlsx"\r\n'
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
        table_name = d['sheets'][0]['tableName']

        # (1) embedding text contains a sampled cell value
        from apps.datasource.crud.table import build_ds_schema_text
        from common.core.db import engine as meta_engine
        from sqlalchemy.orm import Session
        from apps.datasource.models.datasource import CoreDatasource
        with Session(meta_engine) as s:
            ds = s.query(CoreDatasource).filter(CoreDatasource.id == ds_id).first()
            schema_text = build_ds_schema_text(s, ds, include_samples=True)
        assert 'Glenn Fredly Tribute' in schema_text, f'sample value missing: {schema_text!r}'
        print('(1) OK - embedding text includes sampled cell values')

        # (2) FTS GIN index exists for a text column of the imported table
        eng = get_engine_conn()
        with eng.connect() as c:
            idxdefs = [r[0] for r in c.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table_name}).fetchall()]
        assert any('to_tsvector' in x for x in idxdefs), f'no FTS index on {table_name}: {idxdefs}'
        print('(2) OK - GIN to_tsvector index present')

        # (3) FTS prompt guidance gated by engine
        from apps.chat.task.apex_helpers import fts_prompt_addendum
        assert 'to_tsvector' in fts_prompt_addendum('excel', True)
        assert fts_prompt_addendum('mysql', True) == ''
        print('(3) OK - FTS prompt guidance gated by engine')
        print('CONTENT+FTS E2E PASS')
    finally:
        for i, n in created:
            try:
                http('POST', f'{BASE}/datasource/delete/{i}/{n}', b'', headers)
                print(f'cleaned up ds {i}')
            except Exception:
                pass


if __name__ == '__main__':
    run()
