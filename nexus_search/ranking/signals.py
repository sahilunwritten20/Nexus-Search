"""Ranking signals for Nexus Search Phase 5.

Each `compute_<signal>(doc, query, context)` is a pure function returning a
float in [0, 1]. `doc` is the stored core.storage.Document (may be None —
every function must handle that); `query` is the raw query string; `context`
is a ranking.features.SignalContext carrying the Phase 4 hybrid result, the
Stage 1 query understanding, and ranking knobs.

Honesty notes (per SPEC.md):
- source_authority / popularity / click_signal have NO real data source yet
  (link graph is Phase 6, click logs are Phase 7). They return a configured
  NEUTRAL default unless doc.metadata carries an explicit override key — the
  interface is ready for real data later without changing these signatures.
"""
import math
import time
from typing import Optional

from ..core.tokenizer import tokenize

NEUTRAL = 0.5  # "no information" must never look like a penalty


def _terms_of(query: str) -> list[str]:
    return tokenize(query)


def compute_bm25_score(doc, query: str, context) -> float:
    """BM25 contribution from the ALREADY-COMPUTED hybrid result — never
    recomputed. context.bm25_contribution (0..1-ish) or None -> NEUTRAL."""
    value = getattr(context, "bm25_contribution", None)
    return NEUTRAL if value is None else max(0.0, min(1.0, float(value)))


def compute_semantic_similarity(doc, query: str, context) -> float:
    """Vector contribution from the ALREADY-COMPUTED hybrid result."""
    value = getattr(context, "vector_contribution", None)
    return NEUTRAL if value is None else max(0.0, min(1.0, float(value)))


def compute_title_match(doc, query: str, context) -> float:
    """Share of distinct query terms present in the document title."""
    terms = set(_terms_of(query))
    if doc is None or not terms:
        return 0.0
    title_terms = set(tokenize(doc.title or ""))
    return sum(1 for t in terms if t in title_terms) / len(terms)


def compute_url_match(doc, query: str, context) -> float:
    """Share of distinct query terms present in the document URL (if any).
    Uses the shared tokenizer for both sides — no second tokenizer here."""
    terms = set(_terms_of(query))
    url = (doc.metadata.get("url") if doc and doc.metadata else "") or ""
    if not terms or not url:
        return 0.0
    url_terms = set(tokenize(str(url)))
    return sum(1 for t in terms if t in url_terms) / len(terms)


def compute_phrase_match(doc, query: str, context) -> float:
    """1.0 if the query's quoted phrase(s) appear in title+content, else 0.0.
    Phrase detection reuses the same token-sequence rule as BM25 (via the
    understanding / tokenizer), so both retrievers agree what a phrase is."""
    understanding = getattr(context, "understanding", None)
    phrases: list[str] = []
    if understanding is not None:
        from ..core.query_parser import parse_query
        phrases = parse_query(understanding.original).phrases
    if doc is None or not phrases:
        return 0.0
    doc_tokens = tokenize(f"{doc.title} {doc.content}")
    for raw in phrases:
        ph = tokenize(raw)
        if not ph:
            continue
        n = len(ph)
        if any(doc_tokens[i:i + n] == ph for i in range(len(doc_tokens) - n + 1)):
            return 1.0
    return 0.0


def compute_freshness(doc, query: str, context) -> float:
    """Exponential decay from doc.added_at: 0.5 ** (age / half_life).
    half_life comes from context (default 30 days). Missing timestamp -> NEUTRAL."""
    if doc is None or not getattr(doc, "added_at", 0):
        return NEUTRAL
    now = getattr(context, "now", None) or time.time()
    half_life_seconds = getattr(context, "freshness_half_life_days", 30.0) * 86400.0
    if half_life_seconds <= 0:
        return NEUTRAL
    age = max(0.0, now - doc.added_at)
    return 0.5 ** (age / half_life_seconds)


def compute_content_quality(doc, query: str, context) -> float:
    """The ingestion-time quality score stored in metadata['quality']
    (computed ONCE by ingestion/quality.py at ingest time — read it, never
    recompute here). Missing -> NEUTRAL."""
    if doc is None or not doc.metadata:
        return NEUTRAL
    value = doc.metadata.get("quality")
    if not isinstance(value, (int, float)):
        return NEUTRAL
    return max(0.0, min(1.0, float(value)))


def compute_document_quality(doc, query: str, context) -> float:
    """Structure-based quality, distinct from content_quality on purpose:
    content_quality scores writing-like signals at ingest time; this signal
    scores document structure at rank time (title present + reasonable size).
    Kept separate so they can be weighted independently.
    """
    if doc is None:
        return NEUTRAL
    score = 0.3 if (doc.title or "").strip() else 0.0
    length = getattr(doc, "length", 0) or 0
    score += 0.7 * min(1.0, math.log1p(max(0, length)) / math.log1p(2000))
    return min(1.0, score)


def compute_language_relevance(doc, query: str, context) -> float:
    """doc language vs detected query language (Stage 1 understanding).
    NEUTRAL (not 0.0) when either is unknown — missing language metadata is
    not evidence of irrelevance."""
    understanding = getattr(context, "understanding", None)
    q_lang = getattr(understanding, "language", "unknown") if understanding else "unknown"
    d_lang = (doc.metadata.get("language") if doc and doc.metadata else "") or ""
    if not d_lang or q_lang == "unknown":
        return NEUTRAL
    return 1.0 if str(d_lang).lower() == q_lang else 0.0


def compute_source_authority(doc, query: str, context) -> float:
    """PLACEHOLDER interface — no link graph exists until Phase 6.
    Returns doc.metadata['authority_score'] if an upstream system supplied
    one, otherwise context.default_authority (itself defaulting to NEUTRAL)."""
    if doc is not None and doc.metadata:
        value = doc.metadata.get("authority_score")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return getattr(context, "default_authority", NEUTRAL)


def compute_popularity(doc, query: str, context) -> float:
    """PLACEHOLDER interface — no popularity counters exist yet.
    doc.metadata['popularity'] override supported for future wiring."""
    if doc is not None and doc.metadata:
        value = doc.metadata.get("popularity")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return getattr(context, "default_popularity", NEUTRAL)


def compute_click_signal(doc, query: str, context) -> float:
    """PLACEHOLDER interface — no click logs exist anywhere in this codebase
    (Phase 7 territory). doc.metadata['click_score'] override supported.
    We ship the slot, NOT fabricated data."""
    if doc is not None and doc.metadata:
        value = doc.metadata.get("click_score")
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return getattr(context, "default_click", NEUTRAL)
