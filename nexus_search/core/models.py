"""Pydantic request/response models for the FastAPI layer."""
import json
from typing import Optional
from pydantic import BaseModel, Field, field_validator


MAX_CONTENT_CHARS = 10_000_000  # ~10MB of text: attacker input must be bounded
# metadata is arbitrary client JSON smuggled alongside content — bound it too
MAX_METADATA_CHARS = 100_000


class DocumentIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    title: str = Field(default="", max_length=10_000)
    doc_type: str = Field(default="text", max_length=64)
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _metadata_bounded(cls, v: dict) -> dict:
        if len(json.dumps(v, default=str)) > MAX_METADATA_CHARS:
            raise ValueError(f"metadata exceeds {MAX_METADATA_CHARS} serialized chars")
        return v


class BulkDocumentIn(BaseModel):
    # per-document bound applies through nested DocumentIn validation
    documents: list[DocumentIn] = Field(min_length=1, max_length=500)


class SearchResultOut(BaseModel):
    doc_id: str
    score: float
    title: str
    snippet: str
    doc_type: str
    metadata: dict = Field(default_factory=dict)
    chunk_id: Optional[str] = None
    matched_chunks: int = 1
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    source: Optional[str] = None
    # Only populated when the request asked for debug info: each retriever's
    # contribution to the final fused score (they sum to `score`).
    bm25_normalized: Optional[float] = None
    vector_normalized: Optional[float] = None


class SearchMetadata(BaseModel):
    mode: str = "keyword"
    requested_mode: Optional[str] = None  # what the client asked for
    mode_used: Optional[str] = None       # what actually ran (differs on fallback)
    reranked: bool = False                # Phase 5 re-ranker applied?
    bm25_candidates: int = 0
    vector_candidates: int = 0
    merged_candidates: int = 0
    fallback: bool = False
    fallback_reason: Optional[str] = None
    latency_ms: int = 0
    fusion: Optional[str] = None
    sort: Optional[str] = None
    has_more: bool = False       # more results beyond this page? (Stage 4)
    next_cursor: Optional[str] = None  # opaque cursor for the next page (Stage 4)
    facets_truncated: bool = False  # facet pass sampled a >500-doc population?


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    content: str
    doc_type: str
    length: int = 0
    metadata: dict = Field(default_factory=dict)
    added_at: float = 0.0


class SearchResponse(BaseModel):
    query: str
    total_results: int
    offset: int = 0
    top_k: int = 10
    results: list[SearchResultOut]
    metadata: Optional[SearchMetadata] = None
    facets: Optional[dict] = None  # field -> {value: count}; set when facets=... given


class ExplainRequest(BaseModel):
    query: str
    top_k: int = 10
    mode: str = "hybrid"
    fusion: str = "rrf"
    bm25_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    rerank: bool = False  # also surface Stage-2 reranker features per result


class ExplainResult(BaseModel):
    doc_id: str
    final_score: float
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    bm25_normalized: Optional[float] = None
    vector_normalized: Optional[float] = None
    source: str
    title: str
    chunk_id: Optional[str] = None
    # when rerank=true: raw Stage-2 signal values + model-blended score
    features: Optional[dict] = None
    reranked_score: Optional[float] = None


class ExplainResponse(BaseModel):
    query: str
    mode: str
    metadata: SearchMetadata
    results: list[ExplainResult]