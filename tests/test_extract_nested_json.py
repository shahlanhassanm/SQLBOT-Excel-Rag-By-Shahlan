"""Answer selection when a model reply contains more than one JSON object.

AUDIT D-09. extract_nested_json collected every valid top-level JSON value and
returned results[0] — the FIRST. Every consumer (check_sql, chart config, brief,
chart type, self-consistency candidates, cross-datasource legs) treats that as
"the model's answer". A model that prefaces its reply with JSON — a restated
output format, a worked example, an echoed schema — therefore had its EXAMPLE
parsed as its answer.

The repo's own BIRD harness already takes the opposite view: extract_sql uses
m[-1], commented "models like to preface prose".

The fix is an optional prefer_keys argument. Default None keeps the historical
first-match behaviour byte-for-byte, so nothing not explicitly targeted changes.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_extract_nested_json.py -q"
"""

import orjson

from common.utils.utils import extract_nested_json

SQL_KEYS = ('sql', 'success')


# --- backward compatibility: the default must not change -------------------

def test_default_still_returns_the_first_object():
    text = '{"a": 1} then {"b": 2}'
    assert extract_nested_json(text) == '{"a": 1}'


def test_default_single_object_unchanged():
    assert extract_nested_json('noise {"sql": "SELECT 1"} noise') == '{"sql": "SELECT 1"}'


def test_returns_none_when_there_is_no_json():
    assert extract_nested_json("no json here at all") is None
    assert extract_nested_json("") is None


def test_unbalanced_braces_do_not_crash():
    assert extract_nested_json('{"a": 1') is None
    assert extract_nested_json('}}}{{{') is None


def test_arrays_are_still_extracted():
    assert extract_nested_json('prefix [1, 2, 3] suffix') == '[1, 2, 3]'


# --- the D-09 fix ----------------------------------------------------------

def test_prefers_the_last_object_carrying_an_answer_key():
    """The reported failure: an example object precedes the real answer."""
    text = (
        'Here is the required output format:\n'
        '{"success": true, "sql": "SELECT <column> FROM <table>"}\n'
        'And here is my answer:\n'
        '{"success": true, "sql": "SELECT name FROM customers WHERE region = 1"}'
    )
    got = orjson.loads(extract_nested_json(text, prefer_keys=SQL_KEYS))
    assert got["sql"] == "SELECT name FROM customers WHERE region = 1"


def test_ignores_leading_objects_without_the_answer_keys():
    text = '{"thought": "I should look at customers"} {"sql": "SELECT 1"}'
    got = orjson.loads(extract_nested_json(text, prefer_keys=SQL_KEYS))
    assert got["sql"] == "SELECT 1"


def test_refusal_object_is_still_selected():
    """A refusal carries 'success' but no 'sql' — it must still be found."""
    text = '{"note": "thinking"} {"success": false, "message": "cannot answer"}'
    got = orjson.loads(extract_nested_json(text, prefer_keys=SQL_KEYS))
    assert got["success"] is False


def test_falls_back_to_first_when_no_object_has_the_keys():
    """A model that omits the expected key must parse exactly as before, so the
    fix can never make a previously-working reply unparseable."""
    text = '{"a": 1} {"b": 2}'
    assert extract_nested_json(text, prefer_keys=SQL_KEYS) == '{"a": 1}'


def test_chart_config_selection_uses_its_own_key():
    text = '{"sql": "SELECT 1"} {"type": "bar", "axis": {}}'
    got = orjson.loads(extract_nested_json(text, prefer_keys=('type',)))
    assert got["type"] == "bar"


def test_arrays_are_skipped_when_prefer_keys_is_given():
    """Only dicts can carry keys; a leading array must not be selected."""
    text = '[1, 2, 3] {"sql": "SELECT 1"}'
    got = orjson.loads(extract_nested_json(text, prefer_keys=SQL_KEYS))
    assert got["sql"] == "SELECT 1"


def test_single_answer_object_is_unaffected_by_prefer_keys():
    text = '{"success": true, "sql": "SELECT 1"}'
    assert (extract_nested_json(text, prefer_keys=SQL_KEYS)
            == extract_nested_json(text))
