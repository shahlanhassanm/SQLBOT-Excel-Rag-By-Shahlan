"""LLM header-row fallback — the (opt-in) second opinion for sheets where the
offline heuristic is unsure.

Kept in its own module (not header_detection.py) so the detector stays a pure,
dependency-free utility. This module is imported lazily by
``header_detection.resolve_header_row`` only on the low-confidence path, so a
model call happens for at most the small minority of ambiguous sheets — never
for the easy majority the heuristic already resolves.
"""
import logging

from common.core.config import settings
from apps.datasource.utils.header_detection import (
    DETECT_ROWS, build_header_llm_prompt, parse_header_row_answer)

logger = logging.getLogger(__name__)


def llm_detect_header_row(raw, context: str = ""):
    """Ask a (small, configurable) model which row is the header. Returns a valid
    0-based row index, or None to fall back to the heuristic. Never raises."""
    try:
        n = min(DETECT_ROWS, len(raw))
        if n <= 1:
            return None
        prompt = build_header_llm_prompt(raw, max_rows=settings.HEADER_LLM_MAX_ROWS)
        # Imported here (not at module top) so a missing/misconfigured model never
        # breaks import of the datasource utils package.
        from apps.ai_model.model_factory import invoke_small_llm
        text = invoke_small_llm(prompt, settings.HEADER_LLM_MODEL)
        return parse_header_row_answer(text, n)
    except Exception as e:
        logger.warning("llm_detect_header_row failed for %s: %s", context, e)
        return None
