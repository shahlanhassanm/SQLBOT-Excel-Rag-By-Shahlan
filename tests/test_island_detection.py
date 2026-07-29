"""Unit tests for the pure island-detection algorithm.

Run in-container:
    docker cp tests/test_island_detection.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_island_detection.py -q"
"""
import pandas as pd

from apps.datasource.utils.island_detection import detect_islands, region_columns, Bounds


def _grid(rows):
    """Build a header=None frame from list-of-lists; ragged rows padded with ''."""
    width = max((len(r) for r in rows), default=0)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=object)


def test_empty_grid():
    assert detect_islands(_grid([])) == []


def test_all_empty_rows():
    assert detect_islands(_grid([[], [""], ["", ""]])) == []


def test_single_cell():
    assert detect_islands(_grid([["A"]])) == [Bounds(0, 0, 0, 0)]


def test_one_simple_table():
    g = _grid([["Name", "Age"], ["Alice", "30"], ["Bob", "25"]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 1)]


def test_header_only_table():
    assert detect_islands(_grid([["ID", "Name", "Value"]])) == [Bounds(0, 0, 0, 2)]


def test_two_tables_one_empty_row():
    g = _grid([["A", "B"], ["1", "2"], [], ["X", "Y"], ["3", "4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 1), Bounds(3, 4, 0, 1)]


def test_two_tables_multiple_empty_rows():
    g = _grid([["H1", "H2"], ["v1", "v2"], [], [], [], ["H3", "H4"], ["v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 1), Bounds(5, 6, 0, 1)]


def test_table_offset_from_origin():
    g = _grid([
        [], [], [],
        ["", "", "", "", ""],
        ["", "", "H1", "H2", "H3"],
        ["", "", "v1", "v2", "v3"],
        ["", "", "v4", "v5", "v6"],
        [], [],
    ])
    assert detect_islands(g) == [Bounds(4, 6, 2, 4)]


def test_leading_empty_columns():
    g = _grid([["", "", "H1", "H2"], ["", "", "v1", "v2"], ["", "", "v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 2, 3)]


def test_leading_and_trailing_empty_columns():
    g = _grid([["", "H1", "H2", ""], ["", "v1", "v2", ""], ["", "v3", "v4", ""]])
    assert detect_islands(g) == [Bounds(0, 2, 1, 2)]


def test_phantom_trailing_headers_excluded():
    g = _grid([["H1", "H2", "H3_phantom", "H4_phantom"], ["v1", "v2"], ["v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 1)]


def test_phantom_leading_column_excluded():
    g = _grid([["H_phantom", "H1", "H2"], ["", "v1", "v2"], ["", "v3", "v4"]])
    assert detect_islands(g) == [Bounds(0, 2, 1, 2)]


def test_interior_sparse_column_kept():
    g = _grid([["H1", "H2", "H3"], ["v1", "", "v3"], ["v4", "v5", ""]])
    assert detect_islands(g) == [Bounds(0, 2, 0, 2)]


def test_variable_length_rows():
    g = _grid([["H1", "H2", "H3"], ["v1"], ["v4", "v5"], ["v7", "v8", "v9"]])
    assert detect_islands(g) == [Bounds(0, 3, 0, 2)]


def test_side_by_side_default_off_is_one_island():
    g = _grid([["A", "B", "", "D", "E"], ["1", "2", "", "3", "4"]])
    assert detect_islands(g) == [Bounds(0, 1, 0, 4)]


def test_side_by_side_split_when_enabled():
    g = _grid([["A", "B", "", "D", "E"], ["1", "2", "", "3", "4"]])
    assert detect_islands(g, split_side_by_side=True) == [
        Bounds(0, 1, 0, 1), Bounds(0, 1, 3, 4)]


def test_multiple_tables_default_off():
    g = _grid([
        ["A", "B", "", "X", "Y"],
        ["1", "2", "", "5", "6"],
        [],
        ["", "", "", "P", "Q"],
        ["", "", "", "7", "8"],
    ])
    assert detect_islands(g) == [Bounds(0, 1, 0, 4), Bounds(3, 4, 3, 4)]


def test_multiple_tables_split_on():
    g = _grid([
        ["A", "B", "", "X", "Y"],
        ["1", "2", "", "5", "6"],
        [],
        ["", "", "", "P", "Q"],
        ["", "", "", "7", "8"],
    ])
    assert detect_islands(g, split_side_by_side=True) == [
        Bounds(0, 1, 0, 1), Bounds(0, 1, 3, 4), Bounds(3, 4, 3, 4)]


def test_min_rows_filters_small_islands():
    g = _grid([["A", "B"], ["1", "2"], [], ["solo"]])
    # default min_rows=1 keeps the 1-row island; min_rows=2 drops it
    assert detect_islands(g, min_rows=2) == [Bounds(0, 1, 0, 1)]


def test_region_columns_fills_blanks_and_dedupes():
    assert region_columns(["Name", "", "Name", None, "Age"]) == [
        "Name", "col_2", "Name_2", "col_4", "Age"]


def test_region_columns_blank_collides_with_explicit_name():
    # blank at index 1 -> positional "col_2", which collides with the explicit
    # "col_2" already taken, so it is deduped to "col_2_2".
    assert region_columns(["col_2", ""]) == ["col_2", "col_2_2"]


def test_min_rows_drops_single_row_band():
    g = _grid([["Title"], [], ["H1", "H2"], ["v1", "v2"]])
    # the title row is its own 1-row band; min_rows=2 drops it, keeping the table
    assert detect_islands(g, min_rows=2) == [Bounds(2, 3, 0, 1)]


def test_grid_of_tables_recursive_bisection():
    # Four tables in a 2x2 grid with NO globally-empty row: the left tables' blank
    # gap (rows 3-4 cols 0-1) is filled by the right table, and vice-versa.
    # Requires recursive row/col bisection to separate all four.
    g = _grid([
        ["ID", "Value", "", "", "Project", "Owner", "Status", "Budget"],
        ["1", "X", "", "", "ProjectX", "TeamA", "Active", "50000"],
        ["2", "Y", "", "", "ProjectY", "TeamB", "", "75000"],
        ["", "", "", "", "ProjectZ", "TeamC", "Completed", ""],
        ["", "", "", "", "ProjectW", "", "Planning", "120000"],
        ["Single_Column_Island", "", "", "", "", "", "", ""],
        ["Alpha", "", "", "", "", "", "", ""],
        ["Beta", "", "", "", "Event", "Start_Date", "End_Date", ""],
        ["Gamma", "", "", "", "Conference", "2026-07-01", "2026-07-03", ""],
        ["", "", "", "", "Workshop", "2026-08-15", "2026-08-16", ""],
    ])
    got = detect_islands(g, split_side_by_side=True)
    expected = {
        Bounds(0, 2, 0, 1),   # ID / Value
        Bounds(5, 8, 0, 0),   # Single_Column_Island
        Bounds(0, 4, 4, 7),   # Project / Owner / Status / Budget
        Bounds(7, 9, 4, 6),   # Event / Start_Date / End_Date
    }
    assert set(got) == expected, got
