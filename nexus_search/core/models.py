"""Pydantic request/response models for the FastAPI layer."""
from typing import Optional
from pydantic import BaseModel, Field


class DocumentIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    content: str
    title: str = ""
    doc_type: str = "text"
    metadata: dict = Field(default_factory=dict)


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
    bm25_candidates: int = 0
    vector_candidates: int = 0
    merged_candidates: int = 0
    fallback: bool = False
    fallback_reason: Optional[str] = None
    latency_ms: int = 0
    fusion: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    total_results: int
    offset: int = 0
    top_k: int = 10
    results: list[SearchResultOut]
    metadata: Optional[SearchMetadata] = None


class ExplainRequest(BaseModel):
    query: str
    top_k: int = 10
    mode: str = "hybrid"
    fusion: str = "rrf"


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


class ExplainResponse(BaseModel):
    query: str
    mode: str
    metadata: SearchMetadata
    results: list[ExplainResult]