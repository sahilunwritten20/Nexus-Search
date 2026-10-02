"""Query understanding for Nexus Search Phase 5 — fully offline, stdlib-only.

Each stage is a pure function; `understand_query()` composes them into one
dataclass that `HybridSearch.search_page()` can OPTIONALLY consume
(opt-in — existing behavior is unchanged when it is not passed).

What this deliberately is NOT: no ML models, no network calls, no NER model
quality. Spell correction, language detection, intent and entities are all
transparent heuristics built on data the index already has (the postings
vocabulary) or tiny embedded wordlists. They are deterministic and unit-testable;
that determinism is the reason to prefer them over a dependency at prototype scale.
"""
import heapq
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..core.query_parser import parse_query
from ..core.tokenizer import tokenize


# understand_query()'s spell-correction needs the index vocabulary; loading
# it per request scanned the whole vocabulary table every time. Memoize per
# storage object, invalidated when document_count changes (same honesty
# window as Suggester._refresh: a same-count content swap keeps a now-stale
# term, which merely means a typo correction we can't offer — never a wrong
# result being served).
#
# BUG-10: WeakKeyDictionary — keyed by the storage OBJECT, not id(), so a
# dead store's entry dies with it. id()-keyed caches live forever and a
# reused address could serve one store's vocabulary to another (observed
# as full-suite flakiness: corrected_terms came back empty).
import weakref
_VOCAB_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_VOCAB_CACHE_LOCK = threading.Lock()


def _vocabulary(storage) -> frozenset:
    doc_count = storage.document_count()
    with _VOCAB_CACHE_LOCK:
        cached = _VOCAB_CACHE.get(storage)
        if cached is not None and cached[0] == doc_count:
            return cached[1]
    vocab = frozenset(storage.all_terms())
    with _VOCAB_CACHE_LOCK:
        _VOCAB_CACHE[storage] = (doc_count, vocab)
    return vocab

# A tiny built-in synonym map. Interface accepts any dict[str, list[str]], so a
# file-backed map can be plugged in later without changing call sites.
SynonymMap = dict[str, list[str]]

DEFAULT_SYNONYMS: SynonymMap = {
    "car": ["automobile", "vehicle"],
    "fast": ["quick", "speedy"],
    "big": ["large", "huge"],
    "buy": ["purchase", "price"],
    "bug": ["error", "defect"],
    "ml": ["machine", "learning"],
    "ai": ["artificial", "intelligence"],
}

# Pocket stopword lists — just enough for heuristic language DETECTION on
# queries (not documents). Not a replacement for a real detector.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the a an and or of to in is are was for on with as at by it this that you i we".split()),
    "fr": frozenset("le la les de des un une et ou dans pour avec est sont je tu il".split()),
    "de": frozenset("der die das und oder in von zu ist sind ich du mit auf fur nicht".split()),
    "es": frozenset("el la los las de del un una y o en para con es son yo tu".split()),
}

_QUESTION_WORDS = {"what", "who", "where", "when", "why", "how", "which", "is", "are", "does", "do", "can"}
_TRANSACTIONAL = {"buy", "price", "cheap", "deal", "discount", "order", "shop", "sale", "coupon", "purchase"}
_NAVIGATIONAL = {"login", "signin", "sign", "official", "homepage", "site", "download", "app"}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_query(query: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation noise.

    PRESERVES the quoted-phrase and type:/lang: filter syntax: the string
    is parsed by parse_query() first and its filters/phrases are rendered
    back out unchanged; only the free-text part is punctuation-stripped.
    """
    parsed = parse_query(query.strip().lower())
    parts: list[str] = []
    for phrase in parsed.phrases:
        cleaned = re.sub(r"[^\w\s-]", " ", phrase)
        cleaned = " ".join(cleaned.split())
        if cleaned:
            parts.append(f'"{cleaned}"')
    for key, value in sorted(parsed.filters.items()):
        rendered = f"type:{value}" if key == "doc_type" else f"lang:{value}"
        parts.append(rendered)
    free = re.sub(r"[^\w\s-]", " ", " ".join(t for t in parsed.terms))
    parts.append(" ".join(free.split()))
    return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------
# Spelling — bounded in-vocabulary correction (BUG-01 remediation).
#
# Corrections come from the INDEX's vocabulary only: a suggestion can never
# produce a term the index doesn't contain. The pre-fix code expanded the
# full distance-2 edit frontier per out-of-vocabulary term (~O(len^2·26^2)
# strings), which a 2,000-char junk query turned into ~43 s of CPU on the
# open /search endpoint. The bounded version below is equivalent on hits
# (same edit model, same closest-then-alphabetical preference) but scans
# the vocabulary's length window once per term with an early-exit distance
# check, under a per-query comparison budget — a few milliseconds for a
# real typo, bounded regardless of input.
# --------------------------------------------------------------------------

def _env_positive_int(name: str, default: int) -> int:
    """Env knob parsed per call (tests may flip it without a module reload).
    Zero/negative/garbage values fall back to the default — a broken knob
    must not silently disable correction entirely."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _spell_max_term_len() -> int:
    """Terms longer than this are never corrected (default 20)."""
    return _env_positive_int("NEXUS_SPELL_MAX_TERM_LEN", 20)


def _spell_d2_max_term_len() -> int:
    """Distance-2 correction only for terms up to this length (default 12)."""
    return _env_positive_int("NEXUS_SPELL_D2_MAX_TERM_LEN", 12)


def _spell_max_corrected_terms() -> int:
    """At most this many terms corrected per query (default 8)."""
    return _env_positive_int("NEXUS_SPELL_MAX_CORRECTED_TERMS", 8)


def _spell_candidate_budget() -> int:
    """Total vocabulary comparisons allowed per query (default 4000)."""
    return _env_positive_int("NEXUS_SPELL_CANDIDATE_BUDGET", 4_000)


def _length_index(vocab) -> dict[int, list[str]]:
    """Vocabulary bucketed by length; buckets sorted for deterministic scans."""
    index: dict[int, list[str]] = {}
    for term in vocab:
        index.setdefault(len(term), []).append(term)
    for bucket in index.values():
        bucket.sort()
    return index


def _osa_within(term: str, word: str, max_dist: int) -> bool:
    """True iff restricted-Damerau (optimal string alignment) distance
    between `term` and `word` is <= max_dist. Adjacent transpositions count
    as one edit — the same edit model the original expansion used.

    Bails row-by-row the moment the row minimum exceeds max_dist, so
    unrelated words cost ~len(term) work instead of len^2."""
    if abs(len(term) - len(word)) > max_dist:
        return False
    if max_dist <= 0:
        return term == word
    prev2 = None
    prev = list(range(len(word) + 1))
    for i in range(1, len(term) + 1):
        cur = [i] + [0] * len(word)
        row_min = i
        for j in range(1, len(word) + 1):
            cost = 0 if term[i - 1] == word[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and term[i - 1] == word[j - 2]
                    and term[i - 2] == word[j - 1]):
                value = min(value, prev2[j - 2] + 1)
            cur[j] = value
            if value < row_min:
                row_min = value
        if row_min > max_dist:
            return False
        prev2, prev = prev, cur
    return prev[-1] <= max_dist


def _correct_bounded(term: str, index: dict[int, list[str]], max_edits: int,
                     budget: list[int]) -> tuple[Optional[str], bool]:
    """(suggestion | None, exhausted). Distance-1 hits beat distance-2 hits;
    ties break alphabetically (the window is scanned in sorted order).

    `budget` is a one-element [remaining_comparisons] shared across a whole
    query; each vocabulary comparison costs 1. On exhaustion the partial scan
    is DISCARDED (None returned) — a deterministic, honest abort rather than
    an arbitrary partial answer."""
    window = heapq.merge(*(
        index.get(length, ())
        for length in range(len(term) - max_edits, len(term) + max_edits + 1)
    ))
    hits1: list[str] = []
    hits2: list[str] = []
    for word in window:
        if budget[0] <= 0:
            return None, True
        budget[0] -= 1
        if _osa_within(term, word, 1):
            hits1.append(word)
        elif max_edits >= 2 and _osa_within(term, word, 2):
            hits2.append(word)
    if hits1:
        return hits1[0], False  # scanned sorted: first hit is alphabetical-first
    if hits2:
        return hits2[0], False
    return None, False


def correct_spelling(term: str, vocab: set[str], max_edits: int = 2) -> Optional[str]:
    """Best in-vocabulary correction for `term`, or None.

    Rules (both deliberate, and both tested):
    - a term that already exists in the index is NEVER "corrected"
    - only candidates within `max_edits` (restricted Damerau) are considered
    Preference: closest edit distance wins; ties are broken alphabetically
    so results are deterministic (no corpus-frequency magic).

    Bounds (BUG-01): terms above NEXUS_SPELL_MAX_TERM_LEN are never corrected;
    distance-2 applies only up to NEXUS_SPELL_D2_MAX_TERM_LEN; the scan is
    capped by NEXUS_SPELL_CANDIDATE_BUDGET per query (see understand_query)."""
    if not term or term in vocab:
        return None
    if len(term) > _spell_max_term_len():
        return None
    budget = [_spell_candidate_budget()]
    suggestion, _exhausted = _correct_bounded(
        term, _length_index(vocab), max_edits, budget)
    return suggestion


def expand_synonyms(terms: list[str], synonyms: Optional[SynonymMap] = None) -> list[str]:
    """ADDITIONAL candidate terms from the synonym map.

    Expansion-only by contract: the original terms are never removed or
    reordered, so snippets/highlighting (which operate on the original parsed
    query) are unaffected. Synonyms inherit lowercasing; unknown terms map to
    nothing.
    """
    synonyms = DEFAULT_SYNONYMS if synonyms is None else synonyms
    out: list[str] = []
    seen = set(terms)
    for term in terms:
        for syn in synonyms.get(term, []):
            token = syn.lower()
            if token not in seen:
                seen.add(token)
                out.append(token)
    return out


def detect_language(text: str, tokens: Optional[list[str]] = None) -> str:
    """Heuristic language detection via stopword overlap.

    Returns "unknown" (never a guess) when the query has fewer than 3 tokens
    or when the best stopword overlap score is 0 or tied. That contract is a
    feature: false language metadata is worse than none for downstream
    language_relevance, which treats "unknown" as neutral.

    `tokens` may be passed by a caller that already tokenized `text`
    (understand_query shares one tokenization across stages)."""
    if tokens is None:
        tokens = tokenize(text)
    tokens = [t for t in tokens if len(t) > 1]
    if len(tokens) < 3:
        return "unknown"
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _STOPWORDS.items()}
    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "unknown"
    if sorted(scores.values(), reverse=True)[1] == best_score:
        return "unknown"  # tie -> ambiguous
    return best_lang


def detect_intent(text: str, tokens: Optional[list[str]] = None) -> str:
    """Heuristic intent: "navigational" | "transactional" | "informational".

    Purely token/rule based, NOT ML — because there is no labeled intent data
    in this project to train/evaluate one against (see SPEC.md's honesty rule).
    Deterministic rules make the behavior unit-testable and reviewable.

    `tokens` may be passed by a caller that already tokenized `text`."""
    if tokens is None:
        tokens = tokenize(text)
    token_set = set(tokens)
    if token_set & _TRANSACTIONAL:
        return "transactional"
    # single-token domain-like query ("example.com") — note the tokenizer
    # drops the dot, so match the raw text, not the tokens
    domain_like = bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", text.strip().lower()))
    if token_set & _NAVIGATIONAL or domain_like:
        return "navigational"
    if tokens and tokens[0] in _QUESTION_WORDS:
        return "informational"
    if " vs " in f" {text.lower()} " or "versus" in token_set:
        return "informational"  # comparison queries are informational
    return "informational"


def extract_entities(query: str) -> list[str]:
    """Pattern-based entity candidates: quoted phrases + capitalized sequences.

    Honest scope (docstring-as-contract): this catches quoted phrases (which
    parse_query already surfaces) like "machine learning" and simple proper-noun
    shapes like "New York" or "John Smith". It does NOT catch lowercase
    entities, nested entities, or do any linking/disambiguation — that needs a
    real NER model, which this project deliberately doesn't depend on."""
    parsed = parse_query(query)
    entities: list[str] = list(parsed.phrases)
    seen = set(entities)
    # Capitalized multi-word sequences in the free text (not inside quotes).
    for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", query):
        if match.group(1).lower() not in seen:
            seen.add(match.group(1).lower())
            entities.append(match.group(1))
    return entities


@dataclass
class QueryUnderstanding:
    """Structured result of understand_query(); consumed opt-in by search."""
    original: str
    normalized: str
    corrected_terms: dict[str, str] = field(default_factory=dict)  # original -> correction
    expansion_terms: list[str] = field(default_factory=list)
    language: str = "unknown"
    intent: str = "informational"
    entities: list[str] = field(default_factory=list)
    # True when the spell-correction budget ran out mid-query: the query is
    # returned UN-rewritten for the un-corrected terms (never an error). The
    # flag is informational — retrieval still works on the raw terms.
    spelling_exhausted: bool = False

    @property
    def effective_terms(self) -> list[str]:
        """Original terms, with out-of-vocabulary terms replaced by in-index
        corrections (the only allowed replacement), then synonym expansions
        appended. What a retrieval layer should see."""
        parsed = parse_query(self.original)
        out = []
        for term in parsed.terms:
            out.append(self.corrected_terms.get(term, term))
        out.extend(self.expansion_terms)
        return out

    def to_retrieval_query(self) -> str:
        """Render back to query-string form for candidate generation, keeping
        the original phrases and filters verbatim while terms are the
        corrected/expanded effective set. Snippets/highlighting still render
        raw document text (never synonym-substituted text), so this only
        widens WHICH documents are candidates."""
        parsed = parse_query(self.original)
        parts = [f'"{p}"' for p in parsed.phrases]
        parts += [f"type:{v}" if k == "doc_type" else f"lang:{v}"
                  for k, v in sorted(parsed.filters.items())]
        parts.extend(self.effective_terms)
        return " ".join(parts)


def understand_query(query: str, storage=None, synonyms: Optional[SynonymMap] = None) -> QueryUnderstanding:
    """Compose all query-understanding stages.

    `storage` may be None: then spelling correction is skipped (vocabulary
    is unavailable) and the function still returns a valid QueryUnderstanding.

    Spell correction is BOUNDED (BUG-01): at most NEXUS_SPELL_MAX_CORRECTED_TERMS
    terms are corrected, under one shared NEXUS_SPELL_CANDIDATE_BUDGET
    comparison budget. On exhaustion the remaining terms stay un-rewritten and
    `spelling_exhausted` is True — never an exception, never a slower answer.
    """
    normalized = normalize_query(query)
    parsed = parse_query(normalized)

    corrected: dict[str, str] = {}
    spelling_exhausted = False
    if storage is not None and parsed.terms:
        vocab = set(_vocabulary(storage))
        # Only correct terms with ZERO postings — a term the index knows is
        # never rewritten, even if a more popular near-neighbor exists.
        # One batched query for every term (BUG-01: per-term COUNT(*)s
        # were measurable on long queries).
        df = storage.document_frequencies(list(set(parsed.terms)))
        oov = sorted({t for t in parsed.terms if df.get(t, 0) == 0})
        if oov:
            index = _length_index(vocab)
            budget = [_spell_candidate_budget()]
            max_corrected = _spell_max_corrected_terms()
            # attempts bounded too: junk terms produce no corrections, so a
            # success-only cap would still walk every OOV term (BUG-01).
            # Matching the correction cap: 8 attempts finds 8 typos — more
            # than any real query carries.
            max_attempts = max_corrected
            d2_len = _spell_d2_max_term_len()
            attempts = 0
            for term in oov:
                if len(corrected) >= max_corrected or attempts >= max_attempts:
                    break
                attempts += 1
                max_edits = 2 if len(term) <= d2_len else 1
                suggestion, exhausted = _correct_bounded(term, index, max_edits, budget)
                if suggestion:
                    corrected[term] = suggestion
                if exhausted:
                    spelling_exhausted = True
                    break

    expansion = expand_synonyms(list(corrected.values()) or parsed.terms, synonyms)
    # one tokenization shared by language + intent detection (BUG-01: the
    # per-stage tokenize of a 2,000-char query was measurable CPU)
    norm_tokens = tokenize(normalized)

    return QueryUnderstanding(
        original=query,
        normalized=normalized,
        corrected_terms=corrected,
        expansion_terms=expansion,
        language=detect_language(normalized, tokens=norm_tokens),
        intent=detect_intent(normalized, tokens=norm_tokens),
        entities=extract_entities(query),
        spelling_exhausted=spelling_exhausted,
    )
