"""D-37 — permission bypass must key on ADMIN, not on user id 1.

`is_normal_user()` returned `current_user.id != 1`, so "is this caller subject to
row/column permissions?" was answered by an id comparison. Three different
notions of privileged coexist in this codebase; this makes the permission layer
use the same one the rest of the system uses (`isAdmin`).

The change can only TIGHTEN: `isAdmin` is set as `id == 1 and account ==
'admin'`, a strict subset of `id == 1`. Every caller that bypassed before and is
a real admin still bypasses; a caller that is id 1 WITHOUT being the admin
account, or a DTO constructed without `isAdmin`, no longer does.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_d37_admin_bypass.py -q"
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


def _user(**kw):
    from apps.system.schemas.system_schema import UserInfoDTO
    base = dict(id=99, account="alice", name="alice", email="a@b.c", oid=1)
    base.update(kw)
    return UserInfoDTO(**base)


def _is_normal_user():
    from apps.datasource.crud.permission import is_normal_user
    return is_normal_user


# =====================================================================
# the bypass itself
# =====================================================================

def test_real_admin_still_bypasses_permissions():
    """Unchanged behaviour for the genuine administrator — this must not become
    a functional regression that locks the admin out."""
    assert _is_normal_user()(_user(id=1, account="admin", isAdmin=True)) is False


def test_user_id_1_that_is_not_the_admin_account_no_longer_bypasses():
    """THE BUG. `id != 1` gave a permission bypass to anything holding id 1,
    including DTOs built by internal services that never set isAdmin."""
    assert _is_normal_user()(_user(id=1, account="not-admin", isAdmin=False)) is True


def test_ordinary_user_is_still_subject_to_permissions():
    assert _is_normal_user()(_user(id=42, isAdmin=False)) is True


def test_admin_flag_on_a_non_1_id_bypasses():
    """The permission layer now agrees with the rest of the system about what
    'privileged' means, rather than keeping a fourth definition."""
    assert _is_normal_user()(_user(id=7, isAdmin=True)) is False


def test_missing_isadmin_attribute_fails_closed():
    """A DTO-like object without the attribute must be treated as NOT privileged.
    Failing open here would reintroduce exactly the bypass being closed."""
    class _Bare:
        id = 1
        account = "admin"

    assert _is_normal_user()(_Bare()) is True


# =====================================================================
# the internal service user
# =====================================================================

def test_assistant_inner_user_is_subject_to_permissions():
    """`get_assistant_user` builds a DTO with isAdmin defaulting to False. When
    its id happened to be 1 it silently gained a full permission bypass."""
    from apps.system.crud.assistant import get_assistant_user

    assert _is_normal_user()(get_assistant_user(id=1)) is True
    assert _is_normal_user()(get_assistant_user(id=5)) is True


# =====================================================================
# the source-level guarantee
# =====================================================================

def test_permission_layer_no_longer_compares_ids():
    import ast
    import textwrap

    src = (_backend() / "apps/datasource/crud/permission.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "is_normal_user"), None)
    assert fn is not None, "is_normal_user not found"

    # Executable statements only. The docstring quotes the old expression on
    # purpose, to explain what changed, so a plain substring search over the
    # whole function would match its own explanation.
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    code = textwrap.dedent("\n".join(ast.unparse(n) for n in body))
    assert "id != 1" not in code, f"the id comparison is back (D-37): {code}"
    assert "isAdmin" in code, code


def test_benchmark_harnesses_declare_admin_explicitly():
    """The BIRD/eval harnesses construct UserInfoDTO(id=1, account='admin')
    directly, bypassing get_user_info() which is what normally sets isAdmin.
    Without an explicit flag they would flip from 'bypasses permissions' to
    'subject to permissions' and silently change what the benchmark measures."""
    root = _backend() / "tests"
    if not root.exists():
        pytest.skip("backend/tests not present")
    offenders = []
    for p in root.glob("*.py"):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"UserInfoDTO\((.*?)\)", text, re.S):
            if "isAdmin" not in m.group(1):
                offenders.append(p.name)
    assert not offenders, (
        f"harness UserInfoDTO without an explicit isAdmin: {sorted(set(offenders))}")
