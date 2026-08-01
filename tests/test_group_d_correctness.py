"""GROUP D correctness regressions (AUDIT D-17, D-18, D-21, D-23, D-24).

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_group_d_correctness.py -q"
"""

import os
import re

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(rel_path):
    import pathlib
    for c in (pathlib.Path(REPO_ROOT) / "backend" / rel_path,
              pathlib.Path("/opt/sqlbot/app") / rel_path):
        if c.exists():
            return c.read_text(encoding="utf-8")
    pytest.skip(f"source not found: {rel_path}")


# =====================================================================
# D-17 — the LLM header override must not fabricate a confidence score
# =====================================================================

def test_llm_override_reports_the_real_heuristic_confidence(monkeypatch):
    """The fallback only fires when the heuristic is UNSURE. Returning
    max(confidence, CONFIDENCE_THRESHOLD) reported the answer as confident in
    exactly the case nothing measured it — and headerConfidence drives the UI's
    "please review" prompt, so it suppressed the warning when most needed."""
    from apps.datasource.utils import header_detection as hd
    from common.core.config import settings

    raw = pd.DataFrame([["title", None, None],
                        ["a", "b", "c"],
                        [1, 2, 3],
                        [4, 5, 6]])

    monkeypatch.setattr(settings, "HEADER_LLM_ENABLED", True)
    monkeypatch.setattr(settings, "HEADER_LLM_TRIGGER_CONF", 1.0)  # always fire

    heuristic_idx, heuristic_conf = hd.detect_header_row(raw)

    import apps.datasource.utils.header_llm as hl
    monkeypatch.setattr(hl, "llm_detect_header_row",
                        lambda *_a, **_k: heuristic_idx + 1)

    idx, conf = hd.resolve_header_row(raw, context="t")
    assert idx == heuristic_idx + 1, "the override should be taken"
    assert conf == pytest.approx(heuristic_conf), (
        f"confidence must stay the MEASURED {heuristic_conf}, got {conf} — a "
        f"fabricated score hides the low-confidence warning")


def test_no_fabricated_confidence_constant_remains():
    src = _source("apps/datasource/utils/header_detection.py")
    assert "max(confidence, CONFIDENCE_THRESHOLD)" not in src, (
        "the fabricated confidence floor is back (D-17)")


# =====================================================================
# D-18 — has_explicit_row_count must not fire on any stray small number
# =====================================================================

@pytest.mark.parametrize("question", [
    "list all customers in region 3",
    "show every order over 50 dollars",
    "list all Q4 items",
    "display all products in category 7",
    "find all tickets for building 12",
])
def test_incidental_numbers_do_not_suppress_the_limit_lift(question):
    """These are listing questions with an incidental number. Treating them as
    "the user asked for N rows" disabled the completeness lift — the exact
    failure the lift exists to fix."""
    from apps.chat.task.agentic import has_explicit_row_count
    assert has_explicit_row_count(question) is False, question


@pytest.mark.parametrize("question", [
    "top 10 customers",
    "first 5 orders",
    "show me the top 3 products by revenue",
    "last 20 transactions",
    "limit 15",
    "前10条",
    "상위 5개",
])
def test_genuine_row_requests_are_still_detected(question):
    """A quantity adjacent to a count word IS an explicit row request and must
    keep suppressing the lift."""
    from apps.chat.task.agentic import has_explicit_row_count
    assert has_explicit_row_count(question) is True, question


def test_four_digit_years_are_still_ignored():
    from apps.chat.task.agentic import has_explicit_row_count
    assert has_explicit_row_count("list all sales in 2019") is False


# =====================================================================
# D-21 — no bare `except:` anywhere in the backend
# =====================================================================

@pytest.mark.parametrize("module", [
    "apps/db/db.py",
    "common/utils/utils.py",
    "common/audit/schemas/logger_decorator.py",
])
def test_no_bare_except(module):
    """A bare except swallows KeyboardInterrupt, SystemExit and GeneratorExit.
    db.py's was inside convert_value, which runs per CELL of every result row."""
    src = _source(module)
    offenders = [i + 1 for i, line in enumerate(src.splitlines())
                 if re.match(r"^\s*except\s*:\s*(#.*)?$", line)]
    assert not offenders, f"{module} has bare except at lines {offenders}"


# =====================================================================
# D-23 — no mutable default arguments
# =====================================================================

def test_no_mutable_default_on_the_assistant_ui_endpoint():
    src = _source("apps/system/api/assistant.py")
    assert "List[UploadFile] = []" not in src, (
        "mutable default argument reintroduced (D-23)")


# =====================================================================
# D-24 — the deprecated loader must not keep the un-rewound COPY bug
# =====================================================================

def test_deprecated_uploader_delegates_to_the_fixed_loader():
    """insert_pg wrote via to_sql then COPY'd from an exhausted StringIO — the
    bug _insert_df_to_pg documents as fixed. Two loaders, one fixed."""
    src = _source("apps/datasource/api/datasource.py")
    live = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    # exactly one place may build the COPY statement
    assert live.count("copy_expert") <= 1, (
        "more than one COPY implementation remains (D-24)")
    if "def insert_pg(" in live:
        body_start = live.index("def insert_pg(")
        body = live[body_start:body_start + 1200]
        assert "_insert_df_to_pg(" in body, (
            "insert_pg must delegate to the fixed loader, not duplicate it")
