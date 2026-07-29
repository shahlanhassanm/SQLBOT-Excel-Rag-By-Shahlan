"""Unit tests for apps/chat/task/value_linking.py — pure, no DB needed.

Run inside the container:
    docker cp tests/test_value_linking.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_value_linking.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

from apps.chat.task.value_linking import (
    extract_candidate_terms,
    format_value_hints,
    has_candidate_terms,
    is_text_column,
    match_values,
    score_term_value,
    select_probe_columns,
)


# --- candidate term extraction ---------------------------------------------
def test_extracts_proper_nouns_and_codes():
    terms = extract_candidate_terms(
        'Which department has the highest expense, R&D or Marketing?')
    lowered = {t.casefold() for t in terms}
    assert 'r&d' in lowered
    assert 'marketing' in lowered


def test_extracts_quoted_literals():
    terms = extract_candidate_terms('How many transactions are marked as "Paid"?')
    assert 'Paid' in terms


def test_extracts_multiword_proper_noun_and_its_parts():
    terms = extract_candidate_terms('What is the total for GreenGrid Energy?')
    assert 'GreenGrid Energy' in terms
    # the cell value may be just the first word, so parts are candidates too
    assert 'GreenGrid' in terms


def test_question_words_are_not_candidates():
    """Sentence-initial capitals and generic nouns must not become value hints."""
    terms = {t.casefold() for t in extract_candidate_terms('What is the total revenue?')}
    assert 'what' not in terms
    assert 'total' not in terms


def test_gate_is_false_for_pure_aggregate_questions():
    """The gate decides whether any DB probing happens at all."""
    assert has_candidate_terms('What is the total revenue recorded?') is False
    assert has_candidate_terms('what is the average payment delay in days?') is False
    assert has_candidate_terms('How many are marked as Overdue?') is True


def test_bare_numbers_are_not_candidates():
    assert extract_candidate_terms('show me the top 5') == []


# --- scoring ----------------------------------------------------------------
def test_exact_match_scores_one():
    assert score_term_value('Paid', 'Paid') == 1.0
    assert score_term_value('paid', 'PAID') == 1.0


def test_containment_scores_high_but_scaled_by_length():
    strong = score_term_value('GreenGrid Energy', 'GreenGrid')
    weak = score_term_value('a', 'Canada')
    assert strong > 0.85
    assert weak < strong


def test_unrelated_values_score_zero():
    assert score_term_value('Paid', 'Marketing') == 0.0


def test_near_miss_scores_between():
    score = score_term_value('Overdue', 'Over Due')
    assert 0.6 < score < 1.0


# --- matching ---------------------------------------------------------------
def test_match_values_links_term_to_column():
    values = {
        ('finance', 'Status'): ['Paid', 'Overdue', 'Pending'],
        ('finance', 'Department'): ['R&D', 'Sales'],
    }
    hints = match_values(['Overdue'], values, min_score=0.72)
    assert len(hints) == 1
    assert hints[0]['table'] == 'finance'
    assert hints[0]['column'] == 'Status'
    assert hints[0]['value'] == 'Overdue'


def test_match_values_respects_threshold_and_cap():
    values = {('t', 'c'): ['Paid', 'Overdue', 'Pending', 'Partial']}
    assert match_values(['Zebra'], values, min_score=0.72) == []
    hints = match_values(['Paid', 'Overdue', 'Pending'], values, min_score=0.72, max_hints=2)
    assert len(hints) == 2


def test_match_values_sorted_by_score_descending():
    values = {('t', 'Status'): ['Paid', 'Prepaid']}
    hints = match_values(['Paid'], values, min_score=0.5)
    assert hints[0]['value'] == 'Paid'
    assert hints[0]['score'] >= hints[1]['score']


def test_match_values_handles_empty_input():
    assert match_values([], {('t', 'c'): ['x']}) == []
    assert match_values(['x'], {}) == []


# --- prompt formatting ------------------------------------------------------
def test_format_groups_values_by_column():
    hints = [
        {'table': 'finance', 'column': 'Status', 'value': 'Paid', 'term': 'Paid', 'score': 1.0},
        {'table': 'finance', 'column': 'Status', 'value': 'Overdue', 'term': 'Overdue', 'score': 1.0},
        {'table': 'finance', 'column': 'Region', 'value': 'North', 'term': 'North', 'score': 1.0},
    ]
    text = format_value_hints(hints)
    assert '<value-hints>' in text
    assert 'finance.Status contains: "Paid", "Overdue"' in text
    assert 'finance.Region contains: "North"' in text


def test_format_returns_empty_for_no_hints():
    assert format_value_hints([]) == ''


# --- column selection -------------------------------------------------------
def test_is_text_column_covers_dialect_spellings():
    assert is_text_column('varchar')
    assert is_text_column('character varying')
    assert is_text_column('NVARCHAR2')
    assert is_text_column('String')
    assert not is_text_column('numeric')
    assert not is_text_column('int8')
    assert not is_text_column(None)


def test_select_probe_columns_only_picks_text_columns():
    tables = [{'table_name': 't1', 'fields': [
        {'name': 'amount', 'type': 'numeric'},
        {'name': 'status', 'type': 'varchar'},
    ]}]
    assert select_probe_columns(tables, max_tables=5, max_columns=10) == [('t1', 'status')]


def test_select_probe_columns_round_robins_across_tables():
    """A single wide table must not consume the whole probe budget."""
    tables = [
        {'table_name': 'wide', 'fields': [{'name': f'c{i}', 'type': 'text'} for i in range(10)]},
        {'table_name': 'narrow', 'fields': [{'name': 'label', 'type': 'text'}]},
    ]
    selected = select_probe_columns(tables, max_tables=5, max_columns=4)
    assert ('narrow', 'label') in selected
    assert len(selected) == 4


def test_select_probe_columns_respects_caps():
    tables = [{'table_name': f't{i}', 'fields': [{'name': 'c', 'type': 'text'}]} for i in range(10)]
    assert len(select_probe_columns(tables, max_tables=3, max_columns=10)) == 3
    assert select_probe_columns(tables, max_tables=0, max_columns=10) == []


# --- multilingual / case-insensitive extraction ------------------------------
# Added 2026-07-28. Before this, extraction required a quote, a capital letter,
# a symbol, or a CJK ideograph, so ordinary lowercase input and all Korean input
# produced no terms at all and value linking silently did nothing.

def test_lowercase_question_yields_terms():
    """The original defect: chat users do not capitalise."""
    terms = extract_candidate_terms('show me all orders from acme corp',
                                    include_content_words=True)
    assert 'acme corp' in terms
    assert 'acme' in terms


def test_lowercase_yields_nothing_without_the_fallback_flag():
    """Default stays conservative so the documented cost gate is unchanged."""
    assert extract_candidate_terms('show me all orders from acme corp') == []


def test_content_word_pairs_do_not_span_a_stopword():
    """'orders from acme' must not produce the bogus pair 'orders acme'."""
    terms = extract_candidate_terms('show me all orders from acme corp',
                                    include_content_words=True)
    assert 'orders acme' not in terms


def test_korean_question_yields_terms():
    """Hangul is not in the CJK ideograph range; it needed its own coverage."""
    terms = extract_candidate_terms('모든 서울 지점의 주문을 보여줘')
    assert '서울' in terms


def test_korean_stopwords_come_from_config_keywords():
    """The caller folds in AGENTIC_LISTING_KEYWORDS rather than a second list."""
    terms = extract_candidate_terms('모든 서울 지점의 주문을 보여줘',
                                    extra_stopwords={'모든', '보여'})
    assert '모든' not in terms
    assert '서울' in terms


def test_chinese_clause_is_kept_whole_not_ngram_swept():
    """Containment scoring already finds a shorter value inside a longer term."""
    terms = extract_candidate_terms('显示所有来自北京分公司的订单')
    assert terms == ['显示所有来自北京分公司的订单']
    assert score_term_value(terms[0], '北京分公司') >= 0.72


def test_closing_quote_is_not_glued_to_a_word():
    terms = extract_candidate_terms("list rows where status is 'Paid'",
                                    include_content_words=True)
    assert "Paid'" not in terms
    assert 'Paid' in terms


def test_internal_apostrophe_survives():
    terms = extract_candidate_terms("orders for o'brien ltd",
                                    include_content_words=True)
    assert "o'brien" in terms


def test_fallback_is_capped():
    long_question = 'find ' + ' '.join(f'word{i}' for i in range(40))
    terms = extract_candidate_terms(long_question, include_content_words=True)
    assert len(terms) <= 16  # _MAX_FALLBACK_TERMS pairs + singles
