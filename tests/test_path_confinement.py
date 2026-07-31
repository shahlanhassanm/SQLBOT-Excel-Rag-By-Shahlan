"""Security regression tests for caller-supplied path handling (AUDIT D-06, D-07).

Four endpoints joined a request-supplied filename onto settings.EXCEL_PATH with
os.path.join, which silently honours an absolute path or a `../` prefix:

    /system/download-fail-info   (D-07, any authenticated user)
    /datasource/reparseExcel     (D-06, ws_admin)
    /datasource/importToDb       (D-06, ws_admin)

These tests pin the confinement helper. They are pure filesystem tests: no
server, no database, no model.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_path_confinement.py -q"
"""

import os

import pytest

from common.utils.paths import PathEscapeError, resolve_within, safe_join


@pytest.fixture()
def base(tmp_path):
    d = tmp_path / "excel"
    d.mkdir()
    (d / "report_error.xlsx").write_text("x")
    (d / "nested").mkdir()
    (d / "nested" / "deep_error.xlsx").write_text("x")
    # a sibling whose name starts with the base name — the classic string-prefix bug
    (tmp_path / "excel_evil").mkdir()
    (tmp_path / "excel_evil" / "loot_error.xlsx").write_text("x")
    (tmp_path / "outside_error.xlsx").write_text("x")
    return str(d)


# --- accepted --------------------------------------------------------------

def test_plain_filename_is_allowed(base):
    assert resolve_within(base, "report_error.xlsx") == os.path.join(
        os.path.realpath(base), "report_error.xlsx")


def test_nested_relative_path_is_allowed(base):
    got = resolve_within(base, "nested/deep_error.xlsx")
    assert got.startswith(os.path.realpath(base) + os.sep)


def test_nonexistent_but_confined_name_is_allowed(base):
    """Confinement is about location, not existence — the caller checks existence."""
    assert resolve_within(base, "not_created_yet.xlsx").startswith(
        os.path.realpath(base) + os.sep)


# --- rejected --------------------------------------------------------------

def test_parent_traversal_is_rejected(base):
    with pytest.raises(PathEscapeError):
        resolve_within(base, "../outside_error.xlsx")


def test_deep_traversal_is_rejected(base):
    with pytest.raises(PathEscapeError):
        resolve_within(base, "../../../../etc/hostname")


def test_absolute_path_is_rejected(base):
    with pytest.raises(PathEscapeError):
        resolve_within(base, "/etc/hostname")


def test_traversal_hidden_mid_path_is_rejected(base):
    with pytest.raises(PathEscapeError):
        resolve_within(base, "nested/../../outside_error.xlsx")


def test_sibling_directory_with_shared_prefix_is_rejected(base):
    """A plain string-prefix check would wrongly accept `../excel_evil/...`
    because '/tmp/.../excel_evil'.startswith('/tmp/.../excel') is True."""
    with pytest.raises(PathEscapeError):
        resolve_within(base, "../excel_evil/loot_error.xlsx")


def test_empty_and_whitespace_are_rejected(base):
    for bad in ("", "   ", None):
        with pytest.raises(PathEscapeError):
            resolve_within(base, bad)


# --- suffix allow-list -----------------------------------------------------

def test_suffix_allowlist_accepts_match(base):
    assert safe_join(base, "report_error.xlsx", ("_error.xlsx",))


def test_suffix_allowlist_rejects_mismatch(base):
    with pytest.raises(PathEscapeError):
        safe_join(base, "report.xlsx", ("_error.xlsx",))


def test_suffix_is_checked_before_touching_the_filesystem(base):
    """D-07 ordering bug: the original endpoint ran os.path.exists() BEFORE the
    suffix check, so a rejected request still revealed whether a path existed."""
    with pytest.raises(PathEscapeError):
        safe_join(base, "../../../../etc/hostname", ("_error.xlsx",))


def test_suffix_allowlist_is_case_insensitive(base):
    assert safe_join(base, "REPORT_ERROR.XLSX", ("_error.xlsx",))
