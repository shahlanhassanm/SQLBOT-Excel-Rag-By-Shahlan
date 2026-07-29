"""Pure helpers for the agentic SQL pipeline (hybrid ranking, grading, retry,
decomposition). Mirrors the apex_helpers.py contract: no DB, no LLM, no
settings imports — every function is deterministic and unit-testable.

Integration points live in llm.py / ds_embedding.py; this module only holds
logic that can be tested without a running stack.
"""
import hashlib
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from apps.chat.task.apex_helpers import safe_json_loads

# ---------------------------------------------------------------------------
# Tokenization — bilingual corpus (EN identifiers + CJK prose). ASCII words
# are kept whole; CJK runs are decomposed into character bigrams so that
# embedding-free lexical matching still works for Chinese questions.
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


# ---------------------------------------------------------------------------
# Okapi BM25 (pure Python; corpus sizes here are tiny — tens of datasources)
# ---------------------------------------------------------------------------
def bm25_scores(query_tokens: Sequence[str], docs_tokens: Sequence[Sequence[str]],
                k1: float = 1.5, b: float = 0.75) -> List[float]:
    n_docs = len(docs_tokens)
    if n_docs == 0:
        return []
    if not query_tokens:
        return [0.0] * n_docs

    doc_lens = [len(d) for d in docs_tokens]
    avg_len = (sum(doc_lens) / n_docs) or 1.0

    # document frequency per query term
    q_terms = set(query_tokens)
    df: Dict[str, int] = {t: 0 for t in q_terms}
    for d in docs_tokens:
        d_set = set(d)
        for t in q_terms:
            if t in d_set:
                df[t] += 1

    scores: List[float] = []
    for idx, d in enumerate(docs_tokens):
        tf: Dict[str, int] = {}
        for t in d:
            if t in q_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for t in query_tokens:
            f = tf.get(t, 0)
            if f == 0:
                continue
            idf = math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            denom = f + k1 * (1 - b + b * doc_lens[idx] / avg_len)
            score += idf * (f * (k1 + 1)) / denom
        scores.append(score)
    return scores


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
def rrf_fuse(rank_lists: Sequence[Sequence[Any]], k: int = 60) -> Dict[Any, float]:
    fused: Dict[Any, float] = {}
    for ranking in rank_lists:
        for rank, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank + 1)
    return fused


# ---------------------------------------------------------------------------
# Grader verdict parsing — the grader must NEVER break the pipeline, so any
# unparseable output degrades to acceptance.
# ---------------------------------------------------------------------------
def parse_grader_verdict(text: str) -> Tuple[bool, str]:
    data = safe_json_loads(text or '')
    if not isinstance(data, dict) or not isinstance(data.get('pass'), bool):
        return True, 'unparseable verdict; accepting'
    reason = data.get('reason')
    return data['pass'], reason if isinstance(reason, str) else ''


# ---------------------------------------------------------------------------
# Decomposition parsing — validated, deduped, capped. Anything suspicious
# returns [] which means "run the normal single-datasource pipeline".
# ---------------------------------------------------------------------------
MAX_SUB_QUESTIONS = 5


def parse_decomposition(text: str, valid_ds_ids: Set[int]) -> Dict[str, Any]:
    """Parse the router's verdict into ``{'mode': str, 'subs': [...]}``.

    mode:
      - 'single' : one datasource is enough -> normal pipeline (subs ignored).
      - 'fanout' : the SAME kind of data lives in several datasources; run the
                   (same) question against each and UNION the results.
      - 'split'  : the answer needs DIFFERENT facts from different datasources;
                   run each sub-question and combine.
    Anything unparseable degrades to {'mode':'single','subs':[]}.
    """
    single = {'mode': 'single', 'subs': []}
    data = safe_json_loads(text or '')
    if not isinstance(data, dict):
        return single
    mode = data.get('mode')
    if mode not in ('single', 'fanout', 'split'):
        # backward-compat with the old {"multi": true/false} schema
        mode = 'split' if data.get('multi') is True else 'single'
    if mode == 'single':
        return single
    raw_subs = data.get('subs')
    if not isinstance(raw_subs, list):
        return single
    subs: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, str]] = set()
    for item in raw_subs:
        if not isinstance(item, dict):
            continue
        ds_id = item.get('ds_id')
        question = item.get('question')
        if not isinstance(ds_id, int) or ds_id not in valid_ds_ids:
            continue
        if not isinstance(question, str) or not question.strip():
            continue
        key = (ds_id, question.strip())
        if key in seen:
            continue
        seen.add(key)
        subs.append({'ds_id': ds_id, 'question': question.strip()})
        if len(subs) >= MAX_SUB_QUESTIONS:
            break
    if len(subs) < 2:
        return single
    return {'mode': mode, 'subs': subs}


# ---------------------------------------------------------------------------
# Deterministic UNION of fanout legs into one result table (+ Source column)
# ---------------------------------------------------------------------------
def merge_union(legs: Sequence[Dict[str, Any]], dedup: bool = True) -> Dict[str, Any]:
    """Combine several leg results into one table.

    Each leg is ``{'datasource': name, 'fields': [...], 'data': [row dicts]}``.
    Returns ``{'fields': ['source', <union of all leg fields>],
    'data': [row dicts with a 'source' column]}`` — an outer union, so rows keep
    whatever columns they had and missing columns simply render blank.

    With ``dedup=True`` (default) rows that carry identical content across files
    are collapsed into a single row whose 'source' lists every file it appeared
    in (a comma-separated list of the contributing datasource names). This gives
    a clean "list all unique X" answer while still showing provenance.
    """
    combined_fields: List[str] = ['source']
    for leg in legs:
        for f in (leg.get('fields') or []):
            fl = str(f)
            if fl not in combined_fields:
                combined_fields.append(fl)

    if not dedup:
        rows_out: List[Dict[str, Any]] = []
        for leg in legs:
            label = leg.get('datasource') or ''
            for row in (leg.get('data') or []):
                if not isinstance(row, dict):
                    continue
                nr: Dict[str, Any] = {'source': label}
                for k, v in row.items():
                    if k != 'source':
                        nr[k] = v
                rows_out.append(nr)
        return {'fields': combined_fields, 'data': rows_out}

    # dedup by content (everything except 'source'); merge source labels
    order: List[tuple] = []
    by_key: Dict[tuple, Dict[str, Any]] = {}
    for leg in legs:
        label = leg.get('datasource') or ''
        for row in (leg.get('data') or []):
            if not isinstance(row, dict):
                continue
            content = {k: v for k, v in row.items() if k != 'source'}
            key = tuple(sorted((k, str(v)) for k, v in content.items()))
            if key not in by_key:
                by_key[key] = {'content': content, 'sources': []}
                order.append(key)
            if label and label not in by_key[key]['sources']:
                by_key[key]['sources'].append(label)
    combined_data: List[Dict[str, Any]] = []
    for key in order:
        entry = by_key[key]
        nr = {'source': ', '.join(entry['sources'])}
        nr.update(entry['content'])
        combined_data.append(nr)
    return {'fields': combined_fields, 'data': combined_data}


# ---------------------------------------------------------------------------
# Execution-based self-consistency (MBR-Exec / C3 / MCS-SQL).
#
# Generating one SQL and accepting it because it executed is the weakest link in
# the pipeline: a query that runs and returns plausible rows is indistinguishable
# from a correct one. Executing several independently-generated candidates and
# keeping the RESULT that a plurality agrees on turns "did it run" into "do
# independent attempts agree", which is what the survey's selection methods buy.
# ---------------------------------------------------------------------------
def _canonical_cell(value: Any) -> str:
    """Normalise one cell for comparison.

    Numeric values are canonicalised so 1385049.13, 1385049.130 and
    Decimal('1385049.13') all agree; everything else compares as trimmed text.
    """
    if value is None:
        return '\x00null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if number != number or number in (float('inf'), float('-inf')):  # NaN / inf
        return str(value).strip()
    if number == int(number):
        return str(int(number))
    return f'{round(number, 6):.6f}'.rstrip('0').rstrip('.')


def result_fingerprint(fields: Optional[Sequence[str]], rows: Optional[Sequence[Dict[str, Any]]]) -> str:
    """Stable fingerprint of an execution result's VALUES.

    Column names are excluded: two candidates that alias the same aggregate
    differently ("total" vs "sum_amount") computed the same answer and must
    agree. Row order is also excluded — the multiset of rows is what is hashed —
    because independently generated SQL routinely differs only in ORDER BY.

    The trade-off of order-insensitivity is that two candidates differing ONLY in
    sort direction fingerprint identically; that is accepted deliberately, since
    an ordering disagreement is far less damaging than the wrong-value
    disagreement this is built to catch.
    """
    field_list = list(fields or [])
    row_list = list(rows or [])
    encoded: List[str] = []
    for row in row_list:
        if isinstance(row, dict):
            cells = ([_canonical_cell(row.get(f)) for f in field_list] if field_list
                     else [_canonical_cell(v) for v in row.values()])
        else:
            cells = [_canonical_cell(row)]
        encoded.append('\x1f'.join(cells))
    encoded.sort()
    payload = f'{len(row_list)}\x1e' + '\x1e'.join(encoded)
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


def select_by_consensus(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the candidate whose result the plurality agrees on.

    Each candidate is ``{'sql': str, 'result': {'fields','data'}, 'ok': bool}``;
    only ``ok`` candidates vote. The winner carries ``votes`` and ``total_votes``
    so the caller can log/expose the agreement level.

    Tie-breaking, in order:
      1. more votes wins;
      2. a non-empty result beats an equally-supported empty one — two failed
         candidates both returning zero rows "agree" trivially, and that
         agreement is worth less than one candidate that actually found data;
      3. earliest candidate wins, which is the primary (streamed) answer, so a
         fully tied vote reproduces today's behaviour exactly.
    """
    usable = [(i, c) for i, c in enumerate(candidates or []) if c and c.get('ok')]
    if not usable:
        return None

    groups: Dict[str, Dict[str, Any]] = {}
    for index, candidate in usable:
        result = candidate.get('result') or {}
        key = candidate.get('fingerprint') or result_fingerprint(
            result.get('fields'), result.get('data'))
        group = groups.get(key)
        if group is None:
            groups[key] = {'votes': 1, 'first_index': index, 'candidate': candidate,
                           'non_empty': bool(result.get('data'))}
        else:
            group['votes'] += 1

    ranked = sorted(groups.values(),
                    key=lambda g: (-g['votes'], not g['non_empty'], g['first_index']))
    winner = ranked[0]
    chosen = dict(winner['candidate'])
    chosen['votes'] = winner['votes']
    chosen['total_votes'] = len(usable)
    return chosen


# ---------------------------------------------------------------------------
# Result preview for the grader / synthesizer prompts
# ---------------------------------------------------------------------------
def build_result_preview(fields: Optional[Sequence[str]], rows: Optional[Sequence[Dict[str, Any]]],
                         max_rows: int = 8, max_chars: int = 2000) -> str:
    fields = list(fields or [])
    rows = list(rows or [])
    lines = [f"{len(rows)} row(s) returned. Columns: {', '.join(str(f) for f in fields) or '(none)'}"]
    for row in rows[:max_rows]:
        if isinstance(row, dict):
            cells = [str(row.get(f, '')) for f in fields] if fields else [str(v) for v in row.values()]
        else:
            cells = [str(row)]
        lines.append(' | '.join(cells))
    if len(rows) > max_rows:
        lines.append(f'... ({len(rows) - max_rows} more rows omitted)')
    preview = '\n'.join(lines)
    if len(preview) > max_chars:
        preview = preview[:max_chars] + '…'
    return preview


# ---------------------------------------------------------------------------
# Retry feedback block — matches the existing <error-msg> convention used in
# llm.py __init__ for cross-turn error context.
# ---------------------------------------------------------------------------
def format_retry_feedback(stage: str, detail: str, sql: Optional[str]) -> str:
    sql_block = f"\nThe failing SQL was:\n{sql}\n" if sql else "\n"
    return (f"<error-msg>\nPrevious attempt failed at stage [{stage}]: {detail}{sql_block}"
            f"</error-msg>")


# ---------------------------------------------------------------------------
# Retry-eligibility classification for SingleMessageError messages. ONLY the
# parse-failure markers produced inside LLMService.check_sql are retryable;
# genuine LLM refusals (data['success'] == false) must surface to the user.
# ---------------------------------------------------------------------------
_RETRYABLE_MARKERS = (
    'SQL answer is not a valid json object',
    'Cannot parse sql from answer',
    'SQL query is empty',
)


def is_retryable_single_message(msg: str) -> bool:
    if not msg:
        return False
    return any(marker in msg for marker in _RETRYABLE_MARKERS)


# ---------------------------------------------------------------------------
# Raise an over-small trailing LIMIT for "list all" questions. The model often
# caps list queries at a small N (e.g. LIMIT 10) even when the user asked for
# everything; for retrieval questions we lift the OUTER limit to a full cap so
# the complete answer is returned (display is still capped downstream).
# ---------------------------------------------------------------------------
_TRAILING_LIMIT_RE = re.compile(r'(?is)\blimit\s+(\d+)\s*(offset\s+\d+\s*)?$')


def raise_sql_limit(sql: str, target: int = 1000) -> str:
    """If ``sql`` ends with ``LIMIT n`` (optionally ``OFFSET m``) and n < target,
    rewrite it to ``LIMIT target``. Leaves inner/subquery limits untouched and
    leaves SQL without a trailing limit unchanged."""
    if not sql or not sql.strip():
        return sql
    s = sql.rstrip()
    trailing_semi = s.endswith(';')
    core = s[:-1].rstrip() if trailing_semi else s
    m = _TRAILING_LIMIT_RE.search(core)
    if m:
        try:
            n = int(m.group(1))
        except Exception:
            return sql
        if n < target:
            offset = (' ' + m.group(2).strip()) if m.group(2) else ''
            core = core[:m.start()] + f'LIMIT {target}{offset}'
    return core + (';' if trailing_semi else '')


# English fallbacks used when the caller passes no term lists. The real lists
# come from settings (multilingual, env-overridable) so nothing is hardcoded to
# one language; these defaults only keep the function usable standalone/in tests.
DEFAULT_LISTING_TERMS = ['list', 'show', 'display', 'find', 'all', 'every', 'each', 'get', 'give']
DEFAULT_AGGREGATION_TERMS = ['sum', 'total', 'average', 'avg', 'how many', 'number of', 'count',
                             'max', 'min', 'highest', 'lowest', 'trend', 'growth']


def _matches_any(text: str, terms: Sequence[str]) -> bool:
    """Match terms in ``text``. Latin alphabetic terms match on word boundaries;
    CJK / multi-word terms match as substrings (no word boundaries in CJK)."""
    if not text:
        return False
    low = text.lower()
    for raw in terms:
        t = (raw or '').strip().lower()
        if not t:
            continue
        if t.isascii() and all(c.isalpha() or c == ' ' for c in t) and ' ' not in t:
            if re.search(r'\b' + re.escape(t) + r'\b', low):
                return True
        else:
            if t in low:
                return True
    return False


def is_listing_question(question: str, listing_terms: Optional[Sequence[str]] = None,
                        aggregation_terms: Optional[Sequence[str]] = None) -> bool:
    """True for 'list/show all X' retrieval questions (not aggregations).
    Term lists are injected (from settings) so this is language-agnostic."""
    if not question:
        return False
    listing_terms = listing_terms or DEFAULT_LISTING_TERMS
    aggregation_terms = aggregation_terms or DEFAULT_AGGREGATION_TERMS
    return _matches_any(question, listing_terms) and not _matches_any(question, aggregation_terms)


# A standalone 1-3 digit number reads as an explicit row request ("top 10",
# "前10条", "5 records"); 4-digit tokens (years) are ignored. Language-agnostic.
_SMALL_INT_RE = re.compile(r'(?<!\d)\d{1,3}(?!\d)')


def has_explicit_row_count(question: str) -> bool:
    """True when the user explicitly asked for a specific (small) number of rows,
    in which case the model's LIMIT must be respected rather than raised."""
    if not question:
        return False
    return bool(_SMALL_INT_RE.search(question))
