"""Full end-to-end fanout: ask "list all thriller movies" in an auto chat and
assert the SAVED result table is a UNION across files (has a 'source' column
with >1 distinct source). Slow (real qwen model, several minutes).

    docker cp tests/test_fanout_union_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_fanout_union_e2e.py"
"""
import asyncio
import json
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, '/opt/sqlbot/app')
BASE = 'http://localhost:8000/api/v1'


def login():
    from common.utils.crypto import sqlbot_encrypt
    from common.core.config import settings
    loop = asyncio.new_event_loop()
    acc = loop.run_until_complete(sqlbot_encrypt('admin'))
    pw = loop.run_until_complete(sqlbot_encrypt(settings.DEFAULT_PWD))
    loop.close()
    data = urllib.parse.urlencode({'username': acc, 'password': pw}).encode()
    req = urllib.request.Request(f'{BASE}/login/access-token', data,
                                 {'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    res = json.loads(urllib.request.urlopen(req).read())
    tok = res['data']['access_token'] if isinstance(res.get('data'), dict) else res.get('access_token')
    return {'X-SQLBOT-TOKEN': f'Bearer {tok}', 'Content-Type': 'application/json'}


def main():
    H = login()
    # auto chat
    req = urllib.request.Request(f'{BASE}/chat/start', json.dumps({'datasource': None}).encode(), H, method='POST')
    chat = json.loads(urllib.request.urlopen(req).read())
    chat = chat.get('data', chat)
    chat_id = chat['id']
    print(f'auto chat id={chat_id}; asking... (slow, please wait)')

    body = json.dumps({'chat_id': chat_id, 'question': 'list all thriller movies'}).encode()
    req = urllib.request.Request(f'{BASE}/chat/question', body, H, method='POST')
    # consume the stream to completion
    with urllib.request.urlopen(req, timeout=1800) as r:
        for _ in r:
            pass
    print('stream finished; inspecting saved record...')

    from sqlalchemy import create_engine, text as t
    from common.core.config import settings
    eng = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))
    with eng.connect() as c:
        row = c.execute(t("select id, data, chart from chat_record where chat_id=:c and data is not null "
                          "order by id desc limit 1"), {'c': chat_id}).fetchone()
    assert row, 'no record with data saved'
    data = json.loads(row[1])
    chart = json.loads(row[2]) if row[2] else {}
    fields = data.get('fields', [])
    rows = data.get('data', [])
    sources = sorted({r.get('source') for r in rows if isinstance(r, dict) and r.get('source')})
    print(f'record {row[0]}: fields={fields}')
    print(f'  rows={len(rows)} | distinct sources={sources}')
    print(f'  chart.type={chart.get("type")} columns={[c.get("value") for c in chart.get("columns", [])]}')
    assert 'source' in fields, "union table must have a 'source' column"
    assert len(sources) >= 2, f'expected >=2 source files in the union, got {sources}'
    assert chart.get('type') == 'table', 'fanout should render a table chart'
    print('FANOUT UNION E2E PASS — combined table visible with multiple sources')


if __name__ == '__main__':
    main()
