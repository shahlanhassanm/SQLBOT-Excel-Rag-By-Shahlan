"""Run in-container:
    docker cp tests/test_relations.py sqlbot:/tmp/
    docker exec sqlbot sh -c "cd /opt/sqlbot/app && .venv/bin/python -m pytest /tmp/test_relations.py -q"

Covers the join-graph discovery in apps/datasource/relations.py. The inference
path matters most: it runs on spreadsheets, which have no declared constraints,
and a wrong edge is worse than no edge because the model will trust it and
join on it. So the false-positive cases are tested as carefully as the hits.
"""
import types

import pytest

from apps.datasource import relations as R


class FakeDS:
    def __init__(self, t="excel", ds_id=1):
        self.type = t
        self.id = ds_id


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_render_constraint_and_inferred_are_distinguishable():
    tr = R.TableRelations(
        primary_key=["id"],
        foreign_keys=[
            R.Relation("orders", "cust_id", "customers", "id", "constraint", 1.0),
            R.Relation("orders", "sku", "products", "sku", "inferred", 0.83),
        ])
    out = tr.render()
    assert "PK: id" in out
    # declared edges use ->, inferred use ~> plus a confidence, so the model is
    # never told a heuristic guess is a real foreign key
    assert "cust_id -> customers.id" in out
    assert "sku ~> products.sku" in out and "83%" in out


def test_render_empty_is_empty_string():
    assert R.TableRelations().render() == ""


# --------------------------------------------------------------------------
# name heuristics
# --------------------------------------------------------------------------

@pytest.mark.parametrize("col,table", [
    ("id", "anything"), ("customer_id", "orders"), ("CustomerID", "orders"),
    ("sku", "products"), ("order_no", "lines"), ("product_code", "sales"),
])
def test_key_like_accepts_plausible_keys(col, table):
    assert R._key_like(col, table)


@pytest.mark.parametrize("col", ["revenue", "description", "notes", "amount"])
def test_key_like_rejects_plain_measures(col):
    assert not R._key_like(col, "sales")


def test_norm_ignores_case_space_and_punctuation():
    assert R._norm("Customer ID") == R._norm("customer_id") == "customerid"


# --------------------------------------------------------------------------
# constraint SQL dispatch
# --------------------------------------------------------------------------

def test_constraint_sql_covers_the_main_families():
    for t in ("pg", "excel", "mysql", "sqlserver"):
        assert R._constraint_sql(FakeDS(t), "public")


def test_constraint_sql_returns_none_for_unsupported_dialect():
    # None routes the datasource to inference rather than raising
    assert R._constraint_sql(FakeDS("es"), "public") is None


def test_constraint_sql_escapes_quotes_in_schema_name():
    sql = R._constraint_sql(FakeDS("pg"), "we'ird")
    assert "we''ird" in sql


def test_identifier_quoting_is_dialect_correct():
    assert R._quote("odd name", FakeDS("pg")) == '"odd name"'
    assert R._quote("odd name", FakeDS("mysql")) == "`odd name`"
    # embedded quote characters are doubled, not dropped
    assert R._quote('a"b', FakeDS("pg")) == '"a""b"'


def test_qualified_omits_schema_for_schemaless_dialects():
    assert R._qualified("public", "t", FakeDS("mysql")) == "`t`"
    assert R._qualified("public", "t", FakeDS("pg")) == '"public"."t"'


# --------------------------------------------------------------------------
# inference
# --------------------------------------------------------------------------

def _patch_sampling(monkeypatch, values, uniqueness=None, default_uniq=1.0):
    """uniqueness maps (table, col) -> ratio; parent sides should be ~1.0 and
    child sides low, mirroring a real many-to-one foreign key."""
    uniqueness = uniqueness or {}
    monkeypatch.setattr(R, "_sample_values",
                        lambda ds, s, t, c, l: values.get((t, c)))
    monkeypatch.setattr(R, "_uniqueness",
                        lambda ds, s, t, c, l: uniqueness.get((t, c), default_uniq))


def test_infers_edge_when_values_overlap(monkeypatch):
    _patch_sampling(monkeypatch, {
        ("orders", "customer_id"): {"1", "2", "3"},
        ("customers", "customer_id"): {"1", "2", "3", "4", "5"},
    }, uniqueness={("orders", "customer_id"): 0.2,        # child repeats
                   ("customers", "customer_id"): 1.0})    # parent is a key
    out = R.infer_relations(FakeDS(), "public", {
        "orders": ["customer_id", "total"],
        "customers": ["customer_id", "name"],
    })
    edges = [r for tr in out.values() for r in tr.foreign_keys]
    assert len(edges) == 1
    e = edges[0]
    assert (e.table, e.ref_table) == ("orders", "customers")
    assert e.source == "inferred"


def test_spreadsheet_label_join_is_found(monkeypatch):
    """A workbook's only relationship is usually a detail sheet and a summary
    sheet sharing a business label. `Category` is not key-shaped, so it is only
    admitted on near-total value agreement."""
    _patch_sampling(monkeypatch, {
        ("Gateways_r0", "Category"): {"bank", "ewallet", "card"},
        ("Summary_r0", "Category"): {"bank", "ewallet", "card"},
    }, uniqueness={("Gateways_r0", "Category"): 0.1,   # many rows per category
                   ("Summary_r0", "Category"): 1.0})   # one row per category
    out = R.infer_relations(FakeDS(), "public", {
        "Gateways_r0": ["Payment Gateway", "Category", "Country"],
        "Summary_r0": ["Category", "Count", "% of Total"],
    })
    edges = [r for tr in out.values() for r in tr.foreign_keys]
    assert len(edges) == 1
    assert (edges[0].table, edges[0].ref_table) == ("Gateways_r0", "Summary_r0")


def test_partial_overlap_on_an_unnamed_column_is_rejected(monkeypatch):
    """Same shape as above but only half the values agree -- for a column whose
    name gives no evidence, that is not enough to assert a join."""
    _patch_sampling(monkeypatch, {
        ("a", "Region"): {"north", "south", "east", "west"},
        ("b", "Region"): {"north", "south", "atlantis", "narnia"},
    }, uniqueness={("a", "Region"): 0.1, ("b", "Region"): 1.0})
    out = R.infer_relations(FakeDS(), "public",
                            {"a": ["Region"], "b": ["Region"]})
    assert out == {}


# --- regressions from real schemas -----------------------------------------

def test_two_surrogate_id_columns_are_not_a_relationship(monkeypatch):
    """Found on bird__card_games: every table has an `id` of 1..N, so all pairs
    overlapped 100% and each side was unique. That produced 24 bogus edges like
    `legalities.id ~> cards.id`. A foreign key needs a repeating child side."""
    _patch_sampling(monkeypatch, {
        ("cards", "id"): {"1", "2", "3", "4", "5"},
        ("legalities", "id"): {"1", "2", "3", "4", "5"},
        ("rulings", "id"): {"1", "2", "3", "4", "5"},
    }, default_uniq=1.0)                       # both sides unique
    out = R.infer_relations(FakeDS(), "public", {
        "cards": ["id"], "legalities": ["id"], "rulings": ["id"],
    })
    assert out == {}


def test_column_naming_picks_the_right_parent(monkeypatch):
    """Found on bird__codebase_community: `comments.userid` overlapped both
    `badges.userid` and `users.id`, and value overlap alone chose badges. The
    column names its target, so `users` must win."""
    _patch_sampling(monkeypatch, {
        ("comments", "userid"): {"1", "2", "3"},
        ("badges", "userid"): {"1", "2", "3"},
        ("users", "userid"): {"1", "2", "3", "4"},
    }, uniqueness={("comments", "userid"): 0.3,
                   ("badges", "userid"): 0.3,
                   ("users", "userid"): 1.0})
    out = R.infer_relations(FakeDS(), "public", {
        "comments": ["userid"], "badges": ["userid"], "users": ["userid"],
    })
    edges = [r for tr in out.values() for r in tr.foreign_keys]
    assert edges, "expected an edge to users"
    assert all(e.ref_table == "users" for e in edges), \
        f"named target ignored: {[(e.table, e.ref_table) for e in edges]}"


@pytest.mark.parametrize("col,tables,expected", [
    ("user_id", ["users", "posts"], "users"),
    ("userid", ["users"], "users"),
    ("customer_code", ["customers"], "customers"),
    ("sku", ["products"], None),          # names nothing
    ("id", ["users", "posts"], None),     # too generic to name a target
])
def test_referenced_table_resolution(col, tables, expected):
    assert R._referenced_table(col, tables) == expected


def test_no_edge_when_values_do_not_overlap(monkeypatch):
    _patch_sampling(monkeypatch, {
        ("a", "code"): {"x", "y"},
        ("b", "code"): {"1", "2"},
    })
    out = R.infer_relations(FakeDS(), "public",
                            {"a": ["code"], "b": ["code"]})
    assert out == {}


def test_no_edge_when_neither_side_is_unique(monkeypatch):
    """Overlapping values in two non-unique columns is a shared label
    (a status code repeated in both sheets), not a key relationship."""
    _patch_sampling(monkeypatch, {
        ("a", "status_code"): {"open", "closed"},
        ("b", "status_code"): {"open", "closed"},
    }, default_uniq=0.02)
    out = R.infer_relations(FakeDS(), "public",
                            {"a": ["status_code"], "b": ["status_code"]})
    assert out == {}


def test_column_present_in_many_tables_is_treated_as_a_label(monkeypatch):
    _patch_sampling(monkeypatch, {(f"t{i}", "id"): {"1", "2", "3"}
                                  for i in range(10)})
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_MAX_FANOUT", 6)
    out = R.infer_relations(FakeDS(), "public",
                            {f"t{i}": ["id"] for i in range(10)})
    assert out == {}


def test_single_table_datasource_infers_nothing(monkeypatch):
    assert R.infer_relations(FakeDS(), "public", {"only": ["id"]}) == {}


def test_unreadable_column_is_skipped_not_raised(monkeypatch):
    # _sample_values returns None when the probe query fails
    _patch_sampling(monkeypatch, {
        ("a", "id"): None,
        ("b", "id"): {"1", "2"},
    })
    assert R.infer_relations(FakeDS(), "public",
                             {"a": ["id"], "b": ["id"]}) == {}


def test_values_are_compared_case_and_whitespace_insensitively(monkeypatch):
    """Spreadsheet keys are frequently ' 01 ' in one sheet and '01' in another."""
    _patch_sampling(monkeypatch, {
        ("child", "ref_code"): {"a1", "a2"},
        ("parent", "ref_code"): {"a1", "a2", "a3"},
    }, uniqueness={("child", "ref_code"): 0.3,
                   ("parent", "ref_code"): 1.0})
    out = R.infer_relations(FakeDS(), "public",
                            {"child": ["ref_code"], "parent": ["ref_code"]})
    assert any(r.ref_table == "parent" for tr in out.values()
               for r in tr.foreign_keys)


# --------------------------------------------------------------------------
# entry point behaviour
# --------------------------------------------------------------------------

def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", False)
    R.clear_cache()
    assert R.get_relations(FakeDS(), "public", {"a": ["id"], "b": ["id"]}) == {}


def test_discovery_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", True)
    monkeypatch.setattr(R, "discover_constraints",
                        lambda ds, s: (_ for _ in ()).throw(RuntimeError("boom")))
    R.clear_cache()
    # a broken datasource must degrade to "no hints", never propagate
    assert R.get_relations(FakeDS(ds_id=99), "public") == {}


def test_constraints_win_over_inference(monkeypatch):
    """A table with a declared FK is not second-guessed by the heuristic."""
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", True)
    declared = {"orders": R.TableRelations(
        foreign_keys=[R.Relation("orders", "cid", "customers", "id",
                                 "constraint", 1.0)])}
    monkeypatch.setattr(R, "discover_constraints", lambda ds, s: dict(declared))
    called = {"n": 0}

    def _spy(ds, s, tables):
        called["n"] += 1
        assert "orders" not in tables      # already covered, not re-inferred
        return {}

    monkeypatch.setattr(R, "infer_relations", _spy)
    R.clear_cache()
    out = R.get_relations(FakeDS(ds_id=7), "public",
                          {"orders": ["cid"], "customers": ["id"],
                           "items": ["sku"]})
    assert out["orders"].foreign_keys[0].source == "constraint"
    assert called["n"] == 1


def test_foreign_tables_are_filtered_out(monkeypatch):
    """Excel datasources live in the shared `public` schema alongside SQLBot's
    own tables, so information_schema returns chat_log/core_datasource/... too.
    Leaking those into the prompt exposes internal schema and invites joins the
    user cannot execute."""
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", True)
    monkeypatch.setattr(R, "infer_relations", lambda ds, s, t: {})
    monkeypatch.setattr(R, "discover_constraints", lambda ds, s: {
        "Sheet1_abc": R.TableRelations(
            primary_key=["id"],
            foreign_keys=[
                R.Relation("Sheet1_abc", "uid", "Sheet2_def", "uid",
                           "constraint", 1.0),
                # edge into SQLBot's own table -- must be dropped
                R.Relation("Sheet1_abc", "ds", "core_datasource", "id",
                           "constraint", 1.0),
            ]),
        "core_datasource": R.TableRelations(primary_key=["id"]),
        "chat_log": R.TableRelations(primary_key=["id"]),
    })
    R.clear_cache()
    out = R.get_relations(FakeDS(ds_id=123), "public",
                          {"Sheet1_abc": ["id", "uid", "ds"],
                           "Sheet2_def": ["uid"]})
    assert set(out) == {"Sheet1_abc"}
    refs = [r.ref_table for r in out["Sheet1_abc"].foreign_keys]
    assert refs == ["Sheet2_def"]


def test_no_filtering_when_table_list_is_unknown(monkeypatch):
    """tables=None means the caller could not supply the owned set; filtering
    would then wrongly discard everything."""
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", True)
    monkeypatch.setattr(R, "discover_constraints", lambda ds, s: {
        "t": R.TableRelations(primary_key=["id"])})
    R.clear_cache()
    out = R.get_relations(FakeDS(ds_id=124), "public", None)
    assert set(out) == {"t"}


def test_cache_prevents_a_second_discovery(monkeypatch):
    monkeypatch.setattr(R.settings, "SCHEMA_RELATIONS_ENABLED", True)
    calls = {"n": 0}

    def _once(ds, s):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(R, "discover_constraints", _once)
    monkeypatch.setattr(R, "infer_relations", lambda ds, s, t: {})
    R.clear_cache()
    ds = FakeDS(ds_id=42)
    R.get_relations(ds, "public")
    R.get_relations(ds, "public")
    assert calls["n"] == 1
    R.clear_cache(42)
    R.get_relations(ds, "public")
    assert calls["n"] == 2


def test_rows_accepts_both_exec_sql_shapes():
    assert list(R._rows({"data": [(1,)]})) == [(1,)]
    assert list(R._rows([(2,)])) == [(2,)]
    assert list(R._rows(None)) == []


# --------------------------------------------------------------------------
# composite-FK safety and deterministic sampling
# --------------------------------------------------------------------------

def test_pg_constraint_sql_uses_pg_catalog_for_ordinal_pairing():
    """information_schema.constraint_column_usage cannot pair composite FK
    columns; the pg family must read pg_constraint with ordinality instead."""
    sql = R._constraint_sql(FakeDS("pg"), "public")
    assert "pg_constraint" in sql and "WITH ORDINALITY" in sql


def test_cross_joined_composite_fk_is_dropped(monkeypatch):
    """redshift/greenplum stay on information_schema, where a 2-column FK
    arrives as the 2x2 cross product. Emitting those would state fabricated
    edges as declared constraints -- the whole constraint must be dropped."""
    rows = [
        # composite FK (a1,a2) -> parent(b1,b2), cross-joined into 4 rows
        {"rel_table": "child", "rel_column": "a1", "rel_ref_table": "parent",
         "rel_ref_column": "b1", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
        {"rel_table": "child", "rel_column": "a1", "rel_ref_table": "parent",
         "rel_ref_column": "b2", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
        {"rel_table": "child", "rel_column": "a2", "rel_ref_table": "parent",
         "rel_ref_column": "b1", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
        {"rel_table": "child", "rel_column": "a2", "rel_ref_table": "parent",
         "rel_ref_column": "b2", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
        # ordinary single-column FK on the same schema must survive
        {"rel_table": "child", "rel_column": "pid", "rel_ref_table": "parent",
         "rel_ref_column": "id", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk2"},
    ]
    # inject a stub module: importing the real apps.db.db drags in optional
    # xpack dependencies that are not importable in the test context
    import sys
    monkeypatch.setitem(
        sys.modules, "apps.db.db",
        types.SimpleNamespace(exec_sql=lambda ds, sql, origin_column=True: rows))
    out = R.discover_constraints(FakeDS("redshift"), "public")
    edges = [(r.column, r.ref_column) for r in out["child"].foreign_keys]
    assert edges == [("pid", "id")]


def test_correctly_paired_composite_fk_is_kept(monkeypatch):
    """One row per child column (what the pg_catalog/mssql queries return) is a
    correctly paired composite FK and must NOT be dropped."""
    rows = [
        {"rel_table": "child", "rel_column": "a1", "rel_ref_table": "parent",
         "rel_ref_column": "b1", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
        {"rel_table": "child", "rel_column": "a2", "rel_ref_table": "parent",
         "rel_ref_column": "b2", "rel_kind": "FOREIGN KEY", "rel_constraint": "fk1"},
    ]
    import sys
    monkeypatch.setitem(
        sys.modules, "apps.db.db",
        types.SimpleNamespace(exec_sql=lambda ds, sql, origin_column=True: rows))
    out = R.discover_constraints(FakeDS("pg"), "public")
    edges = sorted((r.column, r.ref_column) for r in out["child"].foreign_keys)
    assert edges == [("a1", "b1"), ("a2", "b2")]


def test_bounded_select_is_deterministic_and_dialect_aware():
    """Without ORDER BY two samples of a >limit column are arbitrary subsets,
    so a true FK can look disjoint. SQL Server has no LIMIT keyword."""
    pg = R._bounded_select(FakeDS("pg"), '"t"', '"c"', 500, distinct=True)
    assert "ORDER BY" in pg and pg.rstrip().endswith("LIMIT 500")
    ms = R._bounded_select(FakeDS("sqlserver"), '[t]', '[c]', 500, distinct=True)
    assert "TOP 500" in ms and "LIMIT" not in ms and "ORDER BY" in ms


def test_one_to_one_edge_kept_when_column_names_its_target(monkeypatch):
    """A summary sheet keyed 1:1 to its detail sheet (both sides unique) is a
    real relationship when the column names the other table -- satscores.cds ->
    schools is this shape in BIRD. Plain surrogate `id` collisions must still
    be rejected (covered by test_two_surrogate_id_columns...)."""
    _patch_sampling(monkeypatch, {
        ("profiles", "user_id"): {"1", "2", "3"},
        ("users", "user_id"): {"1", "2", "3"},
    }, default_uniq=1.0)                        # BOTH sides unique
    out = R.infer_relations(FakeDS(), "public", {
        "profiles": ["user_id", "bio"],
        "users": ["user_id", "name"],
    })
    edges = [r for tr in out.values() for r in tr.foreign_keys]
    assert len(edges) == 1
    assert (edges[0].table, edges[0].ref_table) == ("profiles", "users")
