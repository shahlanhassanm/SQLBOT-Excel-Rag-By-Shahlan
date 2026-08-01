"""Cross-datasource fanout must be gated on real row restrictions.

AUDIT D-05 (option 1) / D-12. decompose_question gated on
`is_normal_user(current_user)` — i.e. "is not user id 1" — which disabled the
whole documented multi-file fanout feature for every real account, not just for
users whose rows are actually restricted. AGENTIC_DECOMPOSE_ENABLED is "true" in
docker-compose and important.md describes cross-file answers as active, but in
practice only the seeded admin (id 1) could ever reach them.

The gate is now `_has_row_restrictions`, which asks the question the original
comment already stated: "not a row-permission restricted user". Secondary legs
bypass the permission rewrite (_run_secondary_leg never calls generate_filter),
so this must fail CLOSED.

NOTE: option 2 — retargeting the global is_normal_user() predicate from
`id != 1` to `isAdmin` — has since SHIPPED as D-37, after the required audit of
every UserInfoDTO/BaseUserDTO construction site. The matrix for it lives in
tests/test_authz_matrix.py; this file keeps covering the fanout gate only.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \
        .venv/bin/python -m pytest /tmp/roottests/test_fanout_gate.py -q"
"""

import pytest

from apps.chat.task import llm as llm_mod
from apps.datasource.models.datasource import CoreDatasource


class _User:
    def __init__(self, user_id, oid=1, is_admin=False):
        self.id = user_id
        self.oid = oid
        # D-37 retargeted the permission predicate from `id != 1` to `isAdmin`.
        # The fanout gate deliberately does NOT consult either -- it asks whether
        # row filters actually apply -- so this exists only for the predicate
        # test below.
        self.isAdmin = is_admin


def _svc(user, ds):
    """A bare LLMService with only the attributes the gate reads."""
    svc = llm_mod.LLMService.__new__(llm_mod.LLMService)
    svc.current_user = user
    svc.ds = ds
    return svc


@pytest.fixture()
def ds():
    return CoreDatasource(id=7, oid=1, name="sales", type="excel")


@pytest.fixture(autouse=True)
def _stub_tables(monkeypatch):
    import apps.datasource.crud.table as table_mod
    monkeypatch.setattr(table_mod, "get_readable_table_names",
                        lambda *a, **k: ["orders", "customers"])


def _stub_filters(monkeypatch, result):
    monkeypatch.setattr(llm_mod, "get_row_permission_filters",
                        lambda **k: result)


# --- the fix: ordinary users regain fanout ---------------------------------

def test_ordinary_user_with_no_row_rules_is_not_restricted(monkeypatch, ds):
    """The regression D-12 describes: a normal account with no row rules was
    blocked purely for not being user id 1."""
    _stub_filters(monkeypatch, [])
    assert _svc(_User(42), ds)._has_row_restrictions(object()) is False


def test_user_with_row_rules_is_restricted(monkeypatch, ds):
    """The safety property the original gate was protecting must survive."""
    _stub_filters(monkeypatch, [{"table": "orders", "filter": "region = 1"}])
    assert _svc(_User(42), ds)._has_row_restrictions(object()) is True


def test_bypassing_user_is_not_restricted(monkeypatch, ds):
    """id 1 bypasses row rules entirely, so fanout stays available as before."""
    _stub_filters(monkeypatch, [])
    assert _svc(_User(1), ds)._has_row_restrictions(object()) is False


# --- fail closed -----------------------------------------------------------

def test_lookup_failure_reports_restricted(monkeypatch, ds):
    def _boom(**k):
        raise RuntimeError("permission backend down")
    monkeypatch.setattr(llm_mod, "get_row_permission_filters", _boom)
    assert _svc(_User(42), ds)._has_row_restrictions(object()) is True


def test_non_core_datasource_reports_restricted(monkeypatch):
    """Assistant out-datasources carry no CoreTable rows to check, so they must
    not be assumed unrestricted."""
    _stub_filters(monkeypatch, [])
    assert _svc(_User(42), object())._has_row_restrictions(object()) is True


def test_no_readable_tables_is_not_restricted(monkeypatch, ds):
    """Nothing to restrict; the decompose path has its own candidate checks."""
    import apps.datasource.crud.table as table_mod
    monkeypatch.setattr(table_mod, "get_readable_table_names", lambda *a, **k: [])
    _stub_filters(monkeypatch, [])
    assert _svc(_User(42), ds)._has_row_restrictions(object()) is False


# --- the global predicate must be untouched (option 2 not approved) --------

def test_is_normal_user_predicate_keys_on_admin_not_id():
    """Superseded by D-37. This test previously pinned `id != 1`, because D-05
    option 1 deliberately left the predicate alone and fixed only the fanout
    gate. Option 2 has since landed: the permission layer now uses `isAdmin`,
    the same notion of privileged the rest of the system uses.

    The change can only tighten -- `isAdmin` is `id == 1 and account == 'admin'`,
    a strict subset of `id == 1`."""
    from apps.datasource.crud.permission import is_normal_user
    assert is_normal_user(_User(1, is_admin=True)) is False    # the real admin
    assert is_normal_user(_User(1, is_admin=False)) is True    # id 1, not admin
    assert is_normal_user(_User(2)) is True
