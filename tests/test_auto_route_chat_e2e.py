"""E2E test for auto-route chats (no bound datasource).

Runs INSIDE the container against the live app:
    docker cp tests/test_auto_route_chat_e2e.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_auto_route_chat_e2e.py"

Verifies:
1. POST /chat/start WITHOUT a datasource succeeds (used by the new "Auto" card).
2. The created chat has no bound datasource.
3. POST /chat/start WITH a datasource still works as before (regression).
"""
import json
import sys
import urllib.request

sys.path.insert(0, '/opt/sqlbot/app')

BASE = 'http://localhost:8000/api/v1'


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
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
    token = res['data'].get('access_token') if isinstance(res.get('data'), dict) \
        else res.get('access_token')
    assert token, f'login failed: {res}'
    return {'X-SQLBOT-TOKEN': f'Bearer {token}', 'Content-Type': 'application/json'}


def unwrap(res):
    return res.get('data', res)


def main():
    headers = login()

    # 1+2: auto-route chat — EXACT frontend payload (explicit null, not omitted).
    # Sending {} would mask the Pydantic-v2 "explicit null vs omitted" 422 bug.
    r = http('POST', f'{BASE}/chat/start', json.dumps({'datasource': None, 'origin': 0}).encode(), headers)
    chat = unwrap(r)
    assert chat.get('id'), f'auto chat not created: {r}'
    assert not chat.get('datasource'), f'auto chat must stay unbound: {chat}'
    print(f"auto-route chat OK -> id={chat['id']} datasource={chat.get('datasource')}")

    # confirm it persists unbound in DB
    from sqlalchemy import create_engine, text
    from common.core.config import settings as st
    eng = create_engine(str(st.SQLALCHEMY_DATABASE_URI))
    with eng.connect() as c:
        row = c.execute(text('select datasource from chat where id = :i'),
                        {'i': chat['id']}).fetchone()
    assert row is not None and row[0] is None, f'chat.datasource should be NULL, got {row}'
    print('DB check OK — chat.datasource is NULL (finder will run per question)')

    # 3: regression — explicit-datasource chat still works (use any existing ds)
    lst = unwrap(http('GET', f'{BASE}/datasource/list', headers={
        'X-SQLBOT-TOKEN': headers['X-SQLBOT-TOKEN']}))
    if lst:
        ds_id = lst[0]['id']
        r2 = http('POST', f'{BASE}/chat/start',
                  json.dumps({'datasource': ds_id}).encode(), headers)
        chat2 = unwrap(r2)
        assert chat2.get('id') and chat2.get('datasource') == ds_id, f'explicit ds chat broken: {r2}'
        print(f"explicit-ds chat OK -> id={chat2['id']} datasource={chat2['datasource']}")
    else:
        print('no datasources present; skipped explicit-ds regression check')

    print('E2E PASS')


if __name__ == '__main__':
    main()
