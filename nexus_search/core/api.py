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
from .embedders import HashEmbedder
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
    _embedding_sync.close()
    _hybrid_searcher.close()
    _storage.close()


app = FastAPI(title="Nexus Search — Core", version="0.4.0", lifespan=lifespan)

_storage = Storage(os.environ.get("NEXUS_DB", "nexus_search.db"))
_indexer = Indexer(_storage)
_bm25_searcher = BM25Search(_storage)

_vector_store = VectorStoreManager(os.environ.get("NEXUS_DB", "nexus_search.db"))
_hybrid_searcher = create_hybrid_search(
    _storage, 
    vector_store=_vector_store, 
    db_path=os.environ.get("NEXUS_DB", "nexus_search.db")
)

# EmbeddingSync for incremental updates
_embedding_sync = EmbeddingSync(_vector_store, batch_size=1)
_embedding_sync.attach(_indexer)


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

    # Create a new HybridSearch with custom weights for this request
    hybrid_searcher = HybridSearch(
        _storage,
        bm25_weight=bm25_weight,
        vector_weight=vector_weight,
        vector_store=_vector_store,
        db_path=os.environ.get("NEXUS_DB", "nexus_search.db"),
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

    explanation = _hybrid_searcher.explain(req.query, top_k=req.top_k, mode=search_mode, fusion=req.fusion)
    metadata = SearchMetadata(**explanation["metadata"]) if explanation["metadata"] else SearchMetadata(mode=req.mode)

    return ExplainResponse(
        query=explanation["query"],
        mode=explanation["mode"],
        metadata=metadata,
        results=[ExplainResult(**r) for r in explanation["results"]],
    )


@app.get("/health")
def health():
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
    sync_stats = _embedding_sync.get_stats()
    vector_stats = _vector_store.get_stats()
    return {
        "embedding_sync": sync_stats,
        "vector_store": vector_stats,
    }