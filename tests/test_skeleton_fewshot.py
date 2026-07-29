"""Unit tests for apps/data_training/skeleton.py — pure, no DB needed.

Run inside the container:
    docker cp tests/test_skeleton_fewshot.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_skeleton_fewshot.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from apps.data_training.skeleton import (
    MASK_DOMAIN,
    MASK_NUMBER,
    MASK_VALUE,
    mask_question,
    rerank_by_skeleton,
    schema_domain_terms,
)


# --- masking ----------------------------------------------------------------
def test_masks_quoted_literals():
    assert MASK_VALUE in mask_question('how many are marked as "Paid"')


def test_masks_numbers_and_dates():
    masked = mask_question('top 5 vendors in 2024-01 with amount over 1,000.50')
    assert MASK_NUMBER in masked
    assert '2024-01' not in masked
    assert '1,000.50' not in masked


def test_masking_makes_same_shape_questions_identical():
    """The point of the skeleton: two questions with the same SQL shape but
    different literals collapse to the same string."""
    a = mask_question('show me the top 5 vendors')
    b = mask_question('show me the top 20 vendors')
    assert a == b


def test_masks_supplied_domain_terms():
    masked = mask_question('what is the total revenue by region',
                           domain_terms=['revenue', 'region'])
    assert 'revenue' not in masked
    assert MASK_DOMAIN in masked


def test_domain_masking_prefers_longest_term():
    masked = mask_question('group by order_date', domain_terms=['order', 'order_date'])
    # order_date must be masked whole, not leave a "_date" tail behind
    assert '_date' not in masked


def test_domain_masking_matches_spaced_variant():
    masked = mask_question('group by order date', domain_terms=['order_date'])
    assert MASK_DOMAIN in masked


def test_mask_question_handles_empty():
    assert mask_question('') == ''
    assert mask_question(None) == ''


# --- schema term extraction -------------------------------------------------
def test_schema_domain_terms_pulls_tables_and_columns():
    schema = """【DB_ID】 db
【Schema】
# Table: finance_x, comment
[
(Amount:numeric),
(Status:varchar, payment status)
]
"""
    terms = schema_domain_terms(schema)
    assert 'finance_x' in terms
    assert 'Amount' in terms
    assert 'Status' in terms


def test_schema_domain_terms_empty_input():
    assert schema_domain_terms('') == []


# --- re-ranking -------------------------------------------------------------
def _q(text):
    return {'question': text, 'suggestion-answer': 'SELECT 1'}


def test_rerank_promotes_same_shape_example():
    """The embedding stage ranked a topically-similar but structurally different
    example first; the skeleton re-rank should surface the same-shape one."""
    question = 'what are the top 3 departments by expense'
    candidates = [
        _q('list every department'),               # same topic, wrong shape
        _q('what are the top 10 vendors by amount'),  # different topic, right shape
    ]
    ranked = rerank_by_skeleton(question, candidates, top_k=1)
    assert ranked[0]['question'] == 'what are the top 10 vendors by amount'


def test_rerank_is_stable_when_nothing_matches():
    candidates = [_q('alpha beta'), _q('gamma delta')]
    ranked = rerank_by_skeleton('zzz qqq', candidates, top_k=2)
    assert [c['question'] for c in ranked] == ['alpha beta', 'gamma delta']


def test_rerank_respects_top_k():
    candidates = [_q(f'question number {i}') for i in range(10)]
    assert len(rerank_by_skeleton('question number 3', candidates, top_k=3)) == 3
    assert rerank_by_skeleton('x', candidates, top_k=0) == []


def test_rerank_handles_short_and_empty_input():
    assert rerank_by_skeleton('x', [], top_k=5) == []
    single = [_q('only one')]
    assert rerank_by_skeleton('x', single, top_k=5) == single


def test_rerank_never_drops_below_requested_count_when_available():
    candidates = [_q(f'show me the top {i} items') for i in range(1, 8)]
    ranked = rerank_by_skeleton('show me the top 4 items', candidates, top_k=5)
    assert len(ranked) == 5
    # all inputs are the same skeleton, so every result must come from the pool
    assert all(c in candidates for c in ranked)
