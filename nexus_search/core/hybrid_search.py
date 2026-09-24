"""Hybrid search combining BM25 and vector retrieval for Nexus Search Phase 4."""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

import numpy as np

from .bm25 import BM25Search, SearchResult
from .embedders import EmbedderUnavailable
from .embedding_sync import EmbeddingSync  # noqa: F401  (type hint for __init__)
from .filters import matches_filters
from .query_parser import parse_query
from .storage import Storage
from .tokenizer import tokenize
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.hybrid")

DEFAULT_BM25_WEIGHT = 1.0
DEFAULT_VECTOR_WEIGHT = 1.0
RRF_K = 60


class FusionMode(Enum):
    RRF = "rrf"
    WEIGHTED = "weighted"


class SearchMode(Enum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass
class HybridSearchResult:
    doc_id: str
    score: float
    title: str
    snippet: str
    doc_type: str
    metadata: dict = field(default_factory=dict)
    chunk_id: Optional[str] = None
    matched_chunks: int = 1
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    source: str = "hybrid"
    # set when search_page(debug=True) / explain() asks for the math:
    bm25_normalized: Optional[float] = None  # this side's contribution to score
    vector_normalized: Optional[float] = None


@dataclass
class HybridSearchPage:
    total: int
    results: list[HybridSearchResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a score pool to [0, 1]."""
    if not scores:
        return {}
    values = list(scores.values())
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        # All scores equal: give 1.0 if positive, 0.0 otherwise
        return {k: (1.0 if v > 0 else 0.0) for k, v in scores.items()}
    return {k: (v - min_score) / (max_score - min_score) for k, v in scores.items()}


def _rrf_fuse(bm25_results: dict[str, tuple[float, SearchResult]],
              vector_results: dict[str, tuple[float, str]],
              k: int = RRF_K,
              bm25_weight: float = 1.0,
              vector_weight: float = 1.0) -> list[tuple[str, float, str, float, float]]:
    """Reciprocal Rank Fusion with weights.

    Returns [(doc_id, fused_score, source, bm25_contrib, vector_contrib)]
    sorted by fused score desc. Contributions are exactly what each retriever
    added to the fused score (they sum to it).
    """
    bm25_ranks = {}
    for rank, (doc_id, _) in enumerate(
        sorted(bm25_results.items(), key=lambda x: (-x[1][0], x[0])), 1
    ):
        bm25_ranks[doc_id] = rank

    vector_ranks = {}
    for rank, (doc_id, _) in enumerate(
        sorted(vector_results.items(), key=lambda x: (-x[1][0], x[0])), 1
    ):
        vector_ranks[doc_id] = rank

    fused = []
    for doc_id in set(bm25_ranks) | set(vector_ranks):
        bm25_contrib = bm25_weight / (k + bm25_ranks[doc_id]) if doc_id in bm25_ranks and bm25_weight else 0.0
        vector_contrib = vector_weight / (k + vector_ranks[doc_id]) if doc_id in vector_ranks and vector_weight else 0.0
        source_parts = []
        if doc_id in bm25_ranks:
            source_parts.append("bm25")
        if doc_id in vector_ranks:
            source_parts.append("vector")
        fused.append((doc_id, bm25_contrib + vector_contrib, "+".join(source_parts) or "none",
                      bm25_contrib, vector_contrib))

    fused.sort(key=lambda x: (-x[1], x[0]))
    return fused


def _weighted_fuse(bm25_results: dict[str, tuple[float, SearchResult]],
                   vector_results: dict[str, tuple[float, str]],
                   bm25_weight: float = 1.0,
                   vector_weight: float = 1.0) -> list[tuple[str, float, str, float, float]]:
    """Weighted fusion: min-max normalize each score pool, then weighted sum.

    Returns the same tuple shape as `_rrf_fuse`.
    """
    norm_bm25 = _normalize_scores({d: s for d, (s, _) in bm25_results.items()})
    norm_vector = _normalize_scores({d: s for d, (s, _) in vector_results.items()})

    fused = []
    for doc_id in set(norm_bm25) | set(norm_vector):
        bm25_contrib = bm25_weight * norm_bm25[doc_id] if doc_id in norm_bm25 else 0.0
        vector_contrib = vector_weight * norm_vector[doc_id] if doc_id in norm_vector else 0.0
        source_parts = []
        if doc_id in norm_bm25:
            source_parts.append("bm25")
        if doc_id in norm_vector:
            source_parts.append("vector")
        fused.append((doc_id, bm25_contrib + vector_contrib, "+".join(source_parts) or "none",
                      bm25_contrib, vector_contrib))

    fused.sort(key=lambda x: (-x[1], x[0]))
    return fused


def _fuse(bm25_results, vector_results, fusion: str,
          bm25_weight: float, vector_weight: float):
    if fusion == FusionMode.WEIGHTED.value:
        return _weighted_fuse(bm25_results, vector_results, bm25_weight, vector_weight)
    return _rrf_fuse(bm25_results, vector_results, k=RRF_K,
                     bm25_weight=bm25_weight, vector_weight=vector_weight)


class HybridSearch:
    def __init__(
        self,
        storage: Storage,
        bm25_weight: float = DEFAULT_BM25_WEIGHT,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        vector_store: Optional[VectorStoreManager] = None,
        db_path: str = "nexus_search.db",
        embedding_sync: Optional["EmbeddingSync"] = None,
    ):
        if bm25_weight < 0 or vector_weight < 0:
            raise ValueError("Weights must be >= 0")
        if bm25_weight == 0 and vector_weight == 0:
            raise ValueError("At least one weight must be > 0")

        self.storage = storage
        self.bm25_search = BM25Search(storage)
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight

        self.vector_store = vector_store or VectorStoreManager(db_path)
        # Backward compatibility
        self.embedding_manager = self.vector_store.embedder
        self.vector_index = self.vector_store.store

        # EmbeddingSync for incremental updates (optional)
        self._embedding_sync = embedding_sync

    # ---------------------------------------------------------- candidates

    @staticmethod
    def _candidate_k(top_k: int, candidates: Optional[int]) -> int:
        if candidates is not None:
            return min(max(candidates, 1), 500)
        return min(max(top_k * 5, 50), 500)

    def _bm25_candidates(self, query: str, top_k: int, group_chunks: bool,
                         candidates: Optional[int] = None) -> dict[str, tuple[float, SearchResult]]:
        candidate_k = self._candidate_k(top_k, candidates)
        page = self.bm25_search.search_page(query, top_k=candidate_k, offset=0, group_chunks=group_chunks)
        return {r.doc_id: (r.score, r) for r in page.results}

    def _vector_candidates(self, query: str, top_k: int, group_chunks: bool,
                           allowed_filter: Optional[Callable[[str], bool]] = None,
                           candidates: Optional[int] = None) -> dict[str, tuple[float, str]]:
        # Use parsed query text for embedding (no filters/phrases in embedding)
        parsed = parse_query(query)
        query_text = parsed.text

        candidate_k = self._candidate_k(top_k, candidates)
        vector_results = self.vector_store.search(query_text, top_k=candidate_k, allowed=allowed_filter)

        results = {}
        # Tokenize required phrases once — matches BM25's phrase semantics
        # (token sequence, not substring), so "machine-learning" satisfies
        # the phrase "machine learning" on BOTH retrievers.
        phrase_tokens = [t for t in (tokenize(p) for p in parsed.phrases) if t]
        for vr in vector_results:
            parent = vr.doc_id
            # Get parent doc for chunk grouping
            if group_chunks:
                doc = self.storage.get_document(parent)
                if doc:
                    parent = doc.metadata.get("parent_id", parent)
            # Apply required phrases from the parsed query
            if phrase_tokens:
                doc = self.storage.get_document(vr.doc_id)
                if doc is None:
                    continue
                doc_tokens = tokenize(f"{doc.title} {doc.content}")
                if not all(BM25Search._has_phrase(doc_tokens, ph) for ph in phrase_tokens):
                    continue  # Skip docs that don't contain required phrases
            if parent not in results or vr.score > results[parent][0]:
                results[parent] = (vr.score, vr.doc_id)
        return results

    # -------------------------------------------------------------- merge

    def _vector_only_snippet(self, doc, parsed) -> str:
        """Query-focused snippet for vector-only hits — same helper BM25 uses."""
        terms = parsed.terms if parsed else []
        phrases = parsed.phrases if parsed else []
        return self.bm25_search._snippet(doc, terms, phrases)

    def _merge_results(
        self,
        bm25_results: dict[str, tuple[float, SearchResult]],
        vector_results: dict[str, tuple[float, str]],
        fusion: str = "rrf",
        parsed=None,
    ) -> list[HybridSearchResult]:
        fused = _fuse(bm25_results, vector_results, fusion,
                      self.bm25_weight, self.vector_weight)

        merged = []
        for doc_id, fused_score, source, bm25_contrib, vector_contrib in fused:
            bm25_result = bm25_results.get(doc_id)
            vector_result = vector_results.get(doc_id)

            if bm25_result:
                orig_score, orig_result = bm25_result
                merged.append(HybridSearchResult(
                    doc_id=doc_id,
                    score=fused_score,
                    title=orig_result.title,
                    snippet=orig_result.snippet,
                    doc_type=orig_result.doc_type,
                    metadata=orig_result.metadata,
                    chunk_id=orig_result.chunk_id,
                    matched_chunks=orig_result.matched_chunks,
                    bm25_score=orig_score,
                    vector_score=vector_result[0] if vector_result else None,
                    source=source,
                    bm25_normalized=bm25_contrib,
                    vector_normalized=vector_contrib,
                ))
            else:
                # Vector-only result
                doc = self.storage.get_document(vector_results[doc_id][1])
                if doc:
                    merged.append(HybridSearchResult(
                        doc_id=doc_id,
                        score=fused_score,
                        title=doc.title,
                        snippet=self._vector_only_snippet(doc, parsed),
                        doc_type=doc.doc_type,
                        metadata=doc.metadata,
                        chunk_id=vector_results[doc_id][1] if vector_results[doc_id][1] != doc_id else None,
                        matched_chunks=1,
                        bm25_score=None,
                        vector_score=vector_results[doc_id][0],
                        source=source,
                        bm25_normalized=bm25_contrib,
                        vector_normalized=vector_contrib,
                    ))

        return merged

    def _build_allowed_filter(self, parsed) -> Optional[Callable[[str], bool]]:
        """Build filter callable for vector search pushdown."""
        if not parsed.filters:
            return None

        def allowed(doc_id: str) -> bool:
            doc = self.storage.get_document(doc_id)
            if doc is None:
                return False
            return matches_filters(doc, parsed.filters)
        return allowed

    @staticmethod
    def _meta(mode_used: str, original_mode: SearchMode, start_time: float,
              fallback: bool = False, fallback_reason: Optional[str] = None,
              bm25_candidates: int = 0, vector_candidates: int = 0,
              merged_candidates: int = 0, fusion: str = "rrf") -> dict:
        return {
            "mode": mode_used,
            "requested_mode": original_mode.value,
            "mode_used": mode_used,
            "fallback": fallback,
            "fallback_reason": fallback_reason,
            "latency_ms": int((time.time() - start_time) * 1000),
            "bm25_candidates": bm25_candidates,
            "vector_candidates": vector_candidates,
            "merged_candidates": merged_candidates,
            "fusion": fusion,
        }

    def search_page(
        self,
        query: str,
        top_k: int = 10,
        offset: int = 0,
        group_chunks: bool = True,
        mode: SearchMode = SearchMode.HYBRID,
        fusion: str = "rrf",
        candidates: Optional[int] = None,
        debug: bool = False,
    ) -> HybridSearchPage:
        if top_k <= 0:
            return HybridSearchPage(total=0, results=[], metadata={"mode": mode.value, "fusion": fusion})
        offset = max(offset, 0)
        start_time = time.time()
        original_mode = mode

        parsed = parse_query(query)
        allowed_filter = self._build_allowed_filter(parsed)

        if mode == SearchMode.KEYWORD:
            page = self.bm25_search.search_page(query, top_k=top_k, offset=offset, group_chunks=group_chunks)
            results = [
                HybridSearchResult(
                    doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                    doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                    matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25",
                ) for r in page.results
            ]
            return HybridSearchPage(
                total=page.total,
                results=results,
                metadata=self._meta(mode.value, original_mode, start_time,
                                    bm25_candidates=page.total, merged_candidates=len(results),
                                    fusion=fusion),
            )

        if mode == SearchMode.SEMANTIC:
            try:
                vector_results = self._vector_candidates(query, top_k, group_chunks, allowed_filter, candidates)
            except EmbedderUnavailable as exc:
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                           start_time, f"embedder_unavailable: {exc}", fusion)
            except Exception as exc:
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                           start_time, f"vector_search_error: {exc}", fusion)

            fused = _fuse({}, vector_results, fusion, bm25_weight=0.0, vector_weight=self.vector_weight)
            merged = []
            for doc_id, fused_score, source, _, vector_contrib in fused:
                vr_doc_id = vector_results[doc_id][1]
                doc = self.storage.get_document(vr_doc_id)
                if doc:
                    merged.append(HybridSearchResult(
                        doc_id=doc_id,
                        score=fused_score,
                        title=doc.title,
                        snippet=self._vector_only_snippet(doc, parsed),
                        doc_type=doc.doc_type,
                        metadata=doc.metadata,
                        chunk_id=vr_doc_id if vr_doc_id != doc_id else None,
                        matched_chunks=1,
                        bm25_score=None,
                        vector_score=vector_results[doc_id][0],
                        source="vector",
                        vector_normalized=vector_contrib,
                    ))
            return HybridSearchPage(
                total=len(merged),
                results=merged[offset:offset + top_k],
                metadata=self._meta(mode.value, original_mode, start_time,
                                    vector_candidates=len(vector_results),
                                    merged_candidates=len(merged), fusion=fusion),
            )

        # SearchMode.HYBRID
        # BM25 candidates are computed OUTSIDE the vector try/except: a BM25
        # failure cannot be "fixed" by falling back to BM25, so it propagates.
        bm25_results = {}
        if self.bm25_weight > 0:
            bm25_results = self._bm25_candidates(query, top_k, group_chunks, candidates)

        vector_results = {}
        try:
            if self.vector_weight > 0:
                vector_results = self._vector_candidates(query, top_k, group_chunks, allowed_filter, candidates)
        except EmbedderUnavailable as exc:
            logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
            return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                       start_time, f"embedder_unavailable: {exc}", fusion)
        except Exception as exc:
            logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
            return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                       start_time, f"vector_search_error: {exc}", fusion)

        merged = self._merge_results(bm25_results, vector_results, fusion=fusion, parsed=parsed)
        if not debug:
            # strip diagnostic numbers unless asked for
            for r in merged:
                r.bm25_normalized = None
                r.vector_normalized = None
        return HybridSearchPage(
            total=len(merged),
            results=merged[offset:offset + top_k],
            metadata=self._meta(SearchMode.HYBRID.value, original_mode, start_time,
                                bm25_candidates=len(bm25_results),
                                vector_candidates=len(vector_results),
                                merged_candidates=len(merged), fusion=fusion),
        )

    def _bm25_fallback(self, query: str, top_k: int, offset: int, group_chunks: bool,
                       original_mode: SearchMode, start_time: float,
                       reason: str, fusion: str) -> HybridSearchPage:
        page = self.bm25_search.search_page(query, top_k=top_k, offset=offset, group_chunks=group_chunks)
        results = [
            HybridSearchResult(
                doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25_fallback",
            ) for r in page.results
        ]
        return HybridSearchPage(
            total=page.total,
            results=results,
            metadata=self._meta("keyword", original_mode, start_time,
                                fallback=True, fallback_reason=reason,
                                bm25_candidates=page.total, merged_candidates=len(results),
                                fusion=fusion),
        )

    def search(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID) -> list[HybridSearchResult]:
        return self.search_page(query, top_k=top_k, mode=mode).results

    def explain(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID,
                fusion: str = "rrf") -> dict:
        """Debug explanation: per-result raw scores plus each retriever's actual
        contribution to the final score (`bm25_normalized` / `vector_normalized`).
        In weighted fusion the contributions are weight * min-max-normalized score;
        in RRF fusion they are weight / (k + rank). They sum to `final_score`."""
        page = self.search_page(query, top_k=top_k, mode=mode, fusion=fusion, debug=True)
        return {
            "query": query,
            "mode": mode.value,
            "metadata": page.metadata,
            "results": [
                {
                    "doc_id": r.doc_id,
                    "final_score": r.score,
                    "bm25_score": r.bm25_score,
                    "vector_score": r.vector_score,
                    "bm25_normalized": r.bm25_normalized,
                    "vector_normalized": r.vector_normalized,
                    "source": r.source,
                    "title": r.title,
                    "chunk_id": r.chunk_id,
                }
                for r in page.results
            ],
        }

    def close(self):
        self.vector_store.close()


def create_hybrid_search(
    storage: Storage,
    bm25_weight: float = DEFAULT_BM25_WEIGHT,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    vector_store: Optional[VectorStoreManager] = None,
    db_path: str = "nexus_search.db",
) -> HybridSearch:
    return HybridSearch(storage, bm25_weight, vector_weight, vector_store=vector_store, db_path=db_path)
