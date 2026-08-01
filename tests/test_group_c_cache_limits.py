"""GROUP C cache / resource regressions (AUDIT D-13, D-14, D-15, D-16).

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_group_c_cache_limits.py -q"
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


def _src(rel):
    return (_backend() / rel).read_text(encoding="utf-8")


# =====================================================================
# D-13 — the relations cache key must carry the permission dimension
# =====================================================================

def test_relations_cache_key_includes_the_visible_table_set():
    """`get_relations(ds, schema, tables=...)` receives the caller's PERMITTED
    table/column view. Keying the cache on `{ds_id}:{schema}` alone meant the
    first caller's filtered join graph was served to every other caller for
    SCHEMA_RELATIONS_CACHE_TTL (3600 s) — a user with narrower permissions
    could be handed a graph naming tables they cannot see, and vice versa."""
    from apps.datasource.relations import _cache_key

    class _DS:
        id = 7

    a = _cache_key(_DS(), "public", {"orders": ["id", "customer_id"]})
    b = _cache_key(_DS(), "public", {"orders": ["id"]})
    c = _cache_key(_DS(), "public", {"orders": ["id", "customer_id"],
                                     "customers": ["id"]})
    none_key = _cache_key(_DS(), "public", None)

    assert a != b, "narrowing a column list must change the cache key"
    assert a != c, "adding a visible table must change the cache key"
    assert a != none_key, "the unfiltered view must not share a key with a filtered one"


def test_relations_cache_key_is_stable_and_order_independent():
    """Same permitted view => same key, regardless of dict/list ordering.
    Without this the cache would never hit and every question would re-run
    constraint discovery."""
    from apps.datasource.relations import _cache_key

    class _DS:
        id = 7

    k1 = _cache_key(_DS(), "public", {"orders": ["id", "name"], "customers": ["id"]})
    k2 = _cache_key(_DS(), "public", {"customers": ["id"], "orders": ["name", "id"]})
    assert k1 == k2, "cache key must not depend on table/column ordering"
    assert k1 == _cache_key(_DS(), "public",
                            {"orders": ["id", "name"], "customers": ["id"]})


def test_relations_cache_key_separates_datasources_and_schemas():
    from apps.datasource.relations import _cache_key

    class _A:
        id = 1

    class _B:
        id = 2

    assert _cache_key(_A(), "public", None) != _cache_key(_B(), "public", None)
    assert _cache_key(_A(), "public", None) != _cache_key(_A(), "other", None)


def test_clear_cache_still_targets_one_datasource():
    """clear_cache(ds_id) prefix-matches on the key. The key change must not
    break per-datasource invalidation after a field sync."""
    from apps.datasource import relations as R

    class _DS:
        id = 42

    R._cache_put(R._cache_key(_DS(), "public", {"t": ["a"]}), {"x": 1})
    R._cache_put(R._cache_key(_DS(), "public", None), {"y": 2})

    class _Other:
        id = 43

    R._cache_put(R._cache_key(_Other(), "public", None), {"z": 3})

    R.clear_cache(42)
    remaining = list(R._cache)
    assert all(not k.startswith("42:") for k in remaining), (
        f"clear_cache(42) left entries: {remaining}")
    assert any(k.startswith("43:") for k in remaining), (
        "clear_cache(42) must not evict another datasource")


# =====================================================================
# D-14 — the module-global thread pools must be bounded and configurable
# =====================================================================

def test_thread_pools_are_not_hardcoded_at_200():
    """Two module-global pools of 200 = 400 threads on a --workers 1 uvicorn,
    each able to hold a DB session against a PG_POOL_SIZE=20 pool."""
    for rel in ("apps/chat/task/llm.py", "common/utils/embedding_threads.py"):
        src = _src(rel)
        assert not re.search(r"ThreadPoolExecutor\(max_workers=200\)", src), (
            f"{rel} still hardcodes a 200-thread pool (D-14)")


def test_thread_pool_sizes_come_from_settings():
    from common.core.config import settings

    for name in ("LLM_EXECUTOR_MAX_WORKERS", "EMBEDDING_EXECUTOR_MAX_WORKERS"):
        assert hasattr(settings, name), f"missing setting {name} (D-14)"
        val = getattr(settings, name)
        assert isinstance(val, int) and val > 0, f"{name} must be a positive int"


def test_default_pool_sizes_are_bounded():
    """The point of the fix: the default must be a bounded number a reviewer can
    reason about against PG_POOL_SIZE, not an arbitrary 200."""
    from common.core.config import settings

    total = settings.LLM_EXECUTOR_MAX_WORKERS + settings.EMBEDDING_EXECUTOR_MAX_WORKERS
    assert total <= 200, f"combined default pool size still {total} threads"


# =====================================================================
# D-15 — the LLM client cache must be invalidatable
# =====================================================================

def test_llm_client_cache_can_be_cleared():
    """Finding CORRECTED: LLMConfig.__hash__ already includes api_key, so a
    rotated key yields a DIFFERENT cache entry and a fresh client — the old
    client is never served. What remained is that the stale client, holding the
    old credential, stayed resident with no way to evict it."""
    from apps.ai_model.model_factory import LLMFactory

    assert hasattr(LLMFactory, "clear_cache"), "no way to evict cached LLM clients (D-15)"
    LLMFactory.clear_cache()  # must not raise


def test_llm_config_hash_covers_the_credential():
    """Pins the property that makes D-15 a leak rather than a correctness bug.
    If this ever regresses, a rotated key WOULD serve the old client."""
    from apps.ai_model.model_factory import LLMConfig

    base = dict(model_type="openai", model_name="m", api_base_url="http://x")
    assert hash(LLMConfig(api_key="old", **base)) != hash(LLMConfig(api_key="new", **base))
    assert hash(LLMConfig(api_key="k", **base)) == hash(LLMConfig(api_key="k", **base))


# =====================================================================
# D-16 — the persisted-row cap must not depend on the SQL-generation flag
# =====================================================================

def test_persisted_row_cap_is_independent_of_enable_sql_row_limit():
    """`enable_sql_row_limit` controls whether GENERATED SQL carries a LIMIT.
    The 1000-row cap in save_sql_data is a storage guard on a Text column —
    an unrelated concern. Gating one on the other meant that with
    GENERATE_SQL_QUERY_LIMIT_ENABLED=false (what docker-compose ships) an
    unbounded result set was serialised whole."""
    src = _src("apps/chat/task/llm.py")
    m = re.search(r"def save_sql_data\(.*?\n(.*?)\n    def ", src, re.S)
    assert m, "save_sql_data not found"
    # Comments legitimately NAME the flag to explain why it is not consulted;
    # only executable lines matter here.
    body = "\n".join(ln for ln in m.group(1).splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "enable_sql_row_limit" not in body, (
        "the persisted-row cap is still gated on enable_sql_row_limit (D-16)")
    assert "AGENTIC_PERSISTED_ROW_CAP" in body, (
        "the cap should come from settings, not a literal 1000")


def test_persisted_row_cap_setting_exists_and_is_positive():
    from common.core.config import settings

    cap = settings.AGENTIC_PERSISTED_ROW_CAP
    assert isinstance(cap, int) and cap > 0


def test_benchmark_reads_sql_not_persisted_rows():
    """Guards the assumption that makes D-16 safe to change: bird_eval takes
    `sql` from the result and re-executes it, so capping PERSISTED rows cannot
    move an accuracy number."""
    b = _backend() / "tests" / "bird_eval.py"
    if not b.exists():
        pytest.skip("bird_eval.py not present")
    src = b.read_text(encoding="utf-8")
    assert "save_sql_data" not in src, (
        "bird_eval now depends on persisted rows; re-check the D-16 row cap")
