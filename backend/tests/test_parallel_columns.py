from apps.chat.task.apex_helpers import (
    _parallel_base_name,
    detect_parallel_column_groups,
    parallel_columns_addendum,
)


# A table that repeats a column for two parallel roles: two "grp" columns
# (deduped to grp / grp_1) alongside two entity columns (item_a / item_b).
PARALLEL_SCHEMA = """【DB_ID】 db
【Schema】
# Table: public.records
[
(region:text),
(grp_1:text),
(item_a:text),
(grp:text),
(item_b:text)
]
"""

SINGLE_SCHEMA = """【DB_ID】 db
【Schema】
# Table: public.items
[
(name:text),
(kind:text),
(value:int)
]
"""


def test_base_name_strips_dedup_suffixes():
    assert _parallel_base_name("grp") == "grp"
    assert _parallel_base_name("grp_1") == "grp"
    assert _parallel_base_name("grp.1") == "grp"
    assert _parallel_base_name("grp 1") == "grp"
    assert _parallel_base_name("grp2") == "grp"


def test_base_name_keeps_distinct_names_apart():
    assert _parallel_base_name("item_a") != _parallel_base_name("region")
    # a name that is only digits must not collapse to empty
    assert _parallel_base_name("2020") == "2020"


def test_detects_repeated_columns():
    groups = detect_parallel_column_groups(PARALLEL_SCHEMA)
    assert groups == [("records", ["grp_1", "grp"])]


def test_no_groups_for_single_column_table():
    assert detect_parallel_column_groups(SINGLE_SCHEMA) == []


def test_addendum_names_actual_columns_when_present():
    out = parallel_columns_addendum(PARALLEL_SCHEMA, enabled=True)
    assert "grp_1" in out and "grp" in out
    assert "records" in out
    assert "UNION" in out


def test_addendum_empty_when_no_repeats_or_disabled():
    assert parallel_columns_addendum(SINGLE_SCHEMA, enabled=True) == ""
    assert parallel_columns_addendum(PARALLEL_SCHEMA, enabled=False) == ""
