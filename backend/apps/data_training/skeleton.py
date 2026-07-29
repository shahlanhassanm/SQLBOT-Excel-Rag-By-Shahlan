"""Skeleton-masked few-shot example selection (DAIL-SQL).

Raw question-embedding cosine ranks examples by what they are ABOUT. For
few-shot SQL prompting the useful signal is what they are SHAPED like: an
example that aggregates-and-groups helps a new aggregate-and-group question far
more than an unrelated question about the same table.

DAIL-SQL's finding is that masking the domain-specific tokens out of both the
user question and the candidate questions — leaving the query skeleton — and
ranking on that beats raw question similarity. This module implements the
masking and a pure re-ranking that fuses the skeleton order with the existing
embedding order via Reciprocal Rank Fusion (the same fusion already used for
datasource ranking in ds_embedding.py).

Deliberately a RE-RANKER over already-retrieved rows: no re-embedding, no new
column, no migration. Contract matches agentic.py — pure, no DB/LLM/settings.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set

from apps.chat.task.agentic import bm25_scores, rrf_fuse, tokenize

# Literal-ish spans that carry domain content rather than query shape.
_QUOTED_RE = re.compile(r'''(["'“”‘’`])(?:(?!\1).){1,80}\1''')
# Numbers incl. decimals, thousands separators, percentages and bare years.
_NUMBER_RE = re.compile(r'(?<![\w])[+-]?\d[\d,]*(?:\.\d+)?%?(?![\w])')
# ISO-ish and slash dates, masked before the plain-number rule can shred them.
_DATE_RE = re.compile(r'(?<![\w])\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?(?![\w])')

MASK_VALUE = '<val>'
MASK_NUMBER = '<num>'
MASK_DATE = '<date>'
MASK_DOMAIN = '<col>'


def mask_question(question: str, domain_terms: Optional[Sequence[str]] = None) -> str:
    """Return the query skeleton of ``question``.

    Masks, in order: quoted literals, dates, numbers, then any supplied
    ``domain_terms`` (schema table/column names). Everything left is the
    structural vocabulary — "what is the total X per Y", "how many X are Z" —
    which is what makes two questions call for the same SQL shape.

    ``domain_terms`` is optional because the caller does not always have the
    schema to hand; without it the skeleton is still literal-free, just less
    aggressive.
    """
    if not question:
        return ''
    text = str(question)
    text = _QUOTED_RE.sub(MASK_VALUE, text)
    text = _DATE_RE.sub(MASK_DATE, text)
    text = _NUMBER_RE.sub(MASK_NUMBER, text)

    if domain_terms:
        # Longest first so "order_date" is masked before "order".
        for term in sorted({t for t in domain_terms if t and len(str(t)) >= 3},
                           key=lambda t: len(str(t)), reverse=True):
            term_str = str(term).strip()
            if not term_str:
                continue
            # Underscored/spaced variants of the same identifier both appear in
            # natural questions ("order_date" vs "order date").
            variants = {term_str}
            if '_' in term_str:
                variants.add(term_str.replace('_', ' '))
            for variant in variants:
                text = re.sub(re.escape(variant), MASK_DOMAIN, text, flags=re.IGNORECASE)

    return re.sub(r'\s+', ' ', text).strip()


def schema_domain_terms(schema_str: str, max_terms: int = 400) -> List[str]:
    """Pull table and column names out of a prompt schema string for masking.

    Kept here (rather than importing apex_helpers.parse_schema) so this module
    stays usable on a bare identifier list too; falls back to [] on any input it
    does not recognise.
    """
    if not schema_str:
        return []
    terms: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(r'^#\s*Table:\s*([^\s,]+)', schema_str, re.MULTILINE):
        name = match.group(1).split('.')[-1].strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            terms.append(name)
    for match in re.finditer(r'\(\s*([^:()\s][^:()]*?)\s*:', schema_str):
        name = match.group(1).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            terms.append(name)
        if len(terms) >= max_terms:
            break
    return terms[:max_terms]


def rerank_by_skeleton(question: str,
                       candidates: Sequence[Dict[str, Any]],
                       top_k: int,
                       domain_terms: Optional[Sequence[str]] = None,
                       key: str = 'question') -> List[Dict[str, Any]]:
    """Re-rank ``candidates`` by skeleton similarity fused with their input order.

    ``candidates`` must already be ordered best-first by the embedding/cosine
    stage — that order is one of the two rankings fused. Each candidate is a
    dict carrying the example's natural-language question under ``key``.

    Returns the top ``top_k`` candidates. Falls back to ``candidates[:top_k]``
    on any internal error, so a masking bug degrades to today's behaviour rather
    than emptying the few-shot block.
    """
    if top_k <= 0:
        return []
    items = list(candidates)
    if len(items) <= 1:
        return items[:top_k]

    try:
        q_skeleton = mask_question(question, domain_terms)
        q_tokens = tokenize(q_skeleton)
        if not q_tokens:
            return items[:top_k]

        doc_tokens = [tokenize(mask_question(str(c.get(key) or ''), domain_terms)) for c in items]
        lex_scores = bm25_scores(q_tokens, doc_tokens)

        # Index positions stand in for identity: candidates need no stable id and
        # duplicate questions stay distinguishable.
        embedding_order = list(range(len(items)))
        # Only candidates with a real skeleton overlap join the second ranking;
        # an all-zero lexical ranking would otherwise inject arbitrary input
        # order into the fusion (same guard as ds_embedding._hybrid_rerank).
        scored = [(s, i) for s, i in zip(lex_scores, embedding_order) if s > 0]
        rank_lists: List[List[int]] = [embedding_order]
        if scored:
            rank_lists.append([i for _, i in sorted(scored, key=lambda p: p[0], reverse=True)])

        fused = rrf_fuse(rank_lists)
        ordered = sorted(embedding_order, key=lambda i: fused.get(i, 0.0), reverse=True)
        return [items[i] for i in ordered[:top_k]]
    except Exception:
        return items[:top_k]
