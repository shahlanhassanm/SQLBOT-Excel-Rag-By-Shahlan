"""Benchmark-harness integrity regressions (AUDIT E-03, E-12).

The harness decides what every accuracy claim in this project means, so its
defects are more expensive than most product defects: they change the number
without changing the product.

Run in-container (needs the app on sys.path for the E-03 half):
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_eval_harness_integrity.py -q"
"""

import importlib.util
import pathlib
import socket

import pytest

CANDIDATES = [
    pathlib.Path("/tmp/bird_eval.py"),
    pathlib.Path(__file__).resolve().parent.parent / "backend" / "tests" / "bird_eval.py",
    pathlib.Path("/opt/sqlbot/app/tests/bird_eval.py"),
]


def _harness():
    for c in CANDIDATES:
        if c.exists():
            spec = importlib.util.spec_from_file_location("bird_eval_under_test", c)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception as e:                     # pragma: no cover
                pytest.skip(f"bird_eval.py not importable here: {e}")
            return mod
    pytest.skip("bird_eval.py not found")


# =====================================================================
# E-12 — a timeout is a property of the host, not of the answer
# =====================================================================

class _PGTimeout(Exception):
    pass


@pytest.mark.parametrize("exc", [
    TimeoutError("took too long"),
    socket.timeout("timed out"),
    _PGTimeout("canceling statement due to statement timeout"),
])
def test_timeouts_are_recognised(exc):
    """Scored as wrong for official-EX compatibility, but tagged so a slow GPU
    is visible instead of silently depressing the score."""
    assert _harness()._is_timeout(exc) is True


def test_chained_timeout_is_recognised():
    """Clients wrap the original error, so the check must walk the cause chain."""
    m = _harness()
    try:
        try:
            raise TimeoutError("inner")
        except TimeoutError as inner:
            raise RuntimeError("request failed") from inner
    except RuntimeError as outer:
        assert m._is_timeout(outer) is True


@pytest.mark.parametrize("exc", [
    ValueError("syntax error at or near SELECT"),
    KeyError("db_id"),
    RuntimeError("model returned no SQL"),
])
def test_ordinary_failures_are_not_timeouts(exc):
    """A real wrong answer must not be excused as a timeout — that would
    inflate the score, which is worse than depressing it."""
    assert _harness()._is_timeout(exc) is False


# =====================================================================
# E-03 — the harness must measure the SHIPPING identifier check
# =====================================================================

DDL = """CREATE TABLE schools (
  cdscode text,
  school text,
  Academic Year text,
  county real
);
CREATE TABLE satscores (
  cds text,
  avgscrwrite bigint
);"""


def _sv():
    return pytest.importorskip(
        "apps.chat.task.sql_validate",
        reason="needs the app on sys.path (run in-container)")


def test_ddl_adapter_indexes_every_table_and_column():
    """`build_schema_index` parses the product's M-Schema format; this harness
    emits plain DDL. Feeding DDL straight in produced an EMPTY index, which
    silently DISABLES validation — which is how the harness and the product
    drifted apart (E-03)."""
    m, sv = _harness(), _sv()
    idx = m._schema_index_from_ddl(DDL, sv)
    assert sv.schema_index_is_usable(idx)
    assert set(idx["tables"].values()) == {"schools", "satscores"}
    assert len(idx["columns"][sv.normalize_identifier("schools")]) == 4


def test_ddl_adapter_handles_column_names_containing_spaces():
    """This bank has columns like "Academic Year text" — the type is the last
    token, the name is everything before it."""
    m, sv = _harness(), _sv()
    idx = m._schema_index_from_ddl(DDL, sv)
    cols = idx["columns"][sv.normalize_identifier("schools")]
    assert "Academic Year" in cols.values()


def test_hallucinated_column_is_reported():
    m = _harness()
    _sv()
    out = m._production_identifier_findings(
        "SELECT s.NoSuchColumn FROM schools s", "california_schools")
    if out is None:
        pytest.skip("BIRD schema not reachable in this environment")
    assert any("NoSuchColumn" in line for line in out)


def test_case_only_difference_is_not_reported():
    """The critical guard. PostgreSQL folds unquoted identifiers, so `s.School`
    and `school` are the SAME column. The product reports case mismatches on
    purpose (it wants verbatim copying so quoting is safe), but against this
    bank that is a false positive — and feeding it back would teach the repair
    loop to 'fix' working SQL, the same harm as the lint_sql alias/CTE false
    positives already recorded in the audit."""
    m = _harness()
    _sv()
    out = m._production_identifier_findings(
        "SELECT s.School FROM schools s", "california_schools")
    if out is None:
        pytest.skip("BIRD schema not reachable in this environment")
    assert out == [], f"case-only difference reported as a problem: {out}"


# =====================================================================
# L-C — the product's retry feedback must name the table that owns the column
# =====================================================================

def test_production_feedback_names_the_owning_table():
    """"column X does not exist" tells the model nothing it did not already
    know, so it returns byte-identical SQL and the retry is wasted. Ported from
    the harness, where this rescued a loop ending "no change, giving up" on 28
    of 47 attempts."""
    sv = _sv()
    m = _harness()
    idx = m._schema_index_from_ddl(DDL, sv)
    findings = [{"kind": "unknown-column", "identifier": "d.avgscrwrite",
                 "expected": ""}]
    text = sv.format_identifier_feedback(findings, idx)
    assert "satscores" in text, text
    assert "join that table" in text


def test_production_feedback_unchanged_without_a_schema_index():
    """Back-compat: every existing caller passes one argument and must get the
    exact message it got before."""
    sv = _sv()
    findings = [{"kind": "unknown-column", "identifier": "d.nope", "expected": ""}]
    text = sv.format_identifier_feedback(findings)
    assert 'column "d.nope" does not exist in the provided schema' in text
    assert "belongs to" not in text


def test_owners_hint_excludes_the_table_already_queried():
    """Naming the table the query already used would be noise."""
    sv = _sv()
    m = _harness()
    idx = m._schema_index_from_ddl(DDL, sv)
    assert sv.owners_hint("school", idx, exclude="schools") == ""
    assert "schools" in sv.owners_hint("school", idx)
