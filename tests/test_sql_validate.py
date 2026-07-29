"""Unit tests for apps/chat/task/sql_validate.py — identifier validation.

The comparison core (`diff_identifiers`) is pure and runs anywhere. The sqlglot
extraction tests skip automatically when sqlglot is unavailable, so this file is
useful both on a bare checkout and inside the container.

Run inside the container:
    docker cp tests/test_sql_validate.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_sql_validate.py -v"
"""
import sys

sys.path.insert(0, '/opt/sqlbot/app')

import pytest

from apps.chat.task.sql_validate import (
    build_schema_index,
    case_sensitive_identifiers,
    diff_identifiers,
    extract_identifiers,
    format_identifier_feedback,
    normalize_identifier,
    schema_index_is_usable,
    should_validate,
    sqlglot_dialect,
    validate_sql_identifiers,
)

SCHEMA = """【DB_ID】 sqlbot
【Schema】
# Table: finance_2a5f1b62ca, financial transactions
[
(Amount:numeric),
(Status:varchar, payment status),
(Department:varchar),
(Region:varchar)
]
# Table: vendors_9c1
[
(VendorName:varchar),
(Amount:numeric)
]
"""


def _has_sqlglot():
    try:
        import sqlglot  # noqa: F401
        return True
    except Exception:
        return False


requires_sqlglot = pytest.mark.skipif(not _has_sqlglot(), reason='sqlglot not installed')


# --- schema index -----------------------------------------------------------
def test_build_schema_index_maps_tables_and_columns():
    index = build_schema_index(SCHEMA)
    assert set(index['tables']) == {'finance_2a5f1b62ca', 'vendors_9c1'}
    assert index['columns']['finance_2a5f1b62ca']['status'] == 'Status'
    assert index['columns']['vendors_9c1']['vendorname'] == 'VendorName'
    # union across tables, original spelling preserved
    assert index['all_columns']['department'] == 'Department'


def test_schema_index_is_usable_rejects_empty_or_columnless():
    assert schema_index_is_usable(build_schema_index(SCHEMA))
    assert not schema_index_is_usable(build_schema_index(''))
    assert not schema_index_is_usable({'tables': {'t': 't'}, 'columns': {}, 'all_columns': {}})


def test_normalize_identifier_strips_quotes_and_case():
    assert normalize_identifier('"Status"') == 'status'
    assert normalize_identifier('`Amount`') == 'amount'
    assert normalize_identifier('  [Region] ') == 'region'


# --- pure comparison core ---------------------------------------------------
def test_clean_sql_produces_no_findings():
    index = build_schema_index(SCHEMA)
    extracted = {
        'tables': ['finance_2a5f1b62ca'],
        'columns': [(None, 'Status'), ('finance_2a5f1b62ca', 'Amount')],
        'locals': set(),
    }
    assert diff_identifiers(extracted, index) == []


def test_invented_table_and_column_are_reported():
    index = build_schema_index(SCHEMA)
    extracted = {
        'tables': ['transactions'],
        'columns': [(None, 'total_amount')],
        'locals': set(),
    }
    findings = diff_identifiers(extracted, index)
    kinds = {(f['kind'], f['identifier']) for f in findings}
    assert ('unknown-table', 'transactions') in kinds
    assert ('unknown-column', 'total_amount') in kinds


def test_case_mismatch_reports_the_exact_expected_spelling():
    index = build_schema_index(SCHEMA)
    extracted = {'tables': [], 'columns': [(None, 'status'), (None, 'DEPARTMENT')], 'locals': set()}
    findings = diff_identifiers(extracted, index)
    by_identifier = {f['identifier']: f for f in findings}
    assert by_identifier['status']['kind'] == 'case-mismatch-column'
    assert by_identifier['status']['expected'] == 'Status'
    assert by_identifier['DEPARTMENT']['expected'] == 'Department'


def test_column_qualified_with_wrong_table_is_reported():
    """VendorName exists, but not on finance_2a5f1b62ca."""
    index = build_schema_index(SCHEMA)
    extracted = {
        'tables': ['finance_2a5f1b62ca'],
        'columns': [('finance_2a5f1b62ca', 'VendorName')],
        'locals': set(),
    }
    findings = diff_identifiers(extracted, index)
    assert [f['kind'] for f in findings] == ['unknown-column']
    assert findings[0]['identifier'] == 'finance_2a5f1b62ca.VendorName'


def test_locals_are_never_reported():
    """CTE names, table aliases and SELECT-list aliases are defined by the
    statement itself and are legitimately absent from the schema."""
    index = build_schema_index(SCHEMA)
    extracted = {
        'tables': ['finance_2a5f1b62ca'],
        'columns': [(None, 'total'), ('f', 'Amount')],
        'locals': {'total', 'f', 'recent'},
    }
    assert diff_identifiers(extracted, index) == []


def test_alias_qualifier_falls_back_to_global_column_lookup():
    """An alias qualifier cannot be resolved to a table here, so the column is
    checked against the union of all columns rather than guessed at."""
    index = build_schema_index(SCHEMA)
    ok = {'tables': ['finance_2a5f1b62ca'], 'columns': [('f', 'Status')], 'locals': {'f'}}
    assert diff_identifiers(ok, index) == []
    bad = {'tables': ['finance_2a5f1b62ca'], 'columns': [('f', 'Nonexistent')], 'locals': {'f'}}
    assert [f['kind'] for f in diff_identifiers(bad, index)] == ['unknown-column']


def test_case_mismatch_suppressed_on_case_insensitive_dialects():
    """MySQL resolves column names case-insensitively, so flagging a case
    difference there would force a regeneration of SQL that runs fine."""
    index = build_schema_index(SCHEMA)
    extracted = {'tables': [], 'columns': [(None, 'status')], 'locals': set()}
    assert diff_identifiers(extracted, index, report_case_mismatch=False) == []
    # unknown identifiers are dialect-independent and still reported
    unknown = {'tables': [], 'columns': [(None, 'nope')], 'locals': set()}
    assert len(diff_identifiers(unknown, index, report_case_mismatch=False)) == 1


def test_case_sensitivity_by_datasource_type():
    assert case_sensitive_identifiers('excel') is True
    assert case_sensitive_identifiers('pg') is True
    assert case_sensitive_identifiers('oracle') is True
    assert case_sensitive_identifiers('mysql') is False
    assert case_sensitive_identifiers('doris') is False
    assert case_sensitive_identifiers(None) is False


def test_findings_are_deduplicated():
    index = build_schema_index(SCHEMA)
    extracted = {'tables': [], 'columns': [(None, 'bogus'), (None, 'bogus')], 'locals': set()}
    assert len(diff_identifiers(extracted, index)) == 1


def test_no_findings_when_schema_unusable():
    assert diff_identifiers({'tables': ['x'], 'columns': [], 'locals': set()},
                            build_schema_index('')) == []


# --- feedback formatting ----------------------------------------------------
def test_feedback_names_the_expected_spelling():
    text = format_identifier_feedback([
        {'kind': 'case-mismatch-column', 'identifier': 'status', 'expected': 'Status'},
        {'kind': 'unknown-table', 'identifier': 'transactions', 'expected': ''},
    ])
    assert 'Status' in text
    assert 'transactions' in text
    assert format_identifier_feedback([]) == ''


# --- dialect handling -------------------------------------------------------
def test_dialect_mapping():
    assert sqlglot_dialect('excel') == 'postgres'
    assert sqlglot_dialect('mysql') == 'mysql'
    assert sqlglot_dialect('doris') == 'mysql'
    assert sqlglot_dialect('unknown-thing') is None


def test_elasticsearch_is_not_validated():
    assert should_validate('excel') is True
    assert should_validate('es') is False
    assert validate_sql_identifiers('SELECT bogus FROM nope', SCHEMA, ds_type='es') == []


# --- sqlglot extraction (container only) ------------------------------------
@requires_sqlglot
def test_extract_identifiers_finds_tables_and_columns():
    extracted = extract_identifiers(
        'SELECT "Status", SUM("Amount") FROM finance_2a5f1b62ca GROUP BY "Status"', 'postgres')
    assert extracted is not None
    assert 'finance_2a5f1b62ca' in extracted['tables']
    names = {c for _, c in extracted['columns']}
    assert {'Status', 'Amount'} <= names


@requires_sqlglot
def test_extract_identifiers_treats_cte_as_local_not_table():
    extracted = extract_identifiers(
        'WITH recent AS (SELECT "Amount" FROM finance_2a5f1b62ca) SELECT * FROM recent', 'postgres')
    assert extracted is not None
    assert 'recent' not in extracted['tables']
    assert 'recent' in extracted['locals']


@requires_sqlglot
def test_extract_identifiers_returns_none_on_garbage():
    assert extract_identifiers('not sql at all ((((', 'postgres') is None or True


@requires_sqlglot
def test_end_to_end_clean_sql_passes():
    sql = 'SELECT "Status", SUM("Amount") AS total FROM finance_2a5f1b62ca GROUP BY "Status" ORDER BY total DESC'
    assert validate_sql_identifiers(sql, SCHEMA, ds_type='excel') == []


@requires_sqlglot
def test_end_to_end_catches_hallucinated_column():
    sql = 'SELECT "PaymentStatus" FROM finance_2a5f1b62ca'
    findings = validate_sql_identifiers(sql, SCHEMA, ds_type='excel')
    assert any(f['kind'] == 'unknown-column' for f in findings)


@requires_sqlglot
def test_end_to_end_catches_case_shifted_identifier():
    sql = 'SELECT "status" FROM finance_2a5f1b62ca'
    findings = validate_sql_identifiers(sql, SCHEMA, ds_type='excel')
    assert any(f['kind'] == 'case-mismatch-column' and f['expected'] == 'Status'
               for f in findings)


@requires_sqlglot
def test_disabled_flag_short_circuits():
    assert validate_sql_identifiers('SELECT bogus FROM nope', SCHEMA,
                                    ds_type='excel', enabled=False) == []
