"""GROUP F cleanup regressions (AUDIT D-27, D-28, D-29, D-31, D-32, D-34).

Every deletion in GROUP F is guarded by a proof-of-death assertion here: the
module/function is gone AND nothing live still references it. That is the only
thing that makes a deletion safe to review — "I grepped once" is not.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_group_f_deadcode.py -q"
"""

import json
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _backend():
    for c in (REPO_ROOT / "backend", pathlib.Path("/opt/sqlbot/app")):
        if (c / "main.py").exists():
            return c
    pytest.skip("backend tree not found")


def _py_sources():
    """Every live .py file, excluding tests and the alembic history."""
    root = _backend()
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel.startswith(("tests/", "alembic/", ".venv/")):
            continue
        yield rel, p.read_text(encoding="utf-8", errors="replace")


def _live_lines(text):
    """Source lines with whole-line comments stripped — a reference inside a
    commented-out block is not a live reference."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


# =====================================================================
# D-27 — the accidental `from dis import specialized`
# =====================================================================

def test_no_accidental_dis_import():
    """`dis` is the bytecode disassembler. The import was autocomplete debris
    next to the local `specialized_model_id`; it resolves, so nothing ever
    failed, and it stayed."""
    root = _backend()
    src = (root / "apps/chat/task/llm.py").read_text(encoding="utf-8")
    assert "from dis import" not in src, "the stdlib `dis` import is back (D-27)"


# =====================================================================
# D-29 — MCP must not advertise an operation whose route does not exist
# =====================================================================

def test_mcp_only_exposes_operations_that_exist():
    """include_operations named `get_model_list`, whose route is commented out.
    The MCP tool list is a contract: advertising an operation that cannot be
    called is a defect visible to every MCP client."""
    root = _backend()
    main_src = (root / "main.py").read_text(encoding="utf-8")
    m = re.search(r"include_operations\s*=\s*\[([^\]]*)\]", main_src, re.S)
    assert m, "include_operations not found"
    advertised = set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))

    declared = set()
    for _rel, text in _py_sources():
        for line in _live_lines(text):
            declared.update(re.findall(r"operation_id\s*=\s*[\"']([^\"']+)[\"']", line))

    missing = advertised - declared
    assert not missing, f"MCP advertises operations with no live route: {sorted(missing)}"


# =====================================================================
# D-31 / D-32 — dead modules are gone and nothing references them
# =====================================================================

DEAD_MODULES = [
    "common/core/security_config.py",   # D-31, 160 LOC, fully self-referential
    "common/utils/random.py",           # D-32
    "common/audit/schemas/log_utils.py",  # D-32, shadowed by the xpack import
    "apps/ai_model/llm.py",             # D-32, empty file
]

# NOT dead, despite zero references in this repo. The audit listed it under
# D-32; deleting it broke startup outright:
#   sqlbot_xpack.core -> authentication.api -> cas_client -> common.utils.http_utils
# sqlbot_xpack is closed source and compiled, and its import strings are not
# recoverable from the .so, so NO grep of this repo can establish that a
# common/ module is unused. Only an empirical import probe can.
XPACK_OWNED = ["common/utils/http_utils.py"]


@pytest.mark.parametrize("rel", XPACK_OWNED)
def test_xpack_owned_module_is_not_deleted(rel):
    """Regression for a real break introduced and reverted during GROUP F."""
    assert (_backend() / rel).exists(), (
        f"{rel} has no in-repo references but IS imported by the closed-source "
        f"xpack at startup; deleting it is a hard boot failure")


@pytest.mark.parametrize("rel", DEAD_MODULES)
def test_dead_module_is_deleted(rel):
    assert not (_backend() / rel).exists(), f"{rel} is back (D-31/D-32)"


@pytest.mark.parametrize("rel", DEAD_MODULES)
def test_nothing_imports_the_dead_module(rel):
    """The proof-of-death half. `log_utils` is the one that matters: live code
    imports `build_resource_union_query` from `sqlbot_xpack.audit.curd.audit`,
    NOT from the local copy, so the local copy was a shadowed duplicate."""
    mod = rel[:-3].replace("/", ".")
    pkg, leaf = mod.rsplit(".", 1)
    # Match the FULL dotted path only. A bare-leaf match is useless here because
    # leaves collide across packages: `apps.ai_model.llm` is dead while
    # `apps.chat.task.llm` is the pipeline's core module.
    patterns = [
        rf"\b(?:from|import)\s+{re.escape(mod)}\b",          # import a.b.c
        rf"\bfrom\s+{re.escape(pkg)}\s+import\s+[^#]*\b{re.escape(leaf)}\b",  # from a.b import c
    ]
    offenders = []
    for src_rel, text in _py_sources():
        for i, line in enumerate(_live_lines(text), 1):
            if any(re.search(p, line) for p in patterns):
                offenders.append(f"{src_rel}:{i}: {line.strip()}")
    assert not offenders, f"{rel} still imported by {offenders}"


DEAD_FUNCTIONS = ["get_data_engine", "create_table", "insert_data"]


@pytest.mark.parametrize("fn", DEAD_FUNCTIONS)
def test_dead_engine_helper_is_gone(fn):
    """apps/db/engine.py stays — get_engine_conn/get_engine_config are live.
    Only these three unreferenced helpers go."""
    src = (_backend() / "apps/db/engine.py").read_text(encoding="utf-8")
    assert f"def {fn}(" not in src, f"apps/db/engine.py::{fn} is back (D-32)"


def test_live_engine_helpers_survive():
    """Guards the deletion itself: these two ARE imported (db.py, crud/
    datasource.py, api/datasource.py, backend/tests/test_row_rag_e2e.py)."""
    src = (_backend() / "apps/db/engine.py").read_text(encoding="utf-8")
    for fn in ("get_engine_conn", "get_engine_config", "get_engine_uri"):
        assert f"def {fn}(" in src, f"deleted a LIVE helper: {fn}"


# =====================================================================
# D-28 — the helper scripts must reference paths that exist
# =====================================================================

def test_scripts_reference_real_paths():
    """lint.sh / test.sh / prestart.sh targeted an `app/` package that has never
    existed in this layout, so every one of them fails immediately."""
    root = _backend()
    scripts = root / "scripts"
    if not scripts.exists():
        pytest.skip("scripts/ not present")
    offenders = []
    for sh in scripts.glob("*.sh"):
        text = sh.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"(?<![\w./])app(?:/|\b)", line):
                offenders.append(f"{sh.name}:{i}: {line.strip()}")
    assert not offenders, "scripts still target the non-existent app/: " + str(offenders)


# =====================================================================
# D-34 — the SSR service is not called "vite-project"
# =====================================================================

def test_g2ssr_package_is_named():
    pkg = REPO_ROOT / "g2-ssr" / "package.json"
    if not pkg.exists():
        pytest.skip("g2-ssr not present")
    assert json.loads(pkg.read_text())["name"] != "vite-project", (
        "g2-ssr/package.json still carries the scaffold name (D-34)")
