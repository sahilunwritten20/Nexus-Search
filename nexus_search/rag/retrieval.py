"""P7-1 â€” RAG retrieval over the ONE shared HybridSearch.

There is NO second retrieval path here (PHASE7_PLAN Â§7): `RagRetriever`
holds the shared `HybridSearch` instance the API already owns and calls the
same `search_page(...)` `/search` calls â€” with `group_chunks=False` so the
answer path cites exact stored chunk rows (`parent#chunkN`), plus the same
query-understanding (spell correction / synonym expansion), the same
Phase 5 reranker and the same filter semantics (`type:` / `lang:` via the
query parser's `matches_filters`).

Raw-signal contract (WP12-B2): fused scores are rank/normalization
artifacts and never gate anything. `RagChunk.bm25_score` /
`RagChunk.vector_score` carry the RAW per-retriever scores through fusion
AND rerank, including the `None` cases where only one retriever found the
row â€” these are the only valid inputs to the P7-4 refuse-gate.

Honesty contract: when the vector subsystem is down the retriever degrades
to keyword retrieval with `degraded_reason` set (never a silent swap); when
retrieval itself fails, `retrieval_error` carries the exception TYPE name
only (the service maps it to a clean 503 â€” internals never reach clients).
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("nexus_search.rag.retrieval")

# Bounds reused from /search (api.py MAX_QUERY_CHARS / MAX_QUERY_WORDS):
# attacker-controlled parse cost is bounded at the same points, so a /ask
# query can never be more expensive than a /search query.
MAX_QUERY_CHARS = 2000
MAX_QUERY_WORDS = 128
MAX_TOP_K = 100

# The request-model filters dict supports exactly the two filter families
# the query parser understands (filters.py); anything else would invent a
# second filter semantics â€” refused loudly instead.
_ALLOWED_FILTER_KEYS = {"doc_type", "language"}


def use_rerank_from_env() -> bool:
    """NEXUS_RAG_USE_RERANK (default ON): RAG retrieval applies the same
    Phase 5 reranker (+ Phase 6 authority/popularity weights via their env
    knobs, all default 0.0 = inert) as `/search`. Raw bm25/vector scores
    survive the rerank untouched (they live on the result object)."""
    raw = (os.environ.get("NEXUS_RAG_USE_RERANK") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


@dataclass
class RagChunk:
    """One retrieved stored row, with the raw gate signals attached.

    `chunk_id` is the exact stored row id (`parent#chunkN` for chunk rows,
    the plain doc id otherwise) â€” citations resolve through
    Storage.get_document on this id. `parent_id` is the metadata parent
    when the row is a chunk, else the row id itself."""
    chunk_id: str
    parent_id: str = ""
    chunk_index: Optional[int] = None
    chunk_count: Optional[int] = None
    title: str = ""
    doc_type: str = ""
    url: Optional[str] = None
    # RAW per-retriever scores (None = that retriever did not find this
    # row). Never normalized, never fused â€” the refuse-gate's only inputs.
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    fused_score: float = 0.0
    source: str = "hybrid"
    metadata: dict = field(default_factory=dict)
    text: str = ""  # full row content, from the ONE batched fetch


@dataclass
class RagRetrieval:
    """The retriever's answer for one query."""
    query: str
    language: str = "unknown"        # ranking.query.detect_language() result
    mode: str = "hybrid"
    results: list[RagChunk] = field(default_factory=list)
    total: int = 0
    reranked: bool = False
    degraded_reason: Optional[str] = None    # vector side down -> keyword
    retrieval_error: Optional[str] = None     # exception TYPE name only

    @property
    def degraded(self) -> bool:
        return self.degraded_reason is not None


class RagRetriever:
    """Chunk-granular retrieval over the shared HybridSearch instance."""

    def __init__(self, hybrid, storage, *, bm25_search=None,
                 use_rerank: Optional[bool] = None,
                 understanding_fn: Optional[Callable] = None,
                 rerank_fn: Optional[Callable] = None,
                 weights_fn: Optional[Callable] = None,
                 link_intel=None):
        # `hybrid` may be None: the vector subsystem never came up at boot
        # (api.py's degraded-boot path) â€” keyword-only retrieval with a
        # LOUD degraded_reason. `bm25_search` is then required.
        self.hybrid = hybrid
        self.storage = storage
        self.bm25_search = bm25_search
        self.use_rerank = use_rerank_from_env() if use_rerank is None else use_rerank
        self.link_intel = link_intel
        if understanding_fn is None:
            from ..ranking.query import understand_query
            understanding_fn = understand_query
        self._understanding_fn = understanding_fn
        if rerank_fn is None:
            from ..ranking.ranker import rerank as _rerank
            rerank_fn = _rerank
        self._rerank_fn = rerank_fn
        if weights_fn is None:
            from ..ranking.ranker import RankingWeights
            weights_fn = RankingWeights.from_env
        self._weights_fn = weights_fn

    # ------------------------------------------------------------------ api

    @property
    def embedder_name(self) -> Optional[str]:
        """Identity of the shared embedder (e.g. 'hash:384') â€” gates the
        per-embedder calibrated floors. None when the vector subsystem is
        absent (keyword-only degraded mode)."""
        if self.hybrid is None:
            return None
        try:
            return self.hybrid.vector_store.embedder.name
        except Exception:  # pragma: no cover - defensive, embedder is up
            return None

    def validate_query(self, query: str) -> None:
        """The /search bounds, enforced identically here so /ask queries
        can never be more expensive than /search queries."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError("query too long")
        if len(query.split()) > MAX_QUERY_WORDS:
            raise ValueError(f"query has more than {MAX_QUERY_WORDS} words")

    def retrieve(self, query: str, *, filters: Optional[dict] = None,
                 k: int = 10, mode: str = "hybrid") -> RagRetrieval:
        """Retrieve up to `k` stored rows for `query`.

        `filters` maps {'doc_type': ..., 'language': ...} onto the exact
        `type:` / `lang:` query syntax /search uses â€” rendered INTO the
        query string so the parser + matches_filters + the vector pushdown
        all behave identically (no second filter code path)."""
        self.validate_query(query)
        k = min(max(k, 1), MAX_TOP_K)

        rendered = query
        filter_note = self._render_filters(filters)
        if filter_note:
            rendered = f"{query} {filter_note}"

        from ..ranking.query import detect_language
        language = detect_language(query)

        try:
            results, total, degraded_reason, reranked = self._retrieve_rows(
                rendered, k, mode)
        except Exception:
            # Type name only â€” the API maps this to a clean 503; stack
            # traces / messages never reach a response body.
            logger.exception("rag retrieval failed")
            return RagRetrieval(query=query, language=language, mode=mode,
                                retrieval_error="retrieval_error")

        # ONE batched fetch for every full row text (no N+1 â€” the WP12-B4
        # discipline holds on the RAG path too).
        docs_by_id = self.storage.get_documents([r.doc_id for r in results])
        chunks = []
        for r in results:
            doc = docs_by_id.get(r.doc_id)
            meta = dict(r.metadata or {})
            text = doc.content if doc is not None else ""
            parent = str(meta.get("parent_id") or r.doc_id)
            chunks.append(RagChunk(
                chunk_id=r.doc_id,
                parent_id=parent,
                chunk_index=meta.get("chunk_index"),
                chunk_count=meta.get("chunk_count"),
                title=r.title or (doc.title if doc is not None else ""),
                doc_type=r.doc_type or (doc.doc_type if doc is not None else ""),
                url=meta.get("url"),
                bm25_score=r.bm25_score,
                vector_score=r.vector_score,
                fused_score=float(r.score or 0.0),
                source=r.source or "hybrid",
                metadata=meta,
                text=text,
            ))
        return RagRetrieval(query=query, language=language, mode=mode,
                            results=chunks, total=total,
                            reranked=reranked, degraded_reason=degraded_reason)

    # ------------------------------------------------------------ internals

    @staticmethod
    def _render_filters(filters: Optional[dict]) -> str:
        if not filters:
            return ""
        unknown = set(filters) - _ALLOWED_FILTER_KEYS
        if unknown:
            raise ValueError(f"unsupported filter keys: {sorted(unknown)}")
        parts = []
        if filters.get("doc_type"):
            parts.append(f"type:{filters['doc_type']}")
        if filters.get("language"):
            parts.append(f"lang:{filters['language']}")
        return " ".join(parts)

    def _retrieve_rows(self, query: str, k: int, mode: str):
        """Returns (rows, total, degraded_reason, reranked)."""
        if self.hybrid is None:
            # Degraded boot: vector subsystem never came up. Keyword-only,
            # flagged â€” never a silent swap.
            if self.bm25_search is None:
                raise RuntimeError("no retriever available")
            page = self.bm25_search.search_page(query, top_k=k, offset=0,
                                                group_chunks=False)
            rows = [_row_from_bm25(r) for r in page.results]
            reranked = False
            if self.use_rerank and rows:
                understanding = self._understanding_fn(query, self.storage)
                ranked = self._rerank_fn(
                    rows, query, weights=self._weights_fn(),
                    storage=self.storage, understanding=understanding,
                    link_intel=self.link_intel)
                rows = [rr.result for rr in ranked]
                reranked = True
            return rows, page.total, "vector_subsystem_unavailable", reranked

        from ..core.hybrid_search import SearchMode
        search_mode = SearchMode(mode)

        understanding = self._understanding_fn(query, self.storage)
        page = self.hybrid.search_page(
            query, top_k=k, offset=0, group_chunks=False, mode=search_mode,
            understanding=understanding)

        degraded_reason = None
        meta = page.metadata or {}
        if meta.get("fallback"):
            # The vector side died mid-request and HybridSearch fell back
            # to BM25 â€” surface it (its own metadata reason, type-only).
            degraded_reason = str(meta.get("fallback_reason") or "vector_fallback")

        rows = list(page.results)
        reranked = False
        if self.use_rerank and rows:
            # The same Phase 5 reranker (and Phase 6 weights, all default
            # 0.0 = inert) /search applies â€” behind NEXUS_RAG_USE_RERANK,
            # default ON. Raw bm25/vector scores ride along on the result
            # objects, so they survive the rerank by construction.
            ranked = self._rerank_fn(
                rows, query, weights=self._weights_fn(),
                storage=self.storage, understanding=understanding,
                link_intel=self.link_intel)
            rows = [rr.result for rr in ranked]
            reranked = True
        return rows, page.total, degraded_reason, reranked


def _row_from_bm25(r):
    """Adapt a keyword-only (degraded) SearchResult to the HybridSearchResult
    shape the mapping below reads (bm25 raw score; vector None by
    construction â€” the vector side is absent)."""
    from ..core.hybrid_search import HybridSearchResult
    return HybridSearchResult(
        doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
        doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
        matched_chunks=r.matched_chunks, bm25_score=r.score,
        vector_score=None, source="bm25_degraded",
    )

