"""Unit tests for apps/chat/task/agentic.py — pure helpers, no DB/LLM needed.

Run inside the container:
    docker cp tests/test_agentic_helpers.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_agentic_helpers.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from apps.chat.task.agentic import (
    tokenize,
    bm25_scores,
    rrf_fuse,
    parse_grader_verdict,
    parse_decomposition,
    build_result_preview,
    format_retry_feedback,
    is_retryable_single_message,
    merge_union,
    raise_sql_limit,
    is_listing_question,
)


def test_raise_sql_limit_lifts_small_trailing_limit():
    assert raise_sql_limit('SELECT "x" FROM t LIMIT 10') == 'SELECT "x" FROM t LIMIT 1000'
    assert raise_sql_limit('select a from t limit 5;') == 'select a from t LIMIT 1000;'
    assert raise_sql_limit('SELECT a FROM t LIMIT 50 OFFSET 5') == 'SELECT a FROM t LIMIT 1000 OFFSET 5'


def test_raise_sql_limit_leaves_big_or_absent_limit():
    assert raise_sql_limit('SELECT a FROM t LIMIT 1000') == 'SELECT a FROM t LIMIT 1000'
    assert raise_sql_limit('SELECT a FROM t LIMIT 5000') == 'SELECT a FROM t LIMIT 5000'
    assert raise_sql_limit('SELECT a FROM t') == 'SELECT a FROM t'
    # inner subquery limit must NOT be touched (only trailing)
    s = 'SELECT * FROM (SELECT a FROM t LIMIT 10) z'
    assert raise_sql_limit(s) == s


def test_is_listing_question():
    assert is_listing_question('list all the payment gateways')
    assert is_listing_question('show every thriller movie')
    assert not is_listing_question('total revenue')
    assert not is_listing_question('how many games are there')


def test_is_listing_question_multilingual_via_injected_terms():
    from apps.chat.task.agentic import is_listing_question as ilq
    listing = ['list', '列出', '所有', '목록']
    agg = ['total', '总和', '합계']
    assert ilq('列出所有支付网关', listing, agg)          # Chinese "list all payment gateways"
    assert ilq('지불 게이트웨이 목록', listing, agg)        # Korean "payment gateway list"
    assert not ilq('总和收入', listing, agg)              # Chinese "total revenue"


def test_has_explicit_row_count():
    from apps.chat.task.agentic import has_explicit_row_count as hrc
    assert hrc('top 10 movies') and hrc('前10条') and hrc('show 5 records')
    assert not hrc('list all movies')
    assert not hrc('movies from 2013')   # 4-digit year is not a row count
    assert not hrc('total revenue')


def test_merge_union_combines_and_tags_source():
    legs = [
        {'datasource': 'thriller_2013_movies', 'fields': ['title'],
         'data': [{'title': 'Paranoia'}, {'title': 'The Conjuring'}]},
        {'datasource': 'col', 'fields': ['title', 'year'],
         'data': [{'title': 'Se7en', 'year': 1995}]},
    ]
    u = merge_union(legs)
    assert u['fields'] == ['source', 'title', 'year']
    assert len(u['data']) == 3
    assert u['data'][0] == {'source': 'thriller_2013_movies', 'title': 'Paranoia'}
    assert u['data'][2] == {'source': 'col', 'title': 'Se7en', 'year': 1995}


def test_merge_union_handles_empty_and_nondict():
    u = merge_union([{'datasource': 'a', 'fields': [], 'data': []}])
    assert u['fields'] == ['source'] and u['data'] == []


def test_merge_union_dedups_across_files_and_merges_sources():
    legs = [
        {'datasource': 'Payment_Gateways', 'fields': ['payment gateway'],
         'data': [{'payment gateway': 'Agro Bank'}, {'payment gateway': 'Mizuho'}]},
        {'datasource': 'header', 'fields': ['payment gateway'],
         'data': [{'payment gateway': 'Agro Bank'}, {'payment gateway': 'Fasspay'}]},
    ]
    u = merge_union(legs)  # dedup default
    gws = {r['payment gateway']: r['source'] for r in u['data']}
    assert set(gws) == {'Agro Bank', 'Mizuho', 'Fasspay'}  # 3 unique, not 4
    assert gws['Agro Bank'] == 'Payment_Gateways, header'  # appeared in both
    assert gws['Mizuho'] == 'Payment_Gateways'
    assert gws['Fasspay'] == 'header'


def test_merge_union_no_dedup_keeps_all():
    legs = [
        {'datasource': 'a', 'fields': ['x'], 'data': [{'x': '1'}]},
        {'datasource': 'b', 'fields': ['x'], 'data': [{'x': '1'}]},
    ]
    u = merge_union(legs, dedup=False)
    assert len(u['data']) == 2  # both kept


# ---------------------------------------------------------------- tokenize
def test_tokenize_ascii_words():
    assert tokenize('Total Sales by Region_2024') == ['total', 'sales', 'by', 'region_2024']


def test_tokenize_cjk_bigrams():
    toks = tokenize('销售额')
    assert '销售' in toks and '售额' in toks


def test_tokenize_mixed_and_empty():
    assert tokenize('') == []
    toks = tokenize('订单 order table')
    assert 'order' in toks and 'table' in toks and '订单' in toks


# ---------------------------------------------------------------- bm25
def test_bm25_relevant_doc_ranks_higher():
    docs = [
        tokenize('sales revenue by month, order amount, customer region'),
        tokenize('employee attendance leave vacation HR records'),
        tokenize('warehouse inventory stock levels'),
    ]
    q = tokenize('monthly sales revenue')
    scores = bm25_scores(q, docs)
    assert len(scores) == 3
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_bm25_empty_inputs():
    assert bm25_scores([], [tokenize('a b c')]) == [0.0]
    assert bm25_scores(tokenize('x'), []) == []
    # doc with no overlap scores 0
    assert bm25_scores(tokenize('zzz'), [tokenize('a b c')]) == [0.0]


# ---------------------------------------------------------------- rrf
def test_rrf_fuse_prefers_consistently_ranked():
    # id 2 is ranked 1st and 2nd; id 1 is 2nd and 1st; id 3 trails in both
    fused = rrf_fuse([[2, 1, 3], [1, 2, 3]])
    assert fused[1] == fused[2]  # symmetric
    assert fused[1] > fused[3] and fused[2] > fused[3]


def test_rrf_fuse_missing_member_gets_partial_score():
    fused = rrf_fuse([[1, 2], [2]])
    assert fused[2] > fused[1]  # 2 appears in both lists, ranked higher overall


# ---------------------------------------------------------------- grader verdict
def test_parse_grader_verdict_pass():
    ok, reason = parse_grader_verdict('{"pass": true, "reason": "answers the question"}')
    assert ok is True and 'answers' in reason


def test_parse_grader_verdict_fail_with_wrapping_text():
    ok, reason = parse_grader_verdict('Sure! Here: {"pass": false, "reason": "wrong column used"} hope it helps')
    assert ok is False and 'wrong column' in reason


def test_parse_grader_verdict_garbage_accepts():
    ok, _ = parse_grader_verdict('I am not JSON at all')
    assert ok is True  # graceful accept — grader must never break the pipeline


# ---------------------------------------------------------------- decomposition
def test_parse_decomposition_split_backcompat():
    text = '{"multi": true, "subs": [{"ds_id": 5, "question": "q1"}, {"ds_id": 7, "question": "q2"}]}'
    r = parse_decomposition(text, valid_ds_ids={5, 7})
    assert r['mode'] == 'split'
    assert r['subs'] == [{'ds_id': 5, 'question': 'q1'}, {'ds_id': 7, 'question': 'q2'}]


def test_parse_decomposition_fanout():
    text = ('{"mode": "fanout", "subs": ['
            '{"ds_id": 5, "question": "list thriller movies"},'
            '{"ds_id": 7, "question": "list thriller movies"},'
            '{"ds_id": 8, "question": "list thriller movies"}]}')
    r = parse_decomposition(text, valid_ds_ids={5, 7, 8})
    assert r['mode'] == 'fanout'
    assert len(r['subs']) == 3
    assert {s['ds_id'] for s in r['subs']} == {5, 7, 8}


def test_parse_decomposition_filters_invalid_and_caps():
    text = ('{"mode": "fanout", "subs": ['
            '{"ds_id": 5, "question": "a"}, {"ds_id": 99, "question": "bad"},'
            '{"ds_id": 7, "question": "b"}, {"ds_id": 5, "question": "a"},'
            '{"ds_id": 8, "question": "c"}, {"ds_id": 9, "question": "d"},'
            '{"ds_id": 10, "question": "e"}, {"ds_id": 11, "question": "f"}]}')
    r = parse_decomposition(text, valid_ds_ids={5, 7, 8, 9, 10, 11})
    assert len(r['subs']) == 5  # capped at MAX_SUB_QUESTIONS
    assert all(s['ds_id'] in {5, 7, 8, 9, 10, 11} for s in r['subs'])
    assert sum(1 for s in r['subs'] if s['ds_id'] == 5) == 1  # dup removed


def test_parse_decomposition_single_or_garbage():
    assert parse_decomposition('{"mode": "single", "subs": []}', {1}) == {'mode': 'single', 'subs': []}
    assert parse_decomposition('{"multi": false, "subs": []}', {1}) == {'mode': 'single', 'subs': []}
    assert parse_decomposition('nonsense', {1}) == {'mode': 'single', 'subs': []}
    assert parse_decomposition('{"mode": "fanout", "subs": "oops"}', {1}) == {'mode': 'single', 'subs': []}
    # fewer than 2 valid subs collapses to single
    assert parse_decomposition('{"mode": "fanout", "subs": [{"ds_id": 5, "question": "a"}]}', {5}) == {
        'mode': 'single', 'subs': []}


# ---------------------------------------------------------------- preview
def test_build_result_preview_basic():
    p = build_result_preview(['name', 'total'], [{'name': 'A', 'total': 10}, {'name': 'B', 'total': 20}])
    assert 'name' in p and 'total' in p and 'A' in p and '20' in p
    assert '2 row' in p


def test_build_result_preview_truncates():
    rows = [{'v': 'x' * 50} for _ in range(100)]
    p = build_result_preview(['v'], rows, max_rows=5, max_chars=300)
    assert len(p) <= 360  # max_chars + small suffix allowance
    assert '100 row' in p  # reports true count


def test_build_result_preview_empty():
    p = build_result_preview(['a'], [])
    assert '0 row' in p


# ---------------------------------------------------------------- retry feedback
def test_format_retry_feedback_contains_parts():
    fb = format_retry_feedback('execute', 'syntax error near FROM', 'SELECT * FRO t')
    assert '<error-msg>' in fb and '</error-msg>' in fb
    assert 'execute' in fb and 'syntax error near FROM' in fb and 'SELECT * FRO t' in fb


def test_format_retry_feedback_no_sql():
    fb = format_retry_feedback('parse', 'not valid json', None)
    assert 'parse' in fb and 'not valid json' in fb


# ---------------------------------------------------------------- retryable classification
def test_is_retryable_single_message_markers():
    assert is_retryable_single_message('{"message": "SQL answer is not a valid json object", "traceback": "..."}')
    assert is_retryable_single_message('{"message": "Cannot parse sql from answer", "traceback": "x"}')
    assert is_retryable_single_message('SQL query is empty')


def test_is_retryable_single_message_refusal_not_retryable():
    # LLM refusals / business messages must NOT be retried
    assert not is_retryable_single_message('I can only answer questions related to the data')
    assert not is_retryable_single_message('No available datasource configuration found')
    assert not is_retryable_single_message('')
