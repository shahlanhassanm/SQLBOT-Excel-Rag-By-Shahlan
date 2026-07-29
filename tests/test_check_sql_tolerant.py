"""Verify check_sql / chart-type / brief parsing tolerate model key variants.

Reproduces the exact answer qwen2.5-coder:14b returned for "list all thriller
movies" (no 'success' flag, snake_case 'chart_type', 'dialogue_title') which
the old parser rejected as 'Cannot parse sql from answer'.

    docker cp tests/test_check_sql_tolerant.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python /tmp/test_check_sql_tolerant.py"
"""
import sys
import orjson

sys.path.insert(0, '/opt/sqlbot/app')
from common.utils.utils import extract_nested_json

# the real failing answer
ANSWER = '''```json
{
  "sql": "SELECT \\"Title\\", \\"Primary_Genre\\" FROM \\"public\\".\\"Movies_9dd361bff9\\" WHERE \\"Primary_Genre\\" = 'Thriller' LIMIT 50;",
  "chart_type": "table",
  "dialogue_title": "List of Thriller Movies"
}
```'''

# spec-compliant answer (must still work)
ANSWER_SPEC = '{"success": true, "sql": "SELECT 1", "chart-type": "table", "brief": "One"}'

# explicit refusal (must still be treated as refusal)
ANSWER_REFUSE = '{"success": false, "message": "I can only answer data questions"}'


def parse_sql(res):
    js = extract_nested_json(res)
    assert js is not None, 'extract_nested_json failed'
    d = orjson.loads(js)
    if d.get('success') is False:
        return ('REFUSE', d.get('message'))
    sql = d.get('sql')
    if not isinstance(sql, str) or not sql.strip():
        return ('EMPTY', None)
    return ('SQL', sql)


def chart_type(res):
    d = orjson.loads(extract_nested_json(res))
    if d.get('success') is False:
        return None
    return d.get('chart-type') or d.get('chart_type')


def brief(res):
    d = orjson.loads(extract_nested_json(res))
    if d.get('success') is False:
        return None
    return d.get('brief') or d.get('dialogue_title') or d.get('title')


def main():
    kind, val = parse_sql(ANSWER)
    assert kind == 'SQL' and 'Movies_9dd361bff9' in val, (kind, val)
    assert chart_type(ANSWER) == 'table'
    assert brief(ANSWER) == 'List of Thriller Movies'
    print('qwen answer (no success / snake_case): SQL extracted, chart=table, brief OK')

    assert parse_sql(ANSWER_SPEC)[0] == 'SQL'
    assert chart_type(ANSWER_SPEC) == 'table' and brief(ANSWER_SPEC) == 'One'
    print('spec-compliant answer: still works')

    assert parse_sql(ANSWER_REFUSE)[0] == 'REFUSE'
    print('explicit success=false: still treated as refusal')
    print('ALL PARSE TESTS PASS')


if __name__ == '__main__':
    main()
