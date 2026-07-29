"""Pure helpers turning a source row into the text we embed.

Whole-row embedding: every non-empty column (text AND numeric) is included,
formatted as ``"col: value | col: value"``. No DB or model dependency here.
"""
from typing import Any, Dict, List


def build_row_content_text(row: Dict[str, Any], max_chars: int = 4000) -> str:
    parts: List[str] = []
    for key, value in row.items():
        if value is None:
            continue
        s = str(value).strip()
        if s == "":
            continue
        parts.append(f"{key}: {s}")
    # Truncate by dropping whole trailing entries so the result is always a
    # well-formed "col: value | col: value" string. fallback.py parses this
    # back by splitting on " | " / ": ", so a mid-entry slice would corrupt the
    # reconstructed display row.
    if max_chars is not None:
        while len(parts) > 1 and len(" | ".join(parts)) > max_chars:
            parts.pop()
    text = " | ".join(parts)
    if max_chars is not None and len(text) > max_chars:
        # a single entry still over budget: hard-cap it (last resort)
        text = text[:max_chars]
    return text


def build_rows_content_text(rows: List[Dict[str, Any]], max_chars: int = 4000) -> List[str]:
    return [build_row_content_text(r, max_chars=max_chars) for r in rows]
