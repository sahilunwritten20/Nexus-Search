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
import re
from dataclasses import dataclass, field
from typing import Optional

from ..core.query_parser import parse_query
from ..core.tokenizer import tokenize

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
# Spelling — Norvig-style, but restricted to the INDEX's vocabulary.
# Corrections come from Storage.all_terms()/document_frequency(), so a
# suggestion can never produce a term the index doesn't contain.
# --------------------------------------------------------------------------

_ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _edits1(word: str) -> set[str]:
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    deletes = [L + R[1:] for L, R in splits if R]
    transposes = [L + R[1] + R[0] + R[2:] for L, R in splits if len(R) > 1]
    replaces = [L + c + R[1:] for L, R in splits if R for c in _ALPHABET]
    inserts = [L + c + R for L, R in splits for c in _ALPHABET]
    return set(deletes + transposes + replaces + inserts)


def correct_spelling(term: str, vocab: set[str], max_edits: int = 2) -> Optional[str]:
    """Best in-vocabulary correction for `term`, or None.

    Rules (both are deliberate, and both are tested):
    - a term that already exists in the index is NEVER "corrected"
    - only candidates within `max_edits` edits are considered
    Preference: closest edit distance wins; ties are broken alphabetically
    so results are deterministic (no corpus-frequency magic)."""
    if not term or term in vocab:
        return None

    best: Optional[tuple[int, int, str]] = None  # (distance, -freq, term)
    frontier = {term}
    seen = {term}
    for dist in range(1, max_edits + 1):
        frontier = {e for w in frontier for e in _edits1(w)} - seen
        seen |= frontier
        hits = frontier & vocab
        if hits:
            # best hit at THIS distance; ties broken alphabetically for determinism
            pick = sorted(hits)[0]
            best = (dist, 0, pick)
            break
    return best[2] if best else None


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


def detect_language(text: str) -> str:
    """Heuristic language detection via stopword overlap.

    Returns "unknown" (never a guess) when the query has fewer than 3 tokens
    or when the best stopword overlap score is 0 or tied. That contract is a
    feature: false language metadata is worse than none for downstream
    language_relevance, which treats "unknown" as neutral.
    """
    tokens = [t for t in tokenize(text) if len(t) > 1]
    if len(tokens) < 3:
        return "unknown"
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _STOPWORDS.items()}
    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "unknown"
    if sorted(scores.values(), reverse=True)[1] == best_score:
        return "unknown"  # tie -> ambiguous
    return best_lang


def detect_intent(text: str) -> str:
    """Heuristic intent: "navigational" | "transactional" | "informational".

    Purely token/rule based, NOT ML — because there is no labeled intent data
    in this project to train/evaluate one against (see SPEC.md's honesty rule).
    Deterministic rules make the behavior unit-testable and reviewable."""
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
    """
    normalized = normalize_query(query)
    parsed = parse_query(normalized)

    corrected: dict[str, str] = {}
    if storage is not None:
        vocab = set(storage.all_terms())
        # Only correct terms with ZERO postings — a term the index knows is
        # never rewritten, even if a more popular near-neighbor exists.
        vocab_terms = {t for t in parsed.terms if storage.document_frequency(t) == 0}
        for term in vocab_terms:
            suggestion = correct_spelling(term, vocab)
            if suggestion:
                corrected[term] = suggestion

    expansion = expand_synonyms(list(corrected.values()) or parsed.terms, synonyms)

    return QueryUnderstanding(
        original=query,
        normalized=normalized,
        corrected_terms=corrected,
        expansion_terms=expansion,
        language=detect_language(normalized),
        intent=detect_intent(normalized),
        entities=extract_entities(query),
    )
