from apps.datasource.row_rag.select import filter_by_threshold, build_fallback_table


def _cand(name, cos, row):
    return {"table_name": name, "content_text": "", "cosine": cos, "row": row}


def test_filter_keeps_only_at_or_above_floor():
    cands = [_cand("t", 0.7, {"a": 1}), _cand("t", 0.49, {"a": 2}), _cand("t", 0.5, {"a": 3})]
    kept = filter_by_threshold(cands, min_cosine=0.5)
    assert [c["cosine"] for c in kept] == [0.7, 0.5]


def test_filter_empty_when_all_below_floor():
    cands = [_cand("t", 0.2, {"a": 1})]
    assert filter_by_threshold(cands, min_cosine=0.5) == []


def test_build_table_adds_source_column_and_union_fields():
    cands = [
        _cand("table_ab12", 0.8, {"Name": "First", "Note": "alpha"}),
        _cand("table_cd34", 0.6, {"Name": "Other", "Kind": "beta"}),
    ]
    out = build_fallback_table(cands)
    assert out["fields"][0] == "source"
    assert set(out["fields"]) == {"source", "Name", "Note", "Kind"}
    assert out["data"][0]["source"] == "table_ab12"
    assert out["data"][0]["Name"] == "First"
    # missing columns render blank, not KeyError
    assert out["data"][0].get("Kind", "") == ""


def test_build_table_empty_candidates_returns_empty_structure():
    out = build_fallback_table([])
    assert out == {"fields": ["source"], "data": []}
