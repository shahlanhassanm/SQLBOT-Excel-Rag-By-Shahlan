"""D-37 / D-05 option 2 — `is_normal_user` must key off admin, not off id 1.

`is_normal_user()` gates whether row and column permissions are APPLIED
(True = enforced). It returned `current_user.id != 1`, so *whoever holds id 1*
bypassed every row and column rule — a database fact, not an authorisation
decision. This retargets it to the `isAdmin` flag the rest of the codebase
already uses (`workspace.py` ×9, `datasource.py:33`).

`isAdmin` is set as `id == 1 and account == 'admin'` (user.py:32, mcp.py:67), a
strict SUBSET of `id == 1`. Negating a subset yields a SUPERSET, so the set of
principals subject to permissions can only GROW. The change cannot loosen.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_authz_matrix.py -q"
"""

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _backend():
    for c in (REPO_ROOT / "backend", pathlib.Path("/opt/sqlbot/app")):
        if (c / "main.py").exists():
            return c
    pytest.skip("backend tree not found")


class _User:
    """Duck-typed stand-in; only the attributes the gate reads matter."""

    def __init__(self, uid, is_admin=None, account="u"):
        self.id = uid
        self.account = account
        if is_admin is not None:
            self.isAdmin = is_admin


# =====================================================================
# The matrix. True = row/column permissions ARE enforced for this principal.
# =====================================================================

@pytest.mark.parametrize("label,user,enforced", [
    # the real administrator keeps its bypass — this is existing product
    # behaviour and is NOT what D-37 is about (that question is Q-01)
    ("real admin (id 1, account admin)", _User(1, True, "admin"), False),
    # id 1 that is NOT the admin account no longer inherits the bypass
    ("id 1 but not admin", _User(1, False, "root"), True),
    # ordinary users unchanged
    ("ordinary user", _User(2, False), True),
    ("another ordinary user", _User(999, False), True),
    # service principals
    ("mcp_assistant (id -1)", _User(-1, False), True),
    ("inner assistant (id 1)", _User(1, False, "sqlbot-inner-assistant"), True),
])
def test_permission_enforcement_matrix(label, user, enforced):
    from apps.datasource.crud.permission import is_normal_user

    assert is_normal_user(user) is enforced, label


def test_missing_isadmin_attribute_fails_closed():
    """`BaseUserDTO` (mcp.py:163, the mcp_assistant path) historically had no
    `isAdmin` field at all. A gate that reads the attribute directly would
    AttributeError there; one that defaults to "not admin" enforces permissions.
    Fail closed, never open."""
    from apps.datasource.crud.permission import is_normal_user

    class _NoFlag:
        id = 1
        account = "admin"

    assert is_normal_user(_NoFlag()) is True, (
        "a principal with no isAdmin flag must be treated as NON-admin, so "
        "permissions are enforced")


def test_base_user_dto_now_carries_the_flag():
    """Belt as well as braces: the DTO on the MCP path gains the field so the
    gate is answering a real value rather than a defensive default."""
    from apps.system.schemas.system_schema import BaseUserDTO

    u = BaseUserDTO(id=-1, account="sqlbot-mcp-assistant", oid=1,
                    password="", language="zh-CN", name="mcp")
    assert u.isAdmin is False, "BaseUserDTO must default to NON-admin"


def test_isadmin_implies_id_one():
    """The INVARIANT the safety argument rests on.

    "Can only tighten" is not unconditional — it holds because `isAdmin` is
    assigned as `id == 1 and account == 'admin'` at every site, making it a
    strict subset of `id == 1`. If any code ever set isAdmin=True for a user
    whose id is not 1, the gate would start skipping permissions for someone the
    old code enforced them on — a LOOSENING. This pins both assignment sites so
    that can never happen silently."""
    root = _backend()
    sites = [("apps/system/crud/user.py", "userInfo.isAdmin"),
             ("apps/mcp/mcp.py", "session_user.isAdmin")]
    for rel, lhs in sites:
        src = (root / rel).read_text(encoding="utf-8")
        assigns = [l.strip() for l in src.splitlines()
                   if l.strip().startswith(lhs + " =")]
        assert assigns, f"no {lhs} assignment found in {rel}"
        for a in assigns:
            assert ".id == 1" in a, (
                f"{rel}: isAdmin no longer implies id==1 ({a!r}). The D-37 "
                f"'can only tighten' guarantee depends on this.")


def test_the_change_can_only_tighten():
    """For every REACHABLE principal, the new gate enforces permissions at least
    as often as the old one did.

    Reachable means honouring the invariant above: isAdmin=True only ever
    co-occurs with id==1. Iterating impossible combinations (id=2, isAdmin=True)
    would report a loosening that no code path can actually produce."""
    from apps.datasource.crud.permission import is_normal_user

    for uid in (-1, 0, 1, 2, 17, 999):
        for account in ("admin", "root", "alice"):
            for is_admin in ((True, False) if uid == 1 else (False,)):
                old_enforced = uid != 1
                new_enforced = is_normal_user(_User(uid, is_admin, account))
                assert new_enforced or not old_enforced, (
                    f"LOOSENED for id={uid} isAdmin={is_admin} "
                    f"account={account}: was {old_enforced}, now {new_enforced}")


def test_gate_no_longer_reads_the_raw_id():
    src = (_backend() / "apps/datasource/crud/permission.py").read_text(encoding="utf-8")
    start = src.index("def is_normal_user(")
    body = src[start:start + 1400]
    # Only the executable lines matter: the docstring legitimately QUOTES the
    # old `current_user.id != 1` to explain what changed.
    stmts = [l.strip() for l in body.splitlines() if l.strip().startswith("return ")]
    assert stmts, "no return statement found in is_normal_user"
    ret = stmts[0]
    assert ".id != 1" not in ret, f"still keys off the raw id (D-37): {ret}"
    assert "isAdmin" in ret, f"should key off the isAdmin flag: {ret}"


def test_benchmark_principal_is_unaffected():
    """bird_eval builds UserInfoDTO(id=1, isAdmin=True), so it bypassed before
    and bypasses now. Pins that D-37 cannot move an accuracy number."""
    from apps.datasource.crud.permission import is_normal_user

    assert is_normal_user(_User(1, True, "admin")) is False
