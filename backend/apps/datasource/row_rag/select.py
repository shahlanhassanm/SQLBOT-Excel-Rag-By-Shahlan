"""Pure selection logic for the RAG fallback: confidence filtering and turning
surviving candidates into the {fields, data} table the chat renderer expects."""
from typing import Any, Dict, List


def filter_by_threshold(candidates: List[Dict[str, Any]], min_cosine: float) -> List[Dict[str, Any]]:
    """Keep candidates whose cosine is >= min_cosine, preserving input order
    (caller passes them already sorted by cosine descending)."""
    return [c for c in candidates if float(c.get("cosine", 0.0)) >= min_cosine]


def build_fallback_table(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Outer-union the candidate rows into one table with a leading 'source'
    column (the table_name each row came from). Mirrors merge_union's shape so
    the existing Python table-chart renderer can display it."""
    fields: List[str] = ["source"]
    for c in candidates:
        for k in (c.get("row") or {}).keys():
            if str(k) not in fields:
                fields.append(str(k))
    data: List[Dict[str, Any]] = []
    for c in candidates:
        nr: Dict[str, Any] = {"source": c.get("table_name") or ""}
        for k, v in (c.get("row") or {}).items():
            nr[str(k)] = v
        data.append(nr)
    return {"fields": fields, "data": data}
