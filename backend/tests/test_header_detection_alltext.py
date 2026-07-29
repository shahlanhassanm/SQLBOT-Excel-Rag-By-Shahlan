"""Regression tests for header detection on ALL-TEXT tables, where the
type-divergence signal (text header over numeric/date data) is blind.

Motivating case: a roster whose real header (row 0) repeats a label and sits
above rows that are ENTIRELY text, e.g.

  region | name | grp | alt | grp        <- real header (row 0), repeats "grp"
  Northern District | Alexander ... | alpha | Benjamin ... | beta   <- data

Such a table used to be imported with the FIRST DATA ROW as the header, because
(1) the real header repeats a label (uniqueness penalty) and (2) the data is all
text so the detector couldn't tell header from data. The fix adds a brevity
signal (header cells shorter than the data beneath) plus a position prior
(headers sit near the top), and softens the duplicate-label penalty.
"""
import pandas as pd

from apps.datasource.utils.header_detection import detect_header_row


def _alltext_frame():
    header = ["region", "name", "grp", "alt", "grp"]
    data = [
        ["Northern District", "Alexander Hamilton Group", "alpha", "Benjamin Franklin Group", "beta"],
        ["Southern District", "Charlotte Bronte Society", "beta", "Daniel Defoe Society", "alpha"],
        ["Eastern District", "Eleanor Rigby Collective", "alpha", "Frederick Douglas Union", "beta"],
        ["Western District", "Georgiana Darcy League", "beta", "Henry Fielding League", "alpha"],
        ["Central District", "Isabella Linton Assembly", "beta", "Jonathan Swift Assembly", "alpha"],
        ["Coastal District", "Katherine Earnshaw Circle", "gamma", "Laurence Sterne Circle", "beta"],
        ["Highland District", "Margaret Hale Committee", "beta", "Nathaniel Bumppo Committee", "alpha"],
        ["Lowland District", "Oliver Twist Federation", "beta", "Patricia Highsmith Federation", "alpha"],
        ["Riverside District", "Quentin Compson Board", "beta", "Rebecca Sharp Board", "alpha"],
        ["Lakeside District", "Sydney Carton Council", "beta", "Theodore Laurence Council", "alpha"],
    ]
    return pd.DataFrame([header] + data)


def test_alltext_duplicate_label_header_is_row_zero():
    # the real header (row 0) must win even though it repeats "grp" and the
    # data below is entirely text.
    idx, conf = detect_header_row(_alltext_frame())
    assert idx == 0, f"expected header row 0, got {idx} (conf {conf:.3f})"


def test_deep_short_data_row_does_not_win():
    # a short-ish data row near the end (few rows below it) must not out-score the
    # genuine top header via the brevity bonus — the position prior guards this.
    idx, _ = detect_header_row(_alltext_frame())
    assert idx == 0


def test_numeric_table_header_still_row_zero():
    # regression guard: the classic case (text header over numeric data) is
    # unaffected by the new signals.
    df = pd.DataFrame([
        ["id", "name", "amount"],
        [1, "Alice", 100],
        [2, "Bob", 250],
        [3, "Cara", 80],
    ])
    idx, _ = detect_header_row(df)
    assert idx == 0


def test_title_row_above_header_is_skipped():
    # a sparse title row on top must not be chosen; the real header sits below it.
    df = pd.DataFrame([
        ["Report Summary 2020", None, None, None, None],
        ["region", "name", "grp", "alt", "grp"],
        ["Northern District", "Alexander Hamilton Group", "alpha", "Benjamin Franklin Group", "beta"],
        ["Southern District", "Charlotte Bronte Society", "beta", "Daniel Defoe Society", "alpha"],
    ])
    idx, _ = detect_header_row(df)
    assert idx == 1
