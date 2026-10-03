"""Hybrid search combining BM25 and vector retrieval for Nexus Search Phase 4."""
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable

import numpy as np

from .bm25 import BM25Search, SearchResult
from .embedders import EmbedderUnavailable
from .embedding_sync import EmbeddingSync  # noqa: F401  (type hint for __init__)
from .filters import matches_filters
from .normalize import min_max_normalize
from .query_parser import boolean_match, parse_query
from .storage import Storage
from .tokenizer import tokenize
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.hybrid")

DEFAULT_BM25_WEIGHT = 1.0
DEFAULT_VECTOR_WEIGHT = 1.0
RRF_K = 60


def max_candidates() -> int:
    """Hard bound on a fused candidate pool: NEXUS_MAX_CANDIDATES
    (default 1000). Requests whose offset+top_k window exceeds it cannot be
    served truthfully and are refused by the API with a 400."""
    raw = (os.environ.get("NEXUS_MAX_CANDIDATES") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 1000
    return value if value > 0 else 1000


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
    # Rows actually reachable through paging the fused pool. `total` is the
    # true corpus match count (BM25 side is exact); `pool_size` is the
    # candidate-bounded pageable universe — has_more must use pool_size,
    # otherwise we'd promise pages that come back empty.
    pool_size: Optional[int] = None


# Backward-compatible alias: the implementation lives in core/normalize.py
# so hybrid fusion and the Phase 5 ranker share exactly one min-max.
_normalize_scores = min_max_normalize


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

    @classmethod
    def _candidate_k(cls, top_k: int, candidates: Optional[int],
                     offset: int = 0) -> int:
        """Effective candidate-pool size. The pool MUST cover the requested
        page (BUG-03/04): max(candidates, offset+top_k), clamped to
        NEXUS_MAX_CANDIDATES. Pre-fix, offset pages sliced the empty tail
        of a too-small pool and top_k > candidates silently truncated."""
        base = candidates if candidates is not None else max(top_k * 5, 50)
        return min(max(base, offset + top_k, 1), max_candidates())

    def _bm25_candidates(self, query: str, pool_k: int, group_chunks: bool,
                         highlight: bool = False,
                         ) -> tuple[dict[str, tuple[float, SearchResult]], int]:
        page = self.bm25_search.search_page(query, top_k=pool_k, offset=0,
                                            group_chunks=group_chunks, highlight=highlight)
        # page.total is the TRUE corpus-wide match count (not truncated to
        # the candidate pool) — the honest total for hybrid-mode reporting.
        return {r.doc_id: (r.score, r) for r in page.results}, page.total

    def _vector_candidates(self, query: str, pool_k: int, group_chunks: bool,
                           allowed_filter: Optional[Callable[[str], bool]] = None,
                           ) -> dict[str, tuple[float, str]]:
        # Use parsed query text for embedding (no filters/phrases in embedding)
        parsed = parse_query(query)
        query_text = parsed.text

        vector_results = self.vector_store.search(query_text, top_k=pool_k, allowed=allowed_filter)

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
            # Boolean structure (NOT/AND/title:) applies on BOTH retrievers —
            # a query filter must not behave differently depending on which
            # half of the fusion produced the hit.
            if parsed.has_boolean:
                doc = self.storage.get_document(vr.doc_id)
                if doc is None:
                    continue
                doc_tokens = tokenize(f"{doc.title} {doc.content}")
                if not boolean_match(parsed, doc_tokens, tokenize(doc.title)):
                    continue
            if parent not in results or vr.score > results[parent][0]:
                results[parent] = (vr.score, vr.doc_id)
        return results

    # -------------------------------------------------------------- merge

    def _diversify(self, merged: list[HybridSearchResult], diversity: float,
                   threshold: float = 0.85) -> list[HybridSearchResult]:
        """MMR pass (core/diversity.py). diversity∈(0,1]: higher = novelty wins
        harder. Ran AFTER sorting, BEFORE slicing, so diversity affects which
        items occupy the returned page."""
        from .diversity import mmr_select
        lambda_ = max(0.0, 1.0 - diversity)

        def text_of(r):
            # CONTENT is the diversity signal, not title chrome: near-mirror
            # documents routinely differ only by a running index in the title
            # ("Fox variant 1" vs "Fox variant 2"), and including titles lets
            # copy-farms slip under the Jaccard threshold.
            doc = self.storage.get_document(r.doc_id)
            return doc.content if doc else r.title

        selected = mmr_select(merged, text_of, lambda_=lambda_,
                              similarity_threshold=threshold)
        return [s.result for s in selected]

    def _vector_only_snippet(self, doc, parsed, highlight: bool = False) -> str:
        """Query-focused snippet for vector-only hits — same helper BM25 uses."""
        terms = parsed.terms if parsed else []
        phrases = parsed.phrases if parsed else []
        return self.bm25_search._snippet(doc, terms, phrases, highlight=highlight)

    def _sort_raw(self, results: list, sort: str) -> list:
        """Sort a candidate list (SearchResult or HybridSearchResult).

        - "title": A→Z by title, ties broken by doc_id (deterministic)
        - "freshness": newest first by documents.added_at; a doc missing from
          storage sorts as age 0 → oldest. Stable, deterministic (doc_id tie-break)
        Sorting happens within the current candidate pool (bounded by
        `candidates`) — at prototype scale this is the corpus; at web scale
        this is why sharded indices re-sort per shard."""
        if sort == "title":
            return sorted(results, key=lambda r: ((r.title or "").lower(), r.doc_id))
        if sort == "freshness":
            # WP6: one batched added_at read for the whole candidate list
            # instead of a get_document per result (the N+1 the audit noted);
            # behavior is unchanged (same values, same sort keys).
            added = self.storage.added_at_map([r.doc_id for r in results])
            return sorted(results, key=lambda r: (-added.get(r.doc_id, 0.0), r.doc_id))
        return results

    def _merge_results(
        self,
        bm25_results: dict[str, tuple[float, SearchResult]],
        vector_results: dict[str, tuple[float, str]],
        fusion: str = "rrf",
        parsed=None,
        highlight: bool = False,
        bm25_weight: Optional[float] = None,
        vector_weight: Optional[float] = None,
    ) -> list[HybridSearchResult]:
        fused = _fuse(bm25_results, vector_results, fusion,
                      self.bm25_weight if bm25_weight is None else bm25_weight,
                      self.vector_weight if vector_weight is None else vector_weight)

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
                        snippet=self._vector_only_snippet(doc, parsed, highlight=highlight),
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
        if not parsed.filters and not parsed.not_filters:
            return None

        def allowed(doc_id: str) -> bool:
            doc = self.storage.get_document(doc_id)
            if doc is None:
                return False
            return matches_filters(doc, parsed.filters, parsed.not_filters)
        return allowed

    @staticmethod
    def _meta(mode_used: str, original_mode: SearchMode, start_time: float,
              fallback: bool = False, fallback_reason: Optional[str] = None,
              bm25_candidates: int = 0, vector_candidates: int = 0,
              merged_candidates: int = 0, fusion: str = "rrf",
              sort: str = "relevance") -> dict:
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
            "sort": sort,
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
        understanding=None,
        sort: str = "relevance",
        highlight: bool = False,
        diversity: float = 0.0,
        bm25_weight: Optional[float] = None,
        vector_weight: Optional[float] = None,
    ) -> HybridSearchPage:
        """`understanding` (Phase 5 QueryUnderstanding) is OPT-IN: when given,
        retrieval uses its corrected/expanded effective terms (phrases and
        filters preserved verbatim). When None — the default — behavior is
        exactly the pre-Phase-5 behavior.

        `bm25_weight`/`vector_weight` override the CONSTRUCTOR weights for
        this call only (None -> constructor weights). This is what lets the
        API serve per-request weights from ONE shared instance (BUG-06):
        a fresh HybridSearch per request discarded the BM25 token memo
        across requests; the shared instance keeps it (bounded, keyed by
        (doc_id, added_at), thread-safe)."""
        w_bm25 = self.bm25_weight if bm25_weight is None else bm25_weight
        w_vector = self.vector_weight if vector_weight is None else vector_weight
        if w_bm25 < 0 or w_vector < 0:
            raise ValueError("Weights must be >= 0")
        if w_bm25 == 0 and w_vector == 0:
            raise ValueError("At least one weight must be > 0")
        if top_k <= 0:
            return HybridSearchPage(total=0, results=[], metadata={"mode": mode.value, "fusion": fusion})
        offset = max(offset, 0)
        if understanding is not None:
            # Retrieval-query rewriting widens the pool via corrections +
            # synonym expansion, but the string form only carries flat
            # terms/phrases/filters — a boolean query (NOT/AND/exclusions,
            # title scope, negated filters) must keep its own structure.
            probe = parse_query(query)
            if not probe.has_boolean and not probe.not_filters:
                query = understanding.to_retrieval_query()
        start_time = time.time()
        original_mode = mode

        parsed = parse_query(query)
        allowed_filter = self._build_allowed_filter(parsed)

        if mode == SearchMode.KEYWORD:
            need_pool = sort not in (None, "relevance") or diversity > 0.0
            if not need_pool:
                # exact pre-Phase-5 path: BM25 slices offset/top_k itself
                page = self.bm25_search.search_page(query, top_k=top_k, offset=offset,
                                                    group_chunks=group_chunks, highlight=highlight)
                page_results = page.results
            else:
                # Sort/diversity need the full candidate pool first (bounded
                # by `candidates`, always covering the requested page), then
                # the page comes out of it.
                pool_k = self._candidate_k(top_k, candidates, offset)
                page = self.bm25_search.search_page(query, top_k=pool_k, offset=0,
                                                    group_chunks=group_chunks, highlight=highlight)
                pool_results = page.results
                if diversity > 0.0:
                    interim = [HybridSearchResult(
                        doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                        doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                        matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25",
                    ) for r in pool_results]
                    pool_results = self._diversify(interim, diversity)
                page_results = self._sort_raw(pool_results, sort)[offset:offset + top_k]
            results = [
                HybridSearchResult(
                    doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                    doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                    matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25",
                ) for r in page_results
            ]
            return HybridSearchPage(
                total=page.total,
                pool_size=page.total,
                results=results,
                metadata=self._meta(mode.value, original_mode, start_time,
                                    bm25_candidates=page.total, merged_candidates=len(results),
                                    fusion=fusion, sort=sort),
            )

        if mode == SearchMode.SEMANTIC:
            try:
                # The semantic result universe IS the candidate pool (no
                # relevance threshold by design). Documented caveat: without
                # an explicit `candidates`, deep pages grow the pool per
                # request and windows near the boundary may drift — pass
                # candidates when strict page stability matters.
                pool_k = self._candidate_k(top_k, candidates, offset)
                vector_results = self._vector_candidates(query, pool_k, group_chunks,
                                                          allowed_filter)
            except EmbedderUnavailable as exc:
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                           start_time, f"embedder_unavailable:{type(exc).__name__}", fusion, sort, highlight)
            except Exception as exc:
                logger.warning("Semantic search failed, falling back to BM25: %s", exc)
                return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                           start_time, f"vector_search_error:{type(exc).__name__}", fusion, sort, highlight)

            fused = _fuse({}, vector_results, fusion, bm25_weight=0.0,
                          vector_weight=w_vector)
            merged = []
            for doc_id, fused_score, source, _, vector_contrib in fused:
                vr_doc_id = vector_results[doc_id][1]
                doc = self.storage.get_document(vr_doc_id)
                if doc:
                    merged.append(HybridSearchResult(
                        doc_id=doc_id,
                        score=fused_score,
                        title=doc.title,
                        snippet=self._vector_only_snippet(doc, parsed, highlight=highlight),
                        doc_type=doc.doc_type,
                        metadata=doc.metadata,
                        chunk_id=vr_doc_id if vr_doc_id != doc_id else None,
                        matched_chunks=1,
                        bm25_score=None,
                        vector_score=vector_results[doc_id][0],
                        source="vector",
                        vector_normalized=vector_contrib,
                    ))
            merged = self._sort_raw(merged, sort)
            if diversity > 0.0:
                merged = self._diversify(merged, diversity)
            return HybridSearchPage(
                # semantic mode has no relevance threshold: the candidate
                # pool IS the result set by design (top-N most similar)
                total=len(merged),
                pool_size=len(merged),
                results=merged[offset:offset + top_k],
                metadata=self._meta(mode.value, original_mode, start_time,
                                    vector_candidates=len(vector_results),
                                    merged_candidates=len(merged), fusion=fusion,
                                    sort=sort),
            )

        # SearchMode.HYBRID
        # BM25 candidates are computed OUTSIDE the vector try/except: a BM25
        # failure cannot be "fixed" by falling back to BM25, so it propagates.
        #
        # Pool stability (found by the WP3 walk test): a pool that grows with
        # `offset` re-ranks the boundary between requests — pages overlapped
        # (120 fetched, 95 unique). The pool must be a function of the QUERY,
        # not the page: once BM25 reports the true match count, the pool is
        # grown to cover it (bounded by NEXUS_MAX_CANDIDATES) and refetched
        # once, so every page of this query ranks over the SAME candidate set.
        pool_k = self._candidate_k(top_k, candidates, offset)
        bm25_results = {}
        bm25_total = 0
        if w_bm25 > 0:
            bm25_results, bm25_total = self._bm25_candidates(
                query, pool_k, group_chunks, highlight=highlight)
            stable_k = min(max(pool_k, bm25_total), max_candidates())
            if stable_k > pool_k:
                pool_k = stable_k
                bm25_results, bm25_total = self._bm25_candidates(
                    query, pool_k, group_chunks, highlight=highlight)

        vector_results = {}
        try:
            if w_vector > 0:
                vector_results = self._vector_candidates(query, pool_k, group_chunks,
                                                          allowed_filter)
        except EmbedderUnavailable as exc:
            logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
            return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                       start_time, f"embedder_unavailable:{type(exc).__name__}", fusion, sort, highlight)
        except Exception as exc:
            logger.warning("Hybrid search failed, falling back to BM25: %s", exc)
            return self._bm25_fallback(query, top_k, offset, group_chunks, original_mode,
                                       start_time, f"vector_search_error:{type(exc).__name__}", fusion, sort, highlight)

        merged = self._merge_results(bm25_results, vector_results, fusion=fusion,
                                     parsed=parsed, highlight=highlight,
                                     bm25_weight=w_bm25, vector_weight=w_vector)
        merged = self._sort_raw(merged, sort)
        if diversity > 0.0:
            merged = self._diversify(merged, diversity)
        if not debug:
            # strip diagnostic numbers unless asked for
            for r in merged:
                r.bm25_normalized = None
                r.vector_normalized = None
        return HybridSearchPage(
            # total = corpus-wide match count when BM25 contributes (exact),
            # else the fused pool is the whole answer universe by design.
            total=max(bm25_total, len(merged)) if w_bm25 > 0 else len(merged),
            pool_size=len(merged),
            results=merged[offset:offset + top_k],
            metadata=self._meta(SearchMode.HYBRID.value, original_mode, start_time,
                                bm25_candidates=len(bm25_results),
                                vector_candidates=len(vector_results),
                                merged_candidates=len(merged), fusion=fusion, sort=sort),
        )

    def _bm25_fallback(self, query: str, top_k: int, offset: int, group_chunks: bool,
                       original_mode: SearchMode, start_time: float,
                       reason: str, fusion: str, sort: str = "relevance",
                       highlight: bool = False) -> HybridSearchPage:
        page = self.bm25_search.search_page(query, top_k=top_k, offset=offset,
                                            group_chunks=group_chunks, highlight=highlight)
        results = [
            HybridSearchResult(
                doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
                doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
                matched_chunks=r.matched_chunks, bm25_score=r.score, source="bm25_fallback",
            ) for r in page.results
        ]
        return HybridSearchPage(
            total=page.total,
            pool_size=page.total,
            results=results,
            metadata=self._meta("keyword", original_mode, start_time,
                                fallback=True, fallback_reason=reason,
                                bm25_candidates=page.total, merged_candidates=len(results),
                                fusion=fusion, sort=sort),
        )

    def search(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID) -> list[HybridSearchResult]:
        return self.search_page(query, top_k=top_k, mode=mode).results

    def explain(self, query: str, top_k: int = 10, mode: SearchMode = SearchMode.HYBRID,
                fusion: str = "rrf", bm25_weight: Optional[float] = None,
                vector_weight: Optional[float] = None) -> dict:
        """Debug explanation: per-result raw scores plus each retriever's actual
        contribution to the final score (`bm25_normalized` / `vector_normalized`).
        In weighted fusion the contributions are weight * min-max-normalized score;
        in RRF fusion they are weight / (k + rank). They sum to `final_score`.
        Weight overrides pass through to search_page (per-request weights on
        the shared instance, BUG-06)."""
        page = self.search_page(query, top_k=top_k, mode=mode, fusion=fusion,
                                debug=True, bm25_weight=bm25_weight,
                                vector_weight=vector_weight)
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
