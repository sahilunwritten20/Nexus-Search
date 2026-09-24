"""RankingFeatures + SignalContext for Phase 5.

One field per signal from README's Phase 5 list. `build_context()` assembles
the context signals need from Phase 4's HybridSearchResult + Storage,
`extract_features()` computes all of them for one candidate. Both are pure
and storage-light: the document row is fetched once per candidate here.
"""
import time
from dataclasses import dataclass, field
from typing import Optional

from ..core.storage import Storage
from . import signals
from .query import QueryUnderstanding


@dataclass
class SignalContext:
    """Everything a signal function is allowed to see.

    bm25_contribution / vector_contribution come from the ALREADY-COMPUTED
    Phase 4 hybrid result (normalized contributions when debug=True, else
    raw scores clamped — see ranker.py). Defaults for the placeholder
    signals live here so they can be tuned without touching code."""
    query: str
    understanding: Optional[QueryUnderstanding] = None
    now: float = 0.0
    freshness_half_life_days: float = 30.0
    bm25_contribution: Optional[float] = None
    vector_contribution: Optional[float] = None
    default_authority: float = signals.NEUTRAL
    default_popularity: float = signals.NEUTRAL
    default_click: float = signals.NEUTRAL


@dataclass
class RankingFeatures:
    bm25_score: float = 0.0
    semantic_similarity: float = 0.0
    title_match: float = 0.0
    url_match: float = 0.0
    phrase_match: float = 0.0
    freshness: float = signals.NEUTRAL
    document_quality: float = signals.NEUTRAL
    content_quality: float = signals.NEUTRAL
    language_relevance: float = signals.NEUTRAL
    source_authority: float = signals.NEUTRAL
    popularity: float = signals.NEUTRAL
    click_signal: float = signals.NEUTRAL

    def as_dict(self) -> dict[str, float]:
        return {
            "bm25_score": self.bm25_score,
            "semantic_similarity": self.semantic_similarity,
            "title_match": self.title_match,
            "url_match": self.url_match,
            "phrase_match": self.phrase_match,
            "freshness": self.freshness,
            "document_quality": self.document_quality,
            "content_quality": self.content_quality,
            "language_relevance": self.language_relevance,
            "source_authority": self.source_authority,
            "popularity": self.popularity,
            "click_signal": self.click_signal,
        }


_SIGNAL_FUNCS = {
    "bm25_score": signals.compute_bm25_score,
    "semantic_similarity": signals.compute_semantic_similarity,
    "title_match": signals.compute_title_match,
    "url_match": signals.compute_url_match,
    "phrase_match": signals.compute_phrase_match,
    "freshness": signals.compute_freshness,
    "document_quality": signals.compute_document_quality,
    "content_quality": signals.compute_content_quality,
    "language_relevance": signals.compute_language_relevance,
    "source_authority": signals.compute_source_authority,
    "popularity": signals.compute_popularity,
    "click_signal": signals.compute_click_signal,
}


def extract_features(doc_id: str, storage: Storage, context: SignalContext,
                     doc=None) -> RankingFeatures:
    """Compute every signal for one candidate. `doc` may be passed in if the
    caller already fetched it; otherwise it's read from storage here (once).
    A missing document NEVER crashes feature extraction — placeholders read
    NEUTRAL and match-type signals read 0.0."""
    if doc is None:
        doc = storage.get_document(doc_id)
    values = {name: func(doc, context.query, context) for name, func in _SIGNAL_FUNCS.items()}
    return RankingFeatures(**values)


def build_context(query: str, understanding: Optional[QueryUnderstanding] = None,
                  half_life_days: float = 30.0) -> SignalContext:
    """New context for one query evaluation. `now` is stamped once so every
    candidate in the same rerank shares the same freshness clock."""
    return SignalContext(query=query, understanding=understanding, now=time.time(),
                         freshness_half_life_days=half_life_days)
