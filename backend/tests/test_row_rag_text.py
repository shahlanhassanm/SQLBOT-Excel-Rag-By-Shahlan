from apps.datasource.row_rag.text import build_row_content_text


def test_concatenates_all_columns_label_value():
    row = {"Name": "Widget", "Note": "a small red device with two buttons"}
    out = build_row_content_text(row)
    assert out == "Name: Widget | Note: a small red device with two buttons"


def test_skips_none_and_empty_cells():
    row = {"Name": "X", "Note": None, "Blank": "", "Year": 2013}
    out = build_row_content_text(row)
    assert out == "Name: X | Year: 2013"


def test_includes_numeric_columns():
    # whole-row embedding requirement: numbers are kept, not dropped
    row = {"Id": 7, "Amount": 12.5}
    assert build_row_content_text(row) == "Id: 7 | Amount: 12.5"


def test_truncates_to_max_chars():
    row = {"Plot": "x" * 100}
    out = build_row_content_text(row, max_chars=20)
    assert len(out) <= 20


def test_truncation_drops_whole_entries_not_mid_token():
    # multi-column row: trimming must keep complete "col: value" entries so the
    # output stays parseable (no dangling separator or partial value)
    row = {"A": "first", "B": "second", "C": "third value that overflows"}
    out = build_row_content_text(row, max_chars=20)
    assert len(out) <= 20
    # every surviving segment is a complete "col: value" pair
    for seg in out.split(" | "):
        assert ": " in seg
    assert not out.endswith(" |")
    assert not out.endswith("|")


def test_empty_row_returns_empty_string():
    assert build_row_content_text({}) == ""
