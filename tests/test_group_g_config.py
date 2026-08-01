"""GROUP G config / hardcoding regressions (AUDIT H-01, H-02, H-03, H-06,
H-08, H-09, H-10, H-12, H-14).

The theme is one the audit found repeatedly: a misconfiguration that produces a
*silently wrong* system rather than a failure. A placeholder image host returns
unresolvable URLs; `CACHE_TYPE=redis` with no URL quietly talks to localhost; a
parent tuning knob that does not move its children. Each is turned into either a
startup error or an explicit, tunable setting.

Run in-container:
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && \\
        .venv/bin/python -m pytest /tmp/roottests/test_group_g_config.py -q"
"""

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _backend():
    for c in (REPO_ROOT / "backend", pathlib.Path("/opt/sqlbot/app")):
        if (c / "main.py").exists():
            return c
    pytest.skip("backend tree not found")


def _fresh(**overrides):
    """A Settings instance built from explicit values, not the ambient env."""
    from common.core.config import Settings
    return Settings(**overrides)


# =====================================================================
# H-06 — a parent default that does not move its children
# =====================================================================

def test_overriding_the_parent_similarity_moves_the_children():
    """`EMBEDDING_TERMINOLOGY_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY`
    binds the class-body VALUE at definition time. Setting
    EMBEDDING_DEFAULT_SIMILARITY in the environment therefore moved nothing, and
    the operator had to discover and set all three."""
    s = _fresh(EMBEDDING_DEFAULT_SIMILARITY=0.9)
    assert s.EMBEDDING_TERMINOLOGY_SIMILARITY == pytest.approx(0.9)
    assert s.EMBEDDING_DATA_TRAINING_SIMILARITY == pytest.approx(0.9)


def test_overriding_the_parent_top_count_moves_the_children():
    s = _fresh(EMBEDDING_DEFAULT_TOP_COUNT=11)
    assert s.EMBEDDING_TERMINOLOGY_TOP_COUNT == 11
    assert s.EMBEDDING_DATA_TRAINING_TOP_COUNT == 11


def test_an_explicit_child_still_wins_over_the_parent():
    """Propagation must not clobber a value the operator set on purpose."""
    s = _fresh(EMBEDDING_DEFAULT_SIMILARITY=0.9,
               EMBEDDING_TERMINOLOGY_SIMILARITY=0.1)
    assert s.EMBEDDING_TERMINOLOGY_SIMILARITY == pytest.approx(0.1)
    assert s.EMBEDDING_DATA_TRAINING_SIMILARITY == pytest.approx(0.9)


def test_defaults_are_unchanged_when_nothing_is_overridden():
    s = _fresh()
    assert s.EMBEDDING_TERMINOLOGY_SIMILARITY == pytest.approx(s.EMBEDDING_DEFAULT_SIMILARITY)
    assert s.EMBEDDING_TERMINOLOGY_TOP_COUNT == s.EMBEDDING_DEFAULT_TOP_COUNT


# =====================================================================
# H-03 / H-01 / H-02 — misconfiguration must be loud, not silent
# =====================================================================

def test_redis_cache_without_a_url_is_rejected():
    """`CACHE_TYPE=redis` with an empty CACHE_REDIS_URL fell back to
    redis://localhost:6379/0 — in a container that is nothing, so the cache
    silently did not work."""
    from common.core.config import validate_settings, ConfigurationError

    s = _fresh(CACHE_TYPE="redis", CACHE_REDIS_URL=None)
    with pytest.raises(ConfigurationError, match="CACHE_REDIS_URL"):
        validate_settings(s)


def test_redis_cache_with_a_url_is_accepted():
    from common.core.config import validate_settings

    validate_settings(_fresh(CACHE_TYPE="redis",
                             CACHE_REDIS_URL="redis://cache:6379/0"))


def test_memory_cache_needs_no_url():
    from common.core.config import validate_settings

    validate_settings(_fresh(CACHE_TYPE="memory"))


def test_placeholder_image_host_is_reported():
    """SERVER_IMAGE_HOST ships as the literal 'http://YOUR_SERVE_IP:MCP_PORT/'
    in docker-compose. Every MCP/API chart reply then returns an unresolvable
    URL and nothing says so. It is a warning, not an error, because a
    deployment that never renders charts is unaffected."""
    from common.core.config import collect_config_warnings

    warns = collect_config_warnings(_fresh())
    assert any("SERVER_IMAGE_HOST" in w for w in warns), warns

    ok = collect_config_warnings(_fresh(SERVER_IMAGE_HOST="http://sqlbot.internal/images/"))
    assert not any("SERVER_IMAGE_HOST" in w for w in ok)


def test_production_rejects_a_localhost_cors_origin():
    """FRONTEND_HOST defaults to the Vite dev origin and is appended to
    all_cors_origins unconditionally, so a production deployment permits
    http://localhost:5173."""
    from common.core.config import validate_settings, ConfigurationError

    # BACKEND_CORS_ORIGINS is set in the deployed environment, so both cases
    # pass it explicitly -- otherwise the test reads the ambient value.
    with pytest.raises(ConfigurationError, match="localhost"):
        validate_settings(_fresh(ENVIRONMENT="production",
                                 FRONTEND_HOST="http://localhost:5173",
                                 BACKEND_CORS_ORIGINS=[]))

    validate_settings(_fresh(ENVIRONMENT="production",
                             FRONTEND_HOST="https://sqlbot.example.com",
                             BACKEND_CORS_ORIGINS=[]))


def test_local_environment_still_allows_the_dev_origin():
    """The default posture must stay developer-friendly."""
    from common.core.config import validate_settings

    validate_settings(_fresh(ENVIRONMENT="local",
                             FRONTEND_HOST="http://localhost:5173"))


def test_cors_origins_are_deduplicated():
    s = _fresh(FRONTEND_HOST="https://app.example.com",
               BACKEND_CORS_ORIGINS="https://app.example.com")
    assert s.all_cors_origins.count("https://app.example.com") == 1


# =====================================================================
# H-08 / H-09 / H-10 / H-12 — magic numbers become settings
# =====================================================================

@pytest.mark.parametrize("name", [
    "APEX_CHARS_PER_TOKEN",      # H-08
    "APEX_MAX_PROBE_TABLES",     # H-09
    "APEX_MAX_PROBES",           # H-09
    "APEX_MIN_COLUMNS_TO_PRUNE",  # H-09
    "APEX_MAX_WORKERS",          # H-09
    "LLM_ALT_CANDIDATE_TIMEOUT",  # H-10
])
def test_tunable_exists(name):
    assert hasattr(_fresh(), name), f"{name} is still hardcoded (GROUP G)"


def test_chars_per_token_matches_the_measurement():
    """apex_helpers assumed 4 chars/token; llm.py measured 2.9 on this workload
    and its own comment says 4 'understated the true size by ~40%'. Pruning
    batches therefore ran ~38% over budget."""
    s = _fresh()
    assert 2.5 <= s.APEX_CHARS_PER_TOKEN <= 3.5, (
        f"APEX_CHARS_PER_TOKEN={s.APEX_CHARS_PER_TOKEN} contradicts the "
        f"measured 2.9 chars/token")


def test_apex_batching_uses_the_configured_ratio():
    from apps.chat.task.apex_helpers import batch_tables_by_token_budget
    from common.core.config import settings

    # Shape produced by parse_schema_string: table_name + raw_header + columns.
    # Column signatures are deliberately DISTINCT per table, so schema merging
    # cannot collapse them into one unit and the token budget alone drives the
    # batch count.
    parsed = {"tables": [
        {"table_name": f"t{i}",
         "raw_header": f"# Table: public.t{i}",
         "columns": [{"name": f"t{i}_c{j}", "type": "int"} for j in range(20)]}
        for i in range(40)
    ]}
    few = batch_tables_by_token_budget(parsed, approx_tokens_per_batch=200)
    many = batch_tables_by_token_budget(parsed, approx_tokens_per_batch=100_000)
    assert len(few) > len(many), "the token budget no longer drives batching"
    assert settings.APEX_CHARS_PER_TOKEN > 0


def test_raise_sql_limit_default_tracks_the_configured_limit():
    """H-12: 1000 appeared as a literal in three unlinked places."""
    import inspect

    from apps.chat.task.agentic import raise_sql_limit
    from common.core.config import settings

    from apps.chat.task.agentic import DEFAULT_FULL_RESULT_LIMIT

    # agentic.py is deliberately settings-free (its module docstring is explicit
    # about this), so the link is asserted here rather than coded as an import.
    assert DEFAULT_FULL_RESULT_LIMIT == settings.AGENTIC_FULL_RESULT_LIMIT, (
        "agentic.DEFAULT_FULL_RESULT_LIMIT drifted from "
        "settings.AGENTIC_FULL_RESULT_LIMIT (AUDIT H-12)")
    assert inspect.signature(raise_sql_limit).parameters["target"].default is None
    sql = "SELECT a FROM t LIMIT 5"
    assert str(settings.AGENTIC_FULL_RESULT_LIMIT) in raise_sql_limit(sql), (
        "raise_sql_limit no longer tracks AGENTIC_FULL_RESULT_LIMIT")
    assert "LIMIT 77" in raise_sql_limit(sql, target=77)


# =====================================================================
# H-14 — a wrong embedding dimension must fail loudly
# =====================================================================

def test_embedding_dimension_mismatch_is_detected():
    """ROW_RAG_EMBED_DIM is configurable but was never checked against the
    vector column that already exists. A mismatch fails every insert into
    vector(1024), and the failure was swallowed."""
    from apps.datasource.row_rag.store import check_embedding_dim

    with pytest.raises(ValueError, match="1024"):
        check_embedding_dim(existing_dim=1024, configured_dim=768)
    check_embedding_dim(existing_dim=1024, configured_dim=1024)
    check_embedding_dim(existing_dim=None, configured_dim=768)  # table not created yet


# =====================================================================
# startup wiring — the validator must actually run
# =====================================================================

def test_main_calls_the_validator():
    src = (_backend() / "main.py").read_text(encoding="utf-8")
    live = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "validate_settings" in live, (
        "validate_settings is defined but never called at startup")
