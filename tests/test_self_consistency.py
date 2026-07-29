"""Unit tests for the execution-based self-consistency helpers in
apps/chat/task/agentic.py — pure, no DB/LLM needed.

Run inside the container:
    docker cp tests/test_self_consistency.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_self_consistency.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from apps.chat.task.agentic import result_fingerprint, select_by_consensus


def _result(fields, rows):
    return {'fields': fields, 'data': rows}


# --- fingerprinting ---------------------------------------------------------
def test_identical_results_fingerprint_identically():
    a = _result(['total'], [{'total': 1385049.13}])
    b = _result(['total'], [{'total': 1385049.13}])
    assert result_fingerprint(a['fields'], a['data']) == result_fingerprint(b['fields'], b['data'])


def test_numeric_forms_are_canonicalised():
    """1385049.13 and 1385049.130 are the same answer."""
    a = result_fingerprint(['total'], [{'total': 1385049.13}])
    b = result_fingerprint(['total'], [{'total': '1385049.130'}])
    assert a == b


def test_integral_floats_match_ints():
    assert result_fingerprint(['n'], [{'n': 63.0}]) == result_fingerprint(['n'], [{'n': 63}])


def test_column_aliases_do_not_affect_fingerprint():
    """Two candidates aliasing the same aggregate differently agree on values."""
    a = result_fingerprint(['total'], [{'total': 100}])
    b = result_fingerprint(['sum_amount'], [{'sum_amount': 100}])
    assert a == b


def test_row_order_does_not_affect_fingerprint():
    a = result_fingerprint(['x'], [{'x': 1}, {'x': 2}])
    b = result_fingerprint(['x'], [{'x': 2}, {'x': 1}])
    assert a == b


def test_different_values_fingerprint_differently():
    a = result_fingerprint(['total'], [{'total': 100}])
    b = result_fingerprint(['total'], [{'total': 101}])
    assert a != b


def test_row_count_is_part_of_the_fingerprint():
    a = result_fingerprint(['x'], [{'x': 1}])
    b = result_fingerprint(['x'], [{'x': 1}, {'x': 1}])
    assert a != b


def test_empty_and_none_results_are_stable():
    assert result_fingerprint([], []) == result_fingerprint(None, None)


def test_null_cells_distinguishable_from_empty_string():
    a = result_fingerprint(['x'], [{'x': None}])
    b = result_fingerprint(['x'], [{'x': ''}])
    assert a != b


# --- consensus selection ----------------------------------------------------
def _candidate(value, ok=True, rows=None):
    data = rows if rows is not None else [{'total': value}]
    result = _result(['total'], data)
    return {'sql': f'SELECT {value}', 'result': result, 'ok': ok,
            'fingerprint': result_fingerprint(result['fields'], result['data'])}


def test_majority_wins():
    """Two of three candidates agree on 100; the outlier 999 loses even though
    it was generated first."""
    winner = select_by_consensus([_candidate(999), _candidate(100), _candidate(100)])
    assert winner['sql'] == 'SELECT 100'
    assert winner['votes'] == 2
    assert winner['total_votes'] == 3


def test_full_tie_keeps_the_primary():
    """All candidates disagree -> the first (streamed primary) is kept, so a
    tied vote reproduces the pre-consensus behaviour exactly."""
    winner = select_by_consensus([_candidate(1), _candidate(2), _candidate(3)])
    assert winner['sql'] == 'SELECT 1'
    assert winner['votes'] == 1


def test_non_empty_result_beats_equally_supported_empty_one():
    """Two candidates returning zero rows 'agree' trivially; that agreement is
    worth less than a candidate that actually found data."""
    empty = {'sql': 'empty', 'result': _result(['x'], []), 'ok': True,
             'fingerprint': result_fingerprint(['x'], [])}
    full = _candidate(100)
    winner = select_by_consensus([empty, full])
    assert winner['sql'] == 'SELECT 100'


def test_failed_candidates_do_not_vote():
    winner = select_by_consensus([
        _candidate(100),
        _candidate(999, ok=False),
        _candidate(999, ok=False),
    ])
    assert winner['sql'] == 'SELECT 100'
    assert winner['total_votes'] == 1


def test_returns_none_when_nothing_executed():
    assert select_by_consensus([]) is None
    assert select_by_consensus([_candidate(1, ok=False)]) is None


def test_single_candidate_wins_trivially():
    winner = select_by_consensus([_candidate(42)])
    assert winner['sql'] == 'SELECT 42'
    assert winner['votes'] == 1
    assert winner['total_votes'] == 1
