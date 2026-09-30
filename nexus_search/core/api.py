"""FastAPI application exposing /documents and /search.

Run with: uvicorn nexus_search.core.api:app --reload
DB path: env NEXUS_DB (default nexus_search.db)
"""
import hashlib
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .bm25 import BM25Search
from .embedders import HashEmbedder, EmbedderUnavailable
from .query_cache import QueryCache
from ..ranking.ab import ExperimentLog, assign_variant
from ..ranking.query import understand_query
from .query_parser import parse_query
from ..ranking.ranker import RankingWeights, WeightedSumModel, rerank as rerank_results
from .embedding_sync import EmbeddingSync
from .hybrid_search import HybridSearch, SearchMode, create_hybrid_search
from .indexer import Indexer
from .models import BulkDocumentIn, DocumentIn, DocumentOut, ExplainRequest, ExplainResponse, ExplainResult, SearchMetadata, SearchResponse, SearchResultOut
from .storage import Storage
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.api")

# Fail CLOSED on auth in anything but explicit dev. NEXUS_ENV defaults to
# "production" (opt OUT of auth must be a conscious decision, not a forgotten
# env var); a production boot without NEXUS_API_KEY refuses to start.
_ENV = os.environ.get("NEXUS_ENV", "production").strip().lower() or "production"
_API_KEY = os.environ.get("NEXUS_API_KEY", "")
if not _API_KEY and _ENV != "dev":
    raise RuntimeError(
        "NEXUS_API_KEY is not set and NEXUS_ENV "
        f"!= 'dev' (got {_ENV!r}; unset counts as 'production'). Refusing to "
        "boot with unauthenticated write endpoints. Set NEXUS_API_KEY=... to "
        "run the API, or NEXUS_ENV=dev for local development only.")
if not _API_KEY:
    logger.warning("NEXUS_API_KEY is not set — write endpoints are UNAUTHENTICATED "
                   "(dev mode, only because NEXUS_ENV=dev).")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    if _embedding_sync is not None:
        _embedding_sync.close()
    if _hybrid_searcher is not None:
        _hybrid_searcher.close()
    if experiment_log is not None:
        experiment_log.close()
    if _link_graph is not None:
        _link_graph.close()
    _storage.close()


# OpenAPI explorer is a dev convenience; in production it's information
# disclosure (exact params, paths). NEXUS_ENV=dev keeps them on.
_is_dev = _ENV == "dev"
app = FastAPI(title="Nexus Search — Core", version="0.4.0", lifespan=lifespan,
              docs_url="/docs" if _is_dev else None,
              redoc_url="/redoc" if _is_dev else None,
              openapi_url="/openapi.json" if _is_dev else None)

# CORS is opt-in and explicit (NEXUS_CORS_ORIGINS="https://a,https://b");
# unset = no CORS headers at all — browsers default-deny cross-origin.
_cors_origins = [o.strip() for o in os.environ.get("NEXUS_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                       allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["X-API-Key", "Content-Type"])

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
    # type name only — the raw message can carry paths/model names to clients
    _vector_error = f"embedder_unavailable:{type(exc).__name__}"

if _vector_store is not None:
    _hybrid_searcher = create_hybrid_search(_storage, vector_store=_vector_store, db_path=_NEXUS_DB)
    _embedding_sync = EmbeddingSync(_vector_store, batch_size=1)
    _embedding_sync.attach(_indexer)
else:
    _hybrid_searcher = None
    _embedding_sync = None

# Phase 5 A/B query log — shipped as instrumentation; never gating a result.
try:
    experiment_log = ExperimentLog(_NEXUS_DB)
except Exception as exc:  # logging must never take search down
    logger.warning("Experiment log unavailable: %s", exc)
    experiment_log = None

# Phase 6 link intelligence: read-side ONLY. Weights ship at 0.0 by default
# (RankingWeights.from_env) so this is inert until an operator opts in after
# evaluation (docs/PHASE6_PLAN.md rollout).
try:
    from ..links.graph import LinkGraph
    _link_graph = LinkGraph(_NEXUS_DB)
except Exception as exc:
    logger.warning("Link graph unavailable (ranking stays neutral): %s", exc)
    _link_graph = None

# Query cache: TTL-bounded, and cleared on every write (see query_cache.py)
_query_cache = QueryCache(ttl_seconds=float(os.environ.get("NEXUS_CACHE_TTL", "5")))


def _keyword_only_page(q: str, top_k: int, offset: int, requested_mode: str,
                       reason: str, facets: Optional[str] = None) -> SearchResponse:
    """BM25-only response used when the vector subsystem never came up."""
    if requested_mode == "keyword":
        fallback = False
        mode_used = "keyword"
    else:
        fallback = True
        mode_used = "keyword"
    page = _bm25_searcher.search_page(q, top_k=top_k, offset=offset)
    facet_out = None
    facets_truncated = False
    if facets:  # same honest-count contract as the non-degraded path
        fields = [f.strip() for f in facets.split(",") if f.strip()]
        full = _bm25_searcher.search_page(q, top_k=min(max(page.total, 1), 500), offset=0)
        docs_by_id = _storage.get_documents([r.doc_id for r in full.results])
        from ..core.filters import facet_counts
        facet_out = facet_counts([d for d in docs_by_id.values() if d is not None], fields)
        facets_truncated = page.total > 500
    results = [
        SearchResultOut(
            doc_id=r.doc_id, score=r.score, title=r.title, snippet=r.snippet,
            doc_type=r.doc_type, metadata=r.metadata, chunk_id=r.chunk_id,
            matched_chunks=r.matched_chunks, bm25_score=r.score,
            source="bm25" if not fallback else "bm25_fallback",
        ) for r in page.results
    ]
    has_more = (offset + len(results)) < page.total
    next_cursor = None
    if has_more:
        import base64
        next_cursor = base64.urlsafe_b64encode(
            f"offset:{offset + len(results)}".encode()).decode().rstrip("=")
    return SearchResponse(
        query=q, total_results=page.total, offset=offset, top_k=top_k, results=results,
        metadata=SearchMetadata(
            mode=mode_used, requested_mode=requested_mode, mode_used=mode_used,
            bm25_candidates=page.total, merged_candidates=len(results),
            fallback=fallback, fallback_reason=reason if fallback else None,
            has_more=has_more, next_cursor=next_cursor,
            facets_truncated=facets_truncated,
        ),
        facets=facet_out,
    )


# ---------------------------------------------------------------------------
# AuthN: write endpoints require the X-API-Key header whenever NEXUS_API_KEY
# is set; read-only /search and friends stay open (aligns with how this
# prototype is meant to be embedded). _API_KEY/_ENV and the fail-closed boot
# check live at the top of the module (see above).
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# Rate limiting (slowapi): /search + the write endpoints. Scoped per API key
# when one is presented (hashed — never the raw secret), else per client IP.
# NEXUS_RATE_LIMIT="0" (or "") disables; headers carry X-RateLimit-*/Retry-After.
_RATE_LIMIT = os.environ.get("NEXUS_RATE_LIMIT", "60/minute").strip()


def _rate_limit_key(request: Request) -> str:
    key = request.headers.get("X-API-Key")
    if key:
        return "key:" + hashlib.sha256(key.encode()).hexdigest()[:24]
    return get_remote_address(request) or "unknown"


limiter = Limiter(
    key_func=_rate_limit_key,
    headers_enabled=True,
    enabled=bool(_RATE_LIMIT) and _RATE_LIMIT != "0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Reads are open by DESIGN (embeddable public search). Operators with private
# corpora opt into key-gated reads with NEXUS_REQUIRE_AUTH_FOR_READS=1; the
# dependency itself still no-ops when no NEXUS_API_KEY is configured.
_READ_AUTH = [Depends(require_api_key)] if os.environ.get(
    "NEXUS_REQUIRE_AUTH_FOR_READS", "").strip().lower() in ("1", "true", "yes") else []


def require_api_key(api_key: Optional[str] = Security(_api_key_header)):
    """401 when the server is configured with a key and the caller's doesn't
    match. Constant-time compare; missing config = open dev mode."""
    if not _API_KEY:
        return  # dev mode, open by explicit configuration
    import hmac
    if api_key is None or not hmac.compare_digest(api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


@app.post("/documents", status_code=201, dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def add_document(request: Request, doc: DocumentIn, response: Response):
    try:
        _indexer.add_document(
            doc_id=doc.doc_id, content=doc.content, title=doc.title,
            doc_type=doc.doc_type, metadata=doc.metadata,
        )
        _query_cache.clear()  # writes invalidate cached pages
    except sqlite3.Error:
        logger.exception("storage error indexing %s", doc.doc_id)
        raise HTTPException(status_code=500, detail="storage error")  # details stay in logs
    return {"doc_id": doc.doc_id, "status": "indexed"}


@app.post("/documents/bulk", dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def add_documents_bulk(request: Request, body: BulkDocumentIn, response: Response):
    """Batch write: N docs, one round trip. Per-doc failures are reported,
    not fatal — a bad row must not poison the batch (matches the ingest
    pipeline's dead-letter stance)."""
    indexed, failed = 0, []
    for doc in body.documents:
        try:
            _indexer.add_document(
                doc_id=doc.doc_id, content=doc.content, title=doc.title,
                doc_type=doc.doc_type, metadata=doc.metadata,
            )
            indexed += 1
        except sqlite3.Error:
            logger.exception("storage error indexing %s", doc.doc_id)
            failed.append({"doc_id": doc.doc_id, "error": "storage_error"})
        except ValueError as exc:  # e.g. vector dim mismatch surfaces here
            failed.append({"doc_id": doc.doc_id, "error": type(exc).__name__})
    if indexed:
        _query_cache.clear()  # writes invalidate cached pages
    return {"indexed": indexed, "failed": failed, "total": len(body.documents)}


@app.get("/documents/{doc_id}", response_model=DocumentOut, dependencies=_READ_AUTH)
@limiter.limit(_RATE_LIMIT)
def get_document(request: Request, doc_id: str, response: Response):
    """Read-back endpoint: the stored row for one doc_id (404 when absent).
    Chunk parents return 404 — fetch the chunk row ids directly."""
    doc = _storage.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentOut(doc_id=doc.doc_id, title=doc.title, content=doc.content,
                       doc_type=doc.doc_type, length=doc.length,
                       metadata=doc.metadata, added_at=doc.added_at)


@app.delete("/documents/{doc_id}", dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def delete_document(request: Request, doc_id: str, response: Response):
    if not _indexer.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    _query_cache.clear()  # writes invalidate cached pages
    return {"doc_id": doc_id, "status": "deleted"}


MAX_QUERY_CHARS = 2000  # attacker-controlled parse cost must be bounded
# BUG-01 fast guard: a wall of junk words is rejected before any parsing.
# Real search queries are nowhere near this (public engines cap at ~32
# words); the guard sits at the same default as NEXUS_MAX_QUERY_TERMS so
# anything the parser would have to cap-term-truncate is refused up front
# with a clear 400 instead of served partially.
MAX_QUERY_WORDS = 128


@app.get("/search", response_model=SearchResponse, dependencies=_READ_AUTH)
@limiter.limit(_RATE_LIMIT)
def search(
    request: Request,
    response: Response,
    q: str,
    top_k: int = Query(default=10, ge=1),
    offset: int = Query(default=0, ge=0, le=10_000),
    diversity: float = Query(default=0.0, ge=0.0, le=1.0),
    mode: str = Query(default="hybrid", pattern="^(keyword|semantic|hybrid)$"),
    bm25_weight: float = Query(default=1.0, ge=0),
    vector_weight: float = Query(default=1.0, ge=0),
    fusion: str = Query(default="rrf", pattern="^(rrf|weighted)$"),
    candidates: int = Query(default=50, ge=1, le=500),
    debug: bool = Query(default=False),
    rerank: Optional[bool] = Query(default=None),
    session: Optional[str] = Query(default=None),
    sort: str = Query(default="relevance", pattern="^(relevance|freshness|title)$"),
    highlight: bool = Query(default=False),
    facets: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="q must not be empty")
    if len(q) > MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="q too long")
    if len(q.split()) > MAX_QUERY_WORDS:
        raise HTTPException(status_code=400,
                            detail=f"q has more than {MAX_QUERY_WORDS} words")
    top_k = min(max(top_k, 1), 100)

    # Cursor pagination (ADDITIONAL to offset, never replacing it): the cursor
    # is just a base64'd "offset:N" — stateless, resumable, honest. offset and
    # cursor together -> cursor wins (documented).
    if cursor:
        import base64, binascii
        try:
            pad = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode(cursor + pad).decode("ascii")
            prefix, _, value = raw.partition(":")
            if prefix != "offset" or not value.isdigit():
                raise ValueError
            offset = int(value)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="invalid cursor")
        offset = min(offset, 10_000)

    # Phase 5 A/B instrumentation: `session=...` buckets the request into the
    # deterministic rerank experiment (control/treatment). An explicit
    # rerank=true/false always overrides the bucket. Default (no session, no
    # rerank param) is OFF — identical behavior to pre-Phase-5.
    variant = "off"
    if session:
        variant = assign_variant(session, ["control", "treatment"])
    rerank_flag = rerank if rerank is not None else (variant == "treatment")

    # Validate weights
    if bm25_weight == 0 and vector_weight == 0:
        raise HTTPException(status_code=400, detail="At least one weight must be > 0")

    # Query understanding participates by default: spell-correction (in-
    # vocabulary only) and synonym expansion widen the retrieval pool. One
    # computation serves retrieval, the facet pass, AND rerank features.
    # It is LAZY (BUG-01): a cache hit for a plain search never pays for it,
    # and its cost is bounded in ranking/query.py regardless.
    understanding = None

    def _understanding():
        nonlocal understanding
        if understanding is None:
            understanding = understand_query(q, _storage)
        return understanding

    # Degraded boot: vector subsystem never came up -> honest keyword-only.
    # Same guard as hybrid_search: boolean/negated-filter queries keep their
    # original form (rewriting would drop the structure).
    if _vector_store is None:
        probe = parse_query(q)
        retrieval_q = q if (probe.has_boolean or probe.not_filters) \
            else _understanding().to_retrieval_query()
        return _keyword_only_page(retrieval_q, top_k,
                                  offset, mode, _vector_error, facets=facets)

    if diversity > 0.0 and sort not in (None, "relevance"):
        # both reorder after ranking; combining them silently would be mush
        raise HTTPException(status_code=400,
                            detail="diversity and non-relevance sort cannot be combined")

    # Create a new HybridSearch with custom weights for this request
    hybrid_searcher = HybridSearch(
        _storage,
        bm25_weight=bm25_weight,
        vector_weight=vector_weight,
        vector_store=_vector_store,
        db_path=_NEXUS_DB,
    )

    search_mode = SearchMode(mode)

    cache_key = QueryCache.key_of(
        q, top_k=top_k, offset=offset, mode=mode, bm25_weight=bm25_weight,
        vector_weight=vector_weight, fusion=fusion, candidates=candidates,
        debug=debug, sort=sort, highlight=highlight, rerank=rerank_flag,
        diversity=diversity,
    )
    cached_page = _query_cache.get(cache_key)
    if cached_page is not None:
        page = cached_page
    else:
        page = hybrid_searcher.search_page(
            q, top_k=top_k, offset=offset, mode=search_mode,
            fusion=fusion, candidates=candidates, debug=debug,
            sort=sort, highlight=highlight, diversity=diversity,
            understanding=_understanding(),
        )
        _query_cache.set(cache_key, page)

    # Phase 5 OPT-IN re-ranking. rerank=False (the default) is a strict no-op:
    # the page from search_page is returned byte-for-byte unchanged.
    import dataclasses
    results = page.results
    if rerank_flag:
        ranked = rerank_results(results, q, storage=_storage,
                                understanding=_understanding(),
                                weights=RankingWeights.from_env(), link_intel=_link_graph)
        # copy, never mutate: page.results may be a cached object
        results = [dataclasses.replace(rr.result, score=rr.final_score) for rr in ranked]
        if page.metadata is not None:
            page.metadata = {**page.metadata, "reranked": True}
        if experiment_log is not None:
            experiment_log.record(q, variant, f"{mode}+rerank")
    elif experiment_log is not None and variant != "off":
        experiment_log.record(q, variant, mode)

    metadata = None
    if page.metadata:
        metadata = SearchMetadata(**page.metadata)

    # Stage 4: has_more + next_cursor (computed, never persisted). pool_size
    # is the fused-candidate boundary — total can exceed it (hybrid reports
    # the true corpus match count), and "more pages" is about the pageable
    # pool, not the corpus.
    if metadata is not None:
        pageable = page.pool_size if page.pool_size is not None else page.total
        metadata.has_more = (offset + len(results)) < pageable
        if metadata.has_more:
            import base64
            nxt = base64.urlsafe_b64encode(f"offset:{offset + len(results)}".encode()).decode().rstrip("=")
            metadata.next_cursor = nxt

    # Stage 4: facet counts — computed over the match population (a fresh
    # pass capped at 500 docs), NOT just the visible page. When the cap bites
    # we SAY so instead of serving silently-incomplete counts.
    facet_out = None
    facets_truncated = False
    if facets:
        fields = [f.strip() for f in facets.split(",") if f.strip()]
        _FACET_SAMPLE_CAP = 500
        facets_truncated = page.total > _FACET_SAMPLE_CAP
        full = hybrid_searcher.search_page(q, top_k=min(max(page.total, 1), _FACET_SAMPLE_CAP),
                                           offset=0, mode=search_mode, fusion=fusion,
                                           understanding=_understanding(),  # facets follow retrieval
                                           # the default candidate pool (50) would
                                           # truncate counts below the sample cap
                                           candidates=min(max(page.total, 1), _FACET_SAMPLE_CAP))
        from ..core.filters import facet_counts
        docs_by_id = _storage.get_documents([r.doc_id for r in full.results])
        facet_out = facet_counts([d for d in docs_by_id.values() if d is not None], fields)
    if metadata is not None and facets is not None:
        metadata.facets_truncated = facets_truncated

    return SearchResponse(
        query=q,
        total_results=page.total,
        offset=offset,
        top_k=top_k,
        results=[SearchResultOut(**r.__dict__) for r in results],
        metadata=metadata,
        facets=facet_out,
    )


@app.post("/search/explain", response_model=ExplainResponse,
          dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def explain_search(request: Request, req: ExplainRequest, response: Response):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")
    if len(req.query) > MAX_QUERY_CHARS:
        raise HTTPException(status_code=400, detail="query too long")
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

    results_out = [ExplainResult(**r) for r in explanation["results"]]
    if req.rerank:
        # Stage 2 debug visibility: raw signal values + blended rerank score
        # per result (the same numbers the ranker actually used).
        from ..ranking.features import build_context, extract_features
        from ..ranking.query import understand_query as _understand
        understanding = _understand(req.query, _storage)
        ctx = build_context(req.query, understanding=understanding, link_intel=_link_graph)
        docs_by_id = _storage.get_documents([r.doc_id for r in results_out])
        model = WeightedSumModel(RankingWeights.from_env())
        for row in results_out:
            feats = extract_features(row.doc_id, _storage, ctx,
                                     doc=docs_by_id.get(row.doc_id))
            row.features = feats.as_dict()
            row.reranked_score = model.score(feats)

    return ExplainResponse(
        query=explanation["query"],
        mode=explanation["mode"],
        metadata=metadata,
        results=results_out,
    )


# ONE shared suggester: vocabulary reload happens only when the document
# count actually changes (Suggester._refresh), not per request.
_suggester = None


def _get_suggester():
    global _suggester
    if _suggester is None:
        from ..ranking.suggestions import Suggester
        _suggester = Suggester(_storage)
    return _suggester


@app.get("/suggest", dependencies=_READ_AUTH)
@limiter.limit(_RATE_LIMIT)
def suggest(request: Request, response: Response, q: str,
            limit: int = Query(default=10, ge=1, le=50)):
    """Autocomplete over the live index vocabulary — sorted-list + bisect
    prefix scan (see ranking/suggestions.py for the no-trie rationale)."""
    return {"query": q, "suggestions": _get_suggester().suggest(q, limit=limit)}


@app.get("/related", dependencies=_READ_AUTH)
@limiter.limit(_RATE_LIMIT)
def related(request: Request, response: Response, q: str,
            limit: int = Query(default=5, ge=1, le=20)):
    """Related searches: query-log co-occurrence when it exists, trigram
    term-similarity fallback otherwise. Empty log never errors."""
    rows_cap = 10_000  # bounded co-occurrence scan — see Suggester docstring
    return {"query": q, "related": _get_suggester().related_searches(
        q, experiment_log=experiment_log, limit=limit, max_log_rows=rows_cap)}


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


@app.get("/ready")
def ready():
    """Readiness probe: 200 once the store answers. A degraded vector
    subsystem is BROKEN for readiness (search would silently under-recall) —
    /health carries that nuance; /ready is binary for load balancers."""
    try:
        _storage.conn.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="storage not ready")
    if _vector_store is None:
        raise HTTPException(status_code=503, detail="vector subsystem not ready")
    return {"ready": True}


@app.get("/metrics", dependencies=[Depends(require_api_key)])
def metrics():
    """Ops endpoint: corpus size/embedder identity/queue depth — internal
    information, so it follows the same key gate as the write endpoints."""
    sync_stats = _embedding_sync.get_stats() if _embedding_sync is not None else None
    vector_stats = _vector_store.get_stats() if _vector_store is not None else None
    return {
        "embedding_sync": sync_stats,
        "vector_store": vector_stats,
        "query_cache": _query_cache.stats(),
    }