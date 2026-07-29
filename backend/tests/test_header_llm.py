import pandas as pd

from apps.datasource.utils.header_detection import (
    build_header_llm_prompt, parse_header_row_answer)


def test_parse_plain_integer():
    assert parse_header_row_answer("2", 5) == 2


def test_parse_integer_in_sentence():
    assert parse_header_row_answer("The header is row 1.", 5) == 1
    assert parse_header_row_answer("Header row index: 0", 5) == 0


def test_parse_rejects_out_of_range():
    assert parse_header_row_answer("99", 5) is None
    assert parse_header_row_answer("-1", 5) is None


def test_parse_prefers_last_in_range_integer():
    # reasoning-model style: echoes the grid then gives the final answer last
    assert parse_header_row_answer("Row 0 is a title, Row 1 looks like data... answer: 2", 5) == 2
    # out-of-range trailing number is skipped, last VALID one wins
    assert parse_header_row_answer("could be 99 but really 1", 5) == 1


def test_parse_rejects_non_numeric_or_empty():
    assert parse_header_row_answer("no idea", 5) is None
    assert parse_header_row_answer("", 5) is None
    assert parse_header_row_answer(None, 5) is None


def test_build_prompt_shows_grid_and_asks_for_index():
    df = pd.DataFrame([
        ["Report 2020", None, None],
        ["region", "name", "value"],
        ["North", "Alice", "10"],
    ])
    out = build_header_llm_prompt(df, max_rows=15)
    assert "Row 0:" in out and "Row 1:" in out and "Row 2:" in out
    assert "region" in out and "Alice" in out
    assert "Header row index:" in out
    # blank cells render empty, not the string 'None'
    assert "None" not in out


def test_build_prompt_truncates_long_cells():
    df = pd.DataFrame([["x" * 100, "y"]])
    out = build_header_llm_prompt(df, max_rows=15)
    assert "x" * 41 not in out  # cells capped at 40 chars
