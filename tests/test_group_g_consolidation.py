"""GROUP G part 2 — duplicated implementations and locale-coupled logic
(AUDIT H-11, H-16, H-17, H-19).

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_group_g_consolidation.py -q"
"""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _backend():
    for c in (REPO_ROOT / "backend", pathlib.Path("/opt/sqlbot/app")):
        if (c / "main.py").exists():
            return c
    pytest.skip("backend tree not found")


# =====================================================================
# H-19 — one identifier-quoting implementation, not several
# =====================================================================

@pytest.mark.parametrize("ds_type,ident,expected", [
    ("mysql", "tbl", "`tbl`"),
    ("doris", "tbl", "`tbl`"),
    ("sqlServer", "tbl", "[tbl]"),      # was '"tbl"' via relations._quote
    ("mssql", "tbl", "[tbl]"),
    ("pg", "tbl", '"tbl"'),
    ("oracle", "tbl", '"tbl"'),
])
def test_relations_quoting_matches_the_canonical_implementation(ds_type, ident, expected):
    """relations.py had its own _quote that knew about backtick dialects but not
    bracket dialects, so SQL Server identifiers came out double-quoted while the
    rest of the codebase bracketed them. Both now route through
    apex_helpers.quote_ident."""
    from apps.chat.task.apex_helpers import quote_ident
    from apps.datasource.relations import _quote

    class _DS:
        type = ds_type

    assert _quote(ident, _DS()) == expected
    assert _quote(ident, _DS()) == quote_ident(ident, ds_type)


@pytest.mark.parametrize("ds_type,ident,expected", [
    ("mysql", "we`ird", "`we``ird`"),
    ("sqlServer", "we]ird", "[we]]ird]"),
    ("pg", 'we"ird', '"we""ird"'),
])
def test_quoting_still_escapes_the_closing_delimiter(ds_type, ident, expected):
    """The consolidation must not lose the escaping half."""
    from apps.datasource.relations import _quote

    class _DS:
        type = ds_type

    assert _quote(ident, _DS()) == expected


def test_only_one_quoting_implementation_remains():
    root = _backend()
    impls = []
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("tests/", "alembic/", ".venv/")):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            # a real implementation branches on the dialect's quote character
            if re.search(r'"\s*\+\s*\w+\.replace\(|`"\s*\+|\breplace\("\]", "\]\]"\)', line):
                impls.append(f"{rel}:{i}")
    assert len(impls) <= 2, (
        f"more than one identifier-quoting implementation remains: {impls}")


# =====================================================================
# H-16 — bulk user import must not compare against one hardcoded locale
# =====================================================================

def test_status_cells_are_validated_against_the_generated_template():
    """The import template writes trans('i18n_user.status_enabled') — "Enabled"
    for an English admin — while the validator compared the cell against the
    literal '已启用'. Every row of an English-generated template failed."""
    from apps.system.crud.user_excel import validate_status
    import json as _json

    en = _json.loads(((_backend() / "locales" / "en.json")).read_text(encoding="utf-8"))
    enabled = en["i18n_user"]["status_enabled"]
    disabled = en["i18n_user"]["status_disabled"]

    assert validate_status(enabled).success is True
    assert validate_status(enabled).value == 1
    assert validate_status(disabled).success is True
    assert validate_status(disabled).value == 0


def test_chinese_literals_still_accepted_for_older_templates():
    """Backward compatibility: templates downloaded before this fix carry the
    Chinese strings regardless of the admin's locale."""
    from apps.system.crud.user_excel import validate_status, validate_origin

    assert validate_status("已启用").value == 1
    assert validate_status("已禁用").value == 0
    assert validate_origin("本地创建").success is True


def test_status_rejects_nonsense():
    from apps.system.crud.user_excel import validate_status

    assert validate_status("perhaps").success is False


def test_origin_accepts_the_translated_value():
    import json as _json

    from apps.system.crud.user_excel import validate_origin

    en = _json.loads(((_backend() / "locales" / "en.json")).read_text(encoding="utf-8"))
    assert validate_origin(en["i18n_user"]["local_creation"]).success is True


# =====================================================================
# H-17 — prompt time must not be pinned to the image's timezone
# =====================================================================

def test_prompt_timezone_setting_exists():
    """Asia/Shanghai is baked into the image, so every datetime.now() — including
    the prompt's {current_time} slot — is CST. Temporal questions are answered
    in the wrong timezone for any other tenant."""
    from common.core.config import settings

    assert hasattr(settings, "PROMPT_TIMEZONE")


def test_prompt_time_respects_the_configured_zone():
    from apps.chat.task.llm import current_prompt_time

    utc = current_prompt_time("UTC")
    tokyo = current_prompt_time("Asia/Tokyo")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", utc), utc
    assert utc != tokyo, "the configured timezone had no effect"


def test_unknown_timezone_falls_back_instead_of_crashing():
    """A typo'd zone must not take down question answering."""
    from apps.chat.task.llm import current_prompt_time

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                        current_prompt_time("Not/AZone"))


# =====================================================================
# H-11 — the repeated model-facing truncation length is one setting
# =====================================================================

def test_error_text_truncation_is_configurable():
    from common.core.config import settings

    assert settings.LLM_ERROR_TEXT_MAX_CHARS > 0


def test_no_repeated_1500_literal_remains():
    src = (_backend() / "apps/chat/task/llm.py").read_text(encoding="utf-8")
    live = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    hits = [l.strip() for l in live if "[:1500]" in l]
    assert not hits, f"model-facing error text still truncated by a literal: {hits}"
