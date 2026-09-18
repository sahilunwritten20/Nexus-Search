"""Pydantic request/response models for the FastAPI layer."""
from pydantic import BaseModel, Field


class DocumentIn(BaseModel):
    doc_id: str
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


class SearchResponse(BaseModel):
    query: str
    total_results: int
    results: list[SearchResultOut]
