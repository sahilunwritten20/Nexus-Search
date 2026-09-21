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
    chunk_id: Optional[str] = None  # best-matching chunk, when the doc was chunked
    matched_chunks: int = 1


class SearchResponse(BaseModel):
    query: str
    total_results: int  # total matches, not just this page
    offset: int = 0
    top_k: int = 10
    results: list[SearchResultOut]