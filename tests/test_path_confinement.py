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


# ===========================================================================
# D-06 traversal matrix.
#
# Vectors that are NOT separators on POSIX (backslash, Windows drive letters,
# UNC prefixes, percent-encoding) are legal filename characters, so they must
# resolve to a literal name INSIDE the base rather than escaping. Nothing in
# the request path URL-decodes these values — they arrive in a JSON body — so
# a decoded form never reaches the filesystem. Each case is pinned so that
# stays true if a caller ever adds decoding.
# ===========================================================================

ESCAPING = [
    ("parent traversal",        "../secret_error.xlsx"),
    ("deep traversal",          "../../../../etc/hostname"),
    ("mid-path traversal",      "sub/../../evil.xlsx"),
    ("dotdot alone",            ".."),
    ("absolute path",           "/etc/hostname"),
]

CONFINED = [
    ("backslash traversal",     "..\\..\\evil.xlsx"),
    ("url-encoded traversal",   "%2e%2e%2fevil.xlsx"),
    ("double url-encoded",      "%252e%252e%252fevil.xlsx"),
    ("mixed separators",        "sub/..\\../evil.xlsx"),
    ("windows drive prefix",    "C:\\windows\\evil.xlsx"),
    ("unc path",                "\\\\server\\share\\evil.xlsx"),
    ("unicode fullwidth dots",  "\uff0e\uff0e/evil.xlsx"),
]


@pytest.mark.parametrize("name,vector", ESCAPING, ids=[n for n, _ in ESCAPING])
def test_matrix_escaping_vectors_are_rejected(base, name, vector):
    with pytest.raises(PathEscapeError):
        resolve_within(base, vector)


@pytest.mark.parametrize("name,vector", CONFINED, ids=[n for n, _ in CONFINED])
def test_matrix_non_posix_separators_stay_inside(base, name, vector):
    """Not an escape on POSIX — must resolve to a literal name under the base."""
    got = resolve_within(base, vector)
    assert got.startswith(os.path.realpath(base) + os.sep), f"{name} escaped: {got}"


def test_unicode_nfd_and_nfc_both_confined(base):
    import unicodedata
    for form in ("NFC", "NFD"):
        v = unicodedata.normalize(form, "café_error.xlsx")
        assert resolve_within(base, v).startswith(os.path.realpath(base) + os.sep)


def test_symlink_pointing_outside_the_base_is_rejected(base, tmp_path):
    """realpath resolves the link before the boundary check — this is the whole
    reason resolve_within uses realpath rather than abspath/normpath."""
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret_error.xlsx").write_text("x")
    os.symlink(str(outside), os.path.join(base, "link"))
    with pytest.raises(PathEscapeError):
        resolve_within(base, "link/secret_error.xlsx")


def test_symlink_to_system_directory_is_rejected(base):
    os.symlink("/etc", os.path.join(base, "etclink"))
    with pytest.raises(PathEscapeError):
        resolve_within(base, "etclink/hostname")


def test_null_byte_raises_patherror_not_valueerror(base):
    """D-06: an embedded NUL made os.path.realpath raise a bare ValueError,
    which callers do not catch — surfacing as a 500 instead of a 400."""
    with pytest.raises(PathEscapeError):
        resolve_within(base, "a\x00b_error.xlsx")


def test_null_byte_via_safe_join_is_also_patherror(base):
    with pytest.raises(PathEscapeError):
        safe_join(base, "a\x00b_error.xlsx", ("_error.xlsx",))


# ===========================================================================
# D-06 entry-point audit: every path that joins caller input onto EXCEL_PATH
# must go through safe_join. These tests read the source so a NEW endpoint
# that reintroduces a raw os.path.join is caught by the suite, not by review.
# ===========================================================================

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(rel_path):
    """Read a backend source file from disk.

    Deliberately does NOT import the module: apps.datasource.api.datasource
    cannot be imported standalone (a pre-existing cycle through
    sqlbot_xpack/__init__.py -> get_assistant_info, AUDIT D-39). A static read
    is also the right tool for a "no raw os.path.join" audit.
    """
    import pathlib as _p
    candidates = [
        _p.Path(REPO_ROOT) / "backend" / rel_path,      # running from the repo
        _p.Path("/opt/sqlbot/app") / rel_path,          # running in-container
    ]
    for c in candidates:
        if c.exists():
            return c.read_text(encoding="utf-8")
    pytest.skip(f"source not found for {rel_path}")


def test_datasource_endpoints_do_not_join_caller_paths_directly():
    """/reparseExcel and /importToDb previously did
    os.path.join(path, req.filePath) with an unvalidated caller string."""
    src = _source("apps/datasource/api/datasource.py")
    for banned in ("os.path.join(path, req.filePath)",
                   "os.path.join(path, import_req.filePath)"):
        assert banned not in src, f"raw join reintroduced: {banned}"
    assert src.count("safe_join(path,") >= 2, "both endpoints must use safe_join"


def test_settings_download_endpoint_uses_safe_join():
    src = _source("apps/settings/api/base.py")
    assert "safe_join(" in src
    assert "os.path.join(path, filename)" not in src


def test_upload_extensions_are_a_single_constant():
    """The extension allow-list must not drift between the guard and safe_join."""
    src = _source("apps/datasource/api/datasource.py")
    assert 'UPLOAD_EXTENSIONS = (".xlsx", ".xls", ".csv")' in src
    assert src.count("UPLOAD_EXTENSIONS") >= 3, "constant must be used, not just defined"


def test_import_and_reparse_reject_traversal_extensions(base):
    """A traversal payload that also fails the extension check is rejected on
    the extension first — before the filesystem is touched."""
    UPLOAD_EXTENSIONS = (".xlsx", ".xls", ".csv")
    with pytest.raises(PathEscapeError):
        safe_join(base, "../../../../etc/hostname", UPLOAD_EXTENSIONS)
    with pytest.raises(PathEscapeError):
        safe_join(base, "/etc/passwd.xlsx", UPLOAD_EXTENSIONS)
    # a legitimate name still passes
    assert safe_join(base, "report.xlsx", UPLOAD_EXTENSIONS)


# ===========================================================================
# D-38 — uploaded-filename construction.
#
# Five endpoints built their save name as
#     f"{file.filename.split('.')[0]}_{hash}.{file.filename.split('.')[1]}"
# from the attacker-controlled Content-Disposition filename. An ABSOLUTE name
# produced an absolute save path and os.path.join honoured it, writing outside
# the upload directory. safe_upload_name is now the single implementation.
# ===========================================================================

from common.utils.paths import safe_upload_name  # noqa: E402

XLSX = (".xlsx", ".xls", ".csv")
SUF = "abcdef0123"


def _built(original):
    return safe_upload_name(original, SUF, XLSX)


def test_normal_filename_is_unchanged_in_shape(base):
    """Backward compatibility: the stored name format must not drift."""
    assert _built("report.xlsx") == f"report_{SUF}.xlsx"
    assert _built("Sales Q4.csv") == f"Sales Q4_{SUF}.csv"


@pytest.mark.parametrize("original", [
    "/etc/cron.d/evil.xlsx",          # absolute — the measured escape
    "/tmp/evil.xlsx",
    "../../evil.xlsx",                # relative traversal
    "../evil.xlsx",
    "a/../../../tmp/evil.xlsx",       # mid-path traversal
    "subdir/evil.xlsx",               # subdirectory
    "C:\\windows\\system32\\evil.xlsx",  # windows drive
    "\\\\server\\share\\evil.xlsx",      # UNC
    "..\\..\\evil.xlsx",              # windows relative
])
def test_directory_components_are_stripped(original, base):
    """Whatever the payload, the result is a bare filename that cannot escape."""
    built = _built(original)
    assert "/" not in built and "\\" not in built, f"separator survived: {built}"
    assert built.startswith("evil_") or built.startswith("upload_"), built
    resolved = safe_join(base, built, XLSX)
    assert resolved.startswith(os.path.realpath(base) + os.sep)


@pytest.mark.parametrize("original", [
    "%2e%2e%2fevil.xlsx",             # encoded traversal
    "%252e%252e%252fevil.xlsx",       # double-encoded
])
def test_encoded_traversal_is_literal_and_confined(original, base):
    """Nothing decodes these, so they stay a literal (odd) filename inside base."""
    assert safe_join(base, _built(original), XLSX).startswith(
        os.path.realpath(base) + os.sep)


def test_symlinked_upload_dir_still_confines(base, tmp_path):
    """If EXCEL_PATH itself is a symlink, confinement must use the real path."""
    real = tmp_path / "real_uploads"; real.mkdir()
    link = tmp_path / "linked_uploads"
    os.symlink(str(real), str(link))
    got = safe_join(str(link), _built("report.xlsx"), XLSX)
    assert got.startswith(os.path.realpath(str(real)) + os.sep)


@pytest.mark.parametrize("bad", ["", "   ", None, ".", "..", "/", "\\"])
def test_empty_and_degenerate_names_are_rejected(bad):
    with pytest.raises(PathEscapeError):
        _built(bad)


@pytest.mark.parametrize("bad", [
    "evil.exe", "evil.sh", "evil", "evil.xlsx.exe", "noext.",
])
def test_extension_allowlist_is_enforced(bad):
    with pytest.raises(PathEscapeError):
        _built(bad)


def test_leading_dot_filenames():
    """`.hidden.xlsx` must not stay hidden, and `.xlsx` alone has no stem."""
    assert _built(".hidden.xlsx") == f"hidden_{SUF}.xlsx"
    with pytest.raises(PathEscapeError):
        _built(".xlsx")          # splitext -> ('.xlsx', '') : no extension


def test_multiple_extensions_are_preserved():
    """The old .split('.') turned this into `a_<hash>.tar` — a silent corruption."""
    assert _built("a.tar.gz.xlsx") == f"a.tar.gz_{SUF}.xlsx"
    assert _built("report.2026.01.csv") == f"report.2026.01_{SUF}.csv"


@pytest.mark.parametrize("reserved", ["CON.xlsx", "PRN.xlsx", "NUL.xlsx",
                                      "COM1.xlsx", "LPT1.xlsx", "aux.xlsx"])
def test_windows_reserved_names_stay_confined(reserved, base):
    """Not special on POSIX; assert they produce a confined ordinary file and
    do not crash, so a Windows/SMB-mounted volume is the only open question."""
    built = _built(reserved)
    assert safe_join(base, built, XLSX).startswith(os.path.realpath(base) + os.sep)


def test_extremely_long_filename_is_bounded():
    """NAME_MAX is 255 BYTES; the old code could exceed it and raise OSError."""
    built = _built("a" * 5000 + ".xlsx")
    assert len(built.encode("utf-8")) <= 255, len(built.encode("utf-8"))
    assert built.endswith(f"_{SUF}.xlsx")


def test_long_multibyte_filename_is_bounded_in_bytes():
    """A 3-byte-per-char stem hits NAME_MAX at ~85 chars, not 255."""
    built = _built("\u6570\u636e" * 500 + ".xlsx")
    assert len(built.encode("utf-8")) <= 255
    built.encode("utf-8").decode("utf-8")     # must remain valid UTF-8


def test_unicode_nfc_nfd_both_accepted(base):
    import unicodedata
    for form in ("NFC", "NFD"):
        built = _built(unicodedata.normalize(form, "café.xlsx"))
        assert safe_join(base, built, XLSX).startswith(os.path.realpath(base) + os.sep)


def test_null_byte_in_upload_name_is_rejected():
    with pytest.raises(PathEscapeError):
        safe_join("/tmp", _built("a\x00b.xlsx"), XLSX)


# --- every upload endpoint must use the shared implementation --------------

@pytest.mark.parametrize("module", [
    "apps/datasource/api/datasource.py",
    "apps/terminology/api/terminology.py",
    "apps/data_training/api/data_training.py",
])
def test_no_endpoint_builds_filenames_by_hand(module):
    """Audits LIVE code only — commented-out blocks are dead and tracked by D-30."""
    live = "\n".join(l for l in _source(module).splitlines()
                     if not l.lstrip().startswith("#"))
    assert "file.filename.split('.')" not in live, (
        f"{module} still builds a filename from the raw upload name (D-38)")
    assert "safe_upload_name(" in live, f"{module} must use the shared helper"


@pytest.mark.parametrize("module", [
    "apps/datasource/api/datasource.py",
    "apps/terminology/api/terminology.py",
    "apps/data_training/api/data_training.py",
])
def test_no_endpoint_writes_to_an_unconfined_path(module):
    """Every `open(save_path, "wb")` must be preceded by a safe_join."""
    src = _source(module)
    if 'open(save_path, "wb")' in src:
        assert "safe_join(path," in src, f"{module} writes without confinement"
