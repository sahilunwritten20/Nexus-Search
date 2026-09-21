"""Hybrid search combining BM25 and vector retrieval for Nexus Search Phase 4."""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

import numpy as np

from .bm25 import BM25Search, SearchResult, SearchPage
from .embedders import get_embedder, EmbedderUnavailable
from .embedding_sync import EmbeddingSync
from .filters import matches_filters
from .query_parser import parse_query
from .storage import Storage
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.hybrid")

DEFAULT_BM25_WEIGHT = 1.0
DEFAULT_VECTOR_WEIGHT = 1.0


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


@dataclass
class HybridSearchPage:
    total: int
    results: list[HybridSearchResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        # All scores equal: give 1.0 if positive, 0.0 otherwise
        return {k: (1.0 if v > 0 else 0.0) for k, v in scores.items()}
    return {k: (v - min_score) / (max_score - min_score) for k, v in scores.items()}


def _get_parent_id(doc, group_chunks: bool) -> str:
    if group_chunks:
        return doc.metadata.get("parent_id", doc.doc_id)
    return doc.doc_id


def _rrf_fuse(bm25_results: dict[str, tuple[float, SearchResult]], 
              vector_results: dict[str, tuple[float, str]],
              k: int = 60,
              bm25_weight: float = 1.0,
              vector_weight: float = 1.0) -> list[tuple[str, float, str]]:
    """Reciprocal Rank Fusion with weights.
    
    Returns list of (doc_id, fused_score, source) sorted by score desc.
    """
    # Get ranks for each retriever
    bm25_ranks = {}
    for rank, (doc_id, _) in enumerate(
        sorted(bm25_results.items(), key=lambda x: -x[1][0]), 1
    ):
        bm25_ranks[doc_id] = rank
    
    vector_ranks = {}
    for rank, (doc_id, _) in enumerate(
        sorted(vector_results.items(), key=lambda x: -x[1][0]), 1
    ):
        vector_ranks[doc_id] = rank
    
    all_doc_ids = set(bm25_ranks.keys()) | set(vector_ranks.keys())
    
    fused = []
    for doc_id in all_doc_ids:
        score = 0.0
        source_parts = []
        
        if doc_id in bm25_ranks:
            score += bm25_weight / (k + bm25_ranks[doc_id])
            source_parts.append("bm25")
        if doc_id in vector_ranks:
            score += vector_weight / (k + vector_ranks[doc_id])
            source_parts.append("vector")
        
        source = "+".join(source_parts) if source_parts else "none"
        fused.append((doc_id, score, source))
    
    fused.sort(key=lambda x: (-x[1], x[0]))
    return fused


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
    
    def _bm25_candidates(self, query: str, top_k: int, offset: int, group_chunks: bool) -> dict[str, tuple[float, SearchResult]]:
        # Get candidates: clamp to reasonable range
        candidate_k = min(max(top_k * 5, 50), 500)
        page = self.bm25_search.search_page(query, top_k=candidate_k, offset=0, group_chunks=group_chunks)
        results = {}
        for r in page.results:
            results[r.doc_id] = (r.score, r)
        return results
    
    def _vector_candidates(self, query: str, top_k: int, group_chunks: bool, 
                          allowed_filter: Optional[Callable[[str], bool]] = None) -> dict[str, tuple[float, str]]:
        try:
            # Use parsed query text for embedding (no filters/phrases in embedding)
            parsed = parse_query(query)
            query_text = parsed.text
            
            candidate_k = min(max(top_k * 5, 50), 500)
            vector_results = self.vector_store.search(query_text, top_k=candidate_k, allowed=allowed_filter)
            
            results = {}
            for vr in vector_results:
                parent = vr.doc_id
                # Get parent doc for chunk grouping
                if group_chunks:
                    doc = self.storage.get_document(parent)
                    if doc:
                        parent = doc.metadata.get("parent_id", parent)
                if parent not in results or vr.score > results[parent][0]:
                    results[parent] = (vr.score, vr.doc_id)
            return results
        except EmbedderUnavailable:
            raise
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            raise
    
    def _merge_results(
        self,
        bm25_results: dict[str, tuple[float, SearchResult]],
        vector_results: dict[str, tuple[float, str]],
        group_chunks: bool,
    ) -> list[HybridSearchResult]:
        # Use RRF fusion
        fused = _rrf_fuse(bm25_results, vector_results, k=60, 
                          bm25_weight=self.bm25_weight, vector_weight=self.vector_weight)
        
        merged = []
        for doc_id, fused_score, source in fused:
            bm25_result = bm25_results.get(doc_id)
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
                    vector_score=None,
                    source=source,
                ))
            else:
                # Vector-only result
                doc = self.storage.get_document(vector_results[doc_id][1])
                if doc:
                    merged.append(HybridSearchResult(
                        doc_id=doc_id,
                        score=fused_score,
                        title=doc.title,
                        snippet=doc.content[:200] + ("..." if len(doc.content) > 200 else ""),
                        doc_type=doc.doc_type,
                        metadata=doc.metadata,
                        chunk_id=vector_results[doc_id][1] if vector_results[doc_id][1] != doc_id else None,
                        matched_chunks=1,
                        bm25_score=None,
                        vector_score=vector_results[doc_id][0],
                        source=source,
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
    
    def search_page(
        self,
        query: str,
        top_k: int = 10,
        offset: int = 0,
        group_chunks: bool = True,
        mode: SearchMode = SearchMode.HYBRID,
    ) -> HybridSearchPage:
        start_time = time.time()
        fallback = False
        fallback_reason = None
        bm25_candidates_count = 0
        vector_candidates_count = 0
        original_mode = mode
        
        parsed = parse_query(query)
        allowed_filter = self._build_allowed_filter(parsed)
        
        if mode == SearchMode.KEYWORD:
            page = self.bm25_search.search_page(query, top_k=top_k, offset=offset, group_chunks=group_chunks)
            results = [
                HybridSearchResult(
                    doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                    doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                    matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25"
                ) for r in page.results
            ]
            return HybridSearchPage(
                total=page.total,
                results=results,
                metadata={
                    "mode": mode.value, 
                    "fallback": False, 
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "requested_mode": original_mode.value,
                    "mode_used": mode.value,
                    "bm25_weight": self.bm25_weight,
                    "vector_weight": self.vector_weight,
                }
            )
        
        if mode == SearchMode.SEMANTIC:
            try:
                vector_results = self._vector_candidates(query, top_k, group_chunks, allowed_filter)
                vector_candidates_count = len(vector_results)
                fused = _rrf_fuse({}, vector_results, k=60, 
                                  bm25_weight=0, vector_weight=self.vector_weight)
                # Build results from fused
                merged = []
                for doc_id, fused_score, source in fused:
                    vr_doc_id = vector_results[doc_id][1]
                    doc = self.storage.get_document(vr_doc_id)
                    if doc:
                        merged.append(HybridSearchResult(
                            doc_id=doc_id,
                            score=fused_score,
                            title=doc.title,
                            snippet=doc.content[:200] + ("..." if len(doc.content) > 200 else ""),
                            doc_type=doc.doc_type,
                            metadata=doc.metadata,
                            chunk_id=vr_doc_id if vr_doc_id != doc_id else None,
                            matched_chunks=1,
                            bm25_score=None,
                            vector_score=vector_results[doc_id][0],
                            source="vector",
                        ))
                results = merged[offset:offset + top_k]
                return HybridSearchPage(
                    total=len(merged),
                    results=results,
                    metadata={
                        "mode": mode.value,
                        "vector_candidates": vector_candidates_count,
                        "merged_candidates": len(merged),
                        "fallback": False,
                        "latency_ms": int((time.time() - start_time) * 1000),
                        "requested_mode": original_mode.value,
                        "mode_used": mode.value,
                        "bm25_weight": self.bm25_weight,
                        "vector_weight": self.vector_weight,
                    }
                )
            except EmbedderUnavailable as exc:
                fallback = True
                fallback_reason = f"embedder_unavailable: {exc}"
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                mode = SearchMode.KEYWORD
            except Exception as exc:
                fallback = True
                fallback_reason = f"vector_search_error: {exc}"
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                mode = SearchMode.KEYWORD
        
        if mode == SearchMode.HYBRID:
            try:
                bm25_results = self._bm25_candidates(query, top_k, offset, group_chunks)
                bm25_candidates_count = len(bm25_results)
                vector_results = self._vector_candidates(query, top_k, group_chunks, allowed_filter)
                vector_candidates_count = len(vector_results)
                merged = self._merge_results(bm25_results, vector_results, group_chunks)
                results = merged[offset:offset + top_k]
                return HybridSearchPage(
                    total=len(merged),
                    results=results,
                    metadata={
                        "mode": SearchMode.HYBRID.value,
                        "bm25_candidates": bm25_candidates_count,
                        "vector_candidates": vector_candidates_count,
                        "merged_candidates": len(merged),
                        "fallback": False,
                        "latency_ms": int((time.time() - start_time) * 1000),
                        "requested_mode": original_mode.value,
                        "mode_used": SearchMode.HYBRID.value,
                        "bm25_weight": self.bm25_weight,
                        "vector_weight": self.vector_weight,
                    }
                )
            except EmbedderUnavailable as exc:
                fallback = True
                fallback_reason = f"embedder_unavailable: {exc}"
                logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
                mode = SearchMode.KEYWORD
            except Exception as exc:
                fallback = True
                fallback_reason = f"vector_search_error: {exc}"
                logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
                mode = SearchMode.KEYWORD
        
        if fallback:
            page = self.bm25_search.search_page(query, top_k=top_k, offset=offset, group_chunks=group_chunks)
            results = [
                HybridSearchResult(
                    doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                    doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                    matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25_fallback"
                ) for r in page.results
            ]
            return HybridSearchPage(
                total=page.total,
                results=results,
                metadata={
                    "mode": "keyword (fallback)",
                    "requested_mode": original_mode.value,
                    "mode_used": "keyword",
                    "fallback": True,
                    "fallback_reason": fallback_reason,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "bm25_weight": self.bm25_weight,
                    "vector_weight": self.vector_weight,
                }
            )
        
        return HybridSearchPage(total=0, results=[])

    def search(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID) -> list[HybridSearchResult]:
        return self.search_page(query, top_k=top_k, mode=mode).results

    def explain(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID) -> dict:
        page = self.search_page(query, top_k=top_k, mode=mode)
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
                    "source": r.source,
                    "title": r.title,
                    "chunk_id": r.chunk_id,
                }
                for r in page.results
            ]
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