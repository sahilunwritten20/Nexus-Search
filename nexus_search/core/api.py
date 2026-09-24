"""FastAPI application exposing /documents and /search.

Run with: uvicorn nexus_search.core.api:app --reload
DB path: env NEXUS_DB (default nexus_search.db)
"""
import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .bm25 import BM25Search
from .embedders import HashEmbedder, EmbedderUnavailable
from .embedding_sync import EmbeddingSync
from .hybrid_search import HybridSearch, SearchMode, create_hybrid_search
from .indexer import Indexer
from .models import DocumentIn, ExplainRequest, ExplainResponse, ExplainResult, SearchMetadata, SearchResponse, SearchResultOut
from .storage import Storage
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    if _embedding_sync is not None:
        _embedding_sync.close()
    if _hybrid_searcher is not None:
        _hybrid_searcher.close()
    _storage.close()


app = FastAPI(title="Nexus Search — Core", version="0.4.0", lifespan=lifespan)

_NEXUS_DB = os.environ.get("NEXUS_DB", "nexus_search.db")
_storage = Storage(_NEXUS_DB)
_indexer = Indexer(_storage)
_bm25_searcher = BM25Search(_storage)

# Boot resilience: if the embedder can't load, start in keyword-only
# (degraded) mode instead of crashing at import. Every semantic/hybrid
# request then reports fallback=true with the real reason, and /health
# reports degraded. We do NOT silently swap in the hash embedder.
try:
    _vector_store = VectorStoreManager(_NEXUS_DB)
    _vector_error = None
except EmbedderUnavailable as exc:
    logger.error("Vector subsystem unavailable at startup: %s", exc)
    _vector_store = None
    _vector_error = f"embedder_unavailable: {exc}"

if _vector_store is not None:
    _hybrid_searcher = create_hybrid_search(_storage, vector_store=_vector_store, db_path=_NEXUS_DB)
    _embedding_sync = EmbeddingSync(_vector_store, batch_size=1)
    _embedding_sync.attach(_indexer)
else:
    _hybrid_searcher = None
    _embedding_sync = None


def _keyword_only_page(q: str, top_k: int, offset: int, requested_mode: str,
                       reason: str) -> SearchResponse:
    """BM25-only response used when the vector subsystem never came up."""
    if requested_mode == "keyword":
        fallback = False
        mode_used = "keyword"
    else:
        fallback = True
        mode_used = "keyword"
    page = _bm25_searcher.search_page(q, top_k=top_k, offset=offset)
    results = [
        SearchResultOut(
            doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
            doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
            matched_chunks=r.matched_chunks, bm25_score=r.score,
            source="bm25" if not fallback else "bm25_fallback",
        ) for r in page.results
    ]
    return SearchResponse(
        query=q, total_results=page.total, offset=offset, top_k=top_k, results=results,
        metadata=SearchMetadata(
            mode=mode_used, requested_mode=requested_mode, mode_used=mode_used,
            bm25_candidates=page.total, merged_candidates=len(results),
            fallback=fallback, fallback_reason=reason if fallback else None,
        ),
    )


@app.post("/documents", status_code=201)
def add_document(doc: DocumentIn):
    try:
        _indexer.add_document(
            doc_id=doc.doc_id, content=doc.content, title=doc.title,
            doc_type=doc.doc_type, metadata=doc.metadata,
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")
    return {"doc_id": doc.doc_id, "status": "indexed"}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if not _indexer.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"doc_id": doc_id, "status": "deleted"}


@app.get("/search", response_model=SearchResponse)
def search(
    q: str,
    top_k: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0, le=10_000),
    mode: str = Query(default="hybrid", pattern="^(keyword|semantic|hybrid)$"),
    bm25_weight: float = Query(default=1.0, ge=0),
    vector_weight: float = Query(default=1.0, ge=0),
    fusion: str = Query(default="rrf", pattern="^(rrf|weighted)$"),
    candidates: int = Query(default=50, ge=1, le=500),
    debug: bool = Query(default=False),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="q must not be empty")
    top_k = min(max(top_k, 1), 100)

    # Validate weights
    if bm25_weight == 0 and vector_weight == 0:
        raise HTTPException(status_code=400, detail="At least one weight must be > 0")

    # Degraded boot: vector subsystem never came up -> honest keyword-only
    if _vector_store is None:
        return _keyword_only_page(q, top_k, offset, mode, _vector_error)

    # Create a new HybridSearch with custom weights for this request
    hybrid_searcher = HybridSearch(
        _storage,
        bm25_weight=bm25_weight,
        vector_weight=vector_weight,
        vector_store=_vector_store,
        db_path=_NEXUS_DB,
    )

    search_mode = SearchMode(mode)
    page = hybrid_searcher.search_page(
        q, top_k=top_k, offset=offset, mode=search_mode,
        fusion=fusion, candidates=candidates, debug=debug,
    )

    metadata = None
    if page.metadata:
        metadata = SearchMetadata(**page.metadata)

    return SearchResponse(
        query=q,
        total_results=page.total,
        offset=offset,
        top_k=top_k,
        results=[SearchResultOut(**r.__dict__) for r in page.results],
        metadata=metadata,
    )


@app.post("/search/explain", response_model=ExplainResponse)
def explain_search(req: ExplainRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")
    req.top_k = min(max(req.top_k, 1), 100)

    try:
        search_mode = SearchMode(req.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="mode must be keyword, semantic, or hybrid")
    if req.fusion not in ("rrf", "weighted"):
        raise HTTPException(status_code=400, detail="fusion must be rrf or weighted")
    if req.bm25_weight == 0 and req.vector_weight == 0:
        raise HTTPException(status_code=400, detail="At least one weight must be > 0")

    if _hybrid_searcher is None:
        kw = _keyword_only_page(req.query, req.top_k, 0, req.mode, _vector_error)
        return ExplainResponse(
            query=req.query, mode=req.mode, metadata=kw.metadata,
            results=[ExplainResult(doc_id=r.doc_id, final_score=r.score, bm25_score=r.bm25_score,
                                   source=r.source or "bm25_fallback", title=r.title,
                                   chunk_id=r.chunk_id) for r in kw.results],
        )

    # Per-request weights, same as /search
    searcher = HybridSearch(_storage, bm25_weight=req.bm25_weight,
                            vector_weight=req.vector_weight,
                            vector_store=_vector_store, db_path=_NEXUS_DB)
    explanation = searcher.explain(req.query, top_k=req.top_k, mode=search_mode, fusion=req.fusion)
    metadata = SearchMetadata(**explanation["metadata"]) if explanation["metadata"] else SearchMetadata(mode=req.mode)

    return ExplainResponse(
        query=explanation["query"],
        mode=explanation["mode"],
        metadata=metadata,
        results=[ExplainResult(**r) for r in explanation["results"]],
    )


@app.get("/health")
def health():
    if _vector_store is None:
        return {
            "status": "degraded",
            "documents": _storage.document_count(),
            "vectors": 0,
            "coverage": 0.0,
            "embedder": {"name": "unavailable", "dim": None, "degraded": True,
                         "error": _vector_error},
        }
    vector_stats = _vector_store.get_stats()
    vector_count = vector_stats.get("count", 0)
    doc_count = _storage.document_count()
    coverage = vector_count / doc_count if doc_count > 0 else 1.0

    return {
        "status": "ok" if coverage >= 0.9 else "degraded",
        "documents": doc_count,
        "vectors": vector_count,
        "coverage": coverage,
        "embedder": {
            "name": _vector_store.embedder.name,
            "dim": _vector_store.embedder.dim,
            "degraded": isinstance(_vector_store.embedder, HashEmbedder),
        },
    }


@app.get("/metrics")
def metrics():
    """Prometheus-style metrics endpoint."""
    sync_stats = _embedding_sync.get_stats() if _embedding_sync is not None else None
    vector_stats = _vector_store.get_stats() if _vector_store is not None else None
    return {
        "embedding_sync": sync_stats,
        "vector_store": vector_stats,
    }