"""Value linking: resolve the literals in a question to real cell values.

A question like "how many transactions are Paid vs Overdue" needs
``WHERE status = 'Paid'``, but the prompt only ever showed the model column
NAMES and a handful of sample rows. The literal itself — its exact spelling,
casing, and which column it lives in — is guessed. When the guess is wrong the
SQL executes perfectly and returns zero rows or a wrong count, which is the
failure mode that silently produces a confident wrong answer.

This module retrieves the actual matching cell values BEFORE generation and
shows them to the model, the pre-generation half of what CHESS calls cell
retrieval. The repo already had semantic row retrieval, but only as a fallback
AFTER SQL generation had failed — too late to influence the WHERE clause.

Everything here is pure and stdlib-only: the caller supplies the column values
(see apps/datasource/value_index.py for the bounded, cached fetch), so matching
is unit-testable with no database.

Cost control: `has_candidate_terms` is the gate. A question with no literal-ish
span ("what is the total revenue?") returns False and the caller skips the whole
stage, so aggregate questions pay nothing.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# --- candidate literal spans in a question ---------------------------------
# Quoted spans are unambiguous literals.
_QUOTED_RE = re.compile(r'''["'“”‘’`]([^"'“”‘’`]{1,60})["'“”‘’`]''')
# A capitalised word, optionally continuing into a multi-word proper noun
# ("GreenGrid Energy", "North America"). Leading-word-of-sentence noise is
# filtered later by the stopword check.
_PROPER_RE = re.compile(r'\b[A-Z][A-Za-z0-9&./-]*(?:\s+[A-Z][A-Za-z0-9&./-]*){0,3}\b')
# Tokens that are clearly identifiers/codes rather than prose: R&D, Q3, ACME-1.
_CODE_RE = re.compile(r'\b(?=[A-Za-z0-9&/-]*[&/-])[A-Za-z0-9&/-]{2,20}\b')
# Runs of 2+ characters in a script that does not delimit words with spaces.
# Covers CJK ideographs and Extension A, Japanese kana, and Hangul syllables +
# Jamo. Hangul was missing, so Korean questions produced no terms at all even
# though the AGENTIC_*_KEYWORDS settings ship Korean.
_CJK_RE = re.compile(
    r'[一-鿿㐀-䶿぀-ヿ가-힣ᄀ-ᇿ]{2,20}')
# Words in a space-delimited script. Used by the case-insensitive fallback pass.
# The apostrophe is allowed only between letters so "O'Brien" survives while the
# closing quote of `status is 'Paid'` is not glued onto the word.
_WORD_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9&./-]|'(?=[A-Za-z]))*")

# Question-shaped words that are capitalised only by sentence position or are
# too generic to be a cell value. Kept small and structural on purpose — this is
# not a domain word list.
_STOPWORDS: Set[str] = {
    'what', 'which', 'who', 'whom', 'whose', 'when', 'where', 'why', 'how',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'do', 'does',
    'did', 'has', 'have', 'had', 'can', 'could', 'should', 'would', 'will',
    'show', 'list', 'give', 'find', 'get', 'display', 'tell', 'me', 'my',
    'all', 'every', 'each', 'and', 'or', 'not', 'of', 'in', 'on', 'at', 'to',
    'for', 'from', 'by', 'with', 'per', 'vs', 'versus', 'between', 'total',
    'sum', 'count', 'average', 'avg', 'max', 'min', 'top', 'highest', 'lowest',
    'many', 'much', 'number', 'amount', 'value', 'values', 'data', 'dataset',
    'table', 'row', 'rows', 'column', 'columns', 'record', 'records', 'it',
    'they', 'there', 'that', 'this', 'these', 'those', 'please',
}

_MIN_TERM_LEN = 2
_MAX_TERM_WORDS = 4
# Ceiling on the case-insensitive fallback pass so a long question cannot flood
# the term list. Terms are matched in memory against values already fetched --
# the DB probes are driven by column selection, not by term count -- so the cost
# of an extra term is CPU only, but the noise floor still matters.
_MAX_FALLBACK_TERMS = 8


def _clean_term(term: str) -> str:
    return re.sub(r'\s+', ' ', str(term or '')).strip(' \t\n.,;:!?')


def extract_candidate_terms(question: str,
                            extra_stopwords: Optional[Iterable[str]] = None,
                            include_content_words: bool = False) -> List[str]:
    """Literal-ish spans of ``question``, best-signal first.

    Quoted spans rank ahead of proper nouns, which rank ahead of codes and
    non-space-delimited script runs, which rank ahead of the case-insensitive
    fallback. Deduplicated case-insensitively, order preserved.

    ``extra_stopwords`` lets the caller fold in the configured, multilingual
    AGENTIC_*_KEYWORDS without this module importing settings — it stays pure
    and unit-testable with no database and no config.

    ``include_content_words`` enables the case-insensitive fallback pass, which
    is what makes lowercase questions ("orders from acme corp") yield anything
    at all. It is OFF by default because without schema context there is no way
    to tell a value from a column name, and "what is the total revenue" would
    become a probe — defeating the cost gate this module documents. Callers that
    can supply the schema's identifiers in ``extra_stopwords`` should turn it on;
    ``build_value_hints`` does exactly that.
    """
    if not question:
        return []

    stopwords = _STOPWORDS
    if extra_stopwords:
        stopwords = _STOPWORDS | {
            str(w).strip().casefold() for w in extra_stopwords if str(w).strip()}

    ordered: List[str] = []
    seen: Set[str] = set()

    def _add(raw: str) -> bool:
        term = _clean_term(raw)
        if len(term) < _MIN_TERM_LEN:
            return False
        if len(term.split()) > _MAX_TERM_WORDS:
            return False
        if term.casefold() in stopwords:
            return False
        # a bare number is not a value hint worth linking
        if re.fullmatch(r'[\d.,%-]+', term):
            return False
        key = term.casefold()
        if key in seen:
            return False
        seen.add(key)
        ordered.append(term)
        return True

    for match in _QUOTED_RE.finditer(question):
        _add(match.group(1))
    for match in _PROPER_RE.finditer(question):
        span = _clean_term(match.group(0))
        _add(span)
        # A multi-word proper noun is also worth trying word-by-word: the cell
        # value may be just "GreenGrid" while the question says "GreenGrid Energy".
        words = span.split()
        if len(words) > 1:
            for word in words:
                _add(word)
    for match in _CODE_RE.finditer(question):
        _add(match.group(0))
    # A space-delimited script (Korean) yields one run per word, which is what we
    # want. A non-delimited one (Chinese) yields the whole clause as a single run
    # -- that is left intact rather than n-gram swept, because score_term_value's
    # containment branch already matches a shorter cell value inside a longer
    # term (北京分公司 inside the clause scores ~0.87, above the 0.72 floor).
    # Sweeping n-grams instead produced ~37 junk terms per question.
    for match in _CJK_RE.finditer(question):
        _add(match.group(0))

    # Fallback: content words, case-insensitively. Everything above needs either
    # a quote, a capital, a symbol or a non-Latin script, so "orders from acme
    # corp" -- ordinary lowercase chat input -- previously produced NO terms and
    # value linking silently did nothing. Runs last and is capped so it only
    # tops up the list rather than dominating it.
    if not include_content_words:
        return ordered

    added = 0
    tokens = [_clean_term(m.group(0)) for m in _WORD_RE.finditer(question)]

    def _is_content(word: str) -> bool:
        return len(word) >= _MIN_TERM_LEN and word.casefold() not in stopwords

    # Pairs first: a two-word entity ("acme corp") is a better value candidate
    # than either word alone. Both halves must be adjacent in the ORIGINAL text
    # -- pairing after dropping stopwords would join "orders" to "acme" across
    # the "from" that separates them.
    for first, second in zip(tokens, tokens[1:]):
        if added >= _MAX_FALLBACK_TERMS:
            break
        if _is_content(first) and _is_content(second) and _add(f'{first} {second}'):
            added += 1
    for word in tokens:
        if added >= _MAX_FALLBACK_TERMS:
            break
        if _is_content(word) and _add(word):
            added += 1

    return ordered


def has_candidate_terms(question: str,
                        extra_stopwords: Optional[Iterable[str]] = None,
                        include_content_words: bool = False) -> bool:
    """Gate for the whole stage: is there any literal here worth resolving?"""
    return bool(extract_candidate_terms(question, extra_stopwords,
                                        include_content_words))


# --- matching ---------------------------------------------------------------
def _trigrams(text: str) -> Set[str]:
    s = re.sub(r'\s+', ' ', (text or '').casefold()).strip()
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def score_term_value(term: str, value: str) -> float:
    """Similarity of a question term to a cell value, in [0, 1].

    Exact (case-insensitive) equality scores 1.0; containment in either
    direction scores high; everything else falls back to a character-level
    ratio. Values that share no character trigram with the term score 0 without
    running the expensive comparison.
    """
    term_norm = _clean_term(term).casefold()
    value_norm = _clean_term(value).casefold()
    if not term_norm or not value_norm:
        return 0.0
    if term_norm == value_norm:
        return 1.0
    if value_norm in term_norm or term_norm in value_norm:
        # Long values containing a short common term ("a" in "Canada") must not
        # score high; scale containment by the length ratio.
        shorter, longer = sorted((len(term_norm), len(value_norm)))
        return 0.80 + 0.19 * (shorter / longer)
    if not (_trigrams(term_norm) & _trigrams(value_norm)):
        return 0.0
    return SequenceMatcher(None, term_norm, value_norm).ratio()


def match_values(terms: Sequence[str],
                 values_by_column: Dict[Tuple[str, str], Sequence[str]],
                 min_score: float = 0.72,
                 max_hints: int = 20,
                 max_values_per_column: int = 2000) -> List[Dict[str, Any]]:
    """Match question terms against candidate cell values.

    ``values_by_column`` maps ``(table, column)`` to that column's distinct
    values. Returns hints sorted by descending score::

        [{'table': t, 'column': c, 'value': v, 'term': term, 'score': 0.94}, ...]

    At most one hint per (column, value) and at most ``max_hints`` overall.
    """
    if not terms or not values_by_column:
        return []

    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for (table, column), values in values_by_column.items():
        for value in list(values or [])[:max_values_per_column]:
            if value is None:
                continue
            value_str = str(value)
            if not value_str.strip():
                continue
            for term in terms:
                score = score_term_value(term, value_str)
                if score < min_score:
                    continue
                key = (str(table), str(column), value_str)
                existing = best.get(key)
                if existing is None or score > existing['score']:
                    best[key] = {'table': str(table), 'column': str(column),
                                 'value': value_str, 'term': term, 'score': round(score, 4)}

    hints = sorted(best.values(), key=lambda h: (-h['score'], h['table'], h['column'], h['value']))
    return hints[:max_hints]


def format_value_hints(hints: Sequence[Dict[str, Any]]) -> str:
    """Render hints as a prompt block, grouped by column.

    Phrased as a factual statement of where each literal actually lives, with an
    explicit instruction to copy the value verbatim — the entire point is that
    the model's own spelling of the literal is what we are correcting.
    """
    if not hints:
        return ''

    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    order: List[Tuple[str, str]] = []
    for hint in hints:
        key = (hint.get('table', ''), hint.get('column', ''))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(hint)

    lines: List[str] = [
        '<value-hints>',
        'Terms in the question were matched against the ACTUAL cell values in the '
        'database. Use these exact values in WHERE/filter conditions — do not '
        'invent or re-case them:',
    ]
    for table, column in order:
        values = []
        seen: Set[str] = set()
        for hint in grouped[(table, column)]:
            value = hint.get('value', '')
            if value in seen:
                continue
            seen.add(value)
            values.append(f'"{value}"')
        lines.append(f'  - {table}.{column} contains: ' + ', '.join(values))
    lines.append('</value-hints>')
    return '\n'.join(lines)


# --- column selection -------------------------------------------------------
# Type names that can hold a literal worth linking, matched as substrings so
# every dialect's spelling is covered (varchar / character varying / nvarchar2 /
# String / TEXT ...).
_TEXT_TYPE_MARKERS = ('char', 'text', 'string', 'enum', 'clob', 'nvarchar', 'varchar')


def is_text_column(field_type: Optional[str]) -> bool:
    if not field_type:
        return False
    lowered = str(field_type).casefold()
    return any(marker in lowered for marker in _TEXT_TYPE_MARKERS)


def select_probe_columns(tables: Sequence[Dict[str, Any]],
                         max_tables: int,
                         max_columns: int) -> List[Tuple[str, str]]:
    """Choose the (table, column) pairs worth probing for values.

    ``tables`` is ``[{'table_name': str, 'fields': [{'name','type'}, ...]}, ...]``
    in schema order. Text columns only, round-robin across tables so a single
    wide table cannot consume the whole budget.
    """
    if max_tables <= 0 or max_columns <= 0:
        return []

    per_table: List[List[Tuple[str, str]]] = []
    for table in list(tables)[:max_tables]:
        name = str(table.get('table_name') or '')
        if not name:
            continue
        columns = [(name, str(f.get('name')))
                   for f in (table.get('fields') or [])
                   if f.get('name') and is_text_column(f.get('type'))]
        if columns:
            per_table.append(columns)

    selected: List[Tuple[str, str]] = []
    depth = 0
    while len(selected) < max_columns:
        progressed = False
        for columns in per_table:
            if depth < len(columns):
                selected.append(columns[depth])
                progressed = True
                if len(selected) >= max_columns:
                    break
        if not progressed:
            break
        depth += 1
    return selected
